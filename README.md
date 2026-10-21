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

### What you can paste

**The advisor's full BUY card**, exactly as posted. Its trim targets are used as
a checksum: if they don't match the entry, a number was misread and the alert
is rejected. A card with **no** targets (an empty "Trim Targets" block, or a
paste cut short) is accepted: the bot always assigns its own ladder from the
fill, and checks the price against the live market instead, as for one-line
alerts.

**A one-line alert** from the second analyst:

```
QQQ 718P 1.99              today's QQQ 718 put at $1.99
iwm 271p .7                case and a bare decimal are fine
QQQ 10/16 718P 1.99        a date overrides "today"
```

No date means **today's contract**. That was checked against the live chain:
QQQ 718P at 1.99 matched only the same-day put. The whole message must match;
anything extra is not read as an alert. This format has no checksum, so the
live market stands in for one: the alert is skipped if the ask is more than
**25% below** its price (a typo, or not the contract meant) or if there is no
ask to check against.

**An average-down**, as the advisor's AVERAGING DOWN card or as a one-line
alert with `avg` (also `avg down`, `average down`, `averaging down`) at the
front or back: `avg QQQ 718P 1.50`, `QQQ 718P 1.50 avg`. It is **treated as a
normal buy**, including when you already hold that contract. The card buys at
its "Added @" price.

Holding the contract already, the new contracts **merge into one position**,
since IBKR keeps one per contract:

- the entry becomes the weighted average of what is still held plus what was added
- the trim ladder starts over from that average, as on the advisor's
  "Trim Targets (new avg)"
- **any stop is cleared** until the new ladder's first trim fills, like a fresh
  buy. The AVERAGED DOWN card names the stop it cleared.

A plain BUY for a contract already held is still skipped. Only an average-down
buys more.

### When to expect a skip

| Reason | Rule |
|---|---|
| `ask_above_cap` | ask more than **10% above** the alert price — not chasing |
| `price_mismatch` | alerts with no trim targets (one-line, or a card without them): ask more than **25% below**, or no ask |
| `contract_too_expensive` | alert price above **$3.00**; the walk never pays more either |
| `budget_too_small` | the budget can't buy one contract: a lotto gets **half** a trade's budget, a super lotto **a quarter** |
| market / cutoff | outside 9:30–16:00 ET; **no new entries after 15:30** |
| limits | **2 different contracts** already open, or the **daily loss limit** reached. There is no cap on the number of trades. |
| `duplicate_alert` | same contract at the same price within **15 minutes** |
| `contract_not_found` | IBKR does not list the contract |

### Position management

Measured from your **actual fill price**, never the advisor's quoted entry.

| Event | What happens |
|---|---|
| +25% reached | sell **half** the position |
| +25% trim **fills** | stop moves to your entry fill — the trade can no longer lose |
| +50% reached | sell **a quarter** of the original — a quarter is left running |
| +75% reached | nothing sold, **the trailing stop arms** |
| every further +25% (+100, +125, +150 … no upper end) | nothing sold — each posts a card as the runner climbs; a jump through several steps in one quote posts one card, for the highest |
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

Every money limit is a **formula of the account**, so one config serves both
the live options account and the paper rehearsal:

| | Formula | Live ($1,100) | Paper ($4,000) |
|---|---|---|---|
| Per trade (under $5,000) | 27.27% of the account | $300 | $1,090.91 |
| Contracts open at once | 55% of the account ÷ one trade | 2 | 2 |
| Daily loss limit | 36.36% of the account | $400 | $1,454.54 |
| Trades per day | — | no cap | no cap |
| Lotto / super lotto | half / a quarter of a trade | $150 / $75 | $545 / $273 |

**Paper sizes as a $4,000 account** (`risk.paper_equity`), so each call buys
several contracts and the trims and stops get exercised: $2.18 buys 5, $1.00
buys 10. The paper account is shared with warrior_bot and holds far more, so
it has to be told what it is rehearsing. **Live never uses `paper_equity`**; it
always sizes off its real balance.

At or above $5,000 the guide's unit takes over: `risk.unit_pct_of_account`%
(3%) per trade, with `risk.max_usd_per_trade` as an absolute ceiling on any one
trade.

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
mode: paper                       # dry_run | paper | live
risk:
  paper_equity: 4000              # paper only: rehearse this account size
  small_account_budget_pct: 27.2727   # per trade below $5,000 ($300 on $1,100)
  max_contract_price: 3.00        # skip dearer contracts
  max_deployed_pct: 55            # contracts open = account x 55% / one trade
  max_daily_loss_pct: 36.3636     # halts new entries ($400 on $1,100)
  max_trades_per_day: null        # no cap
  max_usd_per_trade: 1100         # absolute ceiling on one trade
  unit_pct_of_account: 3.0        # at or above $5,000
  lotto_multiplier: 0.5           # "lotto = half"
  super_lotto_multiplier: 0.25    # "super lotto = a quarter"
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
| `!close NVDA` | Close **one** position now; leaves the rest alone and does not halt entries. `!close QQQ 718P` if a ticker is held twice |
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
SPX 7815P · 0DTE
RUNNER +200%
The runner reached +200%. Nothing sold — 4 still running.
Peak         Stop
$1.44        $0.79 (+64.6%)
```

and each stop move the same way, contract first:

```
SPX 7815P · 0DTE
STOP → $0.79
Trailing stop ratcheted up with a new peak.
Stop             Why
$0.48 → $0.79    trail
```

Two deliberate differences from the advisor's cards: the `Trim Targets` block on
an entry card is recomputed off **our** fill, not the entry the card advertised,
and the footer names this bot so a card of ours can never be mistaken for one of
theirs.

### The P&L channel

Optional. Set `PNL_WEBHOOK_URL` in `.env` to a webhook in its own channel and
every sale (trim, stop-out or `!flatten`) posts one card there with just two
numbers:

```
TRIM +25% — SPY 772C · 0DTE
Trade P&L      Day P&L
+$44.50        +$106.17
```

**Trade P&L** is that sale's realized result, measured against the entry. **Day
P&L** is the day's realized total, the same figure the daily loss limit
counts. Both are before commissions. Green for a gain, red for a loss, and
never a ping. If the webhook fails its startup check, P&L cards switch off, a
warning goes to updates, and trading carries on.

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
| Market-data lines | warrior self-caps at 90; IBKR's default is 100 | we add at most one per open contract (2, from the position formula). `doctor` warns if the total would exceed 100. |
| Buying power | shared | the scanner uses at most about 55% of the account it is sized as (`max_deployed_pct`) |
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

### When Gateway drops

The bot keeps going and reconnects by itself:

- **one** alert when the connection drops. It is critical (a ping) only if a
  position is open, because only then is a stop blind; otherwise a quiet warning.
- retries every **30 seconds at most during market hours**, backing off to 5
  minutes outside them
- a pasted alert **connects immediately** rather than waiting for the next retry
- **"Broker reconnected"** when it is back, followed by a re-check of every
  position against IBKR
- while positions are open and it is still down, a reminder every 15 minutes

Gateway only opens its API port **after you sign in**. A Gateway window sitting at
the login screen refuses the bot exactly like a closed one.

Set **Configure → Settings → Lock and Exit → Auto restart** and click **Apply**.
Without it Gateway logs off overnight and stays off. IBKR still requires a
manual sign-in about once a week.

### Shared paper account

IBKR allows one paper account per login, so on paper both bots trade the same
account. warrior_bot is patched to act only on **stocks**: its 15:55 EOD flatten,
daily-loss flatten and kill switch leave option positions alone, and option
fills and positions stay out of its risk numbers. `doctor` checks the patch is
present, on its main, and running.

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
