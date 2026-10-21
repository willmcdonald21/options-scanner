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
import time
from dataclasses import dataclass, field
from datetime import datetime

from options_scanner.broker.base import Broker, BrokerError
from options_scanner.config import Settings
from options_scanner.contracts import ContractSpec, build_spec
from options_scanner.execution import execute_exit, safe_sell_qty
from options_scanner.market_hours import is_market_open, minutes_to_close, now_et, today_et
from options_scanner.models import PositionState
from options_scanner.notifier import (
    Notification,
    broker_disconnected,
    broker_reconnected,
    reconciliation,
    sale_pnl,
    error as error_note,
    near_close_warning,
    phantom_exit,
    runner_level as runner_level_note,
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
    RunnerLevel,
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


# Reconnection pacing. The first retry comes quickly, because a dropped socket
# often comes straight back; after that the gap doubles to a ceiling, so a
# Gateway that is down for the weekend is asked every few minutes rather than
# hammered every poll.
RECONNECT_FIRST_SECONDS = 10.0
RECONNECT_MAX_SECONDS = 300.0
# During the session the ceiling is much lower. On 2026-10-19 Gateway finished
# logging in at 10:46:59, but the bot had backed off to five minutes and did
# not try again until 10:50:04 -- a pasted alert at 10:47 failed in between.
RECONNECT_MAX_IN_SESSION_SECONDS = 30.0

# While positions are open and the broker is gone, the outage is restated this
# often. Silence after one alert would be easy to miss; an alert every poll
# (1.5s) was 2,379 pings in 78 minutes on 2026-10-16.
OUTAGE_REMINDER_SECONDS = 900.0


@dataclass
class _Outage:
    """One continuous stretch without a broker connection."""

    started: float          # monotonic
    next_attempt: float     # monotonic
    backoff: float
    last_alert: float       # monotonic


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
        notify_pnl=None,
    ):
        self.settings = settings
        self.storage = storage
        self.risk = risk
        self.broker = broker
        # The P&L channel: one card per sale. None = no P&L cards.
        self._notify_pnl = notify_pnl
        self._notify = notify
        self.rules: RulesConfig = settings.rules()
        # Which trading day realized P&L is booked against. Injectable so a
        # test is deterministic rather than passing only on the day it was
        # written -- the real clock is the default.
        self.today = today_et
        self.managed: dict[str, Managed] = {}
        self._near_close_warned = False
        self._running = False
        self._outage: _Outage | None = None
        # Injectable so a test can move time instead of waiting for it.
        self._monotonic = time.monotonic
        self._is_market_open = is_market_open

    # --- registration ------------------------------------------------------

    def track(self, position_id: int, state: PositionState, jump_url: str | None = None) -> Managed:
        # Re-tracking (a reconcile after a reconnect) must not lose the link
        # back to the alert, or every later card for the position has none.
        existing = self.managed.get(build_spec(state.option).occ_symbol)
        if jump_url is None and existing is not None:
            jump_url = existing.jump_url
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
            notifications.extend(await self._while_disconnected())
            await self._post(*notifications)
            return notifications
        if self._outage is not None:
            # Reconnected by someone else -- a pasted alert connects on demand.
            notifications.extend(await self._finish_outage(self._outage))

        for symbol in list(self.managed):
            managed = self.managed.get(symbol)
            if managed is None:
                continue
            tick = await self.poll_position(managed)
            notifications.extend(tick.notifications)

        notifications.extend(await self._check_near_close())
        await self._post(*notifications)
        return notifications

    # --- broker outages ------------------------------------------------------

    def _open_position_count(self) -> int:
        """Storage, not just self.managed: after a restart with Gateway down,
        reconcile has not run yet, so nothing is tracked in memory even though
        positions are open."""
        try:
            return max(len(self.managed), len(self.storage.open_positions()))
        except Exception:
            return len(self.managed)

    async def _while_disconnected(self) -> list[Notification]:
        """One poll's worth of outage handling: alert once, retry with backoff,
        and announce the return.

        Before this existed the bot posted a critical alert on every poll and
        never reconnected at all -- once Gateway dropped, the bot stayed blind
        until someone restarted it.
        """
        now = self._monotonic()
        notes: list[Notification] = []

        if self._outage is None:
            self._outage = _Outage(
                started=now,
                next_attempt=now + RECONNECT_FIRST_SECONDS,
                backoff=RECONNECT_FIRST_SECONDS,
                last_alert=now,
            )
            logger.warning("broker disconnected; retrying in %.0fs", RECONNECT_FIRST_SECONDS)
            notes.append(
                broker_disconnected(
                    detail="The broker session is not connected.",
                    open_positions=self._open_position_count(),
                )
            )
            return notes

        outage = self._outage
        ceiling = self._reconnect_ceiling()
        if outage.next_attempt - now > ceiling:
            # The market opened while backed off to the overnight gap.
            outage.backoff = ceiling
            outage.next_attempt = now + ceiling
        if now >= outage.next_attempt:
            try:
                await self.broker.connect()
            except Exception as exc:
                logger.warning("reconnect attempt failed: %s", exc)
            if self.broker.is_connected:
                notes.extend(await self._finish_outage(outage))
                return notes
            outage.backoff = min(outage.backoff * 2, ceiling)
            outage.next_attempt = now + outage.backoff
            logger.info("still disconnected; next attempt in %.0fs", outage.backoff)

        open_count = self._open_position_count()
        if open_count and now - outage.last_alert >= OUTAGE_REMINDER_SECONDS:
            outage.last_alert = now
            notes.append(
                broker_disconnected(
                    detail=f"Still not connected after {(now - outage.started) / 60:.0f} min.",
                    open_positions=open_count,
                )
            )
        return notes

    def _reconnect_ceiling(self) -> float:
        try:
            in_session = self._is_market_open()
        except Exception:
            in_session = True  # unsure: prefer retrying too often over too rarely
        return RECONNECT_MAX_IN_SESSION_SECONDS if in_session else RECONNECT_MAX_SECONDS

    async def _finish_outage(self, outage: _Outage) -> list[Notification]:
        """The connection is back: say so, and re-check positions, which may
        have changed while we could not see them -- a fill, an expiry, a
        manual close."""
        self._outage = None
        down = self._monotonic() - outage.started
        logger.info("broker reconnected after %.0fs", down)
        notes: list[Notification] = [broker_reconnected(down_seconds=down)]
        lines, mismatched = await self.reconcile()
        if lines or mismatched:
            notes.append(reconciliation(lines=lines, mismatched=mismatched))
        return notes

    async def ensure_connected(self) -> bool:
        """Connect now if we are not, ignoring the retry schedule.

        For a pasted alert: the trader is waiting on it, so it must not wait
        out a backoff timer when Gateway may already be back. Returns whether
        the broker is connected afterwards.
        """
        if not self.broker.is_connected:
            try:
                await self.broker.connect()
            except Exception as exc:
                logger.warning("on-demand connect failed: %s", exc)
                return False
            if not self.broker.is_connected:
                return False
        if self._outage is not None:
            await self._post(*await self._finish_outage(self._outage))
        return True

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
                    option=state.option,
                    at_price=action.at_price,
                    multiplier=self.rules.trail_schedule[0][1],
                    jump_url=managed.jump_url,
                )
            )
            return

        if isinstance(action, RunnerLevel):
            apply_action(state, action)
            tick.add(
                runner_level_note(
                    option=state.option,
                    level_pct=action.level_pct,
                    trigger_price=action.trigger_price,
                    entry_price=state.entry_fill,
                    remaining=state.remaining_qty,
                    stop_price=state.stop_price,
                    jump_url=managed.jump_url,
                )
            )
            return

        if isinstance(action, SetStop):
            previous = state.stop_price
            apply_action(state, action)
            tick.add(
                stop_moved(
                    option=state.option,
                    old=previous,
                    new=action.price,
                    reason=action.reason,
                    remaining=state.remaining_qty,
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
            # A rung with nothing to sell: a position already down to its
            # runner. Consume it so it is not retried on every quote, but
            # place nothing. (The levels above the ladder are RunnerLevel
            # actions, not zero-quantity trims.)
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
        await self._book_sale(
            managed,
            qty=outcome.filled_qty,
            fill_price=outcome.avg_price or 0.0,
            action=f"TRIM +{action.level_pct}%",
        )

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

        # Breakeven is driven by the fill, not by the signal -- until these
        # contracts actually sold, we still held them. Resolved before the trim
        # card is built so the stop note can ride along on it, the way the
        # advisor's own trim cards carry theirs.
        stop_updates = on_trim_fill(state, action.level_pct, self.rules)
        stop_note = ""
        stop_cards: list[Notification] = []
        for follow_up in stop_updates:
            previous = state.stop_price
            apply_action(state, follow_up)
            stop_note = (
                f"Stop moved to break-even at {follow_up.price:.2f} — the remaining "
                f"{state.remaining_qty} can no longer lose money."
                if follow_up.reason == "breakeven"
                else f"Stop now {follow_up.price:.2f} ({follow_up.reason})."
            )
            stop_cards.append(
                stop_moved(
                    option=state.option,
                    old=previous,
                    new=follow_up.price,
                    reason=follow_up.reason,
                    remaining=state.remaining_qty,
                    jump_url=managed.jump_url,
                )
            )

        # The trim card goes first and the stop cards after it: the stop moved
        # *because* the trim filled, and posting them the other way round
        # announced a break-even stop before the trim that earned it.
        tick.add(
            trim_executed(
                option=state.option,
                level_pct=action.level_pct,
                qty=outcome.filled_qty,
                fill_price=outcome.avg_price or 0.0,
                entry_price=state.entry_fill,
                remaining=state.remaining_qty,
                original_qty=state.original_qty,
                realized=proceeds - outcome.filled_qty * state.entry_fill * 100,
                stop_note=stop_note,
                jump_url=managed.jump_url,
            )
        )
        for card in stop_cards:
            tick.add(card)

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
        await self._book_sale(
            managed, qty=outcome.filled_qty, fill_price=fill_price, action="STOPPED OUT"
        )

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
                option=state.option,
                qty=outcome.filled_qty,
                stop_price=action.stop_price,
                fill_price=fill_price,
                entry_price=state.entry_fill,
                peak_price=state.peak_bid,
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

    async def _book_sale(
        self, managed: Managed, *, qty: int, fill_price: float, action: str
    ) -> float:
        """Book one sale's realized P&L, and report it to the P&L channel.

        Every sale -- trim, stop-out, flatten -- goes through here, which is
        what guarantees the P&L channel misses none. Returns the sale's P&L.
        A failed P&L post is logged and never interrupts the exit itself.
        """
        realized = (fill_price - managed.state.entry_fill) * qty * 100
        managed.realized += realized
        day = self.today()
        self.risk.record_realized(realized, day)

        if self._notify_pnl is not None:
            try:
                await self._notify_pnl(
                    sale_pnl(
                        option=managed.state.option,
                        action=action,
                        trade_pnl=realized,
                        day_pnl=self.risk.day_summary(day)["realized_pnl"],
                    )
                )
            except Exception:
                logger.exception("P&L card for %s failed to post", managed.symbol)
        return realized

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
            await self._book_sale(
                managed, qty=outcome.filled_qty, fill_price=fill_price, action="FLATTENED"
            )
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
                    option=managed.state.option,
                    qty=outcome.filled_qty,
                    stop_price=fill_price,
                    fill_price=fill_price,
                    entry_price=managed.state.entry_fill,
                    peak_price=managed.state.peak_bid,
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
