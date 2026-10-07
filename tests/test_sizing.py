import pytest

from options_scanner.sizing import compute_contracts, position_cost


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
