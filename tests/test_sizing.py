import pytest

from bot.sizing import compute_contracts


def test_rounds_down_to_whole_contracts():
    assert compute_contracts(entry_price=1.215, cap_usd=1000) == 8  # 1000 / 121.5 = 8.23


def test_minimum_one_contract_even_if_over_cap():
    assert compute_contracts(entry_price=50.0, cap_usd=1000) == 1  # one contract already costs $5000


def test_expensive_underlying_falls_back_to_minimum_one_contract():
    assert compute_contracts(entry_price=11.15, cap_usd=1000) == 1  # floor(1000 / 1115) == 0, clamped to 1


def test_rejects_non_positive_price():
    with pytest.raises(ValueError):
        compute_contracts(entry_price=0, cap_usd=1000)
