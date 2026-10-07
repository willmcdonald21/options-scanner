"""SQLite persistence: audit trail, deduplication and restart state.

Everything the bot must not forget across a restart lives here. That is not
only the open positions but the full rules-engine state for each one -- which
levels have fired, the peak bid, the current stop -- because losing any of
those silently changes behaviour rather than failing: a forgotten fired level
trims twice, and a forgotten peak walks the trailing stop backwards.

The connection is used from a single asyncio thread, so no locking is
required, but `check_same_thread=False` is set because discord.py may run
callbacks on an executor thread.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from options_scanner.models import EntryAlert, OptionKey, PositionState

_SCHEMA_PATH = Path(__file__).resolve().parent.parent / "db" / "schema.sql"

# Alert dispositions, mirrored in db/schema.sql's comment.
STATUS_RECEIVED = "RECEIVED"
STATUS_ACCEPTED = "ACCEPTED"
STATUS_REJECTED = "REJECTED"
STATUS_SKIPPED = "SKIPPED"
STATUS_DUPLICATE = "DUPLICATE"
STATUS_INFO = "INFO"

HALT_KEY = "halted"


class Storage:
    def __init__(self, db_path: str | Path):
        path = Path(db_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._conn.execute("PRAGMA journal_mode = WAL")
        self._conn.executescript(_SCHEMA_PATH.read_text())
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    # --- alerts and deduplication -----------------------------------------

    def alert_seen(self, message_id: int) -> bool:
        """First line of dedup: this exact Discord message. Survives a restart
        because it is a row, not a set in memory."""
        row = self._conn.execute(
            "SELECT 1 FROM alerts WHERE message_id = ?", (message_id,)
        ).fetchone()
        return row is not None

    def find_recent_duplicate(
        self,
        occ_symbol: str,
        entry_price: float,
        within_seconds: float,
        now: datetime | None = None,
    ) -> int | None:
        """Second line of dedup: the same contract at the same entry, recently
        accepted under a *different* message id. That is what an accidental
        re-paste looks like, since re-pasting creates a new message.

        Returns the earlier message id, or None.
        """
        now = now or datetime.utcnow()
        cutoff = (now - timedelta(seconds=within_seconds)).strftime("%Y-%m-%d %H:%M:%S")
        row = self._conn.execute(
            """SELECT message_id FROM alerts
               WHERE occ_symbol = ?
                 AND abs(entry_price - ?) < 1e-9
                 AND status = ?
                 AND received_at >= ?
               ORDER BY received_at DESC LIMIT 1""",
            (occ_symbol, entry_price, STATUS_ACCEPTED, cutoff),
        ).fetchone()
        return int(row["message_id"]) if row else None

    def record_alert(
        self,
        *,
        message_id: int,
        channel_id: int,
        author_id: int,
        raw_text: str,
        status: str,
        jump_url: str | None = None,
        reason: str | None = None,
        alert: EntryAlert | None = None,
    ) -> None:
        """Insert or update the row for one alert. Idempotent on message_id so
        a retry after a crash mid-handling cannot create a second row."""
        option = alert.option if alert else None
        self._conn.execute(
            """INSERT INTO alerts
                 (message_id, channel_id, author_id, jump_url, raw_text, status, reason,
                  occ_symbol, ticker, expiry, strike, right, entry_price, advisor_contracts)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT (message_id) DO UPDATE SET
                 status = excluded.status,
                 reason = excluded.reason,
                 occ_symbol = COALESCE(excluded.occ_symbol, alerts.occ_symbol),
                 entry_price = COALESCE(excluded.entry_price, alerts.entry_price),
                 updated_at = datetime('now')""",
            (
                message_id,
                channel_id,
                author_id,
                jump_url,
                raw_text,
                status,
                reason,
                option.occ_symbol if option else None,
                option.ticker if option else None,
                option.expiry.isoformat() if option else None,
                option.strike if option else None,
                option.right if option else None,
                alert.entry_price if alert else None,
                alert.advisor_contracts if alert else None,
            ),
        )
        self._conn.commit()

    def get_alert(self, message_id: int) -> sqlite3.Row | None:
        return self._conn.execute(
            "SELECT * FROM alerts WHERE message_id = ?", (message_id,)
        ).fetchone()

    # --- positions ---------------------------------------------------------

    def create_position(self, alert_message_id: int, state: PositionState) -> int:
        cursor = self._conn.execute(
            """INSERT INTO positions
                 (alert_message_id, occ_symbol, ticker, expiry, strike, right, entry_fill,
                  original_qty, remaining_qty, peak_bid, stop_price, stop_reason,
                  trail_armed, fired_levels, consecutive_breaches, status)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'OPEN')""",
            (
                alert_message_id,
                state.option.occ_symbol,
                state.option.ticker,
                state.option.expiry.isoformat(),
                state.option.strike,
                state.option.right,
                state.entry_fill,
                state.original_qty,
                state.remaining_qty,
                state.peak_bid,
                state.stop_price,
                state.stop_reason,
                int(state.trail_armed),
                json.dumps(sorted(state.fired_levels)),
                state.consecutive_breaches,
            ),
        )
        self._conn.commit()
        return int(cursor.lastrowid)

    def save_position(self, position_id: int, state: PositionState, realized_pnl: float = 0.0) -> None:
        """Persist the whole rules state after every change. Cheap, and the
        alternative is a restart that silently resets a stop."""
        self._conn.execute(
            """UPDATE positions SET
                 remaining_qty = ?, peak_bid = ?, stop_price = ?, stop_reason = ?,
                 trail_armed = ?, fired_levels = ?, consecutive_breaches = ?,
                 status = ?, realized_pnl = ?,
                 closed_at = CASE WHEN ? = 'CLOSED' AND closed_at IS NULL
                                  THEN datetime('now') ELSE closed_at END,
                 updated_at = datetime('now')
               WHERE id = ?""",
            (
                state.remaining_qty,
                state.peak_bid,
                state.stop_price,
                state.stop_reason,
                int(state.trail_armed),
                json.dumps(sorted(state.fired_levels)),
                state.consecutive_breaches,
                "CLOSED" if state.closed else "OPEN",
                realized_pnl,
                "CLOSED" if state.closed else "OPEN",
                position_id,
            ),
        )
        self._conn.commit()

    def open_positions(self) -> list[tuple[int, PositionState]]:
        rows = self._conn.execute(
            "SELECT * FROM positions WHERE status = 'OPEN' ORDER BY id"
        ).fetchall()
        return [(int(row["id"]), _row_to_state(row)) for row in rows]

    def open_position_count(self) -> int:
        row = self._conn.execute(
            "SELECT count(*) AS n FROM positions WHERE status = 'OPEN'"
        ).fetchone()
        return int(row["n"])

    def position_for_symbol(self, occ_symbol: str) -> tuple[int, PositionState] | None:
        row = self._conn.execute(
            "SELECT * FROM positions WHERE occ_symbol = ? AND status = 'OPEN'", (occ_symbol,)
        ).fetchone()
        return (int(row["id"]), _row_to_state(row)) if row else None

    # --- orders and fills --------------------------------------------------

    def record_order(
        self,
        *,
        occ_symbol: str,
        intent: str,
        side: str,
        qty: int,
        status: str,
        position_id: int | None = None,
        alert_message_id: int | None = None,
        order_type: str = "LMT",
        limit_price: float | None = None,
        trim_level_pct: int | None = None,
        broker_order_id: str | None = None,
        detail: str | None = None,
    ) -> int:
        cursor = self._conn.execute(
            """INSERT INTO orders
                 (position_id, alert_message_id, occ_symbol, intent, side, order_type, qty,
                  limit_price, trim_level_pct, broker_order_id, status, detail)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                position_id,
                alert_message_id,
                occ_symbol,
                intent,
                side,
                order_type,
                qty,
                limit_price,
                trim_level_pct,
                broker_order_id,
                status,
                detail,
            ),
        )
        self._conn.commit()
        return int(cursor.lastrowid)

    def update_order(
        self,
        order_id: int,
        *,
        status: str | None = None,
        filled_qty: int | None = None,
        avg_fill_price: float | None = None,
        broker_order_id: str | None = None,
        limit_price: float | None = None,
        detail: str | None = None,
    ) -> None:
        sets: list[str] = []
        values: list[Any] = []
        for column, value in (
            ("status", status),
            ("filled_qty", filled_qty),
            ("avg_fill_price", avg_fill_price),
            ("broker_order_id", broker_order_id),
            ("limit_price", limit_price),
            ("detail", detail),
        ):
            if value is not None:
                sets.append(f"{column} = ?")
                values.append(value)
        if not sets:
            return
        sets.append("updated_at = datetime('now')")
        values.append(order_id)
        self._conn.execute(f"UPDATE orders SET {', '.join(sets)} WHERE id = ?", values)
        self._conn.commit()

    def record_fill(self, order_id: int, qty: int, price: float) -> None:
        self._conn.execute(
            "INSERT INTO fills (order_id, qty, price) VALUES (?, ?, ?)", (order_id, qty, price)
        )
        self._conn.commit()

    def orders_for_position(self, position_id: int) -> list[sqlite3.Row]:
        return self._conn.execute(
            "SELECT * FROM orders WHERE position_id = ? ORDER BY id", (position_id,)
        ).fetchall()

    # --- per-day counters --------------------------------------------------

    def _ensure_day(self, trading_day: date) -> None:
        self._conn.execute(
            "INSERT OR IGNORE INTO day_stats (trading_day) VALUES (?)", (trading_day.isoformat(),)
        )

    def bump_day(self, trading_day: date, **deltas: float) -> None:
        """Increment any of the day's counters. Column names are validated
        against a fixed set, never interpolated from caller input."""
        allowed = {"entries", "realized_pnl", "alerts_received", "alerts_rejected", "alerts_skipped"}
        unknown = set(deltas) - allowed
        if unknown:
            raise ValueError(f"unknown day_stats column(s): {sorted(unknown)}")
        if not deltas:
            return
        self._ensure_day(trading_day)
        assignments = ", ".join(f"{name} = {name} + ?" for name in deltas)
        self._conn.execute(
            f"UPDATE day_stats SET {assignments} WHERE trading_day = ?",
            (*deltas.values(), trading_day.isoformat()),
        )
        self._conn.commit()

    def day_stats(self, trading_day: date) -> sqlite3.Row:
        self._ensure_day(trading_day)
        self._conn.commit()
        return self._conn.execute(
            "SELECT * FROM day_stats WHERE trading_day = ?", (trading_day.isoformat(),)
        ).fetchone()

    # --- halt flag ---------------------------------------------------------

    def is_halted(self) -> bool:
        return self.get_state(HALT_KEY) == "1"

    def set_halted(self, halted: bool, reason: str = "") -> None:
        """Persisted deliberately: a bot that restarts into an un-halted state
        after being halted would resume trading on its own."""
        self.set_state(HALT_KEY, "1" if halted else "0")
        if halted:
            self.set_state("halt_reason", reason or "manual")

    def halt_reason(self) -> str:
        return self.get_state("halt_reason") or ""

    def get_state(self, key: str) -> str | None:
        row = self._conn.execute("SELECT value FROM bot_state WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else None

    def set_state(self, key: str, value: str) -> None:
        self._conn.execute(
            """INSERT INTO bot_state (key, value) VALUES (?, ?)
               ON CONFLICT (key) DO UPDATE SET value = excluded.value, updated_at = datetime('now')""",
            (key, value),
        )
        self._conn.commit()

    # --- reporting ---------------------------------------------------------

    def day_alerts(self, trading_day: date) -> list[sqlite3.Row]:
        return self._conn.execute(
            "SELECT * FROM alerts WHERE date(received_at) = ? ORDER BY received_at",
            (trading_day.isoformat(),),
        ).fetchall()

    def day_positions(self, trading_day: date) -> list[sqlite3.Row]:
        return self._conn.execute(
            "SELECT * FROM positions WHERE date(opened_at) = ? ORDER BY id",
            (trading_day.isoformat(),),
        ).fetchall()


def _row_to_state(row: sqlite3.Row) -> PositionState:
    return PositionState(
        option=OptionKey(
            ticker=row["ticker"],
            expiry=date.fromisoformat(row["expiry"]),
            strike=row["strike"],
            right=row["right"],
        ),
        entry_fill=row["entry_fill"],
        original_qty=row["original_qty"],
        remaining_qty=row["remaining_qty"],
        peak_bid=row["peak_bid"],
        stop_price=row["stop_price"],
        stop_reason=row["stop_reason"],
        trail_armed=bool(row["trail_armed"]),
        fired_levels=frozenset(json.loads(row["fired_levels"])),
        consecutive_breaches=row["consecutive_breaches"],
        closed=row["status"] == "CLOSED",
    )


def fired_levels_from_json(raw: str) -> frozenset[int]:
    return frozenset(json.loads(raw))


def states_equal(left: PositionState, right: PositionState) -> bool:
    """Used by the restart reconciliation to report whether reloading a
    position from the database reproduced it exactly."""
    fields = (
        "entry_fill", "original_qty", "remaining_qty", "peak_bid", "stop_price",
        "stop_reason", "trail_armed", "fired_levels", "consecutive_breaches", "closed",
    )
    return left.option == right.option and all(
        getattr(left, name) == getattr(right, name) for name in fields
    )


__all__ = [
    "HALT_KEY",
    "STATUS_ACCEPTED",
    "STATUS_DUPLICATE",
    "STATUS_INFO",
    "STATUS_RECEIVED",
    "STATUS_REJECTED",
    "STATUS_SKIPPED",
    "Storage",
    "fired_levels_from_json",
    "states_equal",
]
