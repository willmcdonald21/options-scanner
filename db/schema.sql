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

-- Append-only audit log of every IBKR order the executor placed, recorded
-- once its terminal state (FILLED/REJECTED/TIMEOUT) is known. Not the
-- idempotency source of truth (processed_messages is) -- this exists so
-- "what did message X cause us to do" and "why did this position not
-- update" are answerable without reconstructing it from logs.
CREATE TABLE IF NOT EXISTS orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    message_id INTEGER NOT NULL,
    ticker TEXT NOT NULL,
    expiry TEXT NOT NULL,
    strike REAL NOT NULL,
    right TEXT NOT NULL CHECK (right IN ('C', 'P')),
    ib_order_id INTEGER,
    action TEXT NOT NULL CHECK (action IN ('BUY', 'SELL')),
    contracts INTEGER NOT NULL,
    order_type TEXT NOT NULL DEFAULT 'MKT',
    status TEXT NOT NULL CHECK (status IN ('FILLED', 'REJECTED', 'TIMEOUT')),
    avg_fill_price REAL,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_orders_message_id ON orders (message_id);
