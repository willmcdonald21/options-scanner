import pytest

from bot.exit_plan import (
    allocate,
    build_exit_plan,
    default_targets,
    ladder_stop,
    reached_tier_from_price,
    round_to_tick,
    rung_price,
)
from bot.models import TrimTarget

# The SPY 764C card: entry $1.905, ladder 2.381 / 2.858 / 3.334 / 3.810.
ENTRY = 1.905
TARGETS = (
    TrimTarget(0.25, 2.381),
    TrimTarget(0.50, 2.858),
    TrimTarget(0.75, 3.334),
    TrimTarget(1.00, 3.810),
)
STOP_PCT = 0.30


# --- tick rounding --------------------------------------------------------


def test_three_decimal_channel_prices_round_to_a_submittable_tick():
    # IBKR rejects $2.381 outright, so nothing reaches an order unrounded.
    assert round_to_tick(2.381) == 2.38
    assert round_to_tick(2.858) == 2.86
    assert round_to_tick(3.334) == 3.33


def test_rounding_never_produces_a_zero_price():
    assert round_to_tick(0.0001) == 0.01
    assert round_to_tick(0.0) == 0.01


# --- allocation -----------------------------------------------------------


@pytest.mark.parametrize(
    "qty,expected",
    [
        (4, [1, 1, 1, 1]),
        (5, [2, 1, 1, 1]),
        (6, [2, 2, 1, 1]),
        (9, [3, 2, 2, 2]),
        (3, [1, 1, 1, 0]),
    ],
)
def test_allocate_splits_evenly_with_remainder_to_earliest_tiers(qty, expected):
    assert allocate(qty, 4) == expected
    assert sum(allocate(qty, 4)) == qty


def test_allocate_rejects_zero_tiers():
    with pytest.raises(ValueError):
        allocate(5, 0)


# --- plan shape -----------------------------------------------------------


def test_single_contract_is_all_runner_with_no_resting_limit():
    plan = build_exit_plan(ENTRY, 1, TARGETS, STOP_PCT)

    assert plan.runner_qty == 1
    assert plan.target_qty == 0
    assert [t.tp_price for t in plan.tranches] == [None]
    assert plan.initial_stop == round_to_tick(ENTRY * 0.70)


def test_two_contracts_sell_one_at_the_first_target_and_run_the_other():
    plan = build_exit_plan(ENTRY, 2, TARGETS, STOP_PCT)

    assert [(t.qty, t.tp_price) for t in plan.tranches] == [(1, 2.38), (1, None)]
    assert plan.runner_qty == 1


def test_five_contracts_ladder_across_all_four_targets_with_no_runner():
    plan = build_exit_plan(ENTRY, 5, TARGETS, STOP_PCT)

    assert [(t.tier_index, t.qty, t.tp_price) for t in plan.tranches] == [
        (0, 2, 2.38),
        (1, 1, 2.86),
        (2, 1, 3.33),
        (3, 1, 3.81),
    ]
    assert plan.runner_qty == 0
    assert plan.target_qty == 5


def test_three_contracts_drop_the_empty_fourth_tier():
    plan = build_exit_plan(ENTRY, 3, TARGETS, STOP_PCT)

    assert [t.tier_index for t in plan.tranches] == [0, 1, 2]
    assert all(t.qty == 1 for t in plan.tranches)


def test_every_contract_is_accounted_for():
    for qty in range(1, 30):
        plan = build_exit_plan(ENTRY, qty, TARGETS, STOP_PCT)
        assert sum(t.qty for t in plan.tranches) == qty


def test_missing_ladder_falls_back_to_one_computed_off_entry():
    plan = build_exit_plan(2.00, 4, (), STOP_PCT)

    assert [t.tp_price for t in plan.tranches] == [2.50, 3.00, 3.50, 4.00]


@pytest.mark.parametrize("qty", [0, -1])
def test_non_positive_quantity_is_rejected(qty):
    with pytest.raises(ValueError):
        build_exit_plan(ENTRY, qty, TARGETS, STOP_PCT)


# --- the stop ladder ------------------------------------------------------


def test_stop_starts_below_entry_then_lags_one_rung_behind():
    assert ladder_stop(ENTRY, TARGETS, -1, STOP_PCT) == round_to_tick(ENTRY * 0.70)
    assert ladder_stop(ENTRY, TARGETS, 0, STOP_PCT) == round_to_tick(ENTRY)  # breakeven
    assert ladder_stop(ENTRY, TARGETS, 1, STOP_PCT) == 2.38  # +25% locked
    assert ladder_stop(ENTRY, TARGETS, 2, STOP_PCT) == 2.86  # +50% locked
    assert ladder_stop(ENTRY, TARGETS, 3, STOP_PCT) == 3.33  # +75% locked


def test_the_stop_never_sits_on_the_rung_that_just_filled():
    """A stop placed at the target it just filled is marketable on a
    one-tick pullback -- lagging a rung is the whole point."""
    for reached in range(len(TARGETS)):
        assert ladder_stop(ENTRY, TARGETS, reached, STOP_PCT) < TARGETS[reached].price


def test_the_ladder_only_ever_moves_up():
    stops = [ladder_stop(ENTRY, TARGETS, n, STOP_PCT) for n in range(-1, 6)]
    assert stops == sorted(stops)


def test_ladder_extends_past_the_last_published_target_at_its_own_spacing():
    # +125%, +150% -- a runner has no limit above it, so the rungs keep going.
    assert rung_price(ENTRY, TARGETS, 4) == round_to_tick(ENTRY * 2.25)
    assert rung_price(ENTRY, TARGETS, 5) == round_to_tick(ENTRY * 2.50)
    assert ladder_stop(ENTRY, TARGETS, 5, STOP_PCT) == round_to_tick(ENTRY * 2.25)


def test_negative_tier_index_is_rejected():
    with pytest.raises(ValueError):
        ladder_stop(ENTRY, TARGETS, -2, STOP_PCT)


# --- price -> rung (how a runner ratchets) --------------------------------


@pytest.mark.parametrize(
    "price,expected_tier",
    [
        (1.90, -1),  # below the first rung
        (2.37, -1),
        (2.381, 0),  # exactly on it
        (2.50, 0),
        (2.86, 1),
        (3.40, 2),
        (3.81, 3),
        (4.29, 4),  # into the extended ladder
    ],
)
def test_reached_tier_from_price(price, expected_tier):
    assert reached_tier_from_price(ENTRY, TARGETS, price) == expected_tier


def test_default_targets_honour_the_channels_percentages():
    targets = default_targets(2.00, tuple(t.pct for t in TARGETS))

    assert [t.pct for t in targets] == [0.25, 0.50, 0.75, 1.00]
    assert [t.price for t in targets] == [2.50, 3.00, 3.50, 4.00]
