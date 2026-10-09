"""The position-management rules engine. Pure.

This module decides *what should happen* to an open position and nothing
else: it takes a `PositionState` and a bid, and returns a list of `Action`
intents. It never touches a broker, a clock, a database or Discord, which is
what makes the six price paths in the spec testable as plain data.

Two entry points, because the spec distinguishes a price event from a fill
event:

* `evaluate(state, bid, config)` -- quote-driven. Peak, trims, runner
  milestones, trail arming, trail ratchet, stop breach.
* `on_trim_fill(state, level_pct, config)` -- fill-driven. The breakeven stop
  is set when the first trim *fills*, not when it is signalled, because until
  it fills the contracts are still held.

`apply_action` is the single mutating function here, kept alongside the rules
so a test can advance a position without the position manager, the broker or
any I/O.

The rules mirror the advisor's published playstyle: sell half at +25%, another
quarter at +50%, move the stop to breakeven once trimmed, let the runner climb
75 -> 100 -> 150 -> 200 -> 500 -> 1000 -> 2000, and trail 60% below the peak
from +75% on.

In evaluation order:

1. **Peak** tracks the highest bid seen since entry.
2. **Trims** fire for every unfired rung the bid has reached, selling a
   fraction of the **original** position: 50% at +25%, 25% at +50%. A gap
   through both rungs fires each in order and still leaves the 25% runner.
3. **Runner levels** above the ladder sell nothing. They fire once each, so
   crossing one is a reportable milestone rather than a silent no-op.
4. **Breakeven** (on the first trim's fill) sets the stop to the actual entry
   fill. Before that the position has no stop at all.
5. **Trail arms** when the bid reaches the arming level.
6. **Trail** sets the stop to `peak * multiplier` -- 0.40 by default, i.e.
   60% below the peak, tightening to 0.55 above +200% and 0.70 above +500%.
   It is floored at the entry fill, so arming the trail can never install a
   stop that would book a loss.
7. **Stop breach** requires the bid to sit at or below the stop for two
   consecutive quotes before exiting, so one bad tick on a wide 0DTE spread
   cannot flatten the position.

Worth knowing about rule 6: `peak * 0.40` sits *below* breakeven until the
peak reaches 2.5x entry (+150%). Between +75% and +150% the floor is doing all
the work and the trail is inert. That is what the guide describes, and it is
why the multiplier tightens at the high levels.

The stop only ever moves up. `apply_action` asserts it, so a rules bug that
tried to lower a stop would fail loudly rather than quietly give back profit.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Literal

from options_scanner.models import PositionState

# --- configuration ---------------------------------------------------------

# (level as whole percent, fraction of the *original* position to sell).
#
# The fractions are of the original size, not of the remainder, because that
# is what "sell half at +25%, another quarter at +50%" means: half, then a
# quarter, leaving a quarter to run. Measuring off the remainder would sell
# half then an eighth and leave three eighths.
#
# `trim_qty` still clamps every rung so the runner survives, which is what
# keeps the rule safe on a 2- or 3-contract position.
DEFAULT_TRIM_SCHEDULE: tuple[tuple[int, float], ...] = (
    (25, 0.50),
    (50, 0.25),
)

# Levels the runner climbs through after the ladder is done. Nothing is sold
# here -- each one is a milestone worth reporting, and crossing one can
# tighten the trail via DEFAULT_TRAIL_SCHEDULE.
DEFAULT_RUNNER_LEVELS: tuple[int, ...] = (75, 100, 150, 200, 500, 1000, 2000)

# (peak gain in whole percent at or above which it applies, multiplier of the
# peak). 0.40 is the guide's default: trail 60% below the peak. It tightens as
# the gain gets large, because handing back 60% of a +500% runner is a lot of
# money to give to noise.
DEFAULT_TRAIL_SCHEDULE: tuple[tuple[int, float], ...] = (
    (0, 0.40),
    (200, 0.55),
    (500, 0.70),
)


@dataclass(frozen=True)
class RulesConfig:
    trim_schedule: tuple[tuple[int, float], ...] = DEFAULT_TRIM_SCHEDULE

    # Levels above the ladder that sell nothing but are still reported once.
    runner_levels: tuple[int, ...] = DEFAULT_RUNNER_LEVELS

    # The level at which the trailing stop starts working.
    trail_arm_pct: int = 75

    # How far below the peak the trail sits, as a multiplier of the peak,
    # banded by how far the position has run. See DEFAULT_TRAIL_SCHEDULE.
    trail_schedule: tuple[tuple[int, float], ...] = DEFAULT_TRAIL_SCHEDULE

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

        runners = list(self.runner_levels)
        if runners != sorted(runners):
            raise ValueError(f"runner_levels must be in ascending order, got {runners}")
        if len(set(runners)) != len(runners):
            raise ValueError(f"runner_levels has duplicates: {runners}")
        # A level that is both a trim rung and a runner level would fire twice
        # in one ladder and the second one would find it already consumed.
        clash = sorted(set(runners) & set(levels))
        if clash:
            raise ValueError(f"levels appear in both trim_schedule and runner_levels: {clash}")
        for level in runners:
            if level <= 0:
                raise ValueError(f"runner level must be a positive percent, got {level}")

        if not self.trail_schedule:
            raise ValueError("trail_schedule must not be empty")
        thresholds = [t for t, _ in self.trail_schedule]
        if thresholds[0] != 0:
            raise ValueError(
                f"trail_schedule must start at a 0% threshold so every peak is covered, "
                f"got {thresholds[0]}"
            )
        if thresholds != sorted(thresholds):
            raise ValueError(f"trail_schedule must be in ascending threshold order, got {thresholds}")
        if len(set(thresholds)) != len(thresholds):
            raise ValueError(f"trail_schedule has duplicate thresholds: {thresholds}")
        multipliers = [m for _, m in self.trail_schedule]
        for threshold, multiplier in self.trail_schedule:
            if not 0.0 < multiplier < 1.0:
                # 1.0 would park the stop on the peak and exit on the first
                # tick down; 0.0 or less is not a stop at all.
                raise ValueError(
                    f"trail multiplier at +{threshold}% must be in (0, 1), got {multiplier}"
                )
        if multipliers != sorted(multipliers):
            raise ValueError(
                f"trail_schedule multipliers must not loosen as the gain grows, got {multipliers}"
            )

        if self.confirm_breaches < 1:
            raise ValueError(f"confirm_breaches must be at least 1, got {self.confirm_breaches}")
        if self.min_runner_contracts < 0:
            raise ValueError(f"min_runner_contracts cannot be negative, got {self.min_runner_contracts}")

    @property
    def ladder(self) -> tuple[tuple[int, float | None], ...]:
        """Every level the bid can cross, in ascending order.

        A trim rung carries its fraction; a runner level carries None, which
        is how `evaluate` tells "sell this fraction" from "report this and
        sell nothing".
        """
        rungs: list[tuple[int, float | None]] = [(level, fraction) for level, fraction in self.trim_schedule]
        rungs.extend((level, None) for level in self.runner_levels)
        return tuple(sorted(rungs, key=lambda rung: rung[0]))

    @property
    def all_levels(self) -> tuple[int, ...]:
        return tuple(level for level, _ in self.ladder)


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
class RunnerLevel:
    """The runner crossed +`level_pct`%. Nothing is sold.

    Distinct from `Trim(qty=0)` so it can be reported: these are the levels
    the guide has the runner climbing through, and a milestone nobody can see
    is indistinguishable from the bot having stalled.
    """

    level_pct: int
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


Action = UpdatePeak | Trim | RunnerLevel | SetStop | ArmTrail | Breach | ClearBreach | StopOut


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


def trail_multiplier(
    entry_fill: float,
    peak_bid: float,
    schedule: tuple[tuple[int, float], ...] = DEFAULT_TRAIL_SCHEDULE,
) -> float:
    """The fraction of the peak the stop sits at, for the band the peak gain
    falls in. The highest threshold at or below the gain wins."""
    gain_pct = (peak_bid / entry_fill - 1.0) * 100.0
    multiplier = schedule[0][1]
    for threshold, candidate in schedule:
        if gain_pct >= threshold:
            multiplier = candidate
        else:
            break
    return multiplier


def trail_stop_price(
    entry_fill: float,
    peak_bid: float,
    schedule: tuple[tuple[int, float], ...] = DEFAULT_TRAIL_SCHEDULE,
) -> float:
    """Trail below the peak: `peak * multiplier`, floored at the entry fill.

    The floor matters. At the default 0.40 the raw trail sits below entry
    until the peak reaches 2.5x entry, so without it, arming the trail at
    +75% would install a stop that books a loss on a position that is up 75%.
    Flooring it means the stop is `max(breakeven, 60% below the peak)` -- the
    trail takes over only once it is genuinely worth more than breakeven.
    """
    multiplier = trail_multiplier(entry_fill, peak_bid, schedule)
    return round_price(max(peak_bid * multiplier, entry_fill))


def trim_qty(original_qty: int, remaining: int, fraction: float, min_runner: int) -> int:
    """How many contracts one trim sells.

    The fraction is of `original_qty` -- the size that actually filled -- so
    the two rungs sell half and a quarter of the position and leave a quarter
    running, rather than compounding down the remainder.

    Rounded **up**, so a small position still de-risks: at 2 contracts a
    quarter is 0.5, and rounding down would mean a position that never trims
    and therefore never earns its breakeven stop. Clamped against `remaining`
    so at least `min_runner` contracts always survive -- that clamp, not the
    rounding, is what stops the ladder from closing the position.
    """
    if fraction <= 0.0 or remaining <= min_runner:
        return 0
    wanted = math.ceil(original_qty * fraction)
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

    # 2/3. The ladder. Trim rungs sell a fraction of the original size;
    #      runner levels above them sell nothing and are reported instead.
    #      A gap through several levels fires each one in ascending order.
    remaining = state.remaining_qty
    for level_pct, fraction in config.ladder:
        if level_pct in state.fired_levels:
            continue
        if bid < level_price(state.entry_fill, level_pct):
            continue
        if fraction is None:
            actions.append(RunnerLevel(level_pct=level_pct, trigger_price=bid))
            continue
        qty = trim_qty(state.original_qty, remaining, fraction, config.min_runner_contracts)
        if qty > 0:
            actions.append(Trim(level_pct=level_pct, qty=qty, trigger_price=bid))
            remaining -= qty
        else:
            # The level is still consumed. A position already down to its
            # runner means "nothing to sell here", not "try again on the
            # next tick".
            actions.append(Trim(level_pct=level_pct, qty=0, trigger_price=bid))

    # 4. Breakeven is not here -- it is driven by the trim's fill, in
    #    on_trim_fill(). Until those contracts actually sell, we still hold
    #    them, and a stop premised on a fill that never happened would be a
    #    lie about how protected the position is.

    # 5. Arm the trail.
    armed = state.trail_armed
    if not armed and bid >= level_price(state.entry_fill, config.trail_arm_pct):
        armed = True
        actions.append(ArmTrail(at_price=bid))

    # 6. Ratchet the trail. Only ever upward. The candidate is floored at the
    #    entry fill inside trail_stop_price, so this cannot install a losing
    #    stop on a winning position.
    stop = state.stop_price
    if armed:
        candidate = trail_stop_price(state.entry_fill, peak, config.trail_schedule)
        if stop is None or candidate > stop:
            stop = candidate
            actions.append(SetStop(price=candidate, reason="trail"))

    # 7. Stop breach, with confirmation. Checked last: a bid cannot be both
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

    elif isinstance(action, RunnerLevel):
        # Consumed exactly like a trim rung, so the milestone is reported once
        # rather than on every quote above it.
        state.fired_levels = state.fired_levels | {action.level_pct}

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
    """The next unfired level and its price, for `!status`. None once the
    whole ladder -- trims and runner levels alike -- is exhausted."""
    config = config or RulesConfig()
    for level_pct in config.all_levels:
        if level_pct not in state.fired_levels:
            return level_pct, level_price(state.entry_fill, level_pct)
    return None


__all__ = [
    "DEFAULT_RUNNER_LEVELS",
    "DEFAULT_TRAIL_SCHEDULE",
    "DEFAULT_TRIM_SCHEDULE",
    "Action",
    "ArmTrail",
    "Breach",
    "ClearBreach",
    "RulesConfig",
    "RunnerLevel",
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
    "trail_multiplier",
    "trail_stop_price",
    "trim_qty",
]
