"""Core value types.

Everything here is a plain, immutable dataclass with no broker, Discord or
database dependency, so the parser and the rules engine can be tested
without any I/O at all.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from enum import Enum
from typing import Literal

Right = Literal["C", "P"]


@dataclass(frozen=True)
class OptionKey:
    """A single option contract, unambiguously."""

    ticker: str
    expiry: date
    strike: float
    right: Right

    def __str__(self) -> str:
        return f"{self.ticker} {self.expiry.isoformat()} {self.strike:g}{self.right}"

    @property
    def occ_symbol(self) -> str:
        """OCC 21-character symbol, e.g. "SPXW  261006P07815000".

        Root padded to 6, then YYMMDD, then C/P, then the strike in
        thousandths zero-padded to 8. The root is the *trading class*
        (SPXW for an SPX weekly), not the underlying ticker -- see
        options_scanner/contracts.py for why that distinction matters.
        """
        from options_scanner.contracts import trading_class_for

        root = trading_class_for(self.ticker, self.expiry)
        strike_thousandths = int(round(self.strike * 1000))
        return f"{root:<6}{self.expiry:%y%m%d}{self.right}{strike_thousandths:08d}"


@dataclass(frozen=True)
class TrimTarget:
    """One rung of the advisor's published "Trim Targets" ladder, e.g.
    "25%   $0.594" -> pct=0.25, price=0.594.

    These are used as a *parse checksum* only: the bot recomputes each
    price off the entry and rejects the alert if the two disagree by more
    than a cent. The rungs actually traded come from our own config, not
    from here.
    """

    pct: float
    price: float


class RejectReason(str, Enum):
    """Why an alert was not traded. Every rejection names one of these in
    the updates channel, so a silent drop is never possible."""

    NO_ALERT_CARD = "no_alert_card"
    UNRECOGNIZED_TITLE = "unrecognized_title"
    MISSING_FIELD = "missing_field"
    UNPARSABLE_CONTRACT = "unparsable_contract"
    UNPARSABLE_NUMBER = "unparsable_number"
    EXPIRY_TAG_MISMATCH = "expiry_tag_mismatch"
    EXPIRY_IN_PAST = "expiry_in_past"
    TRIM_TARGET_MISMATCH = "trim_target_mismatch"
    NO_TRIM_TARGETS = "no_trim_targets"
    NON_POSITIVE_PRICE = "non_positive_price"


@dataclass(frozen=True)
class EntryAlert:
    """A validated BUY alert, ready to size and order.

    `advisor_contracts` is recorded for the audit trail only -- position
    size comes from our own config (options_scanner/sizing.py).
    """

    message_id: int
    option: OptionKey
    entry_price: float
    advisor_contracts: int
    advisor_cost: float
    trim_targets: tuple[TrimTarget, ...]
    raw_title: str
    is_zero_dte: bool


@dataclass(frozen=True)
class RejectedAlert:
    """A card that parsed into something recognizable but failed validation,
    or failed to parse at all. Never traded; always reported."""

    message_id: int
    reason: RejectReason
    detail: str
    raw_title: str = ""


@dataclass(frozen=True)
class InfoAlert:
    """Recognized, non-actionable narration (a bare "+25%" milestone ping, an
    AVERAGING DOWN card, a prospective NEW ALERT). Logged, never traded.

    Advisor TRIM / SOLD ALL / EXPIRED cards also land here for now: our own
    position management is independent of the advisor's exits, so they are
    informational until a dedicated handler is added (spec section 9).
    """

    message_id: int
    kind: Literal["milestone", "averaging_down", "new_alert", "advisor_trim", "advisor_exit", "advisor_expired"]
    raw_title: str
    detail: str = ""


ParseResult = EntryAlert | RejectedAlert | InfoAlert


# --- live position state (consumed by the rules engine) --------------------


@dataclass(frozen=True)
class TrimFill:
    """One completed trim, recorded so a level can never fire twice."""

    level_pct: int  # whole percent, e.g. 25
    qty: int
    price: float


@dataclass
class PositionState:
    """Everything the rules engine needs to decide what to do next.

    Mutable because the position manager advances it in place, but the rules
    engine itself only reads it -- `evaluate` returns intents and never
    mutates (see options_scanner/rules.py).

    Profit levels are whole percents (25, 50, 75, 100) rather than fractions,
    so `fired_levels` membership is exact integer comparison. A float set key
    would be one refactor away from a level firing twice.
    """

    option: OptionKey
    entry_fill: float
    original_qty: int
    remaining_qty: int
    peak_bid: float
    stop_price: float | None = None
    stop_reason: Literal["breakeven", "trail"] | None = None
    trail_armed: bool = False
    fired_levels: frozenset[int] = field(default_factory=frozenset)
    consecutive_breaches: int = 0
    closed: bool = False

    @property
    def is_protected(self) -> bool:
        """False between the entry fill and the first trim filling. By design
        per the spec -- worth knowing that it is the riskiest window, and that
        nothing but !flatten or the daily loss limit covers it."""
        return self.stop_price is not None

    @property
    def gain_pct(self) -> float:
        """Current peak gain over the entry fill, as a whole percent."""
        return (self.peak_bid / self.entry_fill - 1.0) * 100.0
