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
from datetime import date
from enum import Enum
from typing import Any

from options_scanner.models import OptionKey

_MONTH_NAMES = (
    "Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec",
)

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

    def to_webhook_embed(self) -> dict:
        """The same card as a raw Discord webhook payload. Webhooks take plain
        JSON rather than a discord.Embed, so this is built by hand and the
        module stays importable without discord.py."""
        embed: dict = {"title": self.title, "color": self.level.color}
        if self.description:
            embed["description"] = self.description
        fields = [
            {"name": name, "value": value or "\u200b", "inline": inline}
            for name, value, inline in self.fields
        ]
        if self.jump_url:
            fields.append(
                {"name": "Alert", "value": f"[jump to alert]({self.jump_url})", "inline": False}
            )
        if fields:
            embed["fields"] = fields
        embed["footer"] = {"text": self.footer or FOOTER}
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


# Mirrors the advisor's own card footer, so our cards read as the same kind of
# object while still being obviously ours.
FOOTER = "options-scanner · real trade via IBKR · not financial advice"


def contract_label(option: OptionKey, today: date | None = None) -> str:
    """The advisor's title shorthand: "SPX 7815P · 0DTE", or "· Oct 9" when it
    is not expiring today. Uses the underlying ticker, not the trading class --
    the advisor writes SPX even though the contract trades as SPXW."""
    if today is None:
        from options_scanner.market_hours import today_et

        today = today_et()
    strike = f"{option.strike:g}"
    tag = "0DTE" if option.expiry == today else f"{_MONTH_NAMES[option.expiry.month - 1]} {option.expiry.day}"
    return f"{option.ticker} {strike}{option.right} · {tag}"


def contract_sentence(option: OptionKey, verb: str = "Entered") -> str:
    """The advisor's description line: "Entered SPX Oct06 '26 7815 Put"."""
    month = _MONTH_NAMES[option.expiry.month - 1]
    right = "Call" if option.right == "C" else "Put"
    return (
        f"{verb} {option.ticker} {month}{option.expiry.day:02d} "
        f"'{option.expiry:%y} {option.strike:g} {right}"
    )


def ladder_block(levels: list[tuple[int, float]]) -> str:
    """The advisor's ladder rows, with the percentages column-aligned the same
    way: "25%   $0.594" / "100%  $0.950"."""
    return "\n".join(f"{str(level) + '%':<6}{price(p)}" for level, p in levels)


def money(value: float) -> str:
    """Sign before the currency symbol: "-$72.00", not "$-72.00"."""
    sign = "-" if value < 0 else ""
    return f"{sign}${abs(value):,.2f}"


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
    option: OptionKey,
    occ_symbol: str,
    entry_price: float,
    advisor_contracts: int,
    our_contracts: int,
    cost: float,
    jump_url: str | None,
    levels: list[tuple[int, float]],
    today: date | None = None,
) -> Notification:
    """What we read off the card, before anything is ordered."""
    note = Notification(
        title=f"PARSED — {contract_label(option, today)}",
        level=Level.INFO,
        description=contract_sentence(option),
        jump_url=jump_url,
    )
    note.add("Advisor entry", price(entry_price))
    note.add("Advisor size", f"{advisor_contracts} contracts")
    note.add("Our size", f"{our_contracts} contracts · {money(cost)}")
    if levels:
        note.add("Trim Targets", ladder_block(levels), inline=False)
    note.add("Contract", f"`{occ_symbol}`", inline=False)
    return note


def alert_rejected(
    *, reason: str, detail: str, raw_title: str, jump_url: str | None,
    option: OptionKey | None = None, today: date | None = None,
) -> Notification:
    suffix = f" — {contract_label(option, today)}" if option else ""
    note = Notification(
        title=f"REJECTED{suffix}",
        level=Level.WARNING,
        description=detail,
        jump_url=jump_url,
    )
    note.add("Reason", f"`{reason}`")
    if raw_title:
        note.add("Card", raw_title, inline=False)
    return note


def alert_skipped(
    *, reason: str, detail: str, jump_url: str | None,
    option: OptionKey | None = None, today: date | None = None,
) -> Notification:
    suffix = f" — {contract_label(option, today)}" if option else ""
    note = Notification(
        title=f"SKIPPED{suffix}",
        level=Level.WARNING,
        description=detail,
        jump_url=jump_url,
    )
    note.add("Reason", f"`{reason}`")
    return note


def alert_duplicate(*, detail: str, jump_url: str | None) -> Notification:
    return Notification(
        title="DUPLICATE — ignored",
        level=Level.INFO,
        description=detail,
        jump_url=jump_url,
    )


def alert_informational(*, kind: str, raw_title: str, detail: str, jump_url: str | None) -> Notification:
    note = Notification(
        title="NOTED — no action",
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
        title="UNREADABLE — nothing traded",
        level=Level.WARNING,
        description=(
            "No alert card was found in that message. If it was meant to be an alert, "
            "the format has probably changed."
        ),
        fields=[("First 120 chars", f"```{preview[:120]}```", False)],
        jump_url=jump_url,
    )


def would_place_entry(
    *,
    option: OptionKey,
    qty: int,
    limit_price: float,
    cap_price: float,
    timeout_seconds: float,
    jump_url: str | None,
    today: date | None = None,
) -> Notification:
    """DRY_RUN: the order that would have gone out."""
    note = Notification(
        title=f"WOULD BUY — {contract_label(option, today)}",
        level=Level.INFO,
        description="Dry run — nothing was sent to the broker.",
        jump_url=jump_url,
    )
    note.add("Order", f"BUY {qty} @ limit {price(limit_price)}")
    note.add("Walk to", f"{price(cap_price)} max")
    note.add("Give up after", f"{timeout_seconds:.0f}s")
    return note


def order_submitted(
    *, option: OptionKey, side: str, qty: int, limit_price: float, jump_url: str | None,
    today: date | None = None,
) -> Notification:
    note = Notification(
        title=f"{side} WORKING — {contract_label(option, today)}",
        level=Level.INFO,
        jump_url=jump_url,
    )
    note.add("Order", f"{side} {qty} @ limit {price(limit_price)}")
    return note


def entry_filled(
    *,
    option: OptionKey,
    qty: int,
    fill_price: float,
    cost: float,
    jump_url: str | None,
    levels: list[tuple[int, float]] | None = None,
    sizing_note: str = "",
    today: date | None = None,
) -> Notification:
    """The entry card, in the advisor's own shape -- except the ladder here is
    ours, recomputed off the fill we actually got.

    `sizing_note` says where the size came from (the unit percent, the tier,
    and whether the equity behind it was real or a dry-run assumption). A size
    reported without that is unauditable after the fact.
    """
    description = contract_sentence(option)
    if sizing_note:
        description += f"\n{sizing_note}"
    note = Notification(
        title=f"BUY — {contract_label(option, today)}",
        level=Level.SUCCESS,
        description=description,
        jump_url=jump_url,
    )
    note.add("Entry", price(fill_price))
    note.add("Contracts", str(qty))
    note.add("Cost", money(cost))
    if levels:
        note.add("Trim Targets", ladder_block(levels), inline=False)
    return note


def partial_fill(
    *, option: OptionKey, filled: int, requested: int, fill_price: float, jump_url: str | None,
    today: date | None = None,
) -> Notification:
    return Notification(
        title=f"PARTIAL FILL — {contract_label(option, today)}",
        level=Level.WARNING,
        description=f"Filled {filled} of {requested} requested. The ladder works off {filled}.",
        fields=[("Entry", price(fill_price), True), ("Contracts", str(filled), True)],
        jump_url=jump_url,
    )


def entry_unfilled(
    *, option: OptionKey, qty: int, cap_price: float, timeout_seconds: float,
    jump_url: str | None, today: date | None = None,
) -> Notification:
    return Notification(
        title=f"NO FILL — {contract_label(option, today)}",
        level=Level.WARNING,
        description=(
            f"No fill for {qty} within {timeout_seconds:.0f}s at or below {price(cap_price)}. "
            "Not chasing."
        ),
        jump_url=jump_url,
    )


def trim_executed(
    *,
    option: OptionKey,
    level_pct: int,
    qty: int,
    fill_price: float,
    entry_price: float,
    remaining: int,
    original_qty: int,
    realized: float,
    stop_note: str = "",
    jump_url: str | None = None,
    today: date | None = None,
) -> Notification:
    """The trim card, matching the advisor's: "Sold N of M @ price · K still
    running." followed by Entry / Exit / Locked In."""
    description = f"Sold {qty} of {original_qty} @ {price(fill_price)} · {remaining} still running."
    if stop_note:
        description += f"\n\n{stop_note}"
    note = Notification(
        title=f"TRIM +{level_pct}% — {contract_label(option, today)}",
        level=Level.SUCCESS,
        description=description,
        jump_url=jump_url,
    )
    note.add("Entry", price(entry_price))
    note.add("Exit", price(fill_price))
    note.add("Locked In", f"+{money(realized)}" if realized >= 0 else money(realized))
    return note


def stop_moved(
    *, option: OptionKey, old: float | None, new: float, reason: str, remaining: int = 0,
    jump_url: str | None = None, today: date | None = None,
) -> Notification:
    was = price(old) if old is not None else "none"
    explanation = {
        "breakeven": (
            f"Stop moved to break-even. The remaining {remaining} can no longer lose money."
            if remaining else "Stop moved to break-even — this position can no longer lose money."
        ),
        "trail": "Trailing stop ratcheted up with a new peak.",
    }.get(reason, reason)
    note = Notification(
        title=f"STOP → {price(new)} — {contract_label(option, today)}",
        level=Level.SUCCESS,
        description=explanation,
        jump_url=jump_url,
    )
    note.add("Stop", f"{was} → {price(new)}")
    note.add("Why", f"`{reason}`")
    return note


def trail_armed(
    *, option: OptionKey, at_price: float, multiplier: float, jump_url: str | None = None,
    today: date | None = None,
) -> Notification:
    """The trail is live. `multiplier` is the fraction of the peak the stop
    sits at, so 0.40 reads as "60% below the peak".

    The card says plainly when the trail is not yet worth anything. At 0.40 the
    raw trail is under the entry fill until the peak reaches 1/0.40 = 2.5x
    entry, so for most of the early climb breakeven is the real stop. Saying
    "trailing stop armed" and leaving that out would overstate how protected
    the position is.
    """
    below_pct = (1.0 - multiplier) * 100.0
    description = f"From here the stop trails {below_pct:.0f}% below the peak."
    inert_until = (1.0 / multiplier - 1.0) * 100.0
    description += (
        f"\nBelow a +{inert_until:.0f}% peak that sits under the entry fill, so break-even "
        "is what is actually protecting the position until then."
    )
    note = Notification(
        title=f"TRAIL ARMED — {contract_label(option, today)}",
        level=Level.SUCCESS,
        description=description,
        jump_url=jump_url,
    )
    note.add("Armed at", price(at_price))
    note.add("Trails at", f"{multiplier:.0%} of peak")
    return note


def runner_level(
    *,
    option: OptionKey,
    level_pct: int,
    trigger_price: float,
    entry_price: float,
    remaining: int,
    stop_price: float | None = None,
    jump_url: str | None = None,
    today: date | None = None,
) -> Notification:
    """A milestone on the runner's climb: 75 -> 100 -> 150 -> 200 -> 500 ->
    1000 -> 2000. Nothing is sold.

    These exist so the climb is visible. The alternative -- consuming the level
    silently -- makes a runner that is up +500% look exactly like a bot that
    has stopped reading quotes.
    """
    note = Notification(
        title=f"RUNNER +{level_pct}% — {contract_label(option, today)}",
        level=Level.SUCCESS,
        description=(
            f"The runner reached +{level_pct}%. Nothing sold — {remaining} still running."
        ),
        jump_url=jump_url,
    )
    note.add("Peak", price(trigger_price))
    if stop_price is not None:
        locked = (stop_price / entry_price - 1.0) * 100.0
        note.add("Stop", f"{price(stop_price)} ({signed_pct(locked)})")
    else:
        # Only reachable below the arming level, and only for a position that
        # could not trim. Worth saying rather than printing a blank field.
        note.add("Stop", "none yet")
    return note


def stopped_out(
    *,
    option: OptionKey,
    qty: int,
    stop_price: float,
    fill_price: float,
    entry_price: float = 0.0,
    peak_price: float = 0.0,
    realized: float,
    pnl_pct: float,
    jump_url: str | None = None,
    today: date | None = None,
) -> Notification:
    """The exit card, matching the advisor's SOLD ALL shape."""
    note = Notification(
        title=f"SOLD ALL {signed_pct(pnl_pct)} — {contract_label(option, today)}",
        level=Level.ERROR if pnl_pct < 0 else Level.SUCCESS,
        description=contract_sentence(option, "Fully out of") + ".",
        jump_url=jump_url,
    )
    if entry_price:
        note.add("Entry → avg exit", f"{price(entry_price)} → {price(fill_price)}")
    else:
        note.add("Exit", price(fill_price))
    note.add("Realized", f"+{money(realized)}" if realized >= 0 else money(realized))
    note.add("Stop was", price(stop_price))
    note.add("Contracts", str(qty))
    if peak_price and entry_price:
        peak_pct = (peak_price / entry_price - 1) * 100
        note.add("Peak", f"{signed_pct(peak_pct)} ({price(peak_price)})")
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
