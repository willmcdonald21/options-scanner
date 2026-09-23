from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Literal

Right = Literal["C", "P"]


@dataclass(frozen=True)
class OptionKey:
    ticker: str
    expiry: date
    strike: float
    right: Right

    def __str__(self) -> str:
        return f"{self.ticker} {self.expiry.isoformat()} {self.strike:g}{self.right}"


@dataclass(frozen=True)
class UnderlyingKey:
    """Ticker+strike+right without expiry -- what TRIM/SOLD ALL/EXPIRED
    messages give us (the title doesn't repeat the full contract line),
    used to look up an already-open position by everything except expiry."""

    ticker: str
    strike: float
    right: Right


@dataclass(frozen=True)
class BuyEvent:
    message_id: int
    option: OptionKey
    entry_price: float
    contracts: int
    cost: float
    is_lotto: bool = False


@dataclass(frozen=True)
class TrimEvent:
    """Partial exit. sold_of_total/still_running are the channel's own
    absolute numbers for this position right now -- authoritative, not
    computed from a running delta we maintain ourselves."""

    message_id: int
    underlying: UnderlyingKey
    tier_pct: float  # e.g. 0.75 for "TRIM +75%"
    sold_this_event: int
    channel_total_before: int  # the "of N" in "Sold 12 of 20"
    channel_remaining_after: int  # "N still running"
    avg_exit_price: float


@dataclass(frozen=True)
class SoldAllEvent:
    """Full exit -- the channel is completely flat on this position now."""

    message_id: int
    underlying: UnderlyingKey
    realized_pct: float
    avg_exit_price: float


@dataclass(frozen=True)
class ExpiredEvent:
    """Contract rode to expiration (0DTE, after hours). No order to place
    -- the market's closed -- just close out our own record the same way
    the user's real contracts will auto-expire at the broker."""

    message_id: int
    underlying: UnderlyingKey
    won: bool


@dataclass(frozen=True)
class InfoEvent:
    """Recognized but explicitly non-actionable: a bare milestone ping
    ("+25% -- close or trim & set SL to breakeven", narration only, not a
    confirmed fill) or an AVERAGING DOWN (channel adds to its own size at
    a new price; the bot doesn't average down its own fills). Logged for
    visibility, never traded on."""

    message_id: int
    kind: Literal["milestone", "averaging_down"]
    raw_title: str


@dataclass(frozen=True)
class UnknownEvent:
    """A bot embed that matched none of the known title patterns above --
    could be a new message type Swift introduces later. Always alert on
    this rather than silently ignoring or guessing at behavior."""

    message_id: int
    raw_embed: dict = field(default_factory=dict)
    reason: str = ""


TradeEvent = BuyEvent | TrimEvent | SoldAllEvent | ExpiredEvent | InfoEvent | UnknownEvent


@dataclass
class OpenPosition:
    option: OptionKey
    channel_total_qty: int  # channel's current (post any averaging-down) size
    channel_remaining_qty: int
    user_original_qty: int
    user_remaining_qty: int
    entry_price: float
    ibkr_order_id_entry: int | None
    status: Literal["OPEN", "CLOSED"] = "OPEN"
