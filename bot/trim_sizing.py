from __future__ import annotations


def compute_trim_sell_qty(
    user_remaining_before: int,
    sold_this_event: int,
    channel_total_before: int,
    channel_remaining_after: int,
) -> int:
    """How many of the user's own remaining contracts to sell for one TRIM
    event, mirroring the channel's trim *rate* rather than its absolute
    count (the user typically holds far fewer contracts than the channel).

    floor(user_remaining_before * sold_this_event / channel_total_before),
    except when channel_remaining_after == 0 -- the channel is now fully
    flat on this position -- in which case the user's entire remaining
    position is sold instead of the floored proportional amount. Without
    that final-tier override, repeated flooring can strand a fractional
    remainder the channel considers fully closed (e.g. a 4-contract
    position trimmed in thirds floors to 1+1+1, leaving 1 contract behind
    with nothing left to trigger its sale).
    """
    if channel_total_before <= 0:
        raise ValueError(f"channel_total_before must be positive, got {channel_total_before}")
    if channel_remaining_after == 0:
        return user_remaining_before
    return (user_remaining_before * sold_this_event) // channel_total_before
