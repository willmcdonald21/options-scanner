"""The position-management rules engine. Pure.

This module decides *what should happen* to an open position and nothing
else: it takes a `PositionState` and a bid, and returns a list of `Action`
intents. It never touches a broker, a clock, a database or Discord, which is
what makes the six price paths in the spec testable as plain data.

Two entry points, because the spec distinguishes a price event from a fill
event:

* `evaluate(state, bid, config)` -- quote-driven. Peak, trims, trail arming,
  trail ratchet, stop breach.
* `on_trim_fill(state, level_pct, config)` -- fill-driven. The breakeven stop
  is set when the first trim *fills*, not when it is signalled, because until
  it fills the contracts are still held.

`apply_action` is the single mutating function here, kept alongside the rules
so a test can advance a position without the position manager, the broker or
any I/O.

The rules, in evaluation order:

1. **Peak** tracks the highest bid seen since entry.
2. **Trims** fire for every unfired level whose price the bid has reached,
   selling a fraction of what *remains* at that moment. A gap through several
   levels fires each one in order, compounding on the reduced quantity.
3. **Breakeven** (on the first trim's fill) sets the stop to the actual entry
   fill. Before that the position has no stop at all.
4. **Trail arms** when the bid reaches the arming level.
5. **Trail** sets the stop to `entry + (1 - giveback) * (peak - entry)`.
6. **Stop breach** requires the bid to sit at or below the stop for two
   consecutive quotes before exiting, so one bad tick on a wide 0DTE spread
   cannot flatten the position.

The stop only ever moves up. `apply_action` asserts it, so a rules bug that
tried to lower a stop would fail loudly rather than quietly give back profit.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Literal

from options_scanner.models import PositionState

# --- configuration ---------------------------------------------------------

# (level as whole percent, fraction of the *remaining* position to sell).
#
# A fraction of the remainder rather than of the original size is what lets
# one rule cover both a 21-contract position and a 2-contract one: it can
# never over-sell and it always leaves a runner.
#
# +100% is listed with a zero fraction on purpose. It is not a trim -- the
# runner is left to ride the trail from there -- but keeping the rung in the
# schedule documents that the decision was made rather than overlooked.
DEFAULT_TRIM_SCHEDULE: tuple[tuple[int, float], ...] = (
    (25, 0.25),
    (50, 0.25),
    (75, 0.25),
    (100, 0.0),
)


@dataclass(frozen=True)
class RulesConfig:
    trim_schedule: tuple[tuple[int, float], ...] = DEFAULT_TRIM_SCHEDULE

    # The level at which the trailing stop starts working.
    trail_arm_pct: int = 75

    # Fraction of the gain the trail is willing to hand back from the peak,
    # so the stop sits at entry + (1 - giveback) * (peak - entry). At 0.60
    # that locks in 40% of the best gain achieved.
    trail_giveback: float = 0.60

    # The trim whose fill promotes the stop to breakeven.
    breakeven_after_level_pct: int = 25

    # Consecutive quotes at or below the stop required to exit. 0DTE spreads
    # are wide enough that a single print through the stop is often noise.
    confirm_breaches: int = 2

    # Never trim the position below this. The runner is the whole point of
    # the ladder.
    min_runner_contracts: int = 1

    def __post_init__(self) -> None:
        if not self.trim_schedule:
            raise ValueError("trim_schedule must not be empty")
        levels = [level for level, _ in self.trim_schedule]
        if levels != sorted(levels):
            raise ValueError(f"trim_schedule must be in ascending level order, got {levels}")
        if len(set(levels)) != len(levels):
            raise ValueError(f"trim_schedule has duplicate levels: {levels}")
        for level, fraction in self.trim_schedule:
            if level <= 0:
                raise ValueError(f"trim level must be a positive percent, got {level}")
            if not 0.0 <= fraction <= 1.0:
                raise ValueError(f"trim fraction for +{level}% must be in [0, 1], got {fraction}")
        if not 0.0 <= self.trail_giveback < 1.0:
            raise ValueError(f"trail_giveback must be in [0, 1), got {self.trail_giveback}")
        if self.confirm_breaches < 1:
            raise ValueError(f"confirm_breaches must be at least 1, got {self.confirm_breaches}")
        if self.min_runner_contracts < 0:
            raise ValueError(f"min_runner_contracts cannot be negative, got {self.min_runner_contracts}")


# --- actions ---------------------------------------------------------------


@dataclass(frozen=True)
class UpdatePeak:
    """The bid set a new high. Emitted only when the peak actually advances."""

    price: float


@dataclass(frozen=True)
class Trim:
    """Sell `qty` because the bid reached +`level_pct`%. The caller may
    combine several of these into one order (spec section 4)."""

    level_pct: int
    qty: int
    trigger_price: float


@dataclass(frozen=True)
class SetStop:
    price: float
    reason: Literal["breakeven", "trail"]


@dataclass(frozen=True)
class ArmTrail:
    """The trail is live from here on."""

    at_price: float


@dataclass(frozen=True)
class Breach:
    """The bid is at or below the stop, but the confirmation window has not
    closed yet. Recorded rather than acted on."""

    count: int
    required: int
    bid: float


@dataclass(frozen=True)
class ClearBreach:
    """The bid recovered above the stop before the window closed."""


@dataclass(frozen=True)
class StopOut:
    """Confirmed. Sell everything remaining at a marketable limit."""

    qty: int
    stop_price: float
    bid: float


Action = UpdatePeak | Trim | SetStop | ArmTrail | Breach | ClearBreach | StopOut


# --- price helpers ---------------------------------------------------------


def round_price(price: float) -> float:
    """Round to a cent. Applied to stop prices, which become order prices and
    so have to sit on a tick. The 1e-9 nudge keeps a value like 0.665 from
    landing on whichever side of the tick binary float happens to put it."""
    return round(price + 1e-9, 2)


def level_price(entry_fill: float, level_pct: int) -> float:
    """The bid at which +`level_pct`% is reached, measured from the actual
    entry fill -- never from the advisor's quoted entry.

    Deliberately *not* rounded to a cent. Bids arrive in cents, so rounding
    0.475 * 1.25 = 0.59375 down to 0.59 would fire the +25% trim at a real
    gain of +24.2%. Comparing against the exact threshold means the level
    fires only once the gain has genuinely been reached (here, at 0.60).
    Callers format this for display; they must not round it for comparison.
    """
    return entry_fill * (1.0 + level_pct / 100.0)


def trail_stop_price(entry_fill: float, peak_bid: float, giveback: float) -> float:
    """Hand back `giveback` of the gain from the peak, keep the rest."""
    return round_price(entry_fill + (1.0 - giveback) * (peak_bid - entry_fill))


def trim_qty(remaining: int, fraction: float, min_runner: int) -> int:
    """How many contracts one trim sells.

    Rounded **up**, so a small position still de-risks: at 2 contracts a
    quarter is 0.5, and rounding down would mean a position that never trims
    and therefore never earns its breakeven stop. Clamped so at least
    `min_runner` contracts always survive -- that clamp, not the rounding, is
    what stops the ladder from closing the position.
    """
    if fraction <= 0.0 or remaining <= min_runner:
        return 0
    wanted = math.ceil(remaining * fraction)
    return max(0, min(wanted, remaining - min_runner))


# --- the rules -------------------------------------------------------------


def evaluate(state: PositionState, bid: float, config: RulesConfig | None = None) -> list[Action]:
    """Decide what one quote implies. Pure: returns intents, mutates nothing.

    Deliberately takes no clock. Time-based behaviour (the near-close warning,
    and the forced exit if it is ever enabled) belongs to the position
    manager, which owns the market calendar; keeping it out of here is what
    makes a price path reproducible as a plain list of bids.
    """
    config = config or RulesConfig()
    actions: list[Action] = []

    if state.closed or state.remaining_qty <= 0 or bid <= 0:
        return actions

    # 1. Peak. Read from a local, since later rules in this same call must
    #    see the updated peak -- a gap that sets a new high should trail off
    #    that high immediately, not one quote later.
    peak = state.peak_bid
    if bid > peak:
        peak = bid
        actions.append(UpdatePeak(price=bid))

    # 2. Trims. Each crossed level sells a fraction of what remains *after*
    #    the earlier levels in this same gap, so three levels crossing at
    #    once compounds rather than selling 25% of the original three times.
    remaining = state.remaining_qty
    for level_pct, fraction in config.trim_schedule:
        if level_pct in state.fired_levels:
            continue
        if bid < level_price(state.entry_fill, level_pct):
            continue
        qty = trim_qty(remaining, fraction, config.min_runner_contracts)
        if qty > 0:
            actions.append(Trim(level_pct=level_pct, qty=qty, trigger_price=bid))
            remaining -= qty
        else:
            # The level is still consumed. A zero-fraction rung (+100%) and a
            # position already down to its runner both mean "nothing to sell
            # here", not "try again on the next tick".
            actions.append(Trim(level_pct=level_pct, qty=0, trigger_price=bid))

    # 3. Breakeven is not here -- it is driven by the trim's fill, in
    #    on_trim_fill(). Until those contracts actually sell, we still hold
    #    them, and a stop premised on a fill that never happened would be a
    #    lie about how protected the position is.

    # 4. Arm the trail.
    armed = state.trail_armed
    if not armed and bid >= level_price(state.entry_fill, config.trail_arm_pct):
        armed = True
        actions.append(ArmTrail(at_price=bid))

    # 5. Ratchet the trail. Only ever upward, and only above whatever the
    #    breakeven stop already guarantees.
    stop = state.stop_price
    if armed:
        candidate = trail_stop_price(state.entry_fill, peak, config.trail_giveback)
        if stop is None or candidate > stop:
            stop = candidate
            actions.append(SetStop(price=candidate, reason="trail"))

    # 6. Stop breach, with confirmation. Checked last: a bid cannot be both
    #    at a trim level and under the stop, because the stop never exceeds
    #    breakeven until the trail arms, and the trail only arms once every
    #    lower level has already fired.
    if stop is not None and bid <= stop:
        count = state.consecutive_breaches + 1
        if count >= config.confirm_breaches:
            actions.append(StopOut(qty=state.remaining_qty, stop_price=stop, bid=bid))
        else:
            actions.append(Breach(count=count, required=config.confirm_breaches, bid=bid))
    elif state.consecutive_breaches:
        actions.append(ClearBreach())

    return actions


def on_trim_fill(
    state: PositionState,
    level_pct: int,
    config: RulesConfig | None = None,
) -> list[Action]:
    """Decide what a filled trim implies. Pure.

    Only one thing today: the first trim filling promotes the stop to the
    actual entry fill, which is the moment the trade stops being able to lose
    money. `>=` rather than `==` so a gap that fills the +25% and +50% trims
    in one combined order still promotes, and so does a position whose first
    trim was a later rung.
    """
    config = config or RulesConfig()
    if state.closed:
        return []
    if level_pct < config.breakeven_after_level_pct:
        return []
    breakeven = round_price(state.entry_fill)
    if state.stop_price is not None and state.stop_price >= breakeven:
        return []  # the trail already guarantees more than breakeven
    return [SetStop(price=breakeven, reason="breakeven")]


# --- state advancement -----------------------------------------------------


def apply_action(state: PositionState, action: Action) -> None:
    """Fold one action into `state`. The only mutating function in this
    module, used by the position manager and by the price-path tests.

    Note that `Trim` here reduces the remaining quantity, which models a
    filled trim. The live position manager applies it when the fill arrives,
    not when the intent is produced.
    """
    if isinstance(action, UpdatePeak):
        state.peak_bid = max(state.peak_bid, action.price)

    elif isinstance(action, Trim):
        state.fired_levels = state.fired_levels | {action.level_pct}
        if action.qty:
            if action.qty > state.remaining_qty:
                raise ValueError(
                    f"trim of {action.qty} exceeds the {state.remaining_qty} contracts held"
                )
            state.remaining_qty -= action.qty

    elif isinstance(action, SetStop):
        if state.stop_price is not None and action.price < state.stop_price:
            raise ValueError(
                f"stop would move down from {state.stop_price} to {action.price}; "
                "the effective stop must never decrease"
            )
        state.stop_price = action.price
        state.stop_reason = action.reason

    elif isinstance(action, ArmTrail):
        state.trail_armed = True

    elif isinstance(action, Breach):
        state.consecutive_breaches = action.count

    elif isinstance(action, ClearBreach):
        state.consecutive_breaches = 0

    elif isinstance(action, StopOut):
        if action.qty > state.remaining_qty:
            raise ValueError(
                f"stop-out of {action.qty} exceeds the {state.remaining_qty} contracts held"
            )
        state.remaining_qty -= action.qty
        state.consecutive_breaches = 0
        if state.remaining_qty <= 0:
            state.closed = True

    else:  # pragma: no cover - exhaustive over Action
        raise TypeError(f"unknown action {action!r}")


def next_level(state: PositionState, config: RulesConfig | None = None) -> tuple[int, float] | None:
    """The next unfired rung and its price, for `!status`. None once the
    ladder is exhausted."""
    config = config or RulesConfig()
    for level_pct, _ in config.trim_schedule:
        if level_pct not in state.fired_levels:
            return level_pct, level_price(state.entry_fill, level_pct)
    return None


__all__ = [
    "DEFAULT_TRIM_SCHEDULE",
    "Action",
    "ArmTrail",
    "Breach",
    "ClearBreach",
    "RulesConfig",
    "SetStop",
    "StopOut",
    "Trim",
    "UpdatePeak",
    "apply_action",
    "evaluate",
    "level_price",
    "next_level",
    "on_trim_fill",
    "round_price",
    "trail_stop_price",
    "trim_qty",
]
