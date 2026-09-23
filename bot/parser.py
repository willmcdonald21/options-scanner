from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime

from bot.models import (
    BuyEvent,
    ExpiredEvent,
    InfoEvent,
    OptionKey,
    SoldAllEvent,
    TradeEvent,
    TrimEvent,
    UnderlyingKey,
    UnknownEvent,
)

_MONTHS = {
    "Jan": 1, "Feb": 2, "Mar": 3, "Apr": 4, "May": 5, "Jun": 6,
    "Jul": 7, "Aug": 8, "Sep": 9, "Oct": 10, "Nov": 11, "Dec": 12,
}

# "Entered SPY Sep22 '26 769 Call" / "Fully out of NVDA Sep23 '26 230
# Call." -- the one line that carries the full, unambiguous contract
# identity (ticker + real expiry incl. year + strike + right), present on
# BUY / SOLD ALL descriptions.
_CONTRACT_LINE_RE = re.compile(
    r"(?:Entered|Fully out of)\s+"
    r"(?P<ticker>[A-Z]+)\s+"
    r"(?P<mon>[A-Za-z]{3})(?P<day>\d{1,2})\s*'(?P<yy>\d{2})\s+"
    r"(?P<strike>[\d.]+)\s+"
    r"(?P<right>Call|Put)"
)

# "QQQ 740C · 0DTE" / "SPY 769C · Sep 22" -- title-line shorthand. Used only
# to pull ticker/strike/right for TRIM / SOLD ALL / EXPIRED / milestone
# events, which don't repeat the full contract line -- expiry for those is
# resolved by matching an already-open position, not parsed here.
_TITLE_UNDERLYING_RE = re.compile(r"(?P<ticker>[A-Z]+)\s+(?P<strike>[\d.]+)(?P<right>[CP])\b")

_TITLE_BUY_RE = re.compile(r"^BUY\b", re.IGNORECASE)
_TITLE_AVG_DOWN_RE = re.compile(r"^AVERAGING DOWN\b", re.IGNORECASE)
_TITLE_TRIM_RE = re.compile(r"^TRIM\s*\+?(?P<pct>\d+)%", re.IGNORECASE)
_TITLE_SOLD_ALL_RE = re.compile(r"^SOLD ALL\s*(?P<sign>[+-])(?P<pct>\d+(?:\.\d+)?)%", re.IGNORECASE)
_TITLE_EXPIRED_RE = re.compile(r"^EXPIRED\s*·?\s*(?P<result>WIN|LOSS)", re.IGNORECASE)
_TITLE_NEW_ALERT_RE = re.compile(r"^NEW ALERT\b", re.IGNORECASE)
_TITLE_MILESTONE_RE = re.compile(r"^[+-]?\d+(?:\.\d+)?%")

_MONEY_RE = re.compile(r"\$?([\d,]+\.?\d*)")


def _parse_money(value: str) -> float:
    match = _MONEY_RE.search(value)
    if not match:
        raise ValueError(f"Could not parse a dollar amount out of {value!r}")
    return float(match.group(1).replace(",", ""))


def _strip_emoji_prefix(title: str) -> str:
    """Embed titles start with an emoji the plain-text chat export either
    drops or renders as a leading space (e.g. " BUY — SPY 769C"). Strip
    any leading non-ASCII/whitespace so title regexes can anchor on ^."""
    return re.sub(r"^[^\x00-\x7F]*\s*", "", title).strip()


def _parse_contract_line(description: str, message_timestamp: datetime) -> OptionKey:
    match = _CONTRACT_LINE_RE.search(description)
    if not match:
        raise ValueError(f"No recognizable contract line in description: {description!r}")
    mon = _MONTHS.get(match.group("mon").capitalize())
    if mon is None:
        raise ValueError(f"Unrecognized month abbreviation in {description!r}")
    year = 2000 + int(match.group("yy"))
    expiry = date(year, mon, int(match.group("day")))
    return OptionKey(
        ticker=match.group("ticker"),
        expiry=expiry,
        strike=float(match.group("strike")),
        right="C" if match.group("right").lower() == "call" else "P",
    )


def _parse_title_underlying(title: str) -> UnderlyingKey:
    match = _TITLE_UNDERLYING_RE.search(title)
    if not match:
        raise ValueError(f"No recognizable ticker/strike/right in title: {title!r}")
    return UnderlyingKey(
        ticker=match.group("ticker"),
        strike=float(match.group("strike")),
        right=match.group("right"),
    )


@dataclass
class ParsedEmbed:
    """Normalized shape both a real discord.Embed (via .to_dict()) and the
    plain-text chat export can be converted into, so parse_embed() has one
    input format regardless of source."""

    message_id: int
    title: str
    description: str
    fields: dict[str, str]
    footer: str
    timestamp: datetime


def parse_embed(embed: ParsedEmbed) -> TradeEvent:
    title = _strip_emoji_prefix(embed.title)

    try:
        if _TITLE_BUY_RE.match(title):
            return _parse_buy(embed, title)
        if _TITLE_NEW_ALERT_RE.match(title):
            return InfoEvent(message_id=embed.message_id, kind="new_alert", raw_title=title)
        if _TITLE_AVG_DOWN_RE.match(title):
            return InfoEvent(message_id=embed.message_id, kind="averaging_down", raw_title=title)
        if _TITLE_TRIM_RE.match(title):
            return _parse_trim(embed, title)
        if _TITLE_SOLD_ALL_RE.match(title):
            return _parse_sold_all(embed, title)
        if _TITLE_EXPIRED_RE.match(title):
            return _parse_expired(embed, title)
        if _TITLE_MILESTONE_RE.match(title):
            return InfoEvent(message_id=embed.message_id, kind="milestone", raw_title=title)
    except ValueError as exc:
        return UnknownEvent(message_id=embed.message_id, raw_embed=embed.__dict__, reason=str(exc))

    return UnknownEvent(
        message_id=embed.message_id, raw_embed=embed.__dict__, reason=f"Unrecognized title pattern: {title!r}"
    )


def _parse_buy(embed: ParsedEmbed, title: str) -> BuyEvent:
    option = _parse_contract_line(embed.description, embed.timestamp)
    is_lotto = "lotto" in embed.description.lower() or any("lotto" in v.lower() for v in embed.fields.values())
    return BuyEvent(
        message_id=embed.message_id,
        option=option,
        entry_price=_parse_money(embed.fields["Entry"]),
        contracts=int(embed.fields["Contracts"]),
        cost=_parse_money(embed.fields["Cost"]),
        is_lotto=is_lotto,
    )


# "Sold 12 of 20 · avg $1.328 · 8 still running" (multiple tiers
# bundled into one message, · before "avg") or "Sold 3 of 15 @ $1.069 ·
# 12 still running." (a single tier's own fill price, no · before "@") --
# both forms seen live.
_TRIM_SUMMARY_RE = re.compile(
    r"Sold\s+(\d+)\s+of\s+(\d+)\s*(?:·\s*)?(?:avg\s*\$?|@\s*\$?)([\d.]+)\s*·\s*(\d+)\s+still running"
)


def _parse_trim(embed: ParsedEmbed, title: str) -> TrimEvent:
    underlying = _parse_title_underlying(title)
    pct_match = _TITLE_TRIM_RE.match(title)
    summary_match = _TRIM_SUMMARY_RE.search(embed.description)
    if not summary_match:
        raise ValueError(f"No 'Sold N of M ... still running' line in TRIM description: {embed.description!r}")
    return TrimEvent(
        message_id=embed.message_id,
        underlying=underlying,
        tier_pct=float(pct_match.group("pct")) / 100.0,
        sold_this_event=int(summary_match.group(1)),
        channel_total_before=int(summary_match.group(2)),
        channel_remaining_after=int(summary_match.group(4)),
        avg_exit_price=float(summary_match.group(3)),
    )


def _parse_sold_all(embed: ParsedEmbed, title: str) -> SoldAllEvent:
    underlying = _parse_title_underlying(title)
    pct_match = _TITLE_SOLD_ALL_RE.match(title)
    sign = -1 if pct_match.group("sign") == "-" else 1
    exit_field = embed.fields.get("Entry → avg exit", "")
    avg_exit_price = 0.0
    if "→" in exit_field:
        avg_exit_price = _parse_money(exit_field.split("→")[1])
    return SoldAllEvent(
        message_id=embed.message_id,
        underlying=underlying,
        realized_pct=sign * float(pct_match.group("pct")) / 100.0,
        avg_exit_price=avg_exit_price,
    )


def _parse_expired(embed: ParsedEmbed, title: str) -> ExpiredEvent:
    underlying = _parse_title_underlying(title)
    match = _TITLE_EXPIRED_RE.match(title)
    return ExpiredEvent(message_id=embed.message_id, underlying=underlying, won=match.group("result").upper() == "WIN")
