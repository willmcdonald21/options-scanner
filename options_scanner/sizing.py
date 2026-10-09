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


def sizing_note(
    *,
    equity: float,
    unit_pct: float,
    tier: str,
    cap_usd: float,
    equity_is_assumed: bool = False,
) -> str:
    """One line saying where a size came from, for the entry card.

    `equity_is_assumed` is set in dry run, which has no broker to ask. A size
    derived from an assumed balance has to say so -- reported bare it reads as
    a real position sizing off a real account.
    """
    parts = [f"{unit_pct:g}% unit"]
    label = TIER_LABELS.get(tier, tier)
    if label:
        multiplier = tier_multiplier(tier)
        parts.append(f"{label} \u00d7{multiplier:g}")
    source = "assumed equity" if equity_is_assumed else "equity"
    parts.append(f"${cap_usd:,.0f} of ${equity:,.0f} {source}")
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
