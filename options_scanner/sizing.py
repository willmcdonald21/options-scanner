"""Position sizing: our own budget, never the advisor's contract count.

The advisor's "Contracts: 25" reflects their account, not ours, so it is
recorded for the audit trail and otherwise ignored.

Two steps, deliberately separate:

1. `unit_cap` turns live account equity into a dollar budget, following the
   guide's "1 unit = 2-5% of your account; lotto = half; super lotto = a
   quarter".
2. `compute_contracts` turns that budget into a contract count.

Keeping them apart is what lets the dollar ceiling be asserted independently
of the contract rounding, and it is the ceiling that bounds the loss.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Literal

# The advisor's three sizing tiers. A "lotto" is a long shot taken small on
# purpose; sizing one like a normal unit is the mistake the tier exists to
# prevent.
Tier = Literal["normal", "lotto", "super_lotto"]

TIER_LABELS: dict[str, str] = {
    "normal": "",
    "lotto": "lotto",
    "super_lotto": "super lotto",
}


def tier_multiplier(
    tier: str,
    *,
    lotto: float = 0.5,
    super_lotto: float = 0.25,
) -> float:
    """What fraction of one unit a tier gets."""
    if tier == "normal":
        return 1.0
    if tier == "lotto":
        return lotto
    if tier == "super_lotto":
        return super_lotto
    raise ValueError(f"unknown sizing tier {tier!r}; expected one of {sorted(TIER_LABELS)}")


def unit_cap(
    equity: float,
    unit_pct: float,
    tier: str = "normal",
    *,
    lotto_multiplier: float = 0.5,
    super_lotto_multiplier: float = 0.25,
    ceiling_usd: float | None = None,
) -> float:
    """The dollar budget for one trade.

    `unit_pct` is a whole percent of `equity` (net liquidation), scaled down
    for a lotto or super lotto. `ceiling_usd` is an absolute cap applied
    *after* the percentage: a percentage alone grows without bound as the
    account grows, and the first live sessions want a number that does not.
    """
    if equity <= 0:
        raise ValueError(f"equity must be positive, got {equity}")
    if not 0 < unit_pct <= 100:
        raise ValueError(f"unit_pct must be in (0, 100], got {unit_pct}")

    multiplier = tier_multiplier(
        tier, lotto=lotto_multiplier, super_lotto=super_lotto_multiplier
    )
    cap = equity * (unit_pct / 100.0) * multiplier

    if ceiling_usd is not None:
        if ceiling_usd <= 0:
            raise ValueError(f"ceiling_usd must be positive, got {ceiling_usd}")
        cap = min(cap, ceiling_usd)
    return cap


# How a trade's budget was arrived at.
#   "unit"          -- a percent of equity, the guide's "1 unit = 2-5%"
#   "small_account" -- a fixed dollar budget, because on a small account a
#                      percent of equity cannot buy a single contract
Basis = Literal["unit", "small_account"]


@dataclass(frozen=True)
class Budget:
    """What one trade may spend, and why."""

    cap_usd: float          # after the tier and the ceiling
    basis: Basis
    sizing_equity: float    # the equity the budget was computed from
    real_equity: float      # what the account actually reports
    base_usd: float = 0.0   # one normal trade's budget, before the tier and ceiling


def sizing_equity(equity: float, max_sizing_equity: float | None = None) -> float:
    """The equity sizing works from: the real figure, capped.

    The cap exists for the shared paper account. IBKR allows one paper account
    per login, so paper testing runs on a balance far larger than the live
    options account. Sizing off the smaller of the two makes paper produce the
    contract counts and skips the live account will. On the live account the
    real balance is already the smaller one, so the cap changes nothing.
    """
    if max_sizing_equity is None:
        return equity
    return min(equity, max_sizing_equity)


def trade_budget(
    equity: float,
    tier: str = "normal",
    *,
    unit_pct: float,
    small_account_below: float,
    small_account_budget_usd: float,
    small_account_budget_pct: float | None = None,
    lotto_multiplier: float = 0.5,
    super_lotto_multiplier: float = 0.25,
    ceiling_usd: float | None = None,
    max_sizing_equity: float | None = None,
) -> Budget:
    """The dollar budget for one trade, choosing the sizing basis.

    Below `small_account_below` a percent of equity cannot buy a contract --
    3% of $1,100 is $33, and the cheapest alerts cost ~$50 a contract -- so a
    small-account budget is used instead: `small_account_budget_pct` of the
    sizing equity when set, so it scales with the account, otherwise the fixed
    `small_account_budget_usd`. At or above it, the guide's percent-of-equity
    unit. The tier multiplier applies either way, and `ceiling_usd` caps the
    result.

    A percentage budget is rounded to the cent, so 27.2727% of $1,100 is
    exactly $300.00 -- unrounded it is $299.997, which buys one fewer $0.75
    contract than $300 does.
    """
    if equity <= 0:
        raise ValueError(f"equity must be positive, got {equity}")
    if small_account_budget_usd <= 0:
        raise ValueError(f"small_account_budget_usd must be positive, got {small_account_budget_usd}")

    sized = sizing_equity(equity, max_sizing_equity)
    multiplier = tier_multiplier(
        tier, lotto=lotto_multiplier, super_lotto=super_lotto_multiplier
    )

    if sized < small_account_below:
        if small_account_budget_pct is not None:
            if not 0 < small_account_budget_pct <= 100:
                raise ValueError(
                    f"small_account_budget_pct must be in (0, 100], got {small_account_budget_pct}"
                )
            base = round(sized * small_account_budget_pct / 100.0, 2)
        else:
            base = small_account_budget_usd
        cap = base * multiplier
        if ceiling_usd is not None:
            if ceiling_usd <= 0:
                raise ValueError(f"ceiling_usd must be positive, got {ceiling_usd}")
            cap = min(cap, ceiling_usd)
        return Budget(
            cap_usd=cap, basis="small_account", sizing_equity=sized, real_equity=equity, base_usd=base
        )

    cap = unit_cap(
        sized,
        unit_pct,
        tier,
        lotto_multiplier=lotto_multiplier,
        super_lotto_multiplier=super_lotto_multiplier,
        ceiling_usd=ceiling_usd,
    )
    base = sized * (unit_pct / 100.0)
    return Budget(cap_usd=cap, basis="unit", sizing_equity=sized, real_equity=equity, base_usd=base)


def affords_one_contract(cap_usd: float, price: float) -> bool:
    """Whether the budget covers a single contract at `price`.

    The question compute_contracts deliberately does not ask: it floors to one
    contract so a slightly-over-budget cheap contract still trades. On a small
    account that floor is not "slightly" -- a super lotto's $75 budget floored
    up to a $295 contract is four times the intended risk -- so the pipeline
    asks this first and skips when the answer is no.
    """
    return cap_usd + 1e-9 >= price * 100


def sizing_note(
    *,
    equity: float,
    unit_pct: float,
    tier: str,
    cap_usd: float,
    equity_is_assumed: bool = False,
    basis: Basis = "unit",
    small_account_budget_usd: float | None = None,
    sized_as: float | None = None,
) -> str:
    """One line saying where a size came from, for the entry card.

    `equity_is_assumed` is set in dry run, which has no broker to ask. A size
    derived from an assumed balance has to say so -- reported bare it reads as
    a real position sizing off a real account.

    `sized_as` is the capped sizing equity, shown when it differs from the
    real balance -- on the shared paper account, a size reported against a
    $32,000 balance without saying it was sized as $1,100 would look wrong.
    """
    if basis == "small_account" and small_account_budget_usd is not None:
        parts = [f"${small_account_budget_usd:,.0f} small-account budget"]
    else:
        parts = [f"{unit_pct:g}% unit"]
    label = TIER_LABELS.get(tier, tier)
    if label:
        multiplier = tier_multiplier(tier)
        parts.append(f"{label} \u00d7{multiplier:g}")
    source = "assumed equity" if equity_is_assumed else "equity"
    line = f"${cap_usd:,.0f} of ${equity:,.0f} {source}"
    if sized_as is not None and sized_as < equity:
        line += f", sized as ${sized_as:,.0f}"
    parts.append(line)
    return "Our size: " + " \u00b7 ".join(parts)


def compute_contracts(
    fill_price: float,
    cap_usd: float,
    max_contracts: int | None = None,
) -> int:
    """How many contracts to buy at `fill_price` under a `cap_usd` budget.

    Rounded down, minimum 1 -- a cap smaller than one contract still takes
    the trade, because skipping an alert silently would be worse than being
    slightly over budget on the cheapest possible position. Size the cap
    accordingly.

    `max_contracts` is the separate hard ceiling from the risk config
    (`max_contracts_per_trade`), applied after the dollar maths. It is what
    pins live trading to 1 contract in phase 6 without touching this logic.
    """
    if fill_price <= 0:
        raise ValueError(f"fill_price must be positive, got {fill_price}")
    if cap_usd <= 0:
        raise ValueError(f"cap_usd must be positive, got {cap_usd}")

    contracts = max(1, math.floor(cap_usd / (fill_price * 100)))

    if max_contracts is not None:
        if max_contracts < 1:
            raise ValueError(f"max_contracts must be at least 1, got {max_contracts}")
        contracts = min(contracts, max_contracts)

    return contracts


def position_cost(contracts: int, fill_price: float) -> float:
    """Total debit for `contracts` at `fill_price`, at the standard 100
    multiplier."""
    return contracts * fill_price * 100
