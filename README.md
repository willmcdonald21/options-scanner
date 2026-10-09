# options-scanner

Copy-trades option alerts pasted into a Discord channel, then manages each
position automatically on the advisor's published playstyle: sell half at +25%
and another quarter at +50%, move the stop to break-even once trimmed, let the
last quarter run, and trail 60% below the peak from +75% onward. Every action
is reported as an embed in a separate updates channel.

Real money is involved. The bot defaults to `dry_run`, and live trading needs
two independent confirmations.

---

## How it works

You paste an advisor alert into the **alerts channel**. The bot:

1. reacts 👀 so you can see the paste was picked up
2. parses the card and validates it (expiry cross-check, trim-target checksum)
3. runs the risk gate (halted? market open? caps? duplicate?)
4. sizes the position **from your own account** (a percent of equity, halved
   for a lotto), not from the advisor's contract count
5. buys with a **limit order**, walked up to a slippage cap — never a market order
6. manages the position until it closes
7. reacts ✅ (ordered), ❌ (rejected) or 🚫 (skipped) and posts the detail to the
   **updates channel**

### Position management

Measured from your **actual fill price**, never the advisor's quoted entry.

| Event | What happens |
|---|---|
| +25% reached | sell **half** the position |
| +25% trim **fills** | stop moves to your entry fill — the trade can no longer lose |
| +50% reached | sell **a quarter** of the original — a quarter is left running |
| +75% reached | nothing sold, **the trailing stop arms** |
| +100 / 150 / 200 / 500 / 1000 / 2000% | nothing sold — each posts a card as the runner climbs |
| trail, once armed | stop = `peak × 0.40`, i.e. 60% below the peak, floored at your entry |
| above a +200% peak | the trail tightens to `peak × 0.55` |
| above a +500% peak | it tightens again to `peak × 0.70` |
| stop hit | bid at/below the stop on **2 consecutive quotes** → marketable limit sell |

Both trim fractions are of the **original** position, so the outcome does not
depend on whether the rungs were crossed in one quote or five.

The stop only ever moves up. There is **no stop before the first trim fills** —
that window is unprotected by design, and `!flatten` plus the daily loss limit
are the only brakes on it.

#### The trail is inert below +150%

Worth understanding before trusting it: `peak × 0.40` sits **below** your entry
until the peak reaches 2.5× entry. The effective stop is
`max(break-even, trail)`, so on a $2.95 fill:

| Peak | Trail says | Effective stop | What is protecting you |
|---|---|---|---|
| +25% (trim fills) | — | $2.95 | break-even |
| +75% (trail arms) | $2.06 | $2.95 | still break-even |
| +100% | $2.36 | $2.95 | still break-even |
| +150% | $2.95 | $2.95 | the trail catches up |
| +200% | $3.54 | $3.54 | the trail, +20% locked in |
| +500% (0.55) | $9.74 | $9.74 | +230% locked in |

Between +75% and +150% "trailing stop armed" means the trail is live, not that
it is protecting anything beyond break-even. The TRAIL ARMED card says so.
This is also why the multiplier tightens at the high levels.

#### Sizing

| | |
|---|---|
| One unit | `risk.unit_pct_of_account`% of live net liquidation (default 3%) |
| Lotto | half a unit |
| Super lotto | a quarter |
| Ceiling | `risk.max_usd_per_trade`, applied **after** the percentage |

The tier is read off the card — the advisor writes `Lotto Trade — RISKY` as a
description line. **It is sometimes said only in chat** ("Super lotto size
less", with no card), which the bot cannot see; that trade sizes as a full
unit. Both tier-detection failures size *up*, so watch the channel on lotto
days.

If the equity read fails, the alert is **skipped** rather than sized off a
guess. In `dry_run` there is no broker to ask, so `risk.fallback_equity` is
used and the card says "assumed equity".

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
  unit_pct_of_account: 3.0        # one unit, as a percent of net liquidation
  lotto_multiplier: 0.5           # "lotto = half"
  super_lotto_multiplier: 0.25    # "super lotto = a quarter"
  fallback_equity: 32000          # dry_run only; a live read failure skips
  max_usd_per_trade: 1000         # absolute ceiling, applied after the percent
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

Updates are shaped like the alert cards you paste in, so they read the same at a
glance:

```
TRIM +25% — SPX 7815P · 0DTE
Sold 11 of 21 @ $0.62 · 10 still running.
Stop moved to break-even at 0.48 — the remaining 10 can no longer lose money.
Entry        Exit         Locked In
$0.48        $0.62        +$154.00
```

and the runner's climb gets its own card at each level:

```
RUNNER +200% — SPX 7815P · 0DTE
The runner reached +200%. Nothing sold — 4 still running.
Peak         Stop
$1.44        $0.79 (+64.6%)
```

Two deliberate differences from the advisor's cards: the `Trim Targets` block on
an entry card is recomputed off **our** fill, not the entry the card advertised,
and the footer names this bot so a card of ours can never be mistaken for one of
theirs.

### Posting transport

Set `UPDATES_WEBHOOK_URL` in `.env` and updates go through that webhook, which
needs no channel permissions at all — a misconfigured bot role then cannot
silence the bot's reporting. Leave it blank and the bot posts directly.

Either way the **bot token is still required**: a webhook cannot read, and the
bot has to read the alerts channel and the commands you type in updates. The
webhook must point at the same channel as `UPDATES_CHANNEL_ID`, and startup
refuses to run if it does not.

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
+25% then reversing, arming the trail then dropping), plus a fuzz over the three
invariants that would cost real money: a stop that moves down, a stop set
below the entry fill, and selling more contracts than are held.

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

## Running alongside warrior_bot

Both bots share one Mac, one IB Gateway and one paper account. They were written
independently and each assumes it is alone, so the overlaps need managing.

```bash
./scripts/ops.sh doctor     # preflight — run this before every session
./scripts/ops.sh status     # where everything stands right now
./scripts/ops.sh restart warrior
./scripts/ops.sh start|stop|restart scanner
./scripts/ops.sh logs [scanner|launchd|warrior|gateway]
```

### Start order

Gateway → warrior_bot → options-scanner. The scanner reconciles against the
broker on startup; doing that on a dead session reports every position as
missing and raises a false alarm.

### What collides, and how it is handled

| Risk | Mechanism | Handling |
|---|---|---|
| **warrior_bot flattening our options** | its reconciliation walks *every* account position every 30s and flattens anything with no resting stop. Our stops are synthetic, so it sees "uncovered". | a `secType == "STK"` filter in warrior_bot, plus an account filter. `doctor` checks the filter is present **and** that the running process is newer than it — a process started before the patch is still running the old code. |
| Client ID collision | IBKR refuses the second connection outright | warrior 11, scanner 12, its scripts 111/161, the probe 19. `doctor` compares them. |
| `reqGlobalCancel()` | took no account argument, so it cancelled everything the login could see — including our working entry limits | **replaced** in warrior_bot with cancelling its own account's orders one at a time. `doctor` fails if the call ever comes back. |
| Market-data lines | warrior self-caps at 90; IBKR's default is 100 | we add at most `max_open_positions`. `doctor` warns if the total would exceed 100. |
| Buying power | shared | the scanner risks at most `max_open_positions × max_usd_per_trade`, since the ceiling binds whatever the percentage says |
| Mac sleep | a sleeping Mac has no synthetic stop | `sudo pmset -c sleep 0 disksleep 0`. `doctor` fails if it is not 0. |
| Gateway dropping | the stop silently stops working | enable **Auto restart** (not Auto logoff) in Configure → Settings → Lock and Exit. `doctor` reads Gateway's own log to tell you whether it is enabled. |

### Account isolation

Both bots now take an account id — `broker.account` here, `trading.account` in
warrior_bot. Blank means "whatever the login manages", which is correct and is a
no-op while the login holds one account.

Once a second account is linked under the same username, **both must be set**:

- IBKR rejects any order that does not name an account when more than one is
  managed, so every order would fail
- an unscoped position read returns the other bot's holdings, which feed
  straight into this bot's sell clamp and warrior_bot's flatten-on-sight
  reconciliation

Both bots refuse to connect in that state rather than discovering it on the
first alert of the day, and `doctor` checks the two ids are set, differ, and are
actually managed by the login. See
[docs/ibkr-separate-accounts.md](docs/ibkr-separate-accounts.md) for how to open
the second account.

What a second account does **not** isolate: one Gateway process (an outage still
stops both), the market-data line pool, and API request pacing. Those follow
from using one login.

### Keeping the scanner up

Its synthetic stop lives only inside the process, so staying up is a
correctness requirement rather than a convenience.

```bash
cp deploy/com.willmcdonald.options-scanner.plist ~/Library/LaunchAgents/
./scripts/ops.sh start scanner
```

`KeepAlive` restarts it on crash, throttled to 60s so a bad config produces a
visible crash-loop rather than a frantic one. No secrets are in the plist — the
bot loads `.env` itself, which also means it does not depend on launchd's
stripped environment.

### Market data is the gate

`doctor`'s market-data check is the one that decides whether this bot can work
at all. The trim ladder and the synthetic stop both trigger off the **bid**; no
bid means the bot enters a position and then never trims and never stops out.

Fix: Client Portal → Market Data Subscriptions → **OPRA (US Options Exchanges)
(NP, L1)**, then Settings → Account Settings → Paper Trading Account → **share
real-time market data with the paper account**. Both steps, or you pay and the
paper account still returns `Error 354`.

---

## Things to know before trusting it with money

- **The bot must stay running.** The stop exists only in this process. If the
  machine sleeps, loses network, or the process dies, an open position has no
  stop. Disable sleep during market hours and run it under a supervisor.
- **No stop until the first trim fills.** A position that never reaches +25% has
  no protection at all.
- **A 1-contract position never trims**, because it cannot without breaking the
  runner, so it never earns a breakeven stop the usual way. It gets one when
  the trail arms at +75% (the trail's floor), which leaves it unprotected from
  entry to +75%. This matters most with `max_contracts_per_trade: 1`, and at a
  3% unit on a five-figure account most alerts size well above one contract.
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
