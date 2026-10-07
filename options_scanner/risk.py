"""Pre-trade risk gate.

One function answers "may we open this position right now", and it answers
with a reason either way so the updates channel can always explain itself.
Every check here is a *refusal to open*; nothing in this module ever touches
an existing position, because the spec is explicit that halting and the daily
loss limit stop new entries while open positions keep being managed.

Checks are ordered cheapest-and-most-decisive first, so the reported reason is
the most useful one when several apply at once.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from enum import Enum

from options_scanner.config import Settings
from options_scanner.market_hours import (
    is_market_open,
    is_past_cutoff,
    is_trading_day,
    now_et,
    today_et,
)
from options_scanner.storage import Storage


class BlockReason(str, Enum):
    HALTED = "halted"
    MARKET_CLOSED = "market_closed"
    NOT_A_TRADING_DAY = "not_a_trading_day"
    PAST_ENTRY_CUTOFF = "past_entry_cutoff"
    MAX_OPEN_POSITIONS = "max_open_positions"
    MAX_TRADES_PER_DAY = "max_trades_per_day"
    DAILY_LOSS_LIMIT = "daily_loss_limit"
    DUPLICATE_ALERT = "duplicate_alert"
    ALREADY_IN_POSITION = "already_in_position"


@dataclass(frozen=True)
class Decision:
    """Allowed, or blocked with a reason fit to post verbatim."""

    allowed: bool
    reason: BlockReason | None = None
    detail: str = ""

    @classmethod
    def allow(cls) -> "Decision":
        return cls(allowed=True)

    @classmethod
    def block(cls, reason: BlockReason, detail: str) -> "Decision":
        return cls(allowed=False, reason=reason, detail=detail)


class RiskGate:
    def __init__(self, settings: Settings, storage: Storage):
        self.settings = settings
        self.storage = storage

    # --- halting -----------------------------------------------------------

    @property
    def halted(self) -> bool:
        return self.storage.is_halted()

    def halt(self, reason: str) -> None:
        self.storage.set_halted(True, reason)

    def resume(self) -> None:
        self.storage.set_halted(False)

    # --- the gate ----------------------------------------------------------

    def check_entry(
        self,
        occ_symbol: str,
        entry_price: float,
        *,
        moment: datetime | None = None,
        trading_day: date | None = None,
    ) -> Decision:
        """May we open a position in `occ_symbol` right now?"""
        settings = self.settings
        moment = moment or now_et()
        trading_day = trading_day or today_et()

        if self.halted:
            why = self.storage.halt_reason() or "manual"
            return Decision.block(BlockReason.HALTED, f"new entries are halted ({why}); use !resume")

        if not is_trading_day(trading_day):
            return Decision.block(
                BlockReason.NOT_A_TRADING_DAY, f"{trading_day:%a %d %b} is not a trading day"
            )

        if not is_market_open(moment):
            return Decision.block(
                BlockReason.MARKET_CLOSED,
                f"market is closed at {moment:%H:%M} ET; 0DTE liquidity outside the session is "
                "too thin for a synthetic stop to be reliable",
            )

        if is_past_cutoff(settings.market.entry_cutoff_time, moment):
            return Decision.block(
                BlockReason.PAST_ENTRY_CUTOFF,
                f"past the {settings.market.entry_cutoff} ET entry cutoff (now {moment:%H:%M} ET)",
            )

        stats = self.storage.day_stats(trading_day)

        realized = float(stats["realized_pnl"])
        if realized <= -abs(settings.risk.max_daily_loss_usd):
            return Decision.block(
                BlockReason.DAILY_LOSS_LIMIT,
                f"daily loss limit hit: ${realized:,.2f} realized against a "
                f"${settings.risk.max_daily_loss_usd:,.0f} limit",
            )

        entries = int(stats["entries"])
        if entries >= settings.risk.max_trades_per_day:
            return Decision.block(
                BlockReason.MAX_TRADES_PER_DAY,
                f"already took {entries} of {settings.risk.max_trades_per_day} allowed trades today",
            )

        if self.storage.position_for_symbol(occ_symbol) is not None:
            return Decision.block(
                BlockReason.ALREADY_IN_POSITION,
                f"already holding {occ_symbol}; the bot does not add to a position",
            )

        open_count = self.storage.open_position_count()
        if open_count >= settings.risk.max_open_positions:
            return Decision.block(
                BlockReason.MAX_OPEN_POSITIONS,
                f"{open_count} of {settings.risk.max_open_positions} position slots already in use",
            )

        duplicate = self.storage.find_recent_duplicate(
            occ_symbol, entry_price, settings.entry.duplicate_window_seconds
        )
        if duplicate is not None:
            return Decision.block(
                BlockReason.DUPLICATE_ALERT,
                f"same contract at ${entry_price:.3f} was already accepted as message {duplicate} "
                f"within the last {settings.entry.duplicate_window_seconds:.0f}s",
            )

        return Decision.allow()

    # --- post-trade bookkeeping -------------------------------------------

    def record_entry(self, trading_day: date | None = None) -> None:
        self.storage.bump_day(trading_day or today_et(), entries=1)

    def record_realized(self, pnl: float, trading_day: date | None = None) -> None:
        """Fold a realized P&L into the day, and auto-halt if that crosses the
        daily loss limit. Auto-halting here rather than only checking on the
        next entry means the limit is reported the moment it is breached."""
        day = trading_day or today_et()
        self.storage.bump_day(day, realized_pnl=pnl)
        stats = self.storage.day_stats(day)
        realized = float(stats["realized_pnl"])
        limit = abs(self.settings.risk.max_daily_loss_usd)
        if realized <= -limit and not self.halted:
            self.halt(f"daily loss limit: ${realized:,.2f} vs ${limit:,.0f}")

    def day_summary(self, trading_day: date | None = None) -> dict[str, float]:
        stats = self.storage.day_stats(trading_day or today_et())
        return {
            "entries": int(stats["entries"]),
            "realized_pnl": float(stats["realized_pnl"]),
            "alerts_received": int(stats["alerts_received"]),
            "alerts_rejected": int(stats["alerts_rejected"]),
            "alerts_skipped": int(stats["alerts_skipped"]),
        }
