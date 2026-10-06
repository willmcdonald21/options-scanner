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
    -- Where the ratcheting stop currently sits, and how far up the trim
    -- ladder it has climbed: -1 = still at the initial STOP_LOSS_PCT
    -- level, 0 = TP1 reached so stop is at breakeven, 1 = TP2 reached, ...
    current_stop_price REAL,
    stop_tier_index INTEGER NOT NULL DEFAULT -1,
    runner_qty INTEGER NOT NULL DEFAULT 0,
    -- The channel's published trim ladder, as JSON [[pct, price], ...].
    -- Persisted rather than recomputed because a runner tranche has no
    -- resting limit to read a price off, and at 1 contract there are no
    -- limit legs at all -- the ladder is the only record of where the
    -- rungs are once the process restarts.
    trim_targets_json TEXT,
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

-- One row per resting exit tranche at IBKR. A take-profit tranche holds a
-- SELL LMT plus its OCA-paired SELL STP for the same quantity; a runner
-- tranche has tp_price IS NULL and a stop leg only.
--
-- Quantities here are immutable once written. The stop for a tranche is
-- *modified in place* (same ib order id, new price) as the ladder
-- ratchets, never resized -- a stop whose quantity exceeds the contracts
-- actually held would go short at IBKR if it triggered.
--
-- These rows are the restart contract: order ids live here so a bot that
-- comes back up can re-attach to brackets already working at the broker
-- instead of double-placing them.
CREATE TABLE IF NOT EXISTS position_targets (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    position_id INTEGER NOT NULL REFERENCES positions (id),
    tier_index INTEGER NOT NULL,
    qty INTEGER NOT NULL,
    tp_price REAL,
    current_stop_price REAL NOT NULL,
    oca_group TEXT NOT NULL,
    lmt_order_id INTEGER,
    stp_order_id INTEGER,
    status TEXT NOT NULL DEFAULT 'LIVE'
        CHECK (status IN ('LIVE', 'TP_FILLED', 'STOP_FILLED', 'CANCELLED')),
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_targets_position ON position_targets (position_id, status);
CREATE INDEX IF NOT EXISTS idx_targets_lmt_order ON position_targets (lmt_order_id);
CREATE INDEX IF NOT EXISTS idx_targets_stp_order ON position_targets (stp_order_id);
