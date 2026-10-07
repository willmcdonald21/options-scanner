# options-scanner

Copy-trades option alerts pasted into a Discord channel, then manages each
position automatically: a trim ladder, a breakeven stop once the first trim
fills, and a trailing stop from +75% onward. Every action is reported as an
embed in a separate updates channel.

Real money is involved. The bot defaults to `dry_run`, and live trading needs
two independent confirmations.

---

## How it works

You paste an advisor alert into the **alerts channel**. The bot:

1. reacts 👀 so you can see the paste was picked up
2. parses the card and validates it (expiry cross-check, trim-target checksum)
3. runs the risk gate (halted? market open? caps? duplicate?)
4. sizes the position **from your config**, not the advisor's contract count
5. buys with a **limit order**, walked up to a slippage cap — never a market order
6. manages the position until it closes
7. reacts ✅ (ordered), ❌ (rejected) or 🚫 (skipped) and posts the detail to the
   **updates channel**

### Position management

Measured from your **actual fill price**, never the advisor's quoted entry.

| Event | What happens |
|---|---|
| +25% reached | sell 25% of the position |
| +25% trim **fills** | stop moves to your entry fill — the trade can no longer lose |
| +50% reached | sell 25% of what remains |
| +75% reached | sell 25% of what remains, **and the trailing stop arms** |
| +100% reached | nothing sold — the runner rides the trail from here |
| trail, once armed | stop = `entry + 0.40 × (peak − entry)`, i.e. gives back 60% of the gain |
| stop hit | bid at/below the stop on **2 consecutive quotes** → marketable limit sell |

The stop only ever moves up. There is **no stop before the first trim fills** —
that window is unprotected by design, and `!flatten` plus the daily loss limit
are the only brakes on it.

Stops are **synthetic**: the bot watches quotes and sends a sell when the level
breaks. No broker-side stop is ever placed, because a wide 0DTE spread triggers
one on a bad quote.

---

## Setup

### 1. Create the Discord bot

1. <https://discord.com/developers/applications> → **New Application**
2. **Bot** tab → **Add Bot**
3. **Enable the `MESSAGE CONTENT INTENT`** under Privileged Gateway Intents.
   Without it every message arrives with empty content, which looks exactly
   like an empty paste.
4. **Reset Token** and copy it. This is the only time it is shown.
5. **OAuth2 → URL Generator**: scope `bot`, permissions:
   - View Channels
   - Read Message History
   - Send Messages
   - Add Reactions
   - Embed Links
6. Open the generated URL and invite the bot to your own server.

### 2. Get the IDs

Discord → **Settings → Advanced → Developer Mode** on. Then right-click to
**Copy ID** for:

- the alerts channel (where you paste)
- the updates channel (where the bot reports) — must be a **different** channel
- yourself (right-click your name → Copy User ID)

### 3. Install

```bash
cd options-scanner
python3.11 -m venv .venv
.venv/bin/pip install -e '.[dev]'
```

### 4. Configure

```bash
cp .env.example .env
$EDITOR .env          # token and the three IDs
$EDITOR config.yaml   # sizing, caps, cutoff
```

`.env` holds secrets and is gitignored. `config.yaml` holds tunables and is
committed — secrets are never read from it, even if you paste them there.

The settings most worth reading before a first run:

```yaml
mode: dry_run                     # dry_run | paper | live
risk:
  max_usd_per_trade: 1000         # your size, not the advisor's
  max_contracts_per_trade: 50     # set to 1 for the first live sessions
  max_open_positions: 3
  max_daily_loss_usd: 1000        # auto-halts entries when breached
  max_trades_per_day: 10
market:
  entry_cutoff: "15:30"           # no new entries after this (ET)
  force_exit_enabled: false       # off: a runner can expire worthless
```

### 5. Check it

```bash
.venv/bin/python -m options_scanner.main --check
```

Validates config, opens the database, and prints the resolved settings without
connecting to anything.

### 6. Run it

```bash
.venv/bin/python -m options_scanner.main
```

Startup **refuses to proceed** unless it has actually verified it can read the
alerts channel and post to the updates channel — a bot that silently lacks
permission on one of them looks identical to a bot with nothing to do.

---

## Modes

`mode` says how much is at risk; `broker.kind` says where fills come from.

| Mode | Behaviour |
|---|---|
| `dry_run` | Parses, validates, risk-checks and sizes, then posts what it *would* do. No broker at all. |
| `paper` | Trades. What that means depends on `broker.kind`. |
| `live` | Real money. Requires `mode: live` **and** `live_confirmed: true` **and** typing `LIVE` at startup. |

| `broker.kind` | Behaviour |
|---|---|
| `simulated` | Fills simulated locally against **real IBKR quotes**. Nothing leaves the process. The default. |
| `ibkr` | Real limit orders sent to IBKR. On port 4002 that is the paper account — but it is a real order path. |

The combinations are policed, because each of these would otherwise misreport
what happened:

- `dry_run` + `ibkr` is refused — a dry run must not be able to reach an order path
- `live` + `simulated` is refused — it would report trades that never happened
- any non-live mode on a known live port (4001, 7496) is refused, so one
  mistyped digit cannot point a paper run at a live account

Override for one run: `--mode dry_run`.

### Working up to real orders

```bash
# 1. decisions only, no broker
.venv/bin/python -m options_scanner.main --mode dry_run

# 2. full lifecycle, simulated fills on real quotes   (config: kind: simulated)
.venv/bin/python -m options_scanner.main --mode paper

# 3. real orders to the IBKR paper account            (config: kind: ibkr)
.venv/bin/python -m options_scanner.main --mode paper
```

Step 3 needs IB Gateway running on the configured port with a free client ID.
`--check` prints which adapter and which port it resolved to.

---

## Commands

Typed in the **updates channel**, by you only. The bot ignores commands from
anyone else, and ignores everything in the alerts channel that is not from you.

| Command | Effect |
|---|---|
| `!status` | Open positions with P&L, peak, current stop, trims done, next rung |
| `!halt` | Stop opening new positions. **Open positions keep being managed.** |
| `!resume` | Allow new entries again |
| `!flatten` | Close everything — asks first; confirm with `!flatten confirm` within 60s |
| `!eod` | Post the end-of-day summary now |

The halt flag is persisted, so a bot that restarts after being halted stays
halted rather than quietly resuming on its own.

---

## Notifications

Everything goes to the updates channel. The alerts channel only ever receives a
reaction — the bot never posts text there.

- 🟩 green — profit events (fill, trim, stop raised)
- 🟥 red — stops hit at a loss, errors
- 🟨 yellow — warnings, skipped and rejected alerts
- 🟦 blue — informational

Errors and criticals **@-mention you** so they generate a push notification:
broker disconnected, stale quotes, daily loss limit hit, a position changing
without one of our orders, and restart reconciliation mismatches. Routine good
news does not ping.

Every notification carries a jump link back to the alert that caused it.

---

## Tests

```bash
.venv/bin/python -m pytest -q                            # all of it, no network
.venv/bin/python -m pytest tests/test_rules.py -q         # the rules engine
.venv/bin/python -m pytest tests/test_position_manager.py -q   # the live lifecycle
```

The parser and the rules engine are pure and fully covered offline. The rules
tests assert the exact trims and stops along each of six price paths (straight
up, gap through several levels, spike and crash, never reaching +25%, reaching
+25% then reversing, arming the trail then dropping), plus a fuzz over the two
invariants that would cost real money: a stop that moves down, and selling more
contracts than are held.

`test_position_manager.py` runs the same ladder through the real pipeline,
rules engine and PaperBroker over a controllable price feed, including a
mid-position restart that asserts the peak, the stop and the fired-level set
come back identically.

---

## Layout

```
options_scanner/
  parser.py          alert card -> validated EntryAlert, or a reasoned rejection
  text_import.py     one pasted message -> alert cards
  contracts.py       trading class, routing, OCC symbol (SPX -> SPXW)
  sizing.py          dollar cap -> contract count
  rules.py           PURE position-management rules engine
  risk.py            halt state, caps, daily loss, the pre-entry gate
  market_hours.py    America/New_York sessions, holidays, early closes
  storage.py         SQLite audit trail, dedup, restart state
  notifier.py        notification builders (colours, pings, jump links)
  execution.py       limit-order walking in, marketable limits out
  position_manager.py the polling loop that drives the rules against quotes
  pipeline.py        the alert path, testable without Discord
  discord_bot.py     thin Discord adapter
  broker/base.py     the Broker interface every adapter implements
  broker/paper.py    simulated fills over a real quote feed
  broker/ibkr_quotes.py  real IBKR quotes, contract qualification
  broker/ibkr.py     real IBKR limit orders, positions, account
  main.py            wiring and startup checks
config.yaml          tunables (committed)
.env                 secrets (gitignored)
db/schema.sql
```

---

## Build status

| Phase | State |
|---|---|
| 1 — parser + validation | done |
| 2 — rules engine + price-path tests | done |
| 3 — Discord bot in `DRY_RUN` | done |
| 4 — PaperBroker, full lifecycle, restart/reconcile | done |
| 5 — IBKR order adapter | code done; **not yet validated against a live Gateway** |
| 6 — live, behind the flag, 1 contract max | not started |

Both adapters exist. `broker.kind` chooses between them, and the quote feed is
the real IBKR one either way.

---

## Things to know before trusting it with money

- **The bot must stay running.** The stop exists only in this process. If the
  machine sleeps, loses network, or the process dies, an open position has no
  stop. Disable sleep during market hours and run it under a supervisor.
- **No stop until the first trim fills.** A position that never reaches +25% has
  no protection at all.
- **A 1-contract position never gets a breakeven stop**, because it cannot trim
  without breaking the runner. It is unprotected until the trail arms at +75%.
  This matters most with `max_contracts_per_trade: 1`.
- **Forced end-of-day exit is off.** A 0DTE runner whose trail never trips
  expires worthless. The bot warns near the close; flip
  `market.force_exit_enabled` to change it.
- **Shared broker account.** If another process trades the same account, its
  account-wide cancels and flattens will reach this bot's positions. The bot
  watches for a position changing without one of its own orders and shouts, but
  that is detection, not prevention. Conversely, this bot's option positions
  are filtered out of nothing on the other side unless that process does its
  own filtering — see the `secType` note below.
- **Client IDs must not collide.** Each API client on one Gateway needs its own
  `broker.client_id`. IBKR refuses the second connection outright if two share
  one.

Not financial advice.
