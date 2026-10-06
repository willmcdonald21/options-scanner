"""Watches our own IBKR activity and ratchets stops up the trim ladder.

Once the exit structure is resting at the broker (bot/orders.py), "a take
profit was reached" is no longer a Discord message -- it is either one of
our own limit orders filling, or, for a runner with no resting limit, a
market print crossing a rung. Both paths converge on the same action:
move every still-live stop up to the level the trade has now proved, and
never let it move back down.

The watcher is deliberately split into event handlers (thin, bound to
ib_async events) and `handle_fill` / `handle_price` (plain methods taking
primitives), so the laddering logic is testable without an event loop.
"""

from __future__ import annotations

import logging

from ib_async import IB, Option, Ticker, Trade

from bot.contracts import resolve_contract
from bot.exit_plan import reached_tier_from_price
from bot.models import OptionKey
from bot.notifier import Notifier
from bot.orders import cancel_all_legs_for_position, ratchet_stops
from bot.position_store import PositionStore
from config.settings import RiskConfig

logger = logging.getLogger("options_scanner.fill_watcher")

# IBKR error codes for "you aren't subscribed to this market data". Without
# ticks a runner's stop still protects it at its current level, it just
# never climbs -- a silent degradation, so it gets a loud alert and an
# automatic downgrade to delayed data.
_NO_MARKET_DATA_CODES = {354, 10167, 10168, 10197}
_DELAYED_MARKET_DATA = 3


class FillWatcher:
    def __init__(
        self,
        ib: IB,
        store: PositionStore,
        risk: RiskConfig,
        notifier: Notifier,
        trade_notifier: Notifier | None = None,
        market_data_type: int = 1,
    ):
        self.ib = ib
        self.store = store
        self.risk = risk
        self.notifier = notifier
        self.trade_notifier = trade_notifier
        self.market_data_type = market_data_type
        self._qualified: dict[OptionKey, Option] = {}
        self._subscribed: dict[int, int] = {}  # conId -> position_id
        self._warned_no_data = False

    # --- wiring -----------------------------------------------------------

    def start(self) -> None:
        self.ib.orderStatusEvent += self._on_order_status
        self.ib.pendingTickersEvent += self._on_pending_tickers
        self.ib.errorEvent += self._on_error
        self.ib.reqMarketDataType(self.market_data_type)

    def stop(self) -> None:
        self.ib.orderStatusEvent -= self._on_order_status
        self.ib.pendingTickersEvent -= self._on_pending_tickers
        self.ib.errorEvent -= self._on_error

    def _on_order_status(self, trade: Trade) -> None:
        if trade.orderStatus.status != "Filled":
            return
        self.handle_fill(trade.order.orderId, trade.orderStatus.avgFillPrice or 0.0)

    def _on_pending_tickers(self, tickers: set[Ticker]) -> None:
        for ticker in tickers:
            con_id = getattr(ticker.contract, "conId", None)
            position_id = self._subscribed.get(con_id) if con_id else None
            if position_id is None:
                continue
            price = _exitable_price(ticker)
            if price is not None:
                self.handle_price(position_id, price)

    def _on_error(self, reqId: int, errorCode: int, errorString: str, contract) -> None:
        if errorCode in _NO_MARKET_DATA_CODES and not self._warned_no_data:
            self._warned_no_data = True
            self.ib.reqMarketDataType(_DELAYED_MARKET_DATA)
            self.notifier.alert(
                f"No real-time option quotes (IBKR {errorCode}: {errorString}). Falling back to "
                "delayed data -- stops still protect open positions but will ratchet late. "
                "Add an OPRA subscription to this account to fix."
            )

    # --- the two ways a target gets reached -------------------------------

    def handle_fill(self, ib_order_id: int, fill_price: float) -> bool:
        """Routes one filled order back to its tranche. Returns True if the
        order was one of ours. Safe to call twice for the same order -- a
        leg that isn't LIVE is ignored, since IBKR can repeat a status."""
        found = self.store.get_leg_by_order_id(ib_order_id)
        if found is None:
            return False
        leg, side = found
        if leg.status != "LIVE" or leg.id is None:
            return True

        position = self.store.get_position_by_id(leg.position_id)
        if position is None:
            logger.warning("Fill on order %s whose position %s is gone", ib_order_id, leg.position_id)
            return True

        self.store.set_leg_status(leg.id, "TP_FILLED" if side == "TP" else "STOP_FILLED")
        remaining = self.store.reduce_remaining(leg.position_id, leg.qty)

        if side == "TP":
            self._notify(
                f"TARGET HIT: sold {leg.qty}x {position.option} @ ${fill_price:.2f} "
                f"(target {leg.tier_index + 1}) -- {remaining} contracts left"
            )
            if remaining > 0:
                self._ratchet(leg.position_id, leg.tier_index)
            else:
                self._release(leg.position_id)
        else:
            self._notify(
                f"STOPPED OUT: sold {leg.qty}x {position.option} @ ${fill_price:.2f} "
                f"(stop was ${leg.current_stop_price:.2f}) -- {remaining} contracts left"
            )
            if remaining <= 0:
                self._release(leg.position_id)
        return True

    def handle_price(self, position_id: int, price: float) -> float | None:
        """A market print crossed a rung. This is how a runner ratchets --
        it has no resting limit to fill -- but it is applied to every
        position, so a stop also moves up when price trades through a
        target whose limit hasn't filled yet."""
        position = self.store.get_position_by_id(position_id)
        if position is None or position.status != "OPEN":
            return None
        reached = reached_tier_from_price(position.entry_price, position.trim_targets, price)
        if reached <= position.stop_tier_index:
            return None
        return self._ratchet(position_id, reached)

    # --- helpers ----------------------------------------------------------

    def _ratchet(self, position_id: int, reached_tier_index: int) -> float | None:
        position = self.store.get_position_by_id(position_id)
        if position is None or position.status != "OPEN":
            return None
        contract = self.qualified_contract(position.option)
        if contract is None:
            self.notifier.alert(
                f"Could not qualify {position.option} to move its stop up to target "
                f"{reached_tier_index + 1}. Stop is still resting at its previous level."
            )
            return None
        return ratchet_stops(
            self.ib,
            self.store,
            contract,
            position,
            reached_tier_index,
            self.risk,
            self.trade_notifier,
        )

    def _release(self, position_id: int) -> None:
        """Fully exited: take any straggler legs off the book and drop the
        market-data subscription."""
        cancel_all_legs_for_position(self.ib, self.store, position_id)
        self.unsubscribe(position_id)

    def _notify(self, message: str) -> None:
        logger.info(message)
        if self.trade_notifier is not None:
            self.trade_notifier.alert(message)

    def qualified_contract(self, option: OptionKey) -> Option | None:
        cached = self._qualified.get(option)
        if cached is not None:
            return cached
        qualified = self.ib.qualifyContracts(resolve_contract(option))
        if not qualified or not qualified[0].conId:
            return None
        self._qualified[option] = qualified[0]
        return qualified[0]

    # --- market data ------------------------------------------------------

    def subscribe(self, position_id: int, option: OptionKey) -> bool:
        contract = self.qualified_contract(option)
        if contract is None:
            return False
        if contract.conId in self._subscribed:
            return True
        self.ib.reqMktData(contract, "", False, False)
        self._subscribed[contract.conId] = position_id
        return True

    def unsubscribe(self, position_id: int) -> None:
        for con_id, pid in list(self._subscribed.items()):
            if pid != position_id:
                continue
            contract = next((c for c in self._qualified.values() if c.conId == con_id), None)
            if contract is not None:
                try:
                    self.ib.cancelMktData(contract)
                except Exception:
                    logger.debug("cancelMktData failed for %s", con_id, exc_info=True)
            del self._subscribed[con_id]

    def subscribe_open_positions(self) -> int:
        """Called at startup and after each new entry. Every open position
        gets a feed, not just the ones holding a runner, so a stop can
        ratchet off a price print even when the matching limit order
        hasn't filled."""
        count = 0
        for position in self.store.list_open():
            if position.id is not None and self.subscribe(position.id, position.option):
                count += 1
        return count

    # --- restart ----------------------------------------------------------

    def reconcile(self) -> list[str]:
        """Checks what we think is resting at IBKR against what actually
        is. A bot that restarts having lost its brackets is the dangerous
        state, so every open position whose legs are missing gets named in
        an alert rather than silently carrying on.

        Returns the problems found, for logging and tests.
        """
        self.ib.reqAllOpenOrders()
        live_ids = {order.orderId for order in self.ib.orders()}
        problems: list[str] = []

        for leg in self.store.list_all_live_legs():
            missing = [
                order_id
                for order_id in (leg.lmt_order_id, leg.stp_order_id)
                if order_id is not None and order_id not in live_ids
            ]
            if not missing:
                continue
            position = self.store.get_position_by_id(leg.position_id)
            label = str(position.option) if position is not None else f"position {leg.position_id}"
            problems.append(
                f"{label}: tranche t{leg.tier_index} ({leg.qty}x, stop ${leg.current_stop_price:.2f}) "
                f"has no live order at IBKR for id(s) {missing}"
            )

        if problems:
            self.notifier.alert(
                "Startup reconciliation found exit orders that are no longer at IBKR. These "
                "positions may be unprotected -- check them before trusting the bot:\n"
                + "\n".join(f"- {p}" for p in problems)
            )
        subscribed = self.subscribe_open_positions()
        logger.info("Reconciled: %s live leg problem(s), %s position(s) subscribed", len(problems), subscribed)
        return problems


def _exitable_price(ticker: Ticker) -> float | None:
    """The price we could actually sell into, preferring the bid. A long
    option is exited on the bid, so ratcheting off the bid means a rung is
    only treated as reached when the gain is genuinely available -- last
    and midpoint are fallbacks for an illiquid book with no bid."""
    for candidate in (ticker.bid, ticker.last, ticker.close):
        if candidate is not None and candidate == candidate and candidate > 0:
            return float(candidate)
    return None
