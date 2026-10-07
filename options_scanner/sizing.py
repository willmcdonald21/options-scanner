"""Position sizing: our own dollar cap, never the advisor's contract count.

The advisor's "Contracts: 25" reflects their account, not ours, so it is
recorded for the audit trail and otherwise ignored.
"""

from __future__ import annotations

import math


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
