from __future__ import annotations

import logging
import time
from typing import Literal

from ib_async import IB, MarketOrder, Option, Trade

from bot.contracts import resolve_contract
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
from bot.position_store import PositionStore
from bot.sizing import compute_contracts
from bot.trim_sizing import compute_trim_sell_qty
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
) -> None:
    """Idempotent per message_id. Never raises -- every failure path alerts
    via notifier and returns, so one bad message never aborts a batch of
    events being processed."""
    if store.already_processed(event.message_id):
        return

    try:
        if isinstance(event, BuyEvent):
            _handle_buy(event, ib, store, notifier, risk, fill_timeout_s)
        elif isinstance(event, TrimEvent):
            _handle_trim(event, ib, store, notifier, fill_timeout_s)
        elif isinstance(event, SoldAllEvent):
            _handle_sold_all(event, ib, store, notifier, fill_timeout_s)
        elif isinstance(event, ExpiredEvent):
            _handle_expired(event, store, notifier)
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
        store.create_open(
            OpenPosition(
                option=event.option,
                channel_total_qty=event.contracts,
                channel_remaining_qty=event.contracts,
                user_original_qty=contracts,
                user_remaining_qty=contracts,
                entry_price=fill_price,
                ibkr_order_id_entry=trade.order.orderId,
            )
        )
    else:
        reason = "was rejected" if outcome == "REJECTED" else f"did not confirm fill within {fill_timeout_s}s"
        notifier.alert(f"BUY order for {event.option} {reason} (message {event.message_id})")

    store.mark_processed(event.message_id)


def _handle_trim(
    event: TrimEvent, ib: IB, store: PositionStore, notifier: Notifier, fill_timeout_s: float
) -> None:
    try:
        position = store.get_open_by_underlying(event.underlying)
    except ValueError as exc:
        notifier.alert(f"Ambiguous open position for {event.underlying} (message {event.message_id}): {exc}")
        return  # not marked processed -- a fixed DB should let this retry

    if position is None:
        notifier.alert(f"TRIM for {event.underlying} but no open position found (message {event.message_id})")
        store.mark_processed(event.message_id)
        return

    sell_qty = compute_trim_sell_qty(
        position.user_remaining_qty, event.sold_this_event, event.channel_total_before, event.channel_remaining_after
    )

    if sell_qty == 0:
        # Expected outcome of proportional flooring, not an error -- still
        # persist the channel's new remaining qty so the *next* trim's
        # proportion is checked against the right baseline.
        store.apply_trim(
            position.option,
            channel_remaining_qty=event.channel_remaining_after,
            user_remaining_qty=position.user_remaining_qty,
        )
        store.mark_processed(event.message_id)
        return

    contract = resolve_contract(position.option)
    qualified = ib.qualifyContracts(contract)
    if not qualified or not qualified[0].conId:
        notifier.alert(f"Could not qualify contract for {position.option} (message {event.message_id})")
        store.mark_processed(event.message_id)
        return

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
        store.apply_trim(
            position.option,
            channel_remaining_qty=event.channel_remaining_after,
            user_remaining_qty=position.user_remaining_qty - sell_qty,
        )
    else:
        # The user's real position didn't change -- local bookkeeping must
        # not claim it did. Next trim's baseline may be slightly stale
        # until a human reconciles; accepted limitation, see plan.
        reason = "was rejected" if outcome == "REJECTED" else f"did not confirm fill within {fill_timeout_s}s"
        notifier.alert(f"TRIM SELL order for {position.option} {reason} (message {event.message_id})")

    store.mark_processed(event.message_id)


def _handle_sold_all(
    event: SoldAllEvent, ib: IB, store: PositionStore, notifier: Notifier, fill_timeout_s: float
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

    if position.user_remaining_qty <= 0:
        # Already flat locally (e.g. a prior TRIM zeroed it out) -- just
        # confirm the close, no order, no alert needed.
        store.close_position(position.option)
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
    else:
        reason = "was rejected" if outcome == "REJECTED" else f"did not confirm fill within {fill_timeout_s}s"
        notifier.alert(
            f"SOLD ALL SELL order for {position.option} {reason} (message {event.message_id}); "
            "manual reconciliation needed, local position left OPEN"
        )

    store.mark_processed(event.message_id)


def _handle_expired(event: ExpiredEvent, store: PositionStore, notifier: Notifier) -> None:
    try:
        position = store.get_open_by_underlying(event.underlying)
    except ValueError as exc:
        notifier.alert(f"Ambiguous open position for {event.underlying} (message {event.message_id}): {exc}")
        return

    if position is None:
        notifier.alert(f"EXPIRED for {event.underlying} but no open position found (message {event.message_id})")
    else:
        store.close_position(position.option)  # never an order -- the market's closed

    store.mark_processed(event.message_id)


def _handle_info(event: InfoEvent, store: PositionStore) -> None:
    store.mark_processed(event.message_id)


def _handle_unknown(event: UnknownEvent, store: PositionStore, notifier: Notifier) -> None:
    notifier.alert(f"Unrecognized message format (id={event.message_id}): {event.reason}")
    store.mark_processed(event.message_id)
