from __future__ import annotations

from dataclasses import dataclass

from bot.models import TrimTarget

# Smallest price increment we'll ever submit. The channel publishes its
# trim ladder to three decimals ($2.381), which IBKR rejects outright --
# every price that reaches an order must be rounded here first. Penny
# increments are valid for the vast majority of equity/ETF options; a few
# classes (and most index options) only accept nickels above $3.00, so a
# rejection on those is expected and surfaces as a REJECTED order rather
# than silent breakage.
TICK = 0.01


def round_to_tick(price: float) -> float:
    """Nearest valid tick, never zero -- a stop or limit of $0.00 would be
    rejected, and for a deep-OTM contract the computed stop can round there."""
    rounded = round(round(price / TICK) * TICK, 2)
    return max(TICK, rounded)


@dataclass(frozen=True)
class Tranche:
    """One slice of the position with its own resting orders. A take-profit
    tranche has a tp_price (limit + OCA-paired stop); a runner has
    tp_price=None (stop only) and rides until stopped out."""

    tier_index: int
    qty: int
    tp_price: float | None

    @property
    def is_runner(self) -> bool:
        return self.tp_price is None


@dataclass(frozen=True)
class ExitPlan:
    tranches: tuple[Tranche, ...]
    initial_stop: float

    @property
    def runner_qty(self) -> int:
        return sum(t.qty for t in self.tranches if t.is_runner)

    @property
    def target_qty(self) -> int:
        return sum(t.qty for t in self.tranches if not t.is_runner)


def default_targets(entry_price: float, pcts: tuple[float, ...] = (0.25, 0.50, 0.75, 1.00)) -> tuple[TrimTarget, ...]:
    """Ladder computed off entry, used when a BUY message arrives without a
    visible "Trim Targets" field -- a position still gets bracketed."""
    return tuple(TrimTarget(pct=pct, price=round_to_tick(entry_price * (1 + pct))) for pct in pcts)


def _ladder_step(targets: tuple[TrimTarget, ...]) -> float:
    """Spacing between rungs, in percent-of-entry, used to extend the ladder
    past the last published target for a runner that keeps climbing."""
    if len(targets) >= 2:
        return targets[-1].pct - targets[-2].pct
    if targets:
        return targets[0].pct
    return 0.25


def rung_price(entry_price: float, targets: tuple[TrimTarget, ...], tier_index: int) -> float:
    """Price of rung `tier_index` (0-based) on the trim ladder, extending
    past the published targets at the ladder's own spacing. A 1-contract
    runner has no resting limit to fill, so its stop keeps ratcheting
    indefinitely rather than freezing at the last published tier."""
    if tier_index < 0:
        raise ValueError(f"tier_index must be non-negative, got {tier_index}")
    if tier_index < len(targets):
        return targets[tier_index].price
    step = _ladder_step(targets)
    last_pct = targets[-1].pct if targets else 0.0
    overshoot = tier_index - len(targets) + 1
    return round_to_tick(entry_price * (1 + last_pct + step * overshoot))


def ladder_stop(
    entry_price: float,
    targets: tuple[TrimTarget, ...],
    reached_tier_index: int,
    stop_loss_pct: float,
) -> float:
    """Where the stop belongs once rung `reached_tier_index` has been hit.

    Lags one rung behind on purpose: a stop placed *at* the tier that just
    filled sits on top of the market and gets taken out by a one-tick
    pullback. Lagging locks the last level the trade actually proved.

        -1 -> entry * (1 - stop_loss_pct)   nothing reached yet
         0 -> entry                         TP1 reached, breakeven
         n -> price of rung n-1             TP(n+1) reached
    """
    if reached_tier_index < -1:
        raise ValueError(f"reached_tier_index must be >= -1, got {reached_tier_index}")
    if reached_tier_index == -1:
        return round_to_tick(entry_price * (1 - stop_loss_pct))
    if reached_tier_index == 0:
        return round_to_tick(entry_price)
    return round_to_tick(rung_price(entry_price, targets, reached_tier_index - 1))


def allocate(qty: int, tier_count: int) -> list[int]:
    """Even split across tiers with the remainder handed to the earliest
    tiers, so 5 over 4 tiers is 2/1/1/1 and 3 over 4 is 1/1/1/0. Trailing
    zero-qty tiers are the caller's to drop."""
    if tier_count <= 0:
        raise ValueError(f"tier_count must be positive, got {tier_count}")
    base, remainder = divmod(qty, tier_count)
    return [base + (1 if i < remainder else 0) for i in range(tier_count)]


def build_exit_plan(
    entry_price: float,
    qty: int,
    targets: tuple[TrimTarget, ...],
    stop_loss_pct: float,
) -> ExitPlan:
    """Split `qty` into resting tranches.

    At 1 or 2 contracts there isn't enough size for a meaningful ladder, so
    the last contract is held back as a runner instead of being parked on a
    single limit: it keeps climbing the ladder with its stop and is only
    ever exited by that stop (or by a SOLD ALL message).

        qty 1   -> runner only
        qty 2   -> 1 at TP1, 1 runner
        qty >=3 -> even split across the published tiers, no runner
    """
    if qty <= 0:
        raise ValueError(f"qty must be positive, got {qty}")
    if entry_price <= 0:
        raise ValueError(f"entry_price must be positive, got {entry_price}")
    if not targets:
        targets = default_targets(entry_price)

    initial_stop = round_to_tick(entry_price * (1 - stop_loss_pct))

    if qty == 1:
        return ExitPlan(tranches=(Tranche(tier_index=0, qty=1, tp_price=None),), initial_stop=initial_stop)
    if qty == 2:
        return ExitPlan(
            tranches=(
                Tranche(tier_index=0, qty=1, tp_price=round_to_tick(targets[0].price)),
                Tranche(tier_index=1, qty=1, tp_price=None),
            ),
            initial_stop=initial_stop,
        )

    quantities = allocate(qty, len(targets))
    tranches = tuple(
        Tranche(tier_index=i, qty=n, tp_price=round_to_tick(targets[i].price))
        for i, n in enumerate(quantities)
        if n > 0
    )
    return ExitPlan(tranches=tranches, initial_stop=initial_stop)


def reached_tier_from_price(entry_price: float, targets: tuple[TrimTarget, ...], price: float) -> int:
    """Highest ladder rung `price` has reached, as a tier index (-1 if it
    hasn't reached the first rung). Used for runners, whose "a target was
    hit" signal is a market tick rather than one of our own fills."""
    reached = -1
    # Cap the extension search: at the ladder's own spacing, this walks
    # well past any realistic 0DTE move and terminates regardless.
    limit = len(targets) + 40
    for tier_index in range(limit):
        if price >= rung_price(entry_price, targets, tier_index) - TICK / 2:
            reached = tier_index
        else:
            break
    return reached


