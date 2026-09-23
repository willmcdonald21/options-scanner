from __future__ import annotations

import math


def compute_contracts(entry_price: float, cap_usd: float) -> int:
    """Number of contracts whose total cost (entry_price * 100/contract)
    stays within cap_usd, rounded down, minimum 1."""
    if entry_price <= 0:
        raise ValueError(f"entry_price must be positive, got {entry_price}")
    return max(1, math.floor(cap_usd / (entry_price * 100)))
