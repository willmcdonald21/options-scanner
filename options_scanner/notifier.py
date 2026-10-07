"""Discord notifications.

Every message the bot produces is built here as a `Notification` -- a plain
dataclass -- and only turned into a `discord.Embed` at the edge. That split is
what lets the whole notification surface be asserted in tests without a
Discord connection.

Conventions, from the spec:
  * everything goes to the updates channel; the alerts channel only ever
    receives a status reaction
  * green for profit events, red for stops and errors, yellow for warnings
  * every notification carries a jump link back to the alert that caused it
  * critical events @-mention the owner so they generate a push notification
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any

# Discord embed colours.
COLOR_GREEN = 0x2ECC71
COLOR_RED = 0xE74C3C
COLOR_YELLOW = 0xF1C40F
COLOR_BLUE = 0x3498DB
COLOR_GREY = 0x95A5A6

# Reactions on the alert message itself. The bot posts no text in the alerts
# channel, so these are the only signal there that a paste was picked up --
# which is exactly why "seen" has to be applied before anything can fail.
REACTION_RECEIVED = "\N{EYES}"
REACTION_PLACED = "\N{WHITE HEAVY CHECK MARK}"
REACTION_REJECTED = "\N{CROSS MARK}"
REACTION_SKIPPED = "\N{NO ENTRY SIGN}"


class Level(str, Enum):
    """Severity, which decides colour and whether the owner is pinged."""

    INFO = "info"
    SUCCESS = "success"
    WARNING = "warning"
    ERROR = "error"
    CRITICAL = "critical"

    @property
    def color(self) -> int:
        return {
            Level.INFO: COLOR_BLUE,
            Level.SUCCESS: COLOR_GREEN,
            Level.WARNING: COLOR_YELLOW,
            Level.ERROR: COLOR_RED,
            Level.CRITICAL: COLOR_RED,
        }[self]

    @property
    def mentions_owner(self) -> bool:
        """Only things that need a human now. Pinging on routine events trains
        the ping to be ignored, which is worse than not pinging at all."""
        return self in (Level.ERROR, Level.CRITICAL)


@dataclass
class Notification:
    """One message destined for the updates channel."""

    title: str
    level: Level = Level.INFO
    description: str = ""
    fields: list[tuple[str, str, bool]] = field(default_factory=list)
    jump_url: str | None = None
    footer: str = ""

    def add(self, name: str, value: Any, inline: bool = True) -> "Notification":
        self.fields.append((name, str(value), inline))
        return self

    @property
    def mentions_owner(self) -> bool:
        return self.level.mentions_owner

    def to_embed(self, dry_run: bool = False):
        """Build the discord.Embed. Imported lazily so this module -- and
        everything that builds notifications -- stays importable without
        discord.py installed."""
        import discord

        title = f"[DRY RUN] {self.title}" if dry_run else self.title
        embed = discord.Embed(title=title, description=self.description, color=self.level.color)
        for name, value, inline in self.fields:
            embed.add_field(name=name, value=value or "​", inline=inline)
        if self.jump_url:
            embed.add_field(name="Alert", value=f"[jump to alert]({self.jump_url})", inline=False)
        if self.footer:
            embed.set_footer(text=self.footer)
        return embed

    def to_text(self) -> str:
        """Plain-text rendering, for log lines and for tests."""
        parts = [f"[{self.level.value.upper()}] {self.title}"]
        if self.description:
            parts.append(self.description)
        parts.extend(f"{name}: {value}" for name, value, _ in self.fields)
        return " | ".join(parts)


# --- builders --------------------------------------------------------------
#
# One function per event the spec lists, so the set of things the bot can say
# is enumerable in one place and each is individually testable.


def money(value: float) -> str:
    return f"${value:,.2f}"


def price(value: float) -> str:
    """Sub-dollar prices keep three decimals: 0DTE options trade in tenths of a
    cent, and rounding to two would hide the gap between two rungs. The 1e-9
    nudge stops a value like 0.7125 printing as 0.712 and looking like a
    mismatch against the advisor's own 0.713."""
    if value < 1:
        # Three decimals, but never fewer than two: "$0.8" reads like a typo
        # where "$0.80" reads like a price.
        text = f"{value + 1e-9:.3f}"
        if text.endswith("0"):
            text = text[:-1]
        return f"${text}"
    return f"${value + 1e-9:.2f}"


def signed_pct(value: float) -> str:
    return f"{value:+.1f}%"


def alert_parsed(
    *,
    occ_symbol: str,
    description: str,
    entry_price: float,
    advisor_contracts: int,
    our_contracts: int,
    cost: float,
    jump_url: str | None,
    levels: list[tuple[int, float]],
) -> Notification:
    note = Notification(
        title=f"Alert parsed — {description}",
        level=Level.INFO,
        jump_url=jump_url,
    )
    note.add("Contract", f"`{occ_symbol}`", inline=False)
    note.add("Advisor entry", price(entry_price))
    note.add("Advisor size", f"{advisor_contracts} contracts")
    note.add("Our size", f"{our_contracts} contracts ≈ {money(cost)}")
    if levels:
        note.add(
            "Trim ladder",
            "\n".join(f"+{lvl}% → {price(p)}" for lvl, p in levels),
            inline=False,
        )
    return note


def alert_rejected(*, reason: str, detail: str, raw_title: str, jump_url: str | None) -> Notification:
    note = Notification(
        title="Alert rejected — not traded",
        level=Level.WARNING,
        description=detail,
        jump_url=jump_url,
    )
    note.add("Reason", f"`{reason}`")
    if raw_title:
        note.add("Card", raw_title, inline=False)
    return note


def alert_skipped(*, reason: str, detail: str, jump_url: str | None) -> Notification:
    note = Notification(
        title="Alert skipped — risk gate",
        level=Level.WARNING,
        description=detail,
        jump_url=jump_url,
    )
    note.add("Reason", f"`{reason}`")
    return note


def alert_duplicate(*, detail: str, jump_url: str | None) -> Notification:
    return Notification(
        title="Duplicate alert ignored",
        level=Level.INFO,
        description=detail,
        jump_url=jump_url,
    )


def alert_informational(*, kind: str, raw_title: str, detail: str, jump_url: str | None) -> Notification:
    note = Notification(
        title="Informational card — no action",
        level=Level.INFO,
        description=detail,
        jump_url=jump_url,
    )
    note.add("Kind", f"`{kind}`")
    note.add("Card", raw_title, inline=False)
    return note


def unreadable_paste(*, preview: str, jump_url: str | None) -> Notification:
    """No card found at all. Reported rather than ignored, because a dropped
    paste and an unreadable paste look identical from the alerts channel."""
    return Notification(
        title="Could not read that paste",
        level=Level.WARNING,
        description=(
            "No alert card was found in that message, so nothing was traded. "
            "If it was meant to be an alert, the format has probably changed."
        ),
        fields=[("First 120 chars", f"```{preview[:120]}```", False)],
        jump_url=jump_url,
    )


def would_place_entry(
    *,
    occ_symbol: str,
    qty: int,
    limit_price: float,
    cap_price: float,
    timeout_seconds: float,
    jump_url: str | None,
) -> Notification:
    """DRY_RUN: the order that would have gone out."""
    note = Notification(
        title="Would place entry order",
        level=Level.INFO,
        description="Dry run — nothing was sent to the broker.",
        jump_url=jump_url,
    )
    note.add("Contract", f"`{occ_symbol}`", inline=False)
    note.add("Order", f"BUY {qty} @ limit {price(limit_price)}")
    note.add("Walk to", f"{price(cap_price)} max")
    note.add("Give up after", f"{timeout_seconds:.0f}s")
    return note


def order_submitted(*, occ_symbol: str, side: str, qty: int, limit_price: float, jump_url: str | None) -> Notification:
    note = Notification(title=f"{side} order submitted", level=Level.INFO, jump_url=jump_url)
    note.add("Contract", f"`{occ_symbol}`", inline=False)
    note.add("Order", f"{side} {qty} @ limit {price(limit_price)}")
    return note


def entry_filled(*, occ_symbol: str, qty: int, fill_price: float, cost: float, jump_url: str | None) -> Notification:
    note = Notification(
        title="Entry filled",
        level=Level.SUCCESS,
        description="All later maths uses this fill price, not the advisor's entry.",
        jump_url=jump_url,
    )
    note.add("Contract", f"`{occ_symbol}`", inline=False)
    note.add("Filled", f"{qty} @ {price(fill_price)}")
    note.add("Cost", money(cost))
    return note


def partial_fill(*, occ_symbol: str, filled: int, requested: int, fill_price: float, jump_url: str | None) -> Notification:
    return Notification(
        title="Partial fill",
        level=Level.WARNING,
        description=f"Filled {filled} of {requested} requested. The ladder will work off {filled}.",
        fields=[("Contract", f"`{occ_symbol}`", False), ("Average", price(fill_price), True)],
        jump_url=jump_url,
    )


def entry_unfilled(*, occ_symbol: str, qty: int, cap_price: float, timeout_seconds: float, jump_url: str | None) -> Notification:
    return Notification(
        title="Entry not filled — cancelled",
        level=Level.WARNING,
        description=(
            f"No fill for {qty} {occ_symbol} within {timeout_seconds:.0f}s at or below "
            f"{price(cap_price)}. Not chasing."
        ),
        jump_url=jump_url,
    )


def trim_executed(
    *,
    occ_symbol: str,
    level_pct: int,
    qty: int,
    fill_price: float,
    remaining: int,
    realized: float,
    jump_url: str | None,
) -> Notification:
    note = Notification(title=f"Trim +{level_pct}% executed", level=Level.SUCCESS, jump_url=jump_url)
    note.add("Contract", f"`{occ_symbol}`", inline=False)
    note.add("Sold", f"{qty} @ {price(fill_price)}")
    note.add("Remaining", f"{remaining} contracts")
    note.add("Locked in", money(realized))
    return note


def stop_moved(*, occ_symbol: str, old: float | None, new: float, reason: str, jump_url: str | None) -> Notification:
    was = price(old) if old is not None else "none"
    explanation = {
        "breakeven": "First trim filled — the position can no longer lose money.",
        "trail": "Trailing stop ratcheted up with a new peak.",
    }.get(reason, reason)
    note = Notification(
        title="Stop moved up",
        level=Level.SUCCESS,
        description=explanation,
        jump_url=jump_url,
    )
    note.add("Contract", f"`{occ_symbol}`", inline=False)
    note.add("Stop", f"{was} → {price(new)}")
    note.add("Why", f"`{reason}`")
    return note


def trail_armed(*, occ_symbol: str, at_price: float, giveback_pct: float, jump_url: str | None) -> Notification:
    note = Notification(
        title="Trailing stop armed",
        level=Level.SUCCESS,
        description=f"From here the stop gives back at most {giveback_pct:.0f}% of the gain from the peak.",
        jump_url=jump_url,
    )
    note.add("Contract", f"`{occ_symbol}`", inline=False)
    note.add("Armed at", price(at_price))
    return note


def stopped_out(
    *,
    occ_symbol: str,
    qty: int,
    stop_price: float,
    fill_price: float,
    realized: float,
    pnl_pct: float,
    jump_url: str | None,
) -> Notification:
    note = Notification(
        title="Stopped out — position closed",
        level=Level.ERROR if pnl_pct < 0 else Level.SUCCESS,
        jump_url=jump_url,
    )
    note.add("Contract", f"`{occ_symbol}`", inline=False)
    note.add("Sold", f"{qty} @ {price(fill_price)}")
    note.add("Stop was", price(stop_price))
    note.add("Realized", f"{money(realized)} ({signed_pct(pnl_pct)})")
    return note


def near_close_warning(*, positions: list[str], minutes_left: float) -> Notification:
    """Forced EOD exit is off by configuration, so this is the only thing
    standing between a runner and a worthless expiry."""
    return Notification(
        title="Positions still open near the close",
        level=Level.CRITICAL,
        description=(
            f"{minutes_left:.0f} minutes to the close and forced exit is disabled. "
            "A 0DTE runner whose trail never trips will expire worthless. "
            "Use `!flatten` to close everything now."
        ),
        fields=[("Open", "\n".join(f"`{p}`" for p in positions), False)],
    )


def stale_quotes(*, occ_symbol: str, seconds: float) -> Notification:
    return Notification(
        title="Quotes are stale — stops are blind",
        level=Level.CRITICAL,
        description=(
            f"No usable quote for `{occ_symbol}` in {seconds:.0f}s. The synthetic stop cannot "
            "trigger without quotes. Check the broker connection."
        ),
    )


def broker_disconnected(*, detail: str) -> Notification:
    return Notification(
        title="Broker disconnected",
        level=Level.CRITICAL,
        description=f"{detail}\nOpen positions are unmanaged until this is restored.",
    )


def phantom_exit(*, occ_symbol: str, expected: int, actual: int) -> Notification:
    """The shared paper account makes this a real possibility: warrior_bot's
    panic path cancels and flattens account-wide, not per client."""
    return Notification(
        title="Position changed without our order",
        level=Level.CRITICAL,
        description=(
            f"We hold {expected} `{occ_symbol}` but the broker reports {actual}. "
            "Something outside this bot moved the position — most likely an account-wide "
            "cancel or flatten from another process on this account."
        ),
    )


def daily_loss_limit(*, realized: float, limit: float) -> Notification:
    return Notification(
        title="Daily loss limit hit — entries halted",
        level=Level.CRITICAL,
        description=(
            f"Realized {money(realized)} against a {money(limit)} limit. New entries are "
            "halted for the day; open positions are still managed. `!resume` to override."
        ),
    )


def halted(*, reason: str) -> Notification:
    return Notification(
        title="New entries halted",
        level=Level.WARNING,
        description=f"{reason}\nOpen positions are still being managed.",
    )


def resumed() -> Notification:
    return Notification(title="New entries resumed", level=Level.SUCCESS)


def flatten_requested(*, positions: list[str]) -> Notification:
    return Notification(
        title="Flatten everything?",
        level=Level.WARNING,
        description=(
            "Reply `!flatten confirm` within 60 seconds to close every bot-managed position "
            "at a marketable limit. Anything else cancels."
        ),
        fields=[("Would close", "\n".join(f"`{p}`" for p in positions) or "nothing", False)],
    )


def startup(*, mode: str, broker: str, summary: str, session: str, open_positions: int) -> Notification:
    note = Notification(
        title="Bot online",
        level=Level.INFO if mode != "live" else Level.WARNING,
        description=summary,
    )
    note.add("Mode", f"`{mode}`")
    note.add("Broker", f"`{broker}`")
    note.add("Session", session, inline=False)
    note.add("Resumed positions", str(open_positions))
    return note


def reconciliation(*, lines: list[str], mismatched: bool) -> Notification:
    return Notification(
        title="Restart reconciliation" + (" — MISMATCH" if mismatched else ""),
        level=Level.CRITICAL if mismatched else Level.INFO,
        description="\n".join(lines) or "No open positions to restore.",
    )


def status_report(*, mode: str, halted_now: bool, session: str, lines: list[str], day: dict) -> Notification:
    note = Notification(
        title="Status",
        level=Level.INFO,
        description="\n\n".join(lines) or "No open positions.",
    )
    note.add("Mode", f"`{mode}`" + (" (halted)" if halted_now else ""))
    note.add("Session", session, inline=False)
    note.add(
        "Today",
        f"{day['entries']} entries · {money(day['realized_pnl'])} realized · "
        f"{day['alerts_received']} alerts ({day['alerts_rejected']} rejected, "
        f"{day['alerts_skipped']} skipped)",
        inline=False,
    )
    return note


def end_of_day(*, day: dict, open_positions: list[str]) -> Notification:
    note = Notification(
        title="End of day",
        level=Level.SUCCESS if day["realized_pnl"] >= 0 else Level.ERROR,
    )
    note.add("Entries", str(day["entries"]))
    note.add("Realized", money(day["realized_pnl"]))
    note.add("Alerts", f"{day['alerts_received']} received, {day['alerts_rejected']} rejected, "
                       f"{day['alerts_skipped']} skipped")
    if open_positions:
        note.add("Still open", "\n".join(f"`{p}`" for p in open_positions), inline=False)
    return note


def error(*, title: str, detail: str, jump_url: str | None = None) -> Notification:
    return Notification(title=title, level=Level.ERROR, description=detail, jump_url=jump_url)
