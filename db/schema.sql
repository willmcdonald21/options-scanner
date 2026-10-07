-- Full audit trail plus the state the bot needs to survive a restart.
--
-- Two principles throughout. Nothing is ever deleted: a row that turned out
-- wrong is superseded, not removed, so "why did this happen" is always
-- answerable after the fact. And anything the bot must remember across a
-- restart lives here rather than in memory, because a synthetic stop that
-- forgets its peak on restart would quietly loosen itself.

-- Every alert seen in the alerts channel, parsed or not. The Discord message
-- id is the primary key, which is the first line of deduplication: the same
-- message can never be processed twice, including after a restart.
CREATE TABLE IF NOT EXISTS alerts (
    message_id INTEGER PRIMARY KEY,
    channel_id INTEGER NOT NULL,
    author_id INTEGER NOT NULL,
    jump_url TEXT,
    raw_text TEXT NOT NULL,
    -- RECEIVED -> ACCEPTED | REJECTED | SKIPPED | DUPLICATE | INFO
    status TEXT NOT NULL,
    reason TEXT,
    -- Parsed contract, present once an entry alert validates. Also the second
    -- line of deduplication: the same contract at the same entry inside the
    -- configured window is a re-paste, even under a new message id.
    occ_symbol TEXT,
    ticker TEXT,
    expiry TEXT,
    strike REAL,
    right TEXT,
    entry_price REAL,
    advisor_contracts INTEGER,
    received_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_alerts_fingerprint ON alerts (occ_symbol, entry_price, received_at);
CREATE INDEX IF NOT EXISTS idx_alerts_status ON alerts (status, received_at);

-- One row per position the bot opened. The rules-engine state is persisted in
-- full (peak, stop, which levels have fired) because every one of those is
-- load-bearing: a forgotten fired level would trim twice, and a forgotten
-- peak would walk the trailing stop backwards.
CREATE TABLE IF NOT EXISTS positions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    alert_message_id INTEGER NOT NULL REFERENCES alerts (message_id),
    occ_symbol TEXT NOT NULL,
    ticker TEXT NOT NULL,
    expiry TEXT NOT NULL,
    strike REAL NOT NULL,
    right TEXT NOT NULL CHECK (right IN ('C', 'P')),
    entry_fill REAL NOT NULL,
    original_qty INTEGER NOT NULL,
    remaining_qty INTEGER NOT NULL,
    peak_bid REAL NOT NULL,
    stop_price REAL,
    stop_reason TEXT CHECK (stop_reason IN ('breakeven', 'trail')),
    trail_armed INTEGER NOT NULL DEFAULT 0,
    fired_levels TEXT NOT NULL DEFAULT '[]',
    consecutive_breaches INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'OPEN' CHECK (status IN ('OPEN', 'CLOSED')),
    realized_pnl REAL NOT NULL DEFAULT 0.0,
    opened_at TEXT NOT NULL DEFAULT (datetime('now')),
    closed_at TEXT,
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- One open position per contract at a time.
CREATE UNIQUE INDEX IF NOT EXISTS idx_positions_open ON positions (occ_symbol) WHERE status = 'OPEN';
CREATE INDEX IF NOT EXISTS idx_positions_status ON positions (status);

-- Append-only log of every order the bot submitted, including the ones that
-- were never filled.
CREATE TABLE IF NOT EXISTS orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    position_id INTEGER REFERENCES positions (id),
    alert_message_id INTEGER REFERENCES alerts (message_id),
    occ_symbol TEXT NOT NULL,
    -- ENTRY | TRIM | STOP_OUT | FLATTEN | FORCE_EXIT
    intent TEXT NOT NULL,
    side TEXT NOT NULL CHECK (side IN ('BUY', 'SELL')),
    order_type TEXT NOT NULL DEFAULT 'LMT',
    qty INTEGER NOT NULL,
    limit_price REAL,
    trim_level_pct INTEGER,
    broker_order_id TEXT,
    -- PENDING | PARTIAL | FILLED | CANCELLED | REJECTED | TIMEOUT | DRY_RUN
    status TEXT NOT NULL,
    filled_qty INTEGER NOT NULL DEFAULT 0,
    avg_fill_price REAL,
    detail TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_orders_position ON orders (position_id);
CREATE INDEX IF NOT EXISTS idx_orders_broker_id ON orders (broker_order_id);

-- Individual fills, kept separately so a partial-fill sequence is
-- reconstructable and the average price is never just asserted.
CREATE TABLE IF NOT EXISTS fills (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id INTEGER NOT NULL REFERENCES orders (id),
    qty INTEGER NOT NULL,
    price REAL NOT NULL,
    filled_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_fills_order ON fills (order_id);

-- Per-day counters for the daily loss limit and trade cap. Keyed by the
-- Eastern trading date, not by UTC.
CREATE TABLE IF NOT EXISTS day_stats (
    trading_day TEXT PRIMARY KEY,
    entries INTEGER NOT NULL DEFAULT 0,
    realized_pnl REAL NOT NULL DEFAULT 0.0,
    alerts_received INTEGER NOT NULL DEFAULT 0,
    alerts_rejected INTEGER NOT NULL DEFAULT 0,
    alerts_skipped INTEGER NOT NULL DEFAULT 0,
    halted_at TEXT,
    halt_reason TEXT
);

-- Small key/value store for state that must outlive the process, notably the
-- !halt flag: a bot that restarts into an un-halted state after being halted
-- would start trading again on its own.
CREATE TABLE IF NOT EXISTS bot_state (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);
