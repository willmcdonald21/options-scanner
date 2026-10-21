import pytest

from options_scanner.sizing import (
    affords_one_contract,
    compute_contracts,
    position_cost,
    sizing_note,
    sizing_equity,
    tier_multiplier,
    trade_budget,
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



# --- the small-account band ----------------------------------------------
#
# The live options account holds ~$1,100. 3% of that is $33, which buys
# nothing, so below $5,000 a fixed $300 budget is used instead.

SMALL = dict(
    unit_pct=3.0,
    small_account_below=5_000.0,
    small_account_budget_usd=300.0,
    ceiling_usd=300.0,
    max_sizing_equity=1_100.0,
)


def _size(equity: float, price: float, tier: str = "normal") -> int | None:
    """Contracts the pipeline would buy, or None for a skip."""
    budget = trade_budget(equity, tier, **SMALL)
    if not affords_one_contract(budget.cap_usd, price):
        return None
    return compute_contracts(price, budget.cap_usd, max_contracts=50)


@pytest.mark.parametrize(
    "price,expected,cost",
    [
        (0.475, 6, 285.0),
        (0.75, 4, 300.0),   # the dearest price where both trim rungs still fire
        (1.40, 2, 280.0),
        (2.95, 1, 295.0),
        (3.00, 1, 300.0),
    ],
)
def test_a_small_account_targets_two_to_three_hundred_per_trade(price, expected, cost):
    assert _size(1_100.0, price) == expected
    assert position_cost(expected, price) == pytest.approx(cost)


def test_the_small_account_band_ends_at_five_thousand():
    assert trade_budget(4_999.99, **{**SMALL, "max_sizing_equity": None}).basis == "small_account"
    assert trade_budget(5_000.00, **{**SMALL, "max_sizing_equity": None}).basis == "unit"


def test_at_or_above_the_band_the_percentage_unit_takes_over():
    budget = trade_budget(
        8_000.0, **{**SMALL, "max_sizing_equity": None, "ceiling_usd": 1_000.0}
    )

    assert budget.basis == "unit"
    assert budget.cap_usd == pytest.approx(240.0)  # 3% of $8,000


@pytest.mark.parametrize(
    "tier,price,expected",
    [
        ("lotto", 0.475, 3),       # $150 buys three
        ("lotto", 1.40, 1),        # $150 buys one
        ("lotto", 2.95, None),     # $150 cannot buy a $295 contract: skip
        ("super_lotto", 0.475, 1),  # $75 buys one
        ("super_lotto", 1.40, None),
        ("super_lotto", 2.95, None),
    ],
)
def test_a_tier_that_cannot_afford_one_contract_is_skipped(tier, price, expected):
    """Floored up to one contract, a super lotto's $75 budget would become a
    $295 position: four times the intended risk on the riskiest trade."""
    assert _size(1_100.0, price, tier) == expected


def test_the_paper_account_sizes_exactly_like_the_live_one():
    """One paper account per login, shared with warrior_bot, holding ~$32k.
    Capped at $1,100 it produces the live account's sizes and skips."""
    for price in (0.475, 0.75, 1.40, 2.95):
        for tier in ("normal", "lotto", "super_lotto"):
            assert _size(32_310.0, price, tier) == _size(1_100.0, price, tier), (price, tier)


def test_the_sizing_equity_cap_only_ever_lowers_equity():
    assert sizing_equity(32_310.0, 1_100.0) == 1_100.0
    assert sizing_equity(900.0, 1_100.0) == 900.0  # a smaller real balance wins
    assert sizing_equity(32_310.0, None) == 32_310.0


def test_the_budget_reports_both_the_real_and_the_sizing_equity():
    budget = trade_budget(32_310.0, **SMALL)

    assert budget.real_equity == 32_310.0
    assert budget.sizing_equity == 1_100.0


def test_the_ceiling_binds_the_small_account_budget_too():
    budget = trade_budget(1_100.0, **{**SMALL, "ceiling_usd": 200.0})

    assert budget.cap_usd == 200.0


def test_affordability_is_exact_at_the_boundary():
    assert affords_one_contract(300.0, 3.00)
    assert not affords_one_contract(299.99, 3.00)
    assert affords_one_contract(75.0, 0.75)


def test_the_sizing_note_names_the_small_account_basis_and_the_cap():
    budget = trade_budget(32_310.0, "lotto", **SMALL)
    note = sizing_note(
        equity=32_310.0, unit_pct=3.0, tier="lotto", cap_usd=budget.cap_usd,
        basis=budget.basis, small_account_budget_usd=300.0, sized_as=budget.sizing_equity,
    )

    assert "$300 small-account budget" in note
    assert "lotto" in note
    assert "$150 of $32,310 equity" in note
    assert "sized as $1,100" in note


def test_the_sizing_note_omits_sized_as_when_nothing_was_capped():
    note = sizing_note(
        equity=1_100.0, unit_pct=3.0, tier="normal", cap_usd=300.0,
        basis="small_account", small_account_budget_usd=300.0, sized_as=1_100.0,
    )

    assert "sized as" not in note


# --- a small-account budget that scales with the account -----------------

PCT = dict(
    unit_pct=3.0,
    small_account_below=5_000.0,
    small_account_budget_usd=300.0,
    small_account_budget_pct=27.2727,
    ceiling_usd=1_100.0,
)


def test_a_percentage_budget_is_exactly_300_on_the_live_account():
    """Rounded to the cent. Unrounded it is $299.997, which buys one fewer
    $0.75 contract than $300 does."""
    budget = trade_budget(1_100.0, **PCT)

    assert budget.cap_usd == 300.00
    assert compute_contracts(0.75, budget.cap_usd) == 4


def test_on_a_4000_account_each_call_buys_several_contracts():
    budget = trade_budget(4_000.0, **PCT)

    assert budget.cap_usd == pytest.approx(1_090.91)
    assert [compute_contracts(p, budget.cap_usd) for p in (3.00, 2.18, 1.00, 0.50)] == [3, 5, 10, 21]


def test_the_tiers_scale_a_percentage_budget_too():
    assert trade_budget(4_000.0, "lotto", **PCT).cap_usd == pytest.approx(545.455)
    assert trade_budget(4_000.0, "super_lotto", **PCT).cap_usd == pytest.approx(272.7275)


def test_the_fixed_dollar_budget_is_used_when_no_percentage_is_set():
    budget = trade_budget(4_000.0, **{**PCT, "small_account_budget_pct": None})

    assert budget.cap_usd == 300.0


def test_the_budget_reports_one_normal_trades_base():
    assert trade_budget(4_000.0, "super_lotto", **PCT).base_usd == pytest.approx(1_090.91)
