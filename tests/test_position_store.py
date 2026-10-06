from datetime import date

import pytest

from bot.models import OpenPosition, OptionKey, TargetLeg, TrimTarget, UnderlyingKey
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


# --- resting exit legs ----------------------------------------------------


def _open_position(store, *, user_qty=5, entry=1.905):
    return store.create_open(
        OpenPosition(
            option=_qqq_740c(),
            channel_total_qty=25,
            channel_remaining_qty=25,
            user_original_qty=user_qty,
            user_remaining_qty=user_qty,
            entry_price=entry,
            ibkr_order_id_entry=1,
            current_stop_price=1.33,
            trim_targets=(TrimTarget(0.25, 2.38), TrimTarget(0.50, 2.86)),
        )
    )


def _leg(position_id, tier_index, *, qty=1, tp_price=2.38, lmt=10, stp=11):
    return TargetLeg(
        position_id=position_id,
        tier_index=tier_index,
        qty=qty,
        tp_price=tp_price,
        current_stop_price=1.33,
        oca_group=f"pos{position_id}-t{tier_index}",
        lmt_order_id=lmt,
        stp_order_id=stp,
    )


def test_create_open_returns_the_id_legs_hang_off(store):
    position_id = _open_position(store)
    assert isinstance(position_id, int)
    assert store.get_position_by_id(position_id).option == _qqq_740c()


def test_the_trim_ladder_round_trips_through_the_database(store):
    """A 1-contract runner has no limit orders to recover its rungs from,
    so the ladder itself has to survive a restart."""
    position_id = _open_position(store)

    position = store.get_position_by_id(position_id)

    assert [(t.pct, t.price) for t in position.trim_targets] == [(0.25, 2.38), (0.50, 2.86)]


def test_a_position_with_no_ladder_reads_back_as_empty(store):
    store.create_open(OpenPosition(_qqq_740c(), 10, 10, 2, 2, 1.0, 1))
    assert store.get_open(_qqq_740c()).trim_targets == ()


def test_legs_are_found_by_either_of_their_order_ids(store):
    position_id = _open_position(store)
    store.add_target_leg(_leg(position_id, 0, lmt=101, stp=102))

    limit_hit = store.get_leg_by_order_id(101)
    stop_hit = store.get_leg_by_order_id(102)

    assert limit_hit is not None and limit_hit[1] == "TP"
    assert stop_hit is not None and stop_hit[1] == "STOP"
    assert limit_hit[0].id == stop_hit[0].id  # same tranche, two sides
    assert store.get_leg_by_order_id(999) is None


def test_only_live_legs_are_listed_once_filtered(store):
    position_id = _open_position(store)
    first = store.add_target_leg(_leg(position_id, 0, lmt=101, stp=102))
    store.add_target_leg(_leg(position_id, 1, lmt=103, stp=104))
    store.set_leg_status(first, "TP_FILLED")

    assert [leg.tier_index for leg in store.list_legs(position_id)] == [0, 1]
    assert [leg.tier_index for leg in store.list_legs(position_id, only_live=True)] == [1]


def test_a_runner_leg_is_identified_by_having_no_limit_price(store):
    position_id = _open_position(store)
    store.add_target_leg(_leg(position_id, 0, tp_price=None, lmt=None, stp=201))

    leg = store.list_legs(position_id)[0]

    assert leg.tp_price is None
    assert leg.is_runner is True


def test_closed_positions_legs_are_left_out_of_the_global_live_list(store):
    position_id = _open_position(store)
    store.add_target_leg(_leg(position_id, 0))
    assert len(store.list_all_live_legs()) == 1

    store.close_position(_qqq_740c())

    assert store.list_all_live_legs() == []


def test_a_stop_can_only_ever_move_up(store):
    """A late or out-of-order fill event must not walk a protective stop
    back down the ladder."""
    position_id = _open_position(store)

    store.set_position_stop(position_id, 1.90, 0)
    store.set_position_stop(position_id, 2.38, 1)
    store.set_position_stop(position_id, 1.33, -1)  # a stale event arriving late

    position = store.get_position_by_id(position_id)
    assert position.current_stop_price == 2.38
    assert position.stop_tier_index == 1


def test_reduce_remaining_counts_down_and_closes_at_zero(store):
    position_id = _open_position(store, user_qty=5)

    assert store.reduce_remaining(position_id, 2) == 3
    assert store.get_open(_qqq_740c()) is not None

    assert store.reduce_remaining(position_id, 3) == 0
    assert store.get_open(_qqq_740c()) is None
    assert store.get_position_by_id(position_id).status == "CLOSED"


def test_reduce_remaining_never_goes_negative(store):
    position_id = _open_position(store, user_qty=2)

    assert store.reduce_remaining(position_id, 99) == 0


def test_migration_adds_the_new_columns_to_an_existing_database(store, tmp_path):
    """schema.sql is replayed on every open, which covers new tables but
    not new columns -- SQLite has no ADD COLUMN IF NOT EXISTS."""
    import sqlite3

    legacy = tmp_path / "legacy.sqlite3"
    conn = sqlite3.connect(legacy)
    conn.executescript(
        """CREATE TABLE positions (
               id INTEGER PRIMARY KEY AUTOINCREMENT, ticker TEXT NOT NULL, expiry TEXT NOT NULL,
               strike REAL NOT NULL, right TEXT NOT NULL, channel_total_qty INTEGER NOT NULL,
               channel_remaining_qty INTEGER NOT NULL, user_original_qty INTEGER NOT NULL,
               user_remaining_qty INTEGER NOT NULL, entry_price REAL NOT NULL,
               ibkr_order_id_entry INTEGER, status TEXT NOT NULL DEFAULT 'OPEN',
               created_at TEXT NOT NULL DEFAULT (datetime('now')),
               updated_at TEXT NOT NULL DEFAULT (datetime('now')));
           INSERT INTO positions (ticker, expiry, strike, right, channel_total_qty,
               channel_remaining_qty, user_original_qty, user_remaining_qty, entry_price)
               VALUES ('QQQ', '2026-09-23', 740.0, 'C', 10, 10, 2, 2, 1.0);"""
    )
    conn.commit()
    conn.close()

    migrated = PositionStore(legacy)
    try:
        position = migrated.get_open(_qqq_740c())
        assert position is not None
        assert position.current_stop_price is None  # pre-existing row, no stop recorded
        assert position.stop_tier_index == -1
        assert position.trim_targets == ()
    finally:
        migrated.close()
