from __future__ import annotations

import json
import sqlite3
from datetime import date
from pathlib import Path
from typing import Literal

from bot.models import OpenPosition, OptionKey, TargetLeg, TrimTarget, UnderlyingKey

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
        self._migrate()
        self._conn.commit()

    def _migrate(self) -> None:
        """schema.sql is replayed on every open, so CREATE ... IF NOT EXISTS
        covers new tables but not columns added to an existing positions
        table -- SQLite has no ADD COLUMN IF NOT EXISTS. Add those here,
        guarded on the live column list."""
        existing = {row["name"] for row in self._conn.execute("PRAGMA table_info(positions)")}
        for column, ddl in (
            ("current_stop_price", "REAL"),
            ("stop_tier_index", "INTEGER NOT NULL DEFAULT -1"),
            ("runner_qty", "INTEGER NOT NULL DEFAULT 0"),
            ("trim_targets_json", "TEXT"),
        ):
            if column not in existing:
                self._conn.execute(f"ALTER TABLE positions ADD COLUMN {column} {ddl}")

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

    def list_open(self) -> list[OpenPosition]:
        rows = self._conn.execute("SELECT * FROM positions WHERE status = 'OPEN'").fetchall()
        return [_row_to_position(row) for row in rows]

    def create_open(self, position: OpenPosition) -> int:
        """Returns the new row id, which position_targets rows reference."""
        cursor = self._conn.execute(
            """INSERT INTO positions
               (ticker, expiry, strike, right, channel_total_qty, channel_remaining_qty,
                user_original_qty, user_remaining_qty, entry_price, ibkr_order_id_entry, status,
                current_stop_price, stop_tier_index, runner_qty, trim_targets_json)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'OPEN', ?, ?, ?, ?)""",
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
                position.current_stop_price,
                position.stop_tier_index,
                position.runner_qty,
                _encode_targets(position.trim_targets),
            ),
        )
        self._conn.commit()
        return int(cursor.lastrowid)

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

    # --- resting exit legs -------------------------------------------------
    #
    # One row per tranche placed at IBKR. Quantities are never updated here;
    # only stop prices (as the ladder ratchets) and status (as legs fill or
    # get cancelled). See db/schema.sql for why resizing a stop is unsafe.

    def add_target_leg(self, leg: TargetLeg) -> int:
        cursor = self._conn.execute(
            """INSERT INTO position_targets
               (position_id, tier_index, qty, tp_price, current_stop_price,
                oca_group, lmt_order_id, stp_order_id, status)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                leg.position_id,
                leg.tier_index,
                leg.qty,
                leg.tp_price,
                leg.current_stop_price,
                leg.oca_group,
                leg.lmt_order_id,
                leg.stp_order_id,
                leg.status,
            ),
        )
        self._conn.commit()
        return int(cursor.lastrowid)

    def list_legs(self, position_id: int, only_live: bool = False) -> list[TargetLeg]:
        sql = "SELECT * FROM position_targets WHERE position_id = ?"
        if only_live:
            sql += " AND status = 'LIVE'"
        sql += " ORDER BY tier_index"
        return [_row_to_leg(row) for row in self._conn.execute(sql, (position_id,)).fetchall()]

    def list_all_live_legs(self) -> list[TargetLeg]:
        """Every live leg across every open position -- the startup
        reconciliation's view of what *should* be resting at IBKR."""
        rows = self._conn.execute(
            """SELECT t.* FROM position_targets t
               JOIN positions p ON p.id = t.position_id
               WHERE t.status = 'LIVE' AND p.status = 'OPEN'
               ORDER BY t.position_id, t.tier_index"""
        ).fetchall()
        return [_row_to_leg(row) for row in rows]

    def get_leg_by_order_id(self, ib_order_id: int) -> tuple[TargetLeg, str] | None:
        """Finds the leg an IBKR order id belongs to, plus which side of the
        pair it is ("TP" or "STOP") -- how a fill event is routed back to a
        tranche."""
        row = self._conn.execute(
            "SELECT * FROM position_targets WHERE lmt_order_id = ?", (ib_order_id,)
        ).fetchone()
        if row is not None:
            return _row_to_leg(row), "TP"
        row = self._conn.execute(
            "SELECT * FROM position_targets WHERE stp_order_id = ?", (ib_order_id,)
        ).fetchone()
        if row is not None:
            return _row_to_leg(row), "STOP"
        return None

    def set_leg_order_ids(self, leg_id: int, lmt_order_id: int | None, stp_order_id: int | None) -> None:
        self._conn.execute(
            """UPDATE position_targets
               SET lmt_order_id = ?, stp_order_id = ?, updated_at = datetime('now')
               WHERE id = ?""",
            (lmt_order_id, stp_order_id, leg_id),
        )
        self._conn.commit()

    def set_leg_stop_price(self, leg_id: int, stop_price: float) -> None:
        self._conn.execute(
            """UPDATE position_targets
               SET current_stop_price = ?, updated_at = datetime('now')
               WHERE id = ?""",
            (stop_price, leg_id),
        )
        self._conn.commit()

    def set_leg_status(self, leg_id: int, status: str) -> None:
        self._conn.execute(
            "UPDATE position_targets SET status = ?, updated_at = datetime('now') WHERE id = ?",
            (status, leg_id),
        )
        self._conn.commit()

    def get_position_by_id(self, position_id: int) -> OpenPosition | None:
        row = self._conn.execute("SELECT * FROM positions WHERE id = ?", (position_id,)).fetchone()
        return _row_to_position(row) if row is not None else None

    def set_position_stop(self, position_id: int, stop_price: float, stop_tier_index: int) -> None:
        """Records where the ladder has climbed to. Guarded so a stop can
        only ever move up: a late or out-of-order fill event must not walk
        a protective stop back down."""
        self._conn.execute(
            """UPDATE positions
               SET current_stop_price = MAX(COALESCE(current_stop_price, 0), ?),
                   stop_tier_index = MAX(stop_tier_index, ?),
                   updated_at = datetime('now')
               WHERE id = ?""",
            (stop_price, stop_tier_index, position_id),
        )
        self._conn.commit()

    def reduce_remaining(self, position_id: int, sold_qty: int) -> int:
        """Subtracts a filled exit quantity from the user's remaining size,
        closing the position when it reaches zero. Returns the new
        remaining quantity."""
        self._conn.execute(
            """UPDATE positions
               SET user_remaining_qty = MAX(0, user_remaining_qty - ?),
                   updated_at = datetime('now')
               WHERE id = ?""",
            (sold_qty, position_id),
        )
        row = self._conn.execute(
            "SELECT user_remaining_qty FROM positions WHERE id = ?", (position_id,)
        ).fetchone()
        remaining = int(row["user_remaining_qty"]) if row is not None else 0
        if remaining <= 0:
            self._conn.execute(
                """UPDATE positions SET status = 'CLOSED', updated_at = datetime('now')
                   WHERE id = ? AND status = 'OPEN'""",
                (position_id,),
            )
        self._conn.commit()
        return remaining

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


def _encode_targets(targets: tuple[TrimTarget, ...]) -> str | None:
    return json.dumps([[t.pct, t.price] for t in targets]) if targets else None


def _decode_targets(raw: str | None) -> tuple[TrimTarget, ...]:
    if not raw:
        return ()
    return tuple(TrimTarget(pct=pct, price=price) for pct, price in json.loads(raw))


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
        current_stop_price=row["current_stop_price"],
        stop_tier_index=row["stop_tier_index"],
        runner_qty=row["runner_qty"],
        trim_targets=_decode_targets(row["trim_targets_json"]),
        id=row["id"],
    )


def _row_to_leg(row: sqlite3.Row) -> TargetLeg:
    return TargetLeg(
        position_id=row["position_id"],
        tier_index=row["tier_index"],
        qty=row["qty"],
        tp_price=row["tp_price"],
        current_stop_price=row["current_stop_price"],
        oca_group=row["oca_group"],
        lmt_order_id=row["lmt_order_id"],
        stp_order_id=row["stp_order_id"],
        status=row["status"],
        id=row["id"],
    )
