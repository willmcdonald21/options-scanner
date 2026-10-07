"""The managed-position loop.

Owns everything that happens to a position after the entry fills: polling
quotes, feeding them to the rules engine, turning its intents into orders,
persisting the result, and reporting it. The rules themselves live in
`options_scanner.rules` and know nothing about any of this.

Three properties this module exists to guarantee:

* **Persist before reporting, report before forgetting.** State is written to
  SQLite after every change, because the peak and the fired-level set are the
  stop. A restart that lost either would quietly loosen the stop rather than
  fail.
* **Never sell more than the broker says we hold.** Local state can drift --
  this account is shared with another process that cancels and flattens
  account-wide -- so every exit is clamped against a fresh position read.
* **Never go quiet.** No quotes, a vanished position, an exit that will not
  fill: each of these is a loud, owner-pinging alert, because with synthetic
  stops the failure mode is silence, not an error.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime

from options_scanner.broker.base import Broker, BrokerError
from options_scanner.config import Settings
from options_scanner.contracts import ContractSpec, build_spec
from options_scanner.execution import execute_exit, safe_sell_qty
from options_scanner.market_hours import minutes_to_close, now_et, today_et
from options_scanner.models import PositionState
from options_scanner.notifier import (
    Notification,
    broker_disconnected,
    error as error_note,
    near_close_warning,
    phantom_exit,
    stale_quotes,
    stop_moved,
    stopped_out,
    trail_armed,
    trim_executed,
)
from options_scanner.risk import RiskGate
from options_scanner.rules import (
    ArmTrail,
    Breach,
    ClearBreach,
    RulesConfig,
    SetStop,
    StopOut,
    Trim,
    UpdatePeak,
    apply_action,
    evaluate,
    on_trim_fill,
)
from options_scanner.storage import Storage

logger = logging.getLogger("options_scanner.position_manager")


@dataclass
class Managed:
    """One live position plus the bookkeeping the loop needs around it."""

    position_id: int
    state: PositionState
    spec: ContractSpec
    jump_url: str | None = None
    realized: float = 0.0
    last_good_quote: datetime | None = None
    stale_warned: bool = False
    phantom_warned: bool = False

    @property
    def symbol(self) -> str:
        return self.spec.occ_symbol


@dataclass
class Tick:
    """What one poll of one position produced. Returned rather than posted so
    the loop can be driven synchronously in a test."""

    notifications: list[Notification] = field(default_factory=list)
    closed: bool = False

    def add(self, *notes: Notification) -> None:
        self.notifications.extend(notes)


class PositionManager:
    def __init__(
        self,
        settings: Settings,
        storage: Storage,
        risk: RiskGate,
        broker: Broker,
        *,
        notify=None,
    ):
        self.settings = settings
        self.storage = storage
        self.risk = risk
        self.broker = broker
        self._notify = notify
        self.rules: RulesConfig = settings.rules()
        self.managed: dict[str, Managed] = {}
        self._near_close_warned = False
        self._running = False

    # --- registration ------------------------------------------------------

    def track(self, position_id: int, state: PositionState, jump_url: str | None = None) -> Managed:
        managed = Managed(
            position_id=position_id,
            state=state,
            spec=build_spec(state.option),
            jump_url=jump_url,
        )
        self.managed[managed.symbol] = managed
        return managed

    def forget(self, symbol: str) -> None:
        self.managed.pop(symbol, None)
        release = getattr(self.broker, "release", None)
        if release is not None:
            try:
                release(symbol)
            except Exception:
                logger.debug("release failed for %s", symbol, exc_info=True)

    # --- restart reconciliation -------------------------------------------

    async def reconcile(self) -> tuple[list[str], bool]:
        """Reload open positions and check them against the broker.

        Returns (report lines, mismatched). A mismatch is never resolved
        silently: the local quantity is corrected downward to what the broker
        reports, because selling against a quantity we do not hold is the one
        error that can open a short, but the discrepancy is always escalated.
        """
        lines: list[str] = []
        mismatched = False

        try:
            broker_positions = {p.occ_symbol: p for p in await self.broker.get_positions()}
        except BrokerError as exc:
            return [f"could not read positions from the broker: {exc}"], True

        for position_id, state in self.storage.open_positions():
            managed = self.track(position_id, state)
            held = broker_positions.get(managed.symbol)
            stop = f"${state.stop_price:.2f}" if state.stop_price else "NONE"
            summary = (
                f"`{managed.symbol}` {state.remaining_qty} held, entry ${state.entry_fill:.3f}, "
                f"peak ${state.peak_bid:.3f}, stop {stop}, trims {sorted(state.fired_levels) or '[]'}"
            )

            if held is None:
                mismatched = True
                lines.append(f"{summary} — **broker reports no position**; marking closed locally")
                state.remaining_qty = 0
                state.closed = True
                self.storage.save_position(position_id, state, managed.realized)
                self.forget(managed.symbol)
                continue

            if held.qty != state.remaining_qty:
                mismatched = True
                lines.append(
                    f"{summary} — **broker reports {held.qty}**; correcting down to the broker's number"
                )
                state.remaining_qty = min(state.remaining_qty, held.qty)
                self.storage.save_position(position_id, state, managed.realized)
            else:
                lines.append(f"{summary} — matches the broker")

        return lines, mismatched

    # --- the loop ----------------------------------------------------------

    async def run(self, stop_event: asyncio.Event | None = None) -> None:
        """Poll every managed position until told to stop."""
        self._running = True
        interval = self.settings.stops.poll_seconds
        logger.info("position manager started (polling every %.1fs)", interval)
        try:
            while self._running:
                if stop_event is not None and stop_event.is_set():
                    break
                try:
                    await self.poll_once()
                except Exception:
                    logger.exception("position manager poll failed")
                    await self._post(
                        error_note(
                            title="Position manager error",
                            detail="A polling cycle failed; see the log. Positions may be unmanaged.",
                        )
                    )
                await asyncio.sleep(interval)
        finally:
            self._running = False
            logger.info("position manager stopped")

    def stop(self) -> None:
        self._running = False

    async def poll_once(self) -> list[Notification]:
        """One cycle over every managed position."""
        notifications: list[Notification] = []

        if not self.broker.is_connected:
            notifications.append(broker_disconnected(detail="The broker session is not connected."))
            await self._post(*notifications)
            return notifications

        for symbol in list(self.managed):
            managed = self.managed.get(symbol)
            if managed is None:
                continue
            tick = await self.poll_position(managed)
            notifications.extend(tick.notifications)

        notifications.extend(await self._check_near_close())
        await self._post(*notifications)
        return notifications

    async def poll_position(self, managed: Managed) -> Tick:
        """Advance one position by one quote."""
        tick = Tick()

        quote = await self._quote(managed, tick)
        if quote is None:
            return tick

        price = quote.exit_price(self.settings.stops.source_blend)
        if price is None:
            self._note_stale(managed, tick)
            return tick

        managed.last_good_quote = quote.asof
        managed.stale_warned = False

        if not await self._confirm_still_held(managed, tick):
            return tick

        for action in evaluate(managed.state, price, self.rules):
            await self._apply(managed, action, tick)
            if managed.state.closed:
                break

        self.storage.save_position(managed.position_id, managed.state, managed.realized)
        if managed.state.closed:
            tick.closed = True
            self.forget(managed.symbol)
        return tick

    # --- acting on one rules action ---------------------------------------

    async def _apply(self, managed: Managed, action, tick: Tick) -> None:
        state = managed.state

        if isinstance(action, (UpdatePeak, Breach, ClearBreach)):
            # Bookkeeping only. A breach is deliberately not announced: it is
            # one quote of noise most of the time, and announcing every one
            # would bury the exit that matters.
            apply_action(state, action)
            return

        if isinstance(action, ArmTrail):
            apply_action(state, action)
            tick.add(
                trail_armed(
                    occ_symbol=managed.symbol,
                    at_price=action.at_price,
                    giveback_pct=self.settings.trail.giveback_pct,
                    jump_url=managed.jump_url,
                )
            )
            return

        if isinstance(action, SetStop):
            previous = state.stop_price
            apply_action(state, action)
            tick.add(
                stop_moved(
                    occ_symbol=managed.symbol,
                    old=previous,
                    new=action.price,
                    reason=action.reason,
                    jump_url=managed.jump_url,
                )
            )
            return

        if isinstance(action, Trim):
            await self._do_trim(managed, action, tick)
            return

        if isinstance(action, StopOut):
            await self._do_stop_out(managed, action, tick)
            return

        apply_action(state, action)

    async def _do_trim(self, managed: Managed, action: Trim, tick: Tick) -> None:
        state = managed.state

        if action.qty == 0:
            # A rung with nothing to sell: the +100% level, or a position
            # already down to its runner. Consume it so it is not retried on
            # every quote, but place nothing.
            apply_action(state, action)
            return

        qty = await safe_sell_qty(self.broker, managed.spec, action.qty)
        if qty == 0:
            tick.add(
                error_note(
                    title=f"Could not trim +{action.level_pct}%",
                    detail=(
                        f"The broker reports nothing sellable in `{managed.symbol}`, so the "
                        f"+{action.level_pct}% trim was not placed. The rung stays open."
                    ),
                    jump_url=managed.jump_url,
                )
            )
            return

        outcome = await self._sell(managed, qty, action.trigger_price)
        if not outcome.any_fill:
            tick.add(
                error_note(
                    title=f"Trim +{action.level_pct}% did not fill",
                    detail=f"{outcome.detail}\nThe rung stays open and will be retried.",
                    jump_url=managed.jump_url,
                )
            )
            return

        # Record only what actually filled, and consume the rung either way:
        # a partially filled trim has had its chance at this level.
        apply_action(state, Trim(action.level_pct, outcome.filled_qty, action.trigger_price))
        proceeds = outcome.filled_qty * (outcome.avg_price or 0.0) * 100
        realized = (outcome.avg_price or 0.0) - state.entry_fill
        managed.realized += realized * outcome.filled_qty * 100
        self.risk.record_realized(realized * outcome.filled_qty * 100, today_et())

        self.storage.record_order(
            occ_symbol=managed.symbol,
            intent="TRIM",
            side="SELL",
            qty=outcome.filled_qty,
            status=outcome.status,
            position_id=managed.position_id,
            limit_price=outcome.avg_price,
            trim_level_pct=action.level_pct,
            detail=outcome.detail,
        )

        tick.add(
            trim_executed(
                occ_symbol=managed.symbol,
                level_pct=action.level_pct,
                qty=outcome.filled_qty,
                fill_price=outcome.avg_price or 0.0,
                remaining=state.remaining_qty,
                realized=proceeds,
                jump_url=managed.jump_url,
            )
        )

        # Breakeven is driven by the fill, not by the signal -- until these
        # contracts actually sold, we still held them.
        for follow_up in on_trim_fill(state, action.level_pct, self.rules):
            previous = state.stop_price
            apply_action(state, follow_up)
            tick.add(
                stop_moved(
                    occ_symbol=managed.symbol,
                    old=previous,
                    new=follow_up.price,
                    reason=follow_up.reason,
                    jump_url=managed.jump_url,
                )
            )

    async def _do_stop_out(self, managed: Managed, action: StopOut, tick: Tick) -> None:
        state = managed.state
        qty = await safe_sell_qty(self.broker, managed.spec, action.qty)
        if qty == 0:
            tick.add(
                error_note(
                    title="Stop triggered but nothing is sellable",
                    detail=(
                        f"`{managed.symbol}` hit its ${action.stop_price:.2f} stop but the broker "
                        "reports no position. Check the account."
                    ),
                    jump_url=managed.jump_url,
                )
            )
            return

        outcome = await self._sell(managed, qty, action.bid)
        if not outcome.any_fill:
            tick.add(
                error_note(
                    title="STOP DID NOT FILL",
                    detail=(
                        f"`{managed.symbol}` hit its ${action.stop_price:.2f} stop and "
                        f"{self.settings.stops.exit_retries} attempts failed to sell {qty}. "
                        f"{outcome.detail}\nThis position is still open and unprotected — "
                        "consider closing it manually."
                    ),
                    jump_url=managed.jump_url,
                )
            )
            return

        apply_action(state, StopOut(outcome.filled_qty, action.stop_price, action.bid))
        fill_price = outcome.avg_price or 0.0
        realized_per = fill_price - state.entry_fill
        realized = realized_per * outcome.filled_qty * 100
        managed.realized += realized
        self.risk.record_realized(realized, today_et())

        self.storage.record_order(
            occ_symbol=managed.symbol,
            intent="STOP_OUT",
            side="SELL",
            qty=outcome.filled_qty,
            status=outcome.status,
            position_id=managed.position_id,
            limit_price=fill_price,
            detail=f"stop {action.stop_price:.2f}; {outcome.detail}",
        )

        pnl_pct = (realized_per / state.entry_fill * 100) if state.entry_fill else 0.0
        tick.add(
            stopped_out(
                occ_symbol=managed.symbol,
                qty=outcome.filled_qty,
                stop_price=action.stop_price,
                fill_price=fill_price,
                realized=managed.realized,
                pnl_pct=pnl_pct,
                jump_url=managed.jump_url,
            )
        )

    async def _sell(self, managed: Managed, qty: int, bid: float):
        return await execute_exit(
            self.broker,
            managed.spec,
            qty,
            bid=bid,
            through_pct=self.settings.stops.exit_through_pct,
            timeout_seconds=self.settings.entry.fill_timeout_seconds,
            retries=self.settings.stops.exit_retries,
        )

    # --- flatten -----------------------------------------------------------

    async def flatten_all(self) -> list[Notification]:
        """Close every managed position now, at a marketable limit."""
        notes: list[Notification] = []
        for symbol in list(self.managed):
            managed = self.managed.get(symbol)
            if managed is None:
                continue
            quote = None
            try:
                quote = await self.broker.get_quote(managed.spec)
            except BrokerError as exc:
                logger.warning("no quote to price a flatten of %s: %s", symbol, exc)
            bid = (quote.bid if quote and quote.bid else None) or managed.state.entry_fill
            qty = await safe_sell_qty(self.broker, managed.spec, managed.state.remaining_qty)
            if qty == 0:
                notes.append(
                    error_note(
                        title=f"Nothing to flatten in {symbol}",
                        detail="The broker reports no position.",
                    )
                )
                self.forget(symbol)
                continue

            outcome = await self._sell(managed, qty, bid)
            if not outcome.any_fill:
                notes.append(
                    error_note(
                        title=f"FLATTEN FAILED for {symbol}",
                        detail=f"{outcome.detail}\nStill open — close it manually.",
                    )
                )
                continue

            fill_price = outcome.avg_price or 0.0
            realized = (fill_price - managed.state.entry_fill) * outcome.filled_qty * 100
            managed.realized += realized
            self.risk.record_realized(realized, today_et())
            apply_action(managed.state, StopOut(outcome.filled_qty, fill_price, bid))
            self.storage.record_order(
                occ_symbol=symbol,
                intent="FLATTEN",
                side="SELL",
                qty=outcome.filled_qty,
                status=outcome.status,
                position_id=managed.position_id,
                limit_price=fill_price,
            )
            self.storage.save_position(managed.position_id, managed.state, managed.realized)
            pnl_pct = (
                (fill_price - managed.state.entry_fill) / managed.state.entry_fill * 100
                if managed.state.entry_fill
                else 0.0
            )
            notes.append(
                stopped_out(
                    occ_symbol=symbol,
                    qty=outcome.filled_qty,
                    stop_price=fill_price,
                    fill_price=fill_price,
                    realized=managed.realized,
                    pnl_pct=pnl_pct,
                    jump_url=managed.jump_url,
                )
            )
            if managed.state.closed:
                self.forget(symbol)
        return notes

    # --- guards ------------------------------------------------------------

    async def _quote(self, managed: Managed, tick: Tick):
        try:
            return await self.broker.get_quote(managed.spec)
        except BrokerError as exc:
            logger.warning("quote failed for %s: %s", managed.symbol, exc)
            self._note_stale(managed, tick, detail=str(exc))
            return None

    def _note_stale(self, managed: Managed, tick: Tick, detail: str = "") -> None:
        """Warn once per stale episode. With synthetic stops, no quotes means no
        stop -- the failure mode is silence, so it has to be made loud."""
        if managed.stale_warned:
            return
        elapsed = self.settings.stops.stale_quote_seconds
        if managed.last_good_quote is not None:
            gap = (now_et() - managed.last_good_quote.astimezone(now_et().tzinfo)).total_seconds()
            if gap < self.settings.stops.stale_quote_seconds:
                return
            elapsed = gap
        managed.stale_warned = True
        tick.add(stale_quotes(occ_symbol=managed.symbol, seconds=elapsed))

    async def _confirm_still_held(self, managed: Managed, tick: Tick) -> bool:
        """Detect a position that changed without one of our orders.

        Not in the spec, but this account is shared with a process whose panic
        path cancels and flattens account-wide, so a position disappearing
        underneath us is a real possibility rather than a theoretical one.
        """
        try:
            positions = await self.broker.get_positions()
        except BrokerError:
            return True  # a read failure is handled by the stale-quote path

        held = next((p.qty for p in positions if p.occ_symbol == managed.symbol), 0)
        expected = managed.state.remaining_qty
        if held >= expected:
            managed.phantom_warned = False
            return True

        if not managed.phantom_warned:
            managed.phantom_warned = True
            tick.add(phantom_exit(occ_symbol=managed.symbol, expected=expected, actual=held))

        managed.state.remaining_qty = held
        if held <= 0:
            managed.state.closed = True
            self.storage.save_position(managed.position_id, managed.state, managed.realized)
            self.forget(managed.symbol)
            return False
        self.storage.save_position(managed.position_id, managed.state, managed.realized)
        return True

    async def _check_near_close(self) -> list[Notification]:
        """Forced exit is disabled by configuration, so this warning is the only
        thing between a 0DTE runner and a worthless expiry."""
        if not self.managed or self.settings.market.force_exit_enabled:
            return []
        left = minutes_to_close()
        if left is None:
            return []
        if left > self.settings.market.near_close_warning_minutes:
            self._near_close_warned = False
            return []
        if self._near_close_warned:
            return []
        self._near_close_warned = True
        return [
            near_close_warning(
                positions=sorted(self.managed), minutes_left=left
            )
        ]

    async def _post(self, *notes: Notification) -> None:
        if self._notify is None:
            return
        for note in notes:
            await self._notify(note)
