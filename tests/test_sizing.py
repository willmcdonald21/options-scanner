import pytest

from options_scanner.sizing import (
    compute_contracts,
    position_cost,
    sizing_note,
    tier_multiplier,
    unit_cap,
)


@pytest.mark.parametrize(
    "fill,cap,expected",
    [
        (0.475, 1000.0, 21),  # the spec's SPX sample: 1000 / 47.50
        (1.905, 1000.0, 5),
        (5.130, 1000.0, 1),
        (1.000, 1000.0, 10),  # exactly on the boundary, not 11
        (0.010, 1000.0, 1000),
    ],
)
def test_dollar_cap_sizing_rounds_down(fill, cap, expected):
    assert compute_contracts(fill, cap) == expected


def test_cost_of_a_sized_position_stays_within_the_cap():
    for fill in (0.05, 0.475, 1.905, 5.13, 9.99):
        contracts = compute_contracts(fill, 1000.0)
        assert position_cost(contracts, fill) <= 1000.0 or contracts == 1


def test_a_contract_too_expensive_for_the_cap_still_takes_one():
    """Skipping an alert silently would be worse than being over budget on
    the cheapest position available -- size the cap accordingly."""
    assert compute_contracts(50.0, 1000.0) == 1
    assert position_cost(1, 50.0) == 5000.0  # over the cap, deliberately


def test_the_hard_contract_cap_wins_over_the_dollar_maths():
    """This is what pins live trading to one contract in phase 6."""
    assert compute_contracts(0.475, 1000.0) == 21
    assert compute_contracts(0.475, 1000.0, max_contracts=1) == 1
    assert compute_contracts(0.475, 1000.0, max_contracts=5) == 5


def test_a_generous_contract_cap_does_not_inflate_the_size():
    assert compute_contracts(1.905, 1000.0, max_contracts=100) == 5


@pytest.mark.parametrize("bad_fill", [0.0, -1.0])
def test_a_non_positive_fill_price_is_rejected(bad_fill):
    with pytest.raises(ValueError, match="fill_price must be positive"):
        compute_contracts(bad_fill, 1000.0)


@pytest.mark.parametrize("bad_cap", [0.0, -100.0])
def test_a_non_positive_cap_is_rejected(bad_cap):
    with pytest.raises(ValueError, match="cap_usd must be positive"):
        compute_contracts(1.0, bad_cap)


def test_a_contract_cap_below_one_is_rejected():
    with pytest.raises(ValueError, match="max_contracts must be at least 1"):
        compute_contracts(1.0, 1000.0, max_contracts=0)


def test_position_cost_uses_the_hundred_multiplier():
    assert position_cost(21, 0.475) == pytest.approx(997.5)
    assert position_cost(1, 1.905) == pytest.approx(190.5)


# --- the unit: a percent of live equity ----------------------------------


def test_one_unit_is_the_configured_percent_of_equity():
    assert unit_cap(32_310.0, 3.0) == pytest.approx(969.30)
    assert unit_cap(100_000.0, 5.0) == pytest.approx(5_000.0)


@pytest.mark.parametrize(
    "tier,expected",
    [
        ("normal", 969.30),
        ("lotto", 484.65),        # half a unit
        ("super_lotto", 242.325),  # a quarter
    ],
)
def test_the_tiers_scale_the_unit_down(tier, expected):
    """The guide's 'lotto = half, super lotto = a quarter'."""
    assert unit_cap(32_310.0, 3.0, tier) == pytest.approx(expected)


def test_the_tier_multipliers_are_configurable():
    assert unit_cap(10_000.0, 10.0, "lotto", lotto_multiplier=0.8) == pytest.approx(800.0)
    assert unit_cap(
        10_000.0, 10.0, "super_lotto", super_lotto_multiplier=0.1
    ) == pytest.approx(100.0)


def test_the_absolute_ceiling_is_applied_after_the_percentage():
    """A percentage alone grows without bound as the account grows. The
    ceiling is what keeps the first live sessions bounded."""
    assert unit_cap(1_000_000.0, 3.0, ceiling_usd=1_000.0) == 1_000.0
    # and it does not raise a small account's size
    assert unit_cap(10_000.0, 3.0, ceiling_usd=1_000.0) == pytest.approx(300.0)


def test_the_ceiling_applies_to_the_tier_adjusted_size_not_the_full_unit():
    cap = unit_cap(1_000_000.0, 3.0, "lotto", ceiling_usd=1_000.0)

    assert cap == 1_000.0  # 15,000 -> ceiling, not 1,000 -> halved to 500


@pytest.mark.parametrize(
    "kwargs,message",
    [
        ({"equity": 0.0, "unit_pct": 3.0}, "equity must be positive"),
        ({"equity": -1.0, "unit_pct": 3.0}, "equity must be positive"),
        ({"equity": 100.0, "unit_pct": 0.0}, r"unit_pct must be in \(0, 100\]"),
        ({"equity": 100.0, "unit_pct": 101.0}, r"unit_pct must be in \(0, 100\]"),
        ({"equity": 100.0, "unit_pct": 3.0, "ceiling_usd": 0.0}, "ceiling_usd must be positive"),
    ],
)
def test_nonsensical_sizing_inputs_are_rejected(kwargs, message):
    with pytest.raises(ValueError, match=message):
        unit_cap(**kwargs)


def test_an_unknown_tier_is_rejected_rather_than_treated_as_normal():
    """A typo in a tier name must not silently size a lotto as a full unit."""
    with pytest.raises(ValueError, match="unknown sizing tier"):
        unit_cap(10_000.0, 3.0, "Lotto")


def test_the_tier_multipliers_match_the_guide():
    assert tier_multiplier("normal") == 1.0
    assert tier_multiplier("lotto") == 0.5
    assert tier_multiplier("super_lotto") == 0.25


# --- the audit line ------------------------------------------------------


def test_the_sizing_note_names_the_unit_the_tier_and_the_equity():
    note = sizing_note(equity=32_310.0, unit_pct=3.0, tier="lotto", cap_usd=484.65)

    assert "3% unit" in note
    assert "lotto" in note
    assert "$485" in note
    assert "$32,310" in note


def test_the_sizing_note_omits_the_tier_for_a_normal_unit():
    note = sizing_note(equity=32_310.0, unit_pct=3.0, tier="normal", cap_usd=969.30)

    assert "lotto" not in note
    assert "3% unit" in note


def test_the_sizing_note_flags_an_assumed_equity():
    """dry_run has no broker to ask. A size derived from a guessed balance that
    does not say so reads as a real sizing off a real account."""
    real = sizing_note(equity=32_310.0, unit_pct=3.0, tier="normal", cap_usd=969.30)
    assumed = sizing_note(
        equity=32_310.0, unit_pct=3.0, tier="normal", cap_usd=969.30, equity_is_assumed=True
    )

    assert "assumed equity" in assumed
    assert "assumed" not in real
