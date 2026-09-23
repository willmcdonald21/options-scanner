from datetime import date

import pytest

from bot.models import OpenPosition, OptionKey, UnderlyingKey
from bot.position_store import PositionStore


@pytest.fixture
def store(tmp_path):
    s = PositionStore(tmp_path / "positions.sqlite3")
    yield s
    s.close()


def _qqq_740c() -> OptionKey:
    return OptionKey("QQQ", date(2026, 9, 23), 740.0, "C")


def test_idempotency_tracks_processed_messages(store):
    assert store.already_processed(123) is False
    store.mark_processed(123)
    assert store.already_processed(123) is True


def test_create_and_get_open_by_full_key(store):
    option = _qqq_740c()
    store.create_open(
        OpenPosition(
            option=option,
            channel_total_qty=10,
            channel_remaining_qty=10,
            user_original_qty=8,
            user_remaining_qty=8,
            entry_price=1.215,
            ibkr_order_id_entry=1,
        )
    )
    found = store.get_open(option)
    assert found is not None
    assert found.user_original_qty == 8
    assert found.status == "OPEN"


def test_get_open_by_underlying_ignores_expiry(store):
    option = _qqq_740c()
    store.create_open(
        OpenPosition(option, 10, 10, 8, 8, 1.215, 1)
    )
    found = store.get_open_by_underlying(UnderlyingKey("QQQ", 740.0, "C"))
    assert found is not None
    assert found.option == option


def test_apply_trim_computes_proportional_mirror_and_closes_when_fully_sold(store):
    option = _qqq_740c()
    store.create_open(OpenPosition(option, 20, 20, 8, 8, 0.885, 1))

    # channel sold 12 of 20 (60%) -- caller (executor) computes the user's
    # own trim size the same way and passes the resulting remaining counts.
    store.apply_trim(option, channel_remaining_qty=8, user_remaining_qty=3)
    found = store.get_open(option)
    assert found.channel_remaining_qty == 8
    assert found.user_remaining_qty == 3
    assert found.status == "OPEN"

    store.apply_trim(option, channel_remaining_qty=0, user_remaining_qty=0)
    found = store.get_open(option)
    assert found is None  # no longer OPEN


def test_close_position_zeroes_out_and_closes(store):
    option = _qqq_740c()
    store.create_open(OpenPosition(option, 10, 10, 8, 8, 1.215, 1))
    store.close_position(option)
    assert store.get_open(option) is None


def test_apply_averaging_down_updates_totals_and_entry_price(store):
    option = _qqq_740c()
    store.create_open(OpenPosition(option, 10, 10, 8, 8, 1.215, 1))
    store.apply_averaging_down(option, new_channel_total_qty=20, new_entry_price=0.885, new_user_qty=16)
    found = store.get_open(option)
    assert found.channel_total_qty == 20
    assert found.channel_remaining_qty == 20
    assert found.entry_price == 0.885
    assert found.user_original_qty == 16
    assert found.user_remaining_qty == 16


def test_list_open_returns_only_open_positions(store):
    open_option = _qqq_740c()
    closed_option = OptionKey("SPY", date(2026, 9, 22), 769.0, "C")
    store.create_open(OpenPosition(open_option, 10, 10, 8, 8, 1.0, 1))
    store.create_open(OpenPosition(closed_option, 10, 10, 8, 8, 1.0, 2))
    store.close_position(closed_option)

    open_positions = store.list_open()
    assert [p.option for p in open_positions] == [open_option]


def test_ambiguous_underlying_lookup_raises(store):
    q1 = OptionKey("QQQ", date(2026, 9, 23), 740.0, "C")
    q2 = OptionKey("QQQ", date(2026, 9, 25), 740.0, "C")
    store.create_open(OpenPosition(q1, 10, 10, 8, 8, 1.0, 1))
    store.create_open(OpenPosition(q2, 5, 5, 4, 4, 1.0, 2))
    with pytest.raises(ValueError):
        store.get_open_by_underlying(UnderlyingKey("QQQ", 740.0, "C"))
