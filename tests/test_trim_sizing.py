import pytest

from bot.trim_sizing import compute_trim_sell_qty


def test_three_contract_user_mirrors_channels_three_equal_tiers():
    # Channel: 15 contracts, trimmed 5/5/5 across 3 tiers.
    assert compute_trim_sell_qty(3, sold_this_event=5, channel_total_before=15, channel_remaining_after=10) == 1
    assert compute_trim_sell_qty(2, sold_this_event=5, channel_total_before=10, channel_remaining_after=5) == 1
    # Final tier: channel goes flat -- sell whatever's left regardless of the floor.
    assert compute_trim_sell_qty(1, sold_this_event=5, channel_total_before=5, channel_remaining_after=0) == 1


def test_four_contract_user_never_leaves_a_leftover():
    assert compute_trim_sell_qty(4, sold_this_event=5, channel_total_before=15, channel_remaining_after=10) == 1
    assert compute_trim_sell_qty(3, sold_this_event=5, channel_total_before=10, channel_remaining_after=5) == 1
    # Final tier forces a full flatten: 2 left, not the floored 1.
    assert compute_trim_sell_qty(2, sold_this_event=5, channel_total_before=5, channel_remaining_after=0) == 2


def test_small_early_trim_can_floor_to_zero():
    assert compute_trim_sell_qty(1, sold_this_event=1, channel_total_before=50, channel_remaining_after=49) == 0


def test_channel_remaining_after_zero_always_flattens_even_with_zero_left():
    assert compute_trim_sell_qty(0, sold_this_event=5, channel_total_before=5, channel_remaining_after=0) == 0


def test_non_positive_channel_total_before_raises():
    with pytest.raises(ValueError):
        compute_trim_sell_qty(4, sold_this_event=1, channel_total_before=0, channel_remaining_after=0)
    with pytest.raises(ValueError):
        compute_trim_sell_qty(4, sold_this_event=1, channel_total_before=-5, channel_remaining_after=3)
