CREATE TABLE IF NOT EXISTS positions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker TEXT NOT NULL,
    expiry TEXT NOT NULL,
    strike REAL NOT NULL,
    right TEXT NOT NULL CHECK (right IN ('C', 'P')),
    channel_total_qty INTEGER NOT NULL,
    channel_remaining_qty INTEGER NOT NULL,
    user_original_qty INTEGER NOT NULL,
    user_remaining_qty INTEGER NOT NULL,
    entry_price REAL NOT NULL,
    ibkr_order_id_entry INTEGER,
    status TEXT NOT NULL DEFAULT 'OPEN' CHECK (status IN ('OPEN', 'CLOSED')),
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_positions_open_key
    ON positions (ticker, expiry, strike, right)
    WHERE status = 'OPEN';

CREATE TABLE IF NOT EXISTS processed_messages (
    message_id INTEGER PRIMARY KEY,
    processed_at TEXT NOT NULL DEFAULT (datetime('now'))
);
