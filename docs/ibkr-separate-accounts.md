# Giving each bot its own IBKR account

Two bots share this machine: `warrior_bot` (equities) and `options-scanner`
(options). Today they also share one paper account, which means shared buying
power, mingled P&L, and a panic path in one that can cancel orders belonging to
the other.

IBKR supports what you want: **linked accounts under a single username**, with
one paper account per live account. Switching between them in Client Portal
works like Robinhood profiles, but the isolation is real — separate positions,
separate buying power, separate statements.

> **Confirm A2 below before doing anything else.** Everything downstream depends
> on whether IBKR will give you a usable second account type.

---

## What you end up with

```
one IBKR username ─┬─ live A (existing) → paper DUR662028  warrior_bot      client 11
                   └─ live B (new)      → paper DU…NEW     options-scanner  client 12

one IB Gateway on :4002 · one login · one OPRA subscription
```

---

## A1. Where the button is

Client Portal → **User menu** (head-and-shoulders icon, top right) →
**Settings → Trading → Open an Additional Account**

Select the **client type**, use the toggle for IRA status, complete the
application. On activation it links to your existing account automatically.

Quirk from IBKR's guide: you may log out mid-application and resume, but you
must **finish or delete** an application before starting another.

## A2. Which account type — confirm this first

IBKR's documentation says you can "create any number of new linked accounts" but
does **not** say whether a second *Individual* account is allowed. The common
restriction is one per client type per person.

Ask IBKR support, verbatim:

> I have an Individual account. I want a second linked account under the same
> username, for a separate automated strategy, with its own paper account. Can I
> open a second Individual account? If not, which client type do you recommend?

If a second Individual is refused, the realistic alternatives are **Joint**,
**Trust**, or **LLC/Corporate** — each with its own paperwork, and Trust/LLC
with real-world setup. **Avoid an IRA:** option permissions there are
restricted and 0DTE strategies are generally not permitted.

## A3. Requirements

| | |
|---|---|
| Matching identity | email, account title, tax ID and physical address must **match** across linked accounts |
| Funding | linked accounts are **separately funded** — the new one starts at $0 |
| Paper account | one per live account; appears once the live account is activated |
| Approval | full application: KYC, suitability questions, possibly a few business days |

The funding requirement is the real cost. You cannot open an empty shell account
just to obtain a paper mirror — the paper account is created off an *activated*
live account.

## A4. Trading permissions — the step that fails quietly

**Paper accounts mirror their live account's trading permissions.** If the new
live account has no option permissions, its paper account cannot trade options
either, and nothing will tell you why.

After activation: Client Portal → switch to the new account → **Settings →
Account Settings → Trading Permissions** → request:

- **Options (United States)**
- Confirm **index options** are included — SPX/SPXW are index options and are
  sometimes listed separately from equity options
- The tier must allow **long calls and puts** (the lowest tier does)

Expect suitability questions. Permission changes can take a day.

## A5. Market data — already paid for

Market data fees are charged **once per username**, not per linked account, so
your existing OPRA subscription covers the new account at no extra cost. This is
the main reason to prefer a linked account over a second IBKR username.

Verify after activation:

1. Client Portal → **Market Data Subscriptions** → **OPRA (US Options
   Exchanges) (NP, L1)** active
2. Settings → Account Settings → **Paper Trading Account** → **"Share
   real-time market data subscriptions with paper trading account"** on

## A6. Finding the account IDs

Both appear in Client Portal's **Account ID selector, bottom left**. The bots
need the *paper* IDs, and the fastest way to read them is from the API:

```bash
cd ~/Developer/options-scanner
.venv/bin/python scripts/ib_probe.py
```

It prints one `account_N=` line per managed account with its paper/live status,
so with two accounts linked you can see both and tell which is which.

## A7. Switching

- **Client Portal** — Account ID selector, bottom left
- **Gateway / TWS** — one login shows both; the API sees both at once
- **The bots** — never switch. Each is pinned to exactly one account. That is
  the whole point.

---

## Why the code must change too

With more than one account under the login, **IBKR rejects any order that does
not name an account.** So once the second account exists this is not optional —
it is the difference between working and every order failing.

Both bots currently call bare `ib.positions()` and set no `order.account`. The
changes needed are tracked in the plan; the summary is:

- every order sets `order.account`
- every read is scoped: `ib.positions(account=…)`, `portfolio(account=…)`,
  `accountValues(account=…)`
- `options-scanner` refuses to start if it sees multiple managed accounts and
  has none configured — better than discovering it on the first rejected order
- **`warrior_bot`'s `reqGlobalCancel()` is replaced** with cancelling only its
  own account's orders. `reqGlobalCancel` takes no account parameter and cancels
  everything the login can see, so it is the one leak a second account alone
  does not close

## What this fixes, and what it does not

**Fixed:** positions, buying power, P&L, statements, warrior_bot's position
reconciliation, and the `reqGlobalCancel` leak.

**Still shared, by design:** one Gateway process (an outage still stops both),
the market-data line pool, API request pacing. These follow from using one
login and are acceptable.

## Sources

- [Open an Additional Account](https://www.ibkrguides.com/clientportal/createnewlinkedaccount.htm)
- [Link All of My Existing Accounts Under a Single Username and Password](https://ibkrguides.com/clientportal/linkexistingaccounts.htm)
- [Linked Accounts (glossary)](https://www.interactivebrokers.com/campus/glossary-terms/linked-accounts/)
- [Link Existing Account Scenarios](https://www.interactivebrokers.com/en/trading/linked-accounts.php)
- [Requesting a Paper Trading Account](https://www.interactivebrokers.com/campus/trading-lessons/request-paper-trading-account/)

---

# When the account is approved

The code in both repos is already in place and inert. Blank accounts mean
"whatever the login manages", which is correct for one account. Switching over
is two config lines and a verification.

### 1. Confirm the paper account exists and has option permissions

```bash
cd ~/Developer/options-scanner
.venv/bin/python scripts/ib_probe.py            # one account_N= line per account
```

Both paper accounts should be listed with `paper=yes`. If only one appears, the
new live account is activated but its paper mirror has not been created yet —
that is an IBKR-side wait, not a config problem.

Then confirm option permissions landed on the **new** account (A4 above). Paper
mirrors the live account's permissions, so this is the step that silently breaks
everything else.

### 2. Decide which account each bot gets

The options account is the **new** one; warrior_bot keeps the existing
`DUR662028`, because it has the position history and the journal refers to it.

### 3. Set both config lines

```yaml
# ~/Developer/options-scanner/config.yaml
broker:
  account: "DU…NEW"        # the new one

# ~/Developer/VolatilityTrader/config/config.yaml
trading:
  account: "DUR662028"     # the existing one
```

Neither bot will start with these blank once the login manages two accounts.
That is deliberate: IBKR rejects every order in that state, so failing at
connect beats failing on the first alert of the day.

### 4. Verify before trading

```bash
cd ~/Developer/options-scanner
./scripts/ops.sh doctor
```

The `Account isolation` section checks the two ids are set, differ, and are
actually managed by the login.

### 5. Restart both bots

```bash
./scripts/ops.sh restart warrior     # its supervisor brings it back in ~30s
./scripts/ops.sh restart scanner
```

### 6. The test that proves it

This is the one the single-account setup could never pass. Do it in market
hours, with both accounts flat:

1. Open a 1-contract paper option position in the **scanner's** account by hand,
   and leave a working (unfilled) limit order in it — a buy well below the
   market will sit there.
2. Run warrior_bot's kill switch:
   `cd ~/Developer/VolatilityTrader && .venv/bin/python scripts/kill_switch.py`
3. Confirm **both** survive: the option position is untouched and the working
   order is still working.

Before the scoping work, step 2 would have cancelled that order via
`reqGlobalCancel` and flattened the position. If either disappears, stop and
re-check both `account` settings.

### What is still shared

One Gateway process, the market-data line pool, and API request pacing. A
Gateway outage still stops both bots. Those follow from using one login and are
accepted.
