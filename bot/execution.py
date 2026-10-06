from __future__ import annotations

import logging
import time
from typing import Literal

from ib_async import IB, MarketOrder, Option, Trade

from bot.contracts import resolve_contract
from bot.exit_plan import build_exit_plan, default_targets
from bot.models import (
    BuyEvent,
    ExpiredEvent,
    InfoEvent,
    OpenPosition,
    SoldAllEvent,
    TradeEvent,
    TrimEvent,
    UnknownEvent,
)
from bot.notifier import Notifier
from bot.orders import cancel_all_legs_for_position, place_exit_structure
from bot.position_store import PositionStore
from bot.sizing import compute_contracts
from config.settings import RiskConfig

logger = logging.getLogger("options_scanner.execution")

FillOutcome = Literal["FILLED", "REJECTED", "TIMEOUT"]


def handle_event(
    event: TradeEvent,
    ib: IB,
    store: PositionStore,
    notifier: Notifier,
    risk: RiskConfig,
    fill_timeout_s: float = 10.0,
    trade_notifier: Notifier | None = None,
) -> None:
    """Idempotent per message_id. Never raises -- every failure path alerts
    via notifier and returns, so one bad message never aborts a batch of
    events being processed.

    trade_notifier, if given, gets a separate one-line post for every
    successful SELL fill (TRIM or SOLD ALL) -- a "what did the bot actually
    sell" feed, distinct from notifier's failure/rejection alerts."""
    if store.already_processed(event.message_id):
        return

    try:
        if isinstance(event, BuyEvent):
            _handle_buy(event, ib, store, notifier, risk, fill_timeout_s)
        elif isinstance(event, TrimEvent):
            _handle_trim(event, ib, store, notifier, fill_timeout_s, trade_notifier)
        elif isinstance(event, SoldAllEvent):
            _handle_sold_all(event, ib, store, notifier, fill_timeout_s, trade_notifier)
        elif isinstance(event, ExpiredEvent):
            _handle_expired(event, ib, store, notifier)
        elif isinstance(event, InfoEvent):
            _handle_info(event, store)
        elif isinstance(event, UnknownEvent):
            _handle_unknown(event, store, notifier)
    except Exception as exc:
        # Unexpected failure -- don't mark_processed, since we don't know
        # what state things are in; safer to retry than silently skip.
        logger.exception("Unhandled error processing message %s", event.message_id)
        notifier.alert(f"Unhandled error processing message {event.message_id} ({type(event).__name__}): {exc}")


def _place_market_order_and_confirm(
    ib: IB,
    contract: Option,
    action: Literal["BUY", "SELL"],
    quantity: int,
    timeout_s: float,
) -> tuple[Trade, FillOutcome]:
    """Places a market order and polls until it reaches a terminal state or
    timeout_s elapses. Never raises for a rejection/timeout -- returns the
    trade plus an outcome tag and lets the caller decide what to do. Does
    NOT auto-cancel on timeout: racing a cancel against an order that may
    still be filling is worse than alerting and letting a human check."""
    trade = ib.placeOrder(contract, MarketOrder(action, quantity))
    deadline = time.monotonic() + timeout_s
    while not trade.isDone() and time.monotonic() < deadline:
        ib.sleep(0.25)

    if trade.orderStatus.status == "Filled":
        return trade, "FILLED"
    if trade.isDone():
        return trade, "REJECTED"
    return trade, "TIMEOUT"


def _handle_buy(
    event: BuyEvent, ib: IB, store: PositionStore, notifier: Notifier, risk: RiskConfig, fill_timeout_s: float
) -> None:
    contract = resolve_contract(event.option)
    qualified = ib.qualifyContracts(contract)
    if not qualified or not qualified[0].conId:
        notifier.alert(f"Could not qualify contract for {event.option} (message {event.message_id})")
        store.mark_processed(event.message_id)
        return

    # Sized off the user's own risk cap, not event.contracts -- that field
    # is parsed straight from the channel's own Contracts field and
    # reflects the channel's account size, not this account's.
    contracts = compute_contracts(event.entry_price, risk.max_usd_per_trade)

    trade, outcome = _place_market_order_and_confirm(ib, qualified[0], "BUY", contracts, fill_timeout_s)
    store.record_order(
        message_id=event.message_id,
        option=event.option,
        ib_order_id=trade.order.orderId,
        action="BUY",
        contracts=contracts,
        status=outcome,
        avg_fill_price=trade.orderStatus.avgFillPrice or None,
    )

    if outcome == "FILLED":
        fill_price = trade.orderStatus.avgFillPrice or event.entry_price
        # The channel's published ladder is relative to *its* entry; ours
        # is relative to the fill we actually got, which can differ by a
        # few cents on a market order. Keep the channel's percentages and
        # re-base the prices on our fill so the rungs mean what they say.
        targets = (
            default_targets(fill_price, tuple(t.pct for t in event.trim_targets))
            if event.trim_targets
            else default_targets(fill_price)
        )
        plan = build_exit_plan(fill_price, contracts, targets, risk.stop_loss_pct)
        position_id = store.create_open(
            OpenPosition(
                option=event.option,
                channel_total_qty=event.contracts,
                channel_remaining_qty=event.contracts,
                user_original_qty=contracts,
                user_remaining_qty=contracts,
                entry_price=fill_price,
                ibkr_order_id_entry=trade.order.orderId,
                current_stop_price=plan.initial_stop,
                stop_tier_index=-1,
                runner_qty=plan.runner_qty,
                trim_targets=targets,
            )
        )
        try:
            place_exit_structure(ib, store, qualified[0], position_id, plan, risk)
        except Exception as exc:
            # The entry filled but the brackets didn't -- an unprotected
            # position is the one state that always needs a human.
            logger.exception("Failed to place exit structure for %s", event.option)
            notifier.alert(
                f"FILLED {contracts}x {event.option} but could NOT place exit orders: {exc}. "
                "Position is UNPROTECTED -- set a stop manually."
            )
        else:
            rungs = ", ".join(
                f"t{t.tier_index}:{t.qty}x@{t.tp_price:.2f}" if t.tp_price is not None else f"t{t.tier_index}:{t.qty}x runner"
                for t in plan.tranches
            )
            logger.info(
                "Bracketed %s: %s contracts, stop %.2f, tranches [%s]",
                event.option,
                contracts,
                plan.initial_stop,
                rungs,
            )
    else:
        reason = "was rejected" if outcome == "REJECTED" else f"did not confirm fill within {fill_timeout_s}s"
        notifier.alert(f"BUY order for {event.option} {reason} (message {event.message_id})")

    store.mark_processed(event.message_id)


def _handle_trim(
    event: TrimEvent,
    ib: IB,
    store: PositionStore,
    notifier: Notifier,
    fill_timeout_s: float,
    trade_notifier: Notifier | None = None,
) -> None:
    """Read-only. Our own trim targets are already resting at IBKR from the
    entry (see _handle_buy), so mirroring the channel's trim here would
    sell a second time on top of a limit order that has either already
    filled or is about to. All this does is keep the channel's own
    remaining count current, for context in later messages.

    ib and fill_timeout_s are unused, kept so every handler shares one
    signature and reinstating reactive trims stays a one-function change
    (bot/trim_sizing.py still holds the proportional-sizing logic).
    """
    try:
        position = store.get_open_by_underlying(event.underlying)
    except ValueError as exc:
        notifier.alert(f"Ambiguous open position for {event.underlying} (message {event.message_id}): {exc}")
        return  # not marked processed -- a fixed DB should let this retry

    if position is None:
        # Not an error worth alerting on any more: our own stop or targets
        # may well have closed this position before the channel trimmed it.
        logger.info(
            "TRIM +%.0f%% for %s noted; no open position on our side (message %s)",
            event.tier_pct * 100,
            event.underlying,
            event.message_id,
        )
        store.mark_processed(event.message_id)
        return

    store.apply_trim(
        position.option,
        channel_remaining_qty=event.channel_remaining_after,
        user_remaining_qty=position.user_remaining_qty,  # unchanged -- we placed no order
    )
    logger.info(
        "TRIM +%.0f%% for %s noted (channel %s -> %s remaining); our own %s contracts are "
        "managed by resting orders, stop at %s",
        event.tier_pct * 100,
        position.option,
        event.channel_total_before,
        event.channel_remaining_after,
        position.user_remaining_qty,
        f"${position.current_stop_price:.2f}" if position.current_stop_price else "unset",
    )
    store.mark_processed(event.message_id)


def _handle_sold_all(
    event: SoldAllEvent,
    ib: IB,
    store: PositionStore,
    notifier: Notifier,
    fill_timeout_s: float,
    trade_notifier: Notifier | None = None,
) -> None:
    try:
        position = store.get_open_by_underlying(event.underlying)
    except ValueError as exc:
        notifier.alert(f"Ambiguous open position for {event.underlying} (message {event.message_id}): {exc}")
        return

    if position is None:
        notifier.alert(f"SOLD ALL for {event.underlying} but no open position found (message {event.message_id})")
        store.mark_processed(event.message_id)
        return

    # Cancel first, always. Selling while our own limits and stops are
    # still working would either double-sell (a target filling alongside
    # the market order) or leave orphan orders resting against a flat
    # position, which at IBKR means going short if one later triggers.
    if position.id is not None:
        cancelled = cancel_all_legs_for_position(ib, store, position.id)
        if cancelled:
            logger.info("Cancelled %s resting exit order(s) for %s before flattening", cancelled, position.option)

    if position.user_remaining_qty <= 0:
        # Already flat locally -- our own stop or targets got there first.
        store.close_position(position.option)
        logger.info("SOLD ALL for %s: already flat on our side, nothing to sell", position.option)
        store.mark_processed(event.message_id)
        return

    contract = resolve_contract(position.option)
    qualified = ib.qualifyContracts(contract)
    if not qualified or not qualified[0].conId:
        notifier.alert(f"Could not qualify contract for {position.option} (message {event.message_id})")
        store.mark_processed(event.message_id)
        return

    sell_qty = position.user_remaining_qty
    trade, outcome = _place_market_order_and_confirm(ib, qualified[0], "SELL", sell_qty, fill_timeout_s)
    store.record_order(
        message_id=event.message_id,
        option=position.option,
        ib_order_id=trade.order.orderId,
        action="SELL",
        contracts=sell_qty,
        status=outcome,
        avg_fill_price=trade.orderStatus.avgFillPrice or None,
    )

    if outcome == "FILLED":
        store.close_position(position.option)
        if trade_notifier is not None:
            fill_price = trade.orderStatus.avgFillPrice or 0.0
            trade_notifier.alert(f"SOLD ALL {sell_qty}x {position.option} @ ${fill_price:.2f} -- position closed")
    else:
        reason = "was rejected" if outcome == "REJECTED" else f"did not confirm fill within {fill_timeout_s}s"
        notifier.alert(
            f"SOLD ALL SELL order for {position.option} {reason} (message {event.message_id}); "
            "manual reconciliation needed, local position left OPEN"
        )

    store.mark_processed(event.message_id)


def _handle_expired(event: ExpiredEvent, ib: IB, store: PositionStore, notifier: Notifier) -> None:
    try:
        position = store.get_open_by_underlying(event.underlying)
    except ValueError as exc:
        notifier.alert(f"Ambiguous open position for {event.underlying} (message {event.message_id}): {exc}")
        return

    if position is None:
        notifier.alert(f"EXPIRED for {event.underlying} but no open position found (message {event.message_id})")
    else:
        # Never an order -- the market's closed. The resting brackets still
        # need taking off the book: a GTC stop on an expired contract would
        # otherwise linger at IBKR.
        if position.id is not None:
            cancel_all_legs_for_position(ib, store, position.id)
        store.close_position(position.option)

    store.mark_processed(event.message_id)


def _handle_info(event: InfoEvent, store: PositionStore) -> None:
    store.mark_processed(event.message_id)


def _handle_unknown(event: UnknownEvent, store: PositionStore, notifier: Notifier) -> None:
    notifier.alert(f"Unrecognized message format (id={event.message_id}): {event.reason}")
    store.mark_processed(event.message_id)
