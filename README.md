# options-scanner

Copy-trades option alerts pasted into a Discord channel, then manages each
position automatically: sell a sixth at each of +25/+50/+75/+100%, move the
stop to break-even at +25%, then trail it two levels behind the price (+25% at
+75%, +50% at +100%) and let the last third run. Every action is reported as an
embed in a separate updates channel.

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
5. buys with a **market order**, unless the live ask is more than **15% above**
   the alert price
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
TSLA 397.5C                no price: buy at market, whatever the ask
```

No date means **today's contract**. That was checked against the live chain:
QQQ 718P at 1.99 matched only the same-day put. The whole message must match;
anything extra is not read as an alert. This format has no checksum, so the
live market stands in for one: the alert is skipped if the ask is more than
**25% below** its price (a typo, or not the contract meant) or if there is no
ask to check against.

**With no price**, there is nothing to hold the ask against, so there is **no
price buffer**: neither the 15%-above cap nor the 25%-below check applies, and
it buys at market whatever the ask. Every other check still does (halt, session,
cutoff, daily loss limit, position slots, the contract existing, the
per-contract limit if set), and it still needs a live ask, because that is what
the size is worked out from. Two no-price alerts for the same contract within
the duplicate window count as a re-paste; add a price to buy it again sooner.
Entries are market orders either way.

**An average-down**, as the advisor's AVERAGING DOWN card or as a one-line
alert with `avg` (also `avg down`, `average down`, `averaging down`) at the
front or back: `avg QQQ 718P 1.50`, `QQQ 718P 1.50 avg`, or with no price,
`avg QQQ 718P`. It is **treated as a
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
| `ask_above_cap` | ask more than **15% above** the alert price — not chasing. Not applied when the alert names no price |
| `no_ask` | no live ask to check the price against, so no market order. With no price in the alert, no ask means nothing to size from, dry run included |
| `price_mismatch` | alerts with no trim targets (one-line, or a card without them): ask more than **25% below**, or no ask. Not applied when the alert names no price |
| `budget_too_small` | the budget can't buy one contract: a lotto gets **half** a trade's budget, a super lotto **a quarter** |
| market / cutoff | outside 9:30–16:00 ET; **no new entries after 15:30** |
| limits | **2 different contracts** already open, or the **daily loss limit** reached. There is no cap on the number of trades. |
| `duplicate_alert` | same contract at the same price within **15 minutes** |
| `contract_not_found` | IBKR does not list the contract |

### Position management

Measured from your **actual fill price**, never the advisor's quoted entry.

| Event | What happens |
|---|---|
| entry fills | stop at **−35%** of your fill, set on the first quote — the most a trade can lose before +25% |
| +25% reached | sell **1/6** of the original; stop moves to your entry fill — the trade can no longer lose |
| +50% reached | sell another 1/6; the stop holds at break-even |
| +75% reached | sell another 1/6; stop moves to **+25%** |
| +100% reached | sell another 1/6; stop moves to **+50%** — about a third is left running |
| every further +25% (+125, +150 … no upper end) | nothing sold, the stop stays at +50% — each posts a card as the runner climbs; a jump through several steps in one quote posts one card, for the highest |
| stop hit | bid at/below the stop on **2 consecutive quotes** → marketable limit sell |

The stop trails **two levels behind the price** (`stops.steps` in
`config.yaml`). On 6 contracts filled at $1.00:

| Bid reaches | Sell | Left | Stop | Worst case from here |
|---|---|---|---|---|
| entry $1.00 | — | 6 | $0.65 | −$210 |
| $1.25 (+25%) | 1 | 5 | $1.00 | +$25 |
| $1.50 (+50%) | 1 | 4 | $1.00 | +$75 |
| $1.75 (+75%) | 1 | 3 | $1.25 | +$225 |
| $2.00 (+100%) | 1 | 2 | $1.50 | +$350 |
| above | — | 2 runners | $1.50 | +$350 |

The runners are sold by hand (`!close`) or ridden to
expiration; the peak trail (`trail:`) is switched off. The stops are driven by
the **price**, not by a trim filling, so a position too small to trim at a
level (1 contract never trims) still gets that level's stop. A gap through
several levels in one quote sells each trim and moves the stop once, to the
highest.

Trims are percents of the **original** position, rounded to the **nearest**
contract with a minimum of 1 (`trim.rounding: nearest`), and at least one
contract always runs:

| Contracts | Sell at +25 / +50 / +75 / +100 | Runners |
|---|---|---|
| 1 | — | 1 |
| 2 | 1 / – / – / – | 1 |
| 3 | 1 / 1 / – / – | 1 |
| 4 | 1 / 1 / 1 / – | 1 |
| 5–9 | 1 each | 1–5 |
| 10–15 | 2 each | 2–7 |
| 16–21 | 3 each | 4–9 |

Small positions run out of contracts before the ladder does, but the step
stops still apply to whatever is left. `rounding: up` sells more per level
instead (7 contracts → 2 each, 1 runner).

#### The runner lock

When the runners outnumber the per-level sell by **2 or more**, half of them
(rounded down) get their own stop at +100% that trails **one level** behind the
price, with no upper end (`stops.runner_lock`). The other runners keep +50%.

| Price reaches | Locked runners' stop | Other runners' stop |
|---|---|---|
| +100% ($2.00) | +75% ($1.75) | +50% ($1.50) |
| +125% ($2.25) | +100% ($2.00) | +50% |
| +150% ($2.50) | +125% ($2.25) | +50% |
| every +25% after | one level behind | +50% |

| Contracts | Sell per level | Runners | Locked | Others |
|---|---|---|---|---|
| 1–6, 10, 11, 16 | — | — | none | all |
| 7 / 8 / 9 | 1 | 3 / 4 / 5 | 1 / 2 / 2 | 2 / 2 / 3 |
| 12 / 13 | 2 | 4 / 5 | 2 / 2 | 2 / 3 |
| 14 / 15 | 2 | 6 / 7 | 3 / 3 | 3 / 4 |
| 17 | 3 | 5 | 2 | 3 |
| 20 | 3 | 8 | 4 | 4 |
| 50 | 8 | 18 | 9 | 9 |

9 contracts at $1.00 that peak at $2.50 and fall back: the trims bank $250, the
2 locked runners sell at $2.25 (+$250), the other 3 at $1.50 (+$150) — about
+$650 on $900. The locked stop has its own 2-quote confirmation and its own
SOLD LOCKED card; a drop through both stops at once sells everything in one
order. The lock fires once per position and survives a restart.

The stop only ever moves up. Before +25% it is the **−35% initial stop**
(`[0, -35]` in `stops.steps`): on a ~$650 trade on the $2,400 account that is
about −$210 to −$235 at worst, against the whole $650 with no stop. Stops round
to the cent, so a cheap contract's lands a little tighter ($0.47 → $0.31,
−34%), and a fast drop can fill below it: the exit needs 2 quotes under the
stop and sells 2% through the bid. At about −$210 a stop-out, the $873 daily
loss limit halts new entries after the fifth.

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

Contracts are counted at the **live ask** when it is above the alert price,
since that is what a market order pays: $1,000 buys 21 at a $0.475 alert but
18 at a $0.54 ask.

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
$EDITOR .env          # token and the three IDs; optional webhooks for P&L and the board
$EDITOR config.yaml   # sizing, caps, cutoff
```

`.env` holds secrets and is gitignored. `config.yaml` holds tunables and is
committed — secrets are never read from it, even if you paste them there.

The settings most worth reading before a first run:

```yaml
mode: paper                       # dry_run | paper | live
entry:
  max_slippage_pct: 15            # skip if the ask is more than 15% above the alert
risk:
  paper_equity: 4000              # paper only: rehearse this account size
  small_account_budget_pct: 27.2727   # per trade below $5,000 ($300 on $1,100)
  max_contract_price: null        # no per-contract ceiling
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
| `ibkr` | Real orders sent to IBKR: market buys, limit sells. On port 4002 that is the paper account — but it is a real order path. |

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

### Closing from the alerts channel

Typed in the **alerts channel** (where you paste the alerts), by you only, as
the whole message. No `!` needed; case does not matter.

| You type | Effect | Answer |
|---|---|---|
| `close !` | **Emergency switch:** closes everything now, one step, no confirmation. New alerts are still taken — `!halt` to stop those too | "Closing everything: NVDA 220C, SPY 772C" |
| `close NVDA` | Closes that position now; everything else is left alone | "Closing NVDA 220C" |
| `close NVDA` with two NVDA open | Closes **nothing** and asks which | "2 NVDA positions are open — say which: `close NVDA 220C` or `close NVDA 225C`" |
| `close NVDA 225C` | Closes just that contract | "Closing NVDA 225C" |
| `close TSLA` with none open | Closes nothing | "No open TSLA position", with what is open |

The message gets 👀, then ✅ closed, ❓ say which, or ❌ nothing matched. A close
that does not complete answers "Still open: …" and pings you. The SOLD and P&L
cards go to their usual channels; the short answer goes back to the alerts
channel through `ALERTS_WEBHOOK_URL`, or to updates when that is blank.

Only a one-line message of exactly that shape is a command, so an advisor line
like "Close or trim & set SL to breakeven." is still read as an alert.
`close all` is the ticker ALL, not everything — with no ALL position it closes
nothing and says to type `close !`.

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

### The positions board

Optional. Set `POSITIONS_WEBHOOK_URL` in `.env` to a webhook in a channel of
its own (Channel → Edit Channel → Integrations → Webhooks → New Webhook; it
can be in another server) and the bot keeps **one message** there, edited in
place, that always shows what is open:

```
Open positions
Day P&L +$206.00 · realized +$80.00 · open +$126.00
`paper` · broker connected · taking entries
market open, 214 min to 16:00 ET close
Updated 11:00:05 (2 seconds ago)

SPY 772C · 0DTE
+42.0% · bid $1.42 · entry $1.00
Open P&L +$126.00 on 3 of 6 · realized +$50.00
Stop $1.00 (break-even) · peak $1.55
Next +50% at $1.50
```

- **Open P&L is valued on the bid**, the price the stops judge against and
  what the position would actually sell for.
- **It refreshes every 2 seconds** while positions are open in the session
  (`board.refresh_seconds`). At other times it changes only when something
  does, plus a heartbeat every 60 seconds (`board.idle_refresh_seconds`).
- **"Updated … ago" is the liveness check.** Discord keeps that counter
  ticking on its own, so a board whose bot has died visibly ages ("14 minutes
  ago") instead of passing for live. On a clean stop (including
  `ops.sh stop`/`restart`) the board turns grey and says **Bot offline**,
  with how many positions are left with no stop.
- It reads what the position manager already polled, so it makes **no quote
  requests of its own**, and it runs as its own task: a slow Discord costs the
  board, never a stop.
- A restart edits the same message. Delete it and a new one is posted.
  Commands still go in updates; the board channel is read-only.

Yellow means a health problem (broker down, entries halted), grey means nothing
is open, otherwise green or red for the day. It never pings. If the webhook
fails its startup check, or points at the alerts or updates channel, the board
switches off with a warning in updates and trading carries on.

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
  board.py           the positions board: one message, edited in place
  execution.py       market orders in, marketable limits out
  position_manager.py the polling loop that drives the rules against quotes
  pipeline.py        the alert path, testable without Discord
  discord_bot.py     thin Discord adapter
  broker/base.py     the Broker interface every adapter implements
  broker/paper.py    simulated fills over a real quote feed
  broker/ibkr_quotes.py  real IBKR quotes, contract qualification
  broker/ibkr.py     real IBKR orders, positions, account
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
- **The −35% initial stop is the only protection before +25%.** It is set on
  the first quote after the fill (~1.5s), so the very first seconds of a trade
  are covered by `close !` alone. On a restart, an open position below +25%
  with no stop gets it on its first quote — and sells within seconds if it is
  already more than 35% down.
- **A 1-contract position never trims**, because it cannot without breaking the
  runner. The step stops are driven by the price, so it still moves to
  break-even at +25%, +25% at +75% and +50% at +100%. (With `stops.steps`
  empty, breakeven waits for a trim fill and a 1-contract position goes
  unprotected until the peak trail arms.)
- **A market entry can pay more than the buffer.** The 15% check runs on the
  quote just before the order goes out. Once sent, a market order pays
  whatever the book asks, so on a thin or fast 0DTE book the fill can land
  above it. The entry card shows the real fill, and the ladder is measured
  from it.
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
