from __future__ import annotations

import sqlite3
from datetime import date
from pathlib import Path
from typing import Literal

from bot.models import OpenPosition, OptionKey, UnderlyingKey

_SCHEMA_PATH = Path(__file__).resolve().parent.parent / "db" / "schema.sql"


class PositionStore:
    """SQLite-backed store for open option positions and processed-message
    idempotency, keyed on (ticker, expiry, strike, right). One open row per
    key at a time (enforced by a partial unique index in schema.sql).

    Every mutation here takes the channel's absolute post-event numbers
    (total contracts, remaining contracts) rather than deltas this class
    computes itself -- confirmed necessary against real traffic, where an
    AVERAGING DOWN's new total and a TRIM's "of N" both refer to the
    channel's current live size, not the original first-entry size."""

    def __init__(self, db_path: str | Path):
        self._conn = sqlite3.connect(db_path)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._conn.executescript(_SCHEMA_PATH.read_text())
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    def already_processed(self, message_id: int) -> bool:
        row = self._conn.execute(
            "SELECT 1 FROM processed_messages WHERE message_id = ?", (message_id,)
        ).fetchone()
        return row is not None

    def mark_processed(self, message_id: int) -> None:
        self._conn.execute(
            "INSERT OR IGNORE INTO processed_messages (message_id) VALUES (?)", (message_id,)
        )
        self._conn.commit()

    def get_open(self, option: OptionKey) -> OpenPosition | None:
        row = self._conn.execute(
            """SELECT * FROM positions
               WHERE ticker = ? AND expiry = ? AND strike = ? AND right = ? AND status = 'OPEN'""",
            (option.ticker, option.expiry.isoformat(), option.strike, option.right),
        ).fetchone()
        return _row_to_position(row) if row is not None else None

    def get_open_by_underlying(self, underlying: UnderlyingKey) -> OpenPosition | None:
        """Look up an open position by everything except expiry -- what
        TRIM / SOLD ALL / EXPIRED / AVERAGING DOWN messages give us, since
        their title doesn't repeat the full contract line."""
        rows = self._conn.execute(
            """SELECT * FROM positions
               WHERE ticker = ? AND strike = ? AND right = ? AND status = 'OPEN'""",
            (underlying.ticker, underlying.strike, underlying.right),
        ).fetchall()
        if len(rows) > 1:
            raise ValueError(
                f"Ambiguous match: {len(rows)} open positions for {underlying} across different expiries"
            )
        return _row_to_position(rows[0]) if rows else None

    def create_open(self, position: OpenPosition) -> None:
        self._conn.execute(
            """INSERT INTO positions
               (ticker, expiry, strike, right, channel_total_qty, channel_remaining_qty,
                user_original_qty, user_remaining_qty, entry_price, ibkr_order_id_entry, status)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'OPEN')""",
            (
                position.option.ticker,
                position.option.expiry.isoformat(),
                position.option.strike,
                position.option.right,
                position.channel_total_qty,
                position.channel_remaining_qty,
                position.user_original_qty,
                position.user_remaining_qty,
                position.entry_price,
                position.ibkr_order_id_entry,
            ),
        )
        self._conn.commit()

    def apply_averaging_down(
        self,
        option: OptionKey,
        new_channel_total_qty: int,
        new_entry_price: float,
        new_user_qty: int,
    ) -> None:
        """new_user_qty is the user's own new total after their own
        additional buy fills -- computed by the caller (executor), not here."""
        self._conn.execute(
            """UPDATE positions
               SET channel_total_qty = ?, channel_remaining_qty = ?, entry_price = ?,
                   user_original_qty = ?, user_remaining_qty = ?, updated_at = datetime('now')
               WHERE ticker = ? AND expiry = ? AND strike = ? AND right = ? AND status = 'OPEN'""",
            (
                new_channel_total_qty,
                new_channel_total_qty,
                new_entry_price,
                new_user_qty,
                new_user_qty,
                option.ticker,
                option.expiry.isoformat(),
                option.strike,
                option.right,
            ),
        )
        self._conn.commit()

    def apply_trim(
        self,
        option: OptionKey,
        channel_remaining_qty: int,
        user_remaining_qty: int,
    ) -> None:
        status = "CLOSED" if user_remaining_qty <= 0 else "OPEN"
        self._conn.execute(
            """UPDATE positions
               SET channel_remaining_qty = ?, user_remaining_qty = ?, status = ?, updated_at = datetime('now')
               WHERE ticker = ? AND expiry = ? AND strike = ? AND right = ? AND status = 'OPEN'""",
            (
                channel_remaining_qty,
                user_remaining_qty,
                status,
                option.ticker,
                option.expiry.isoformat(),
                option.strike,
                option.right,
            ),
        )
        self._conn.commit()

    def record_order(
        self,
        message_id: int,
        option: OptionKey,
        ib_order_id: int | None,
        action: Literal["BUY", "SELL"],
        contracts: int,
        status: Literal["FILLED", "REJECTED", "TIMEOUT"],
        avg_fill_price: float | None = None,
        order_type: str = "MKT",
    ) -> None:
        """Append-only audit row for one placeOrder call, recorded once its
        terminal state is known -- see db/schema.sql for why this is
        separate from processed_messages idempotency tracking."""
        self._conn.execute(
            """INSERT INTO orders
               (message_id, ticker, expiry, strike, right, ib_order_id, action,
                contracts, order_type, status, avg_fill_price)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                message_id,
                option.ticker,
                option.expiry.isoformat(),
                option.strike,
                option.right,
                ib_order_id,
                action,
                contracts,
                order_type,
                status,
                avg_fill_price,
            ),
        )
        self._conn.commit()

    def close_position(self, option: OptionKey) -> None:
        """Used for both SOLD ALL (after selling the user's full remaining
        contracts) and EXPIRED (no order placed -- the market's closed --
        just records that the channel's position is done)."""
        self._conn.execute(
            """UPDATE positions
               SET channel_remaining_qty = 0, user_remaining_qty = 0, status = 'CLOSED', updated_at = datetime('now')
               WHERE ticker = ? AND expiry = ? AND strike = ? AND right = ? AND status = 'OPEN'""",
            (option.ticker, option.expiry.isoformat(), option.strike, option.right),
        )
        self._conn.commit()


def _row_to_position(row: sqlite3.Row) -> OpenPosition:
    return OpenPosition(
        option=OptionKey(row["ticker"], date.fromisoformat(row["expiry"]), row["strike"], row["right"]),
        channel_total_qty=row["channel_total_qty"],
        channel_remaining_qty=row["channel_remaining_qty"],
        user_original_qty=row["user_original_qty"],
        user_remaining_qty=row["user_remaining_qty"],
        entry_price=row["entry_price"],
        ibkr_order_id_entry=row["ibkr_order_id_entry"],
        status=row["status"],
    )
