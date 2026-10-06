"""Resting-order plumbing shared by the executor and the fill watcher.

The exit structure for a position is a set of independent *tranches*. A
take-profit tranche is a SELL LMT at its trim target plus a SELL STP for
the same quantity, both in a one-cancels-all group, so whichever side
triggers first takes its partner off the book. A runner tranche is a stop
on its own, with no limit above it.

Why per-tranche pairs rather than one stop for the whole position: a
single stop would have to shrink every time a target filled, and between
the fill and the resize its quantity exceeds the contracts actually held
-- if it triggered in that window it would sell short. Tranche quantities
here are written once and never change. Only stop *prices* move, and only
upward, as the ladder ratchets.
"""

from __future__ import annotations

import logging

from ib_async import IB, LimitOrder, Option, Order, StopLimitOrder, StopOrder

from bot.exit_plan import ExitPlan, ladder_stop, round_to_tick
from bot.models import OpenPosition, TargetLeg, TrimTarget
from bot.notifier import Notifier
from bot.position_store import PositionStore
from config.settings import RiskConfig

logger = logging.getLogger("options_scanner.orders")

# One-cancels-all, "cancel all remaining orders in the block". Applied to
# a limit/stop pair so a filled target removes its own stop.
_OCA_CANCEL_ALL = 1

# Brackets outlive the session that placed them. A DAY order would be
# purged at the close, leaving an overnight position (e.g. a Sep 29 entry
# on a Sep 30 expiry) completely unprotected the next morning.
_TIF = "GTC"


def _oca_group(position_id: int, tier_index: int) -> str:
    return f"pos{position_id}-t{tier_index}"


def build_stop_order(qty: int, stop_price: float, risk: RiskConfig, oca_group: str | None) -> Order:
    stop_price = round_to_tick(stop_price)
    if risk.stop_order_type == "STP_LMT":
        limit = round_to_tick(stop_price * (1 - risk.stop_limit_offset_pct))
        order = StopLimitOrder("SELL", qty, limit, stop_price)
    else:
        order = StopOrder("SELL", qty, stop_price)
    order.tif = _TIF
    if oca_group:
        order.ocaGroup = oca_group
        order.ocaType = _OCA_CANCEL_ALL
    return order


def build_limit_order(qty: int, price: float, oca_group: str) -> Order:
    order = LimitOrder("SELL", qty, round_to_tick(price))
    order.tif = _TIF
    order.ocaGroup = oca_group
    order.ocaType = _OCA_CANCEL_ALL
    return order


def place_exit_structure(
    ib: IB,
    store: PositionStore,
    contract: Option,
    position_id: int,
    plan: ExitPlan,
    risk: RiskConfig,
) -> list[TargetLeg]:
    """Places every tranche of `plan` and records it. Each leg is persisted
    before its orders go out, so a crash mid-placement leaves a row to
    reconcile against rather than an untracked order at the broker."""
    legs: list[TargetLeg] = []
    for tranche in plan.tranches:
        group = _oca_group(position_id, tranche.tier_index)
        leg = TargetLeg(
            position_id=position_id,
            tier_index=tranche.tier_index,
            qty=tranche.qty,
            tp_price=tranche.tp_price,
            current_stop_price=plan.initial_stop,
            oca_group=group,
        )
        leg.id = store.add_target_leg(leg)

        stop_trade = ib.placeOrder(contract, build_stop_order(tranche.qty, plan.initial_stop, risk, group))
        leg.stp_order_id = stop_trade.order.orderId
        if tranche.tp_price is not None:
            lmt_trade = ib.placeOrder(contract, build_limit_order(tranche.qty, tranche.tp_price, group))
            leg.lmt_order_id = lmt_trade.order.orderId

        store.set_leg_order_ids(leg.id, leg.lmt_order_id, leg.stp_order_id)
        logger.info(
            "Placed tranche t%s for position %s: %sx stop %.2f%s",
            tranche.tier_index,
            position_id,
            tranche.qty,
            plan.initial_stop,
            f" / limit {tranche.tp_price:.2f}" if tranche.tp_price is not None else " (runner)",
        )
        legs.append(leg)
    return legs


def cancel_legs(ib: IB, store: PositionStore, legs: list[TargetLeg]) -> int:
    """Cancels both sides of each live leg and marks it CANCELLED. Tolerant
    of an order already gone at IBKR -- cancelling something that filled a
    moment ago raises, and that's not a failure worth aborting on.

    An id missing from ib.orders() is cancelled by id anyway rather than
    skipped: these are GTC orders that outlive the session that placed
    them, so after a restart the local book may not have caught up, and
    silently leaving a stop resting against a position we just flattened
    is the one outcome worth avoiding.
    """
    cancelled = 0
    open_orders = {order.orderId: order for order in ib.orders()}
    for leg in legs:
        for order_id in (leg.lmt_order_id, leg.stp_order_id):
            if order_id is None:
                continue
            order = open_orders.get(order_id) or Order(orderId=order_id)
            try:
                ib.cancelOrder(order)
                cancelled += 1
            except Exception:
                logger.warning("Could not cancel order %s (likely already done)", order_id, exc_info=True)
        if leg.id is not None:
            store.set_leg_status(leg.id, "CANCELLED")
    return cancelled


def cancel_all_legs_for_position(ib: IB, store: PositionStore, position_id: int) -> int:
    return cancel_legs(ib, store, store.list_legs(position_id, only_live=True))


def ratchet_stops(
    ib: IB,
    store: PositionStore,
    contract: Option,
    position: OpenPosition,
    reached_tier_index: int,
    risk: RiskConfig,
    trade_notifier: Notifier | None = None,
) -> float | None:
    """Moves the stop on every still-live tranche up to the level earned by
    reaching `reached_tier_index`. Returns the new stop price, or None when
    the ladder hasn't actually advanced.

    Stops are modified in place -- re-placed under the same IBKR order id,
    same quantity, new trigger -- rather than cancelled and re-placed,
    which would leave the position briefly naked.
    """
    if position.id is None:
        return None
    if reached_tier_index <= position.stop_tier_index:
        return None  # already at or above this rung; never walk a stop down

    targets = position.trim_targets
    if not risk.runner_ladder_extends and targets:
        reached_tier_index = min(reached_tier_index, len(targets) - 1)

    new_stop = ladder_stop(position.entry_price, targets, reached_tier_index, risk.stop_loss_pct)
    if position.current_stop_price is not None and new_stop <= position.current_stop_price:
        store.set_position_stop(position.id, position.current_stop_price, reached_tier_index)
        return None

    live_legs = store.list_legs(position.id, only_live=True)
    open_orders = {order.orderId: order for order in ib.orders()}
    moved = 0
    for leg in live_legs:
        if leg.stp_order_id is None or leg.id is None:
            continue
        existing = open_orders.get(leg.stp_order_id)
        order = existing if existing is not None else build_stop_order(leg.qty, new_stop, risk, leg.oca_group)
        order.orderId = leg.stp_order_id
        order.auxPrice = new_stop
        if risk.stop_order_type == "STP_LMT":
            order.lmtPrice = round_to_tick(new_stop * (1 - risk.stop_limit_offset_pct))
        order.transmit = True
        ib.placeOrder(contract, order)
        store.set_leg_stop_price(leg.id, new_stop)
        moved += 1

    store.set_position_stop(position.id, new_stop, reached_tier_index)
    message = (
        f"STOP -> ${new_stop:.2f} on {position.option} ({moved} tranche(s), "
        f"{position.user_remaining_qty} contracts) -- target {reached_tier_index + 1} reached, "
        f"stop now at {_rung_label(targets, reached_tier_index)}"
    )
    logger.info(message)
    if trade_notifier is not None:
        trade_notifier.alert(message)
    return new_stop


def _rung_label(targets: tuple[TrimTarget, ...], reached_tier_index: int) -> str:
    """Human-readable name for where the stop now sits -- it lags the rung
    just reached by one, so target 1 reached means a breakeven stop."""
    if reached_tier_index == 0:
        return "breakeven"
    previous = reached_tier_index - 1
    if previous < len(targets):
        return f"the +{targets[previous].pct * 100:.0f}% rung"
    return f"extended rung {previous + 1}"
