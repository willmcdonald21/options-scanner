"""The positions board: one Discord message that always shows what is open.

It lives in its own channel, posted through its own webhook, and is edited in
place rather than re-posted, so the channel holds one card that is always
current instead of a feed to scroll.

Two halves:
  * `snapshot` + `render_board` -- read the live state and turn it into a card.
    Pure apart from the reads, so every line of the card is tested offline.
  * `PositionsBoard` -- keeps that one message up to date.

The board is a report, never a control. It reads what the position manager
already knows (the manager stores each poll's bid), so it adds no quote
requests, no market-data lines and no IBKR pacing, and it runs as its own task
so a slow Discord can never delay a stop.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from options_scanner.market_hours import EASTERN, describe as describe_session, now_et
from options_scanner.models import OptionKey
from options_scanner.notifier import (
    COLOR_GREEN,
    COLOR_GREY,
    COLOR_RED,
    COLOR_YELLOW,
    Level,
    Notification,
    contract_label,
    price,
    signed_money,
    signed_pct,
)
from options_scanner.rules import next_level

BOARD_FOOTER = "options-scanner · positions board · edited in place"


@dataclass(frozen=True)
class BoardRow:
    """One open position, as the board shows it."""

    option: OptionKey
    entry: float
    remaining: int
    original: int
    bid: float | None
    peak: float
    stop: float | None
    realized: float
    next_level: tuple[int, float] | None
    quote_age: float | None  # seconds since the last good quote; None = none yet

    @property
    def unrealized(self) -> float | None:
        """Valued on the bid: what the position would sell for now, and the
        same number the stops judge against."""
        if self.bid is None:
            return None
        return (self.bid - self.entry) * self.remaining * 100

    @property
    def gain_pct(self) -> float | None:
        if self.bid is None or not self.entry:
            return None
        return (self.bid / self.entry - 1.0) * 100.0


@dataclass(frozen=True)
class BoardSnapshot:
    mode: str
    rows: tuple[BoardRow, ...]
    realized_today: float
    # None when there is no broker at all (dry run).
    broker_connected: bool | None
    outage_seconds: float | None
    halted: bool
    halt_reason: str
    session: str
    stale_after: float
    # The profit level that sets the first stop: the first step stop, or with
    # no steps the first trim (breakeven waits for it to fill).
    first_stop_pct: int | None = None

    @property
    def unrealized(self) -> float:
        return sum(row.unrealized for row in self.rows if row.unrealized is not None)

    @property
    def day_total(self) -> float:
        return self.realized_today + self.unrealized

    @property
    def healthy(self) -> bool:
        return self.broker_connected is not False and not self.halted


def _age(asof: datetime | None, now: datetime) -> float | None:
    if asof is None:
        return None
    return max(0.0, (now - asof.astimezone(EASTERN)).total_seconds())


def snapshot(*, settings, storage, risk, manager=None, broker=None, now: datetime | None = None) -> BoardSnapshot:
    """Read the live state the board shows.

    From the position manager when there is one: its positions carry the last
    bid. Without one (dry run) from storage, which has no prices.
    """
    now = now or now_et()
    rules = manager.rules if manager is not None else settings.rules()
    rows: list[BoardRow] = []

    if manager is not None:
        held = [
            (m.state, m.last_bid, m.realized, _age(m.last_good_quote, now))
            for m in manager.managed.values()
        ]
    else:
        held = [
            (state, None, storage.position_realized(pid), None)
            for pid, state in storage.open_positions()
        ]

    for state, bid, realized, age in held:
        rows.append(
            BoardRow(
                option=state.option,
                entry=state.entry_fill,
                remaining=state.remaining_qty,
                original=state.original_qty,
                bid=bid,
                peak=state.peak_bid,
                stop=state.stop_price,
                realized=realized,
                next_level=next_level(state, rules),
                quote_age=age,
            )
        )
    rows.sort(key=lambda r: (r.option.expiry, r.option.ticker, r.option.strike, r.option.right))

    return BoardSnapshot(
        mode=settings.mode.value,
        rows=tuple(rows),
        realized_today=float(risk.day_summary()["realized_pnl"]),
        broker_connected=None if broker is None else bool(broker.is_connected),
        outage_seconds=manager.outage_seconds() if manager is not None else None,
        halted=storage.is_halted(),
        halt_reason=storage.halt_reason(),
        session=describe_session(now),
        stale_after=settings.stops.stale_quote_seconds,
        first_stop_pct=_first_stop_pct(settings),
    )


def _first_stop_pct(settings) -> int | None:
    if settings.stops.steps:
        return min(level for level, _ in settings.stops.steps)
    if settings.trim.schedule:
        return min(level for level, _ in settings.trim.schedule)
    return None


# --- rendering --------------------------------------------------------------


def _stop_text(row: BoardRow, first_stop_pct: int | None) -> str:
    if row.stop is None:
        until = f" until +{first_stop_pct}%" if first_stop_pct else ""
        return f"Stop **none{until}**"
    locks = (row.stop / row.entry - 1.0) * 100.0 if row.entry else 0.0
    why = "break-even" if abs(locks) < 0.05 else f"locks {signed_pct(locks)}"
    return f"Stop {price(row.stop)} ({why})"


def _row_text(row: BoardRow, snap: BoardSnapshot) -> str:
    lines = []
    if row.bid is None:
        lines.append(f"No quote yet · entry {price(row.entry)}")
    else:
        lines.append(f"**{signed_pct(row.gain_pct)}** · bid {price(row.bid)} · entry {price(row.entry)}")

    held = f"{row.remaining} of {row.original}"
    if row.unrealized is None:
        lines.append(f"Open P&L — on {held}")
    else:
        lines.append(f"Open P&L **{signed_money(row.unrealized)}** on {held}")
    if row.realized:
        lines[-1] += f" · realized {signed_money(row.realized)}"

    lines.append(f"{_stop_text(row, snap.first_stop_pct)} · peak {price(row.peak)}")
    if row.next_level is not None:
        level, at = row.next_level
        lines.append(f"Next +{level}% at {price(at)}")
    if row.quote_age is not None and row.quote_age >= snap.stale_after:
        lines.append(f"⚠️ last quote {row.quote_age:.0f}s ago")
    return "\n".join(lines)


def _health_line(snap: BoardSnapshot) -> str:
    parts = [f"`{snap.mode}`"]
    if snap.broker_connected is None:
        parts.append("no broker (dry run)")
    elif snap.broker_connected:
        parts.append("broker connected")
    else:
        down = f" for {snap.outage_seconds / 60:.0f} min" if snap.outage_seconds else ""
        parts.append(f"**broker down{down}**")
    parts.append(f"**entries halted** ({snap.halt_reason})" if snap.halted else "taking entries")
    line = " · ".join(parts)
    if snap.broker_connected is False and snap.rows:
        line += "\nThe stops cannot see prices until it is back."
    return line


def _color(snap: BoardSnapshot) -> int:
    if not snap.healthy:
        return COLOR_YELLOW
    if not snap.rows:
        return COLOR_GREY
    return COLOR_GREEN if snap.day_total >= 0 else COLOR_RED


def render_board(snap: BoardSnapshot, *, updated_at: datetime | None) -> Notification:
    """The board card. `updated_at` None leaves out the timestamp line, which
    is how the refresher tells a real change from the clock ticking."""
    title = "Open positions" if snap.rows else "No open positions"
    if snap.mode == "dry_run":
        title = f"[DRY RUN] {title}"

    day = f"**Day P&L {signed_money(snap.day_total)}**"
    if snap.rows:
        day += f" · realized {signed_money(snap.realized_today)} · open {signed_money(snap.unrealized)}"
    description = [day, _health_line(snap), snap.session]
    if updated_at is not None:
        # Discord renders these in the viewer's own clock and keeps the
        # relative one ticking, so a board whose bot has died visibly ages
        # ("14 minutes ago") instead of passing for live.
        stamp = int(updated_at.timestamp())
        description.append(f"Updated <t:{stamp}:T> (<t:{stamp}:R>)")

    note = Notification(
        title=title,
        level=Level.INFO,
        description="\n".join(description),
        footer=BOARD_FOOTER,
        color=_color(snap),
    )
    for row in snap.rows:
        note.add(contract_label(row.option), _row_text(row, snap), inline=False)
    return note


def offline_board(*, stopped_at: datetime, open_positions: int) -> Notification:
    """What the board says once the bot has stopped: it is no longer live, and
    anything open has no synthetic stop until the bot is back."""
    stamp = int(stopped_at.timestamp())
    lines = [f"Stopped <t:{stamp}:T> (<t:{stamp}:R>). This board is not updating."]
    if open_positions:
        lines.append(
            f"**{open_positions} position{'' if open_positions == 1 else 's'} still open with no "
            "stop** until the bot is running again."
        )
    return Notification(
        title="Bot offline",
        level=Level.INFO,
        description="\n".join(lines),
        footer=BOARD_FOOTER,
        color=COLOR_GREY,
    )
