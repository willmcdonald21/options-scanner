"""Rules engine tests: the six price paths from the spec, plus invariants.

No broker, no clock, no I/O. A path is a list of bids; `walk` feeds them
through `evaluate`, applies the resulting actions, and models each trim
filling immediately so the breakeven promotion is exercised too.
"""

from dataclasses import dataclass, field
from datetime import date

import pytest

from options_scanner.models import OptionKey, PositionState
from options_scanner.rules import (
    ArmTrail,
    Breach,
    ClearBreach,
    RulesConfig,
    SetStop,
    StopOut,
    Trim,
    UpdatePeak,
    apply_action,
    evaluate,
    level_price,
    next_level,
    on_trim_fill,
    round_price,
    trail_stop_price,
    trim_qty,
)

# The spec's SPX sample. Entry 0.475, so the rungs sit at:
#   +25% 0.59375   +50% 0.7125   +75% 0.83125   +100% 0.95
ENTRY = 0.475
QTY = 21

CFG = RulesConfig()


def _option() -> OptionKey:
    return OptionKey("SPX", date(2026, 10, 6), 7815.0, "P")


def _state(entry: float = ENTRY, qty: int = QTY) -> PositionState:
    return PositionState(
        option=_option(),
        entry_fill=entry,
        original_qty=qty,
        remaining_qty=qty,
        peak_bid=entry,
    )


@dataclass
class Walk:
    """What a price path produced, in order."""

    state: PositionState
    actions: list = field(default_factory=list)

    @property
    def trims(self) -> list[Trim]:
        return [a for a in self.actions if isinstance(a, Trim) and a.qty > 0]

    @property
    def stops(self) -> list[SetStop]:
        return [a for a in self.actions if isinstance(a, SetStop)]

    @property
    def stop_outs(self) -> list[StopOut]:
        return [a for a in self.actions if isinstance(a, StopOut)]

    @property
    def stop_prices(self) -> list[float]:
        return [s.price for s in self.stops]

    @property
    def sold(self) -> int:
        return sum(t.qty for t in self.trims) + sum(s.qty for s in self.stop_outs)

    @property
    def armed(self) -> bool:
        return any(isinstance(a, ArmTrail) for a in self.actions)


def walk(bids: list[float], state: PositionState | None = None, config: RulesConfig = CFG) -> Walk:
    """Feed a price path through the engine, filling every trim immediately.

    Immediate fills are the right model for a rules test: the point is which
    intents the rules produce, not how the broker behaves. Phase 4 covers
    partial and delayed fills.
    """
    state = state if state is not None else _state()
    result = Walk(state=state)
    for bid in bids:
        for action in evaluate(state, bid, config):
            result.actions.append(action)
            apply_action(state, action)
            # Only a trim that actually sells something produces a fill, and
            # the breakeven stop is driven by the fill. A rung consumed with
            # nothing to sell (the +100% rung, or a position already down to
            # its runner) never fills and so never promotes the stop.
            if isinstance(action, Trim) and action.qty > 0:
                for follow_up in on_trim_fill(state, action.level_pct, config):
                    result.actions.append(follow_up)
                    apply_action(state, follow_up)
    return result


# --- path 1: straight up through every level ------------------------------


def test_straight_up_fires_each_level_once_and_leaves_a_runner():
    result = walk([0.50, 0.60, 0.72, 0.84, 0.96, 1.20])

    assert [(t.level_pct, t.qty) for t in result.trims] == [(25, 6), (50, 4), (75, 3)]
    assert result.state.remaining_qty == 8
    assert result.state.fired_levels == {25, 50, 75, 100}
    assert not result.state.closed


def test_the_hundred_percent_rung_never_sells():
    """The user's rule: leave +100% running and let the trail carry it."""
    result = walk([0.50, 0.60, 0.72, 0.84, 0.96, 2.00, 5.00])

    assert 100 in result.state.fired_levels
    assert all(t.level_pct != 100 for t in result.trims)
    assert result.state.remaining_qty == 8  # unchanged by the +100% rung


def test_straight_up_sets_breakeven_then_ratchets_the_trail():
    result = walk([0.50, 0.60, 0.72, 0.84, 0.96])

    reasons = [(s.reason, s.price) for s in result.stops]
    assert reasons[0] == ("breakeven", 0.48)  # round_price(0.475)
    assert all(r == "trail" for r, _ in reasons[1:])
    assert result.state.stop_price == trail_stop_price(ENTRY, 0.96, 0.60)


def test_a_level_cannot_fire_twice_even_if_price_lingers():
    result = walk([0.60, 0.60, 0.61, 0.60, 0.62])

    assert [t.level_pct for t in result.trims] == [25]


# --- path 2: gap through several levels at once ---------------------------


def test_a_single_gap_through_three_levels_fires_all_three():
    """Spec: 'If price gaps through several levels at once, execute each
    crossed level's trim.'"""
    result = walk([0.90])

    assert [(t.level_pct, t.qty) for t in result.trims] == [(25, 6), (50, 4), (75, 3)]
    assert result.state.remaining_qty == 8


def test_a_gap_compounds_on_the_reduced_quantity():
    """Each crossed level sells a quarter of what is left after the earlier
    ones, not a quarter of the original three times over."""
    result = walk([0.90])

    # 21 -> ceil(21*.25)=6 -> 15 -> ceil(15*.25)=4 -> 11 -> ceil(11*.25)=3 -> 8
    assert [t.qty for t in result.trims] == [6, 4, 3]
    assert sum(t.qty for t in result.trims) == 13


def test_a_gap_straight_past_every_rung_also_arms_and_trails():
    result = walk([2.00])

    assert result.armed
    assert result.state.fired_levels == {25, 50, 75, 100}
    assert result.state.stop_price == trail_stop_price(ENTRY, 2.00, 0.60)
    assert result.state.stop_price == 1.09  # 0.475 + 0.40 * 1.525


def test_a_gap_trails_off_the_new_peak_in_the_same_quote():
    """The peak must be visible to the trail rule within the same evaluation,
    or a gap would trail off the previous high for one tick."""
    result = walk([3.00])

    assert result.state.peak_bid == 3.00
    assert result.state.stop_price == trail_stop_price(ENTRY, 3.00, 0.60)


# --- path 3: spike then crash ---------------------------------------------


def test_a_spike_then_crash_stops_out_at_the_trail():
    result = walk([2.00, 1.05, 1.00])

    stop = trail_stop_price(ENTRY, 2.00, 0.60)  # 1.09
    assert result.stop_outs
    assert result.stop_outs[0].stop_price == stop
    assert result.state.remaining_qty == 0
    assert result.state.closed


def test_a_crash_still_keeps_the_locked_in_gain():
    """Trimmed 13 of 21 on the way up, stopped the last 8 out above entry."""
    result = walk([2.00, 1.05, 1.00])

    assert sum(t.qty for t in result.trims) == 13
    assert result.stop_outs[0].qty == 8
    assert result.sold == 21  # every contract accounted for
    assert result.stop_outs[0].stop_price > ENTRY


def test_a_single_tick_through_the_stop_does_not_exit():
    """Confirmation window: 0DTE spreads print through a stop on noise."""
    result = walk([2.00, 1.00, 1.50])

    breaches = [a for a in result.actions if isinstance(a, Breach)]
    assert len(breaches) == 1
    assert breaches[0].count == 1
    assert result.stop_outs == []
    assert not result.state.closed


def test_a_recovered_breach_resets_the_counter():
    result = walk([2.00, 1.00, 1.50, 1.00, 1.50])

    assert any(isinstance(a, ClearBreach) for a in result.actions)
    assert result.stop_outs == []
    assert result.state.consecutive_breaches == 0


def test_two_consecutive_breaches_exit_even_without_a_lower_second_tick():
    result = walk([2.00, 1.09, 1.09])

    assert len(result.stop_outs) == 1
    assert result.state.closed


def test_nothing_happens_once_the_position_is_closed():
    result = walk([2.00, 1.00, 1.00, 1.50, 3.00])

    assert len(result.stop_outs) == 1
    # Later quotes are ignored entirely -- no resurrection, no new trims.
    assert result.state.remaining_qty == 0
    assert result.state.closed


# --- path 4: never reaches +25% ------------------------------------------


def test_a_position_that_never_reaches_the_first_level_is_never_protected():
    """By design, per the spec: no stop exists at entry, and nothing arms it
    until the first trim fills. This is the riskiest window in the system."""
    result = walk([0.47, 0.50, 0.55, 0.59, 0.30, 0.10, 0.01])

    assert result.trims == []
    assert result.stops == []
    assert result.stop_outs == []
    assert result.state.stop_price is None
    assert result.state.is_protected is False
    assert result.state.remaining_qty == QTY


def test_the_level_threshold_is_exact_not_rounded_down():
    """0.475 * 1.25 = 0.59375. A bid of 0.59 is +24.2%, not +25%."""
    assert walk([0.59]).trims == []
    assert [t.level_pct for t in walk([0.60]).trims] == [25]


# --- path 5: reaches +25%, then reverses to entry -------------------------


def test_reaching_the_first_level_then_falling_back_stops_out_at_breakeven():
    result = walk([0.60, 0.50, 0.47, 0.46])

    assert [t.level_pct for t in result.trims] == [25]
    assert result.stops[0] == SetStop(price=0.48, reason="breakeven")
    assert result.stop_outs
    assert result.stop_outs[0].stop_price == 0.48
    assert result.state.closed


def test_breakeven_means_the_trade_can_no_longer_lose():
    result = walk([0.60, 0.50, 0.47, 0.46])

    trimmed_qty = sum(t.qty for t in result.trims)
    trim_proceeds = trimmed_qty * 0.60
    stop_proceeds = result.stop_outs[0].qty * result.stop_outs[0].stop_price
    cost = QTY * ENTRY

    assert trim_proceeds + stop_proceeds > cost


def test_the_trail_never_arms_if_the_arming_level_is_not_reached():
    result = walk([0.60, 0.72, 0.80])

    assert not result.armed
    assert [s.reason for s in result.stops] == ["breakeven"]


# --- path 6: arms the trail, then drops ----------------------------------


def test_arming_the_trail_then_dropping_exits_above_breakeven():
    result = walk([0.84, 0.60, 0.55])

    assert result.armed
    expected = trail_stop_price(ENTRY, 0.84, 0.60)  # 0.475 + 0.40*0.365 = 0.62
    assert expected == 0.62
    assert result.stop_outs[0].stop_price == 0.62
    assert result.stop_outs[0].stop_price > round_price(ENTRY)


def test_a_pullback_that_stays_above_the_trail_does_not_exit():
    """0.70 and 0.65 are both above the 0.62 trail -- a drop is not a breach."""
    result = walk([0.84, 0.70, 0.65])

    assert result.stop_outs == []
    assert result.state.stop_price == 0.62
    assert result.state.remaining_qty == 8


def test_the_trail_is_ignored_while_it_sits_below_breakeven():
    """Between +25% and the arming level the breakeven stop is what protects
    the position; the trail is not armed yet and must not loosen anything."""
    result = walk([0.60])

    assert result.state.stop_price == 0.48
    assert not result.armed


def test_the_trail_only_raises_the_stop_never_lowers_it():
    result = walk([0.84, 2.00, 1.20, 1.15])

    assert result.stop_prices == sorted(result.stop_prices)
    high_water = max(result.stop_prices)
    assert result.state.stop_price == high_water


def test_a_pullback_does_not_move_the_stop_at_all():
    after_peak = walk([2.00])
    stop_at_peak = after_peak.state.stop_price

    result = walk([2.00, 1.80, 1.60, 1.40])

    assert result.state.stop_price == stop_at_peak
    assert result.stop_outs == []


# --- small positions ------------------------------------------------------


@pytest.mark.parametrize(
    "qty,expected_trims,expected_remaining",
    [
        (1, [], 1),  # nothing to trim without breaking the runner
        (2, [(25, 1)], 1),
        (3, [(25, 1), (50, 1)], 1),
        (4, [(25, 1), (50, 1), (75, 1)], 1),
        (8, [(25, 2), (50, 2), (75, 1)], 3),
    ],
)
def test_small_positions_still_ladder_and_keep_a_runner(qty, expected_trims, expected_remaining):
    result = walk([0.90], state=_state(qty=qty))

    assert [(t.level_pct, t.qty) for t in result.trims] == expected_trims
    assert result.state.remaining_qty == expected_remaining


def test_a_one_contract_position_is_never_protected_by_breakeven():
    """It cannot trim, so it never earns the breakeven stop -- it only exits
    on the trail, once armed. Worth knowing before sizing to one contract."""
    result = walk([0.60, 0.70], state=_state(qty=1))

    assert result.trims == []
    assert result.state.stop_price is None

    armed = walk([0.90], state=_state(qty=1))
    assert armed.armed
    assert armed.state.stop_price == trail_stop_price(ENTRY, 0.90, 0.60)


def test_a_runner_at_the_minimum_consumes_levels_without_selling():
    """A level reached while down to the runner must be marked fired, or it
    would be retried on every single quote forever."""
    result = walk([0.90, 1.00, 2.00], state=_state(qty=2))

    assert result.state.remaining_qty == 1
    assert result.state.fired_levels == {25, 50, 75, 100}


# --- invariants -----------------------------------------------------------


@pytest.mark.parametrize(
    "path",
    [
        [0.50, 0.60, 0.72, 0.84, 0.96, 1.20, 0.90, 0.60],
        [2.00, 1.00, 1.00],
        [0.47, 0.30, 0.10],
        [0.60, 0.50, 0.47, 0.46],
        [0.84, 2.00, 5.00, 2.00, 2.00],
        [0.90, 0.90, 0.90],
        [5.00, 0.01],
    ],
)
def test_contracts_sold_never_exceed_contracts_held(path):
    result = walk(path)

    assert result.sold <= QTY
    assert result.state.remaining_qty >= 0
    assert result.sold + result.state.remaining_qty == QTY


@pytest.mark.parametrize(
    "path",
    [
        [0.50, 0.60, 0.72, 0.84, 0.96, 1.20, 0.90, 0.60],
        [0.84, 2.00, 1.50, 3.00, 1.00],
        [0.60, 0.90, 0.70, 1.50, 0.80],
    ],
)
def test_the_stop_is_monotonic_over_any_path(path):
    result = walk(path)

    assert result.stop_prices == sorted(result.stop_prices)


def test_randomized_walks_never_break_the_invariants():
    """Fuzz the two properties that would cost real money if violated: a stop
    that moves down, and selling more than is held."""
    import random

    rng = random.Random(20261006)
    for _ in range(400):
        state = _state(qty=rng.choice([1, 2, 3, 5, 8, 21, 50]))
        held = state.original_qty
        bid = ENTRY
        path = []
        for _ in range(40):
            bid = max(0.01, round(bid * rng.uniform(0.75, 1.35), 2))
            path.append(bid)
        result = walk(path, state=state)

        assert result.stop_prices == sorted(result.stop_prices), path
        assert result.sold + result.state.remaining_qty == held, path
        assert result.state.remaining_qty >= 0, path
        if not result.state.closed:
            assert result.state.remaining_qty >= 1, path


def test_a_stop_moving_down_is_rejected_loudly():
    """apply_action is the backstop if a future rules change gets this wrong."""
    state = _state()
    apply_action(state, SetStop(price=0.80, reason="trail"))

    with pytest.raises(ValueError, match="must never decrease"):
        apply_action(state, SetStop(price=0.70, reason="trail"))


def test_over_selling_is_rejected_loudly():
    state = _state(qty=3)

    with pytest.raises(ValueError, match="exceeds the 3 contracts held"):
        apply_action(state, Trim(level_pct=25, qty=4, trigger_price=1.0))


# --- helpers and config --------------------------------------------------


def test_level_price_is_measured_off_the_actual_fill():
    assert level_price(0.475, 25) == pytest.approx(0.59375)
    assert level_price(0.500, 25) == pytest.approx(0.625)
    assert level_price(0.475, 100) == pytest.approx(0.95)


def test_trail_gives_back_the_configured_fraction_of_the_gain():
    # Entry 0.475, peak 0.95 (+100%): keep 40% of the 0.475 gain.
    assert trail_stop_price(0.475, 0.95, 0.60) == 0.67
    # A bigger peak locks in proportionally more.
    assert trail_stop_price(0.475, 2.85, 0.60) == 1.43


def test_the_trail_is_always_above_entry_once_there_is_any_gain():
    for peak in (0.48, 0.60, 0.95, 2.00, 10.0):
        assert trail_stop_price(ENTRY, peak, 0.60) >= ENTRY


@pytest.mark.parametrize(
    "remaining,fraction,expected",
    [
        (21, 0.25, 6),  # rounds up from 5.25
        (20, 0.25, 5),
        (4, 0.25, 1),
        (3, 0.25, 1),
        (2, 0.25, 1),  # rounds up from 0.5 so a small position still trims
        (1, 0.25, 0),  # the runner is untouchable
        (0, 0.25, 0),
        (21, 0.0, 0),  # the +100% rung
        (21, 1.0, 20),  # clamped to leave the runner
    ],
)
def test_trim_qty(remaining, fraction, expected):
    assert trim_qty(remaining, fraction, min_runner=1) == expected


def test_next_level_reports_the_upcoming_rung_for_status():
    state = _state()
    assert next_level(state) == (25, pytest.approx(0.59375))

    walk([0.90], state=state)  # clears 25/50/75, not the 0.95 rung
    assert next_level(state) == (100, pytest.approx(0.95))

    walk([1.00], state=state)
    assert next_level(state) is None  # ladder exhausted


def test_a_custom_schedule_is_honoured():
    config = RulesConfig(trim_schedule=((50, 0.5), (200, 0.0)), trail_arm_pct=200)
    result = walk([0.60, 0.72, 1.50], config=config)

    assert [(t.level_pct, t.qty) for t in result.trims] == [(50, 11)]
    assert result.armed


@pytest.mark.parametrize(
    "kwargs,message",
    [
        ({"trim_schedule": ()}, "must not be empty"),
        ({"trim_schedule": ((50, 0.25), (25, 0.25))}, "ascending"),
        ({"trim_schedule": ((25, 0.25), (25, 0.5))}, "duplicate levels"),
        ({"trim_schedule": ((25, 1.5),)}, r"in \[0, 1\]"),
        ({"trim_schedule": ((0, 0.25),)}, "positive percent"),
        ({"trail_giveback": 1.0}, r"in \[0, 1\)"),
        ({"confirm_breaches": 0}, "at least 1"),
        ({"min_runner_contracts": -1}, "cannot be negative"),
    ],
)
def test_a_nonsensical_config_is_rejected_at_construction(kwargs, message):
    with pytest.raises(ValueError, match=message):
        RulesConfig(**kwargs)


def test_a_zero_or_negative_bid_is_ignored():
    """A stale or malformed quote must not be read as a crash to zero."""
    state = _state()
    assert evaluate(state, 0.0) == []
    assert evaluate(state, -1.0) == []


def test_peak_only_emits_when_it_advances():
    result = walk([0.50, 0.49, 0.50, 0.55])

    peaks = [a for a in result.actions if isinstance(a, UpdatePeak)]
    assert [p.price for p in peaks] == [0.50, 0.55]


def test_on_trim_fill_does_not_lower_an_existing_trail_stop():
    """A gap can arm and set a trail above breakeven before the +25% trim
    fills; the breakeven promotion must not undo that."""
    state = _state()
    walk([2.00], state=state)
    high_stop = state.stop_price

    assert on_trim_fill(state, 25) == []
    assert state.stop_price == high_stop


def test_on_trim_fill_ignores_levels_below_the_breakeven_trigger():
    config = RulesConfig(breakeven_after_level_pct=50)
    state = _state()

    assert on_trim_fill(state, 25, config) == []
    assert on_trim_fill(state, 50, config) == [SetStop(price=0.48, reason="breakeven")]
