"""Persistence, deduplication, and the state that must survive a restart."""

from datetime import date, datetime, timedelta

import pytest
from conftest import SPX_OCC, TRADING_DAY

from options_scanner.models import EntryAlert, OptionKey, PositionState, TrimTarget
from options_scanner.storage import (
    STATUS_ACCEPTED,
    STATUS_REJECTED,
    Storage,
    states_equal,
)

OPTION = OptionKey("SPX", date(2026, 10, 6), 7815.0, "P")


def _alert(message_id: int = 1, entry_price: float | None = 0.475) -> EntryAlert:
    return EntryAlert(
        message_id=message_id,
        option=OPTION,
        entry_price=entry_price,
        advisor_contracts=25,
        advisor_cost=1188.0,
        trim_targets=(TrimTarget(0.25, 0.594),),
        raw_title="BUY — SPX 7815P · 0DTE",
        is_zero_dte=True,
    )


def _state(**overrides) -> PositionState:
    base = dict(
        option=OPTION, entry_fill=0.475, original_qty=21, remaining_qty=21, peak_bid=0.475
    )
    base.update(overrides)
    return PositionState(**base)


def _seed_alert(
    storage: Storage,
    message_id: int = 1,
    status: str = STATUS_ACCEPTED,
    entry_price: float | None = 0.475,
) -> None:
    storage.record_alert(
        message_id=message_id,
        channel_id=1001,
        author_id=3003,
        raw_text="raw",
        status=status,
        alert=_alert(message_id, entry_price),
    )


# --- alerts and dedup -----------------------------------------------------


def test_an_unseen_message_is_not_a_duplicate(storage):
    assert storage.alert_seen(42) is False


def test_a_recorded_message_is_seen(storage):
    _seed_alert(storage, 42)
    assert storage.alert_seen(42) is True


def test_recording_the_same_message_twice_keeps_one_row(storage):
    _seed_alert(storage, 42)
    storage.record_alert(
        message_id=42, channel_id=1001, author_id=3003, raw_text="raw", status=STATUS_REJECTED
    )
    rows = storage._conn.execute("SELECT * FROM alerts WHERE message_id = 42").fetchall()
    assert len(rows) == 1
    assert rows[0]["status"] == STATUS_REJECTED  # latest disposition wins


def test_an_update_does_not_erase_the_parsed_contract(storage):
    """A later status change must not blank the fields the fingerprint needs."""
    _seed_alert(storage, 42)
    storage.record_alert(
        message_id=42, channel_id=1001, author_id=3003, raw_text="raw", status=STATUS_REJECTED
    )
    assert storage.get_alert(42)["occ_symbol"] == SPX_OCC
    assert storage.get_alert(42)["entry_price"] == 0.475


def test_the_fingerprint_finds_a_recent_repaste(storage):
    _seed_alert(storage, 100)
    assert storage.find_recent_duplicate(SPX_OCC, 0.475, 900) == 100


def test_the_fingerprint_ignores_a_different_entry_price(storage):
    _seed_alert(storage, 100)
    assert storage.find_recent_duplicate(SPX_OCC, 0.600, 900) is None


def test_the_fingerprint_ignores_a_different_contract(storage):
    _seed_alert(storage, 100)
    assert storage.find_recent_duplicate("SPXW  261006P07820000", 0.475, 900) is None


def test_the_fingerprint_expires(storage):
    _seed_alert(storage, 100)
    later = datetime.utcnow() + timedelta(seconds=3600)
    assert storage.find_recent_duplicate(SPX_OCC, 0.475, 900, now=later) is None


def test_only_accepted_alerts_count_as_duplicates(storage):
    """A rejected alert must not block a corrected re-paste of the same trade."""
    _seed_alert(storage, 100, status=STATUS_REJECTED)
    assert storage.find_recent_duplicate(SPX_OCC, 0.475, 900) is None


def test_a_no_price_alert_is_stored_without_a_price(storage):
    _seed_alert(storage, 100, entry_price=None)
    assert storage.get_alert(100)["entry_price"] is None


def test_a_no_price_repaste_of_the_same_contract_is_a_duplicate(storage):
    """`TSLA 397.5C` sent twice must not buy twice: NULL never equals NULL in
    the price comparison, so a missing price is matched as missing."""
    _seed_alert(storage, 100, entry_price=None)
    assert storage.find_recent_duplicate(SPX_OCC, None, 900) == 100


def test_a_no_price_alert_and_a_priced_one_are_not_duplicates(storage):
    _seed_alert(storage, 100, entry_price=None)
    assert storage.find_recent_duplicate(SPX_OCC, 0.475, 900) is None
    _seed_alert(storage, 200, entry_price=0.475)
    assert storage.find_recent_duplicate(SPX_OCC, None, 900) == 100


def test_a_no_price_duplicate_still_needs_the_same_contract(storage):
    _seed_alert(storage, 100, entry_price=None)
    assert storage.find_recent_duplicate("SPXW  261006P07820000", None, 900) is None


def test_a_no_price_duplicate_expires(storage):
    _seed_alert(storage, 100, entry_price=None)
    later = datetime.utcnow() + timedelta(seconds=3600)
    assert storage.find_recent_duplicate(SPX_OCC, None, 900, now=later) is None


# --- positions ------------------------------------------------------------


def test_a_position_round_trips_exactly(storage):
    """Every field matters: a forgotten fired level trims twice, and a
    forgotten peak walks the trailing stop backwards."""
    _seed_alert(storage)
    state = _state(
        remaining_qty=8,
        peak_bid=2.00,
        stop_price=1.09,
        stop_reason="trail",
        trail_armed=True,
        fired_levels=frozenset({25, 50, 75, 100}),
        consecutive_breaches=1,
    )
    position_id = storage.create_position(1, state)
    storage.save_position(position_id, state)

    (loaded_id, loaded), = storage.open_positions()
    assert loaded_id == position_id
    assert states_equal(state, loaded)


def test_reloading_restores_the_fired_levels_as_integers(storage):
    _seed_alert(storage)
    state = _state(fired_levels=frozenset({25, 50}))
    storage.create_position(1, state)

    (_, loaded), = storage.open_positions()
    assert loaded.fired_levels == {25, 50}
    assert all(isinstance(level, int) for level in loaded.fired_levels)


def test_an_unprotected_position_round_trips_with_no_stop(storage):
    _seed_alert(storage)
    storage.create_position(1, _state())

    (_, loaded), = storage.open_positions()
    assert loaded.stop_price is None
    assert loaded.is_protected is False


def test_closing_a_position_removes_it_from_the_open_list(storage):
    _seed_alert(storage)
    state = _state()
    position_id = storage.create_position(1, state)
    state.remaining_qty = 0
    state.closed = True
    storage.save_position(position_id, state, realized_pnl=250.0)

    assert storage.open_positions() == []
    assert storage.open_position_count() == 0
    row = storage._conn.execute("SELECT * FROM positions WHERE id = ?", (position_id,)).fetchone()
    assert row["status"] == "CLOSED"
    assert row["closed_at"] is not None
    assert row["realized_pnl"] == 250.0


def test_only_one_open_position_per_contract(storage):
    import sqlite3

    _seed_alert(storage)
    storage.create_position(1, _state())
    with pytest.raises(sqlite3.IntegrityError):
        storage.create_position(1, _state())


def test_a_position_is_findable_by_its_symbol(storage):
    _seed_alert(storage)
    position_id = storage.create_position(1, _state())

    found = storage.position_for_symbol(SPX_OCC)
    assert found is not None and found[0] == position_id
    assert storage.position_for_symbol("SPXW  261006C07815000") is None


# --- orders and fills -----------------------------------------------------


def test_orders_and_fills_are_recorded_against_a_position(storage):
    _seed_alert(storage)
    position_id = storage.create_position(1, _state())
    order_id = storage.record_order(
        occ_symbol=SPX_OCC, intent="ENTRY", side="BUY", qty=21, status="PENDING",
        position_id=position_id, limit_price=0.475,
    )
    storage.update_order(order_id, status="FILLED", filled_qty=21, avg_fill_price=0.477)
    storage.record_fill(order_id, 10, 0.476)
    storage.record_fill(order_id, 11, 0.478)

    (order,) = storage.orders_for_position(position_id)
    assert (order["status"], order["filled_qty"], order["avg_fill_price"]) == ("FILLED", 21, 0.477)
    fills = storage._conn.execute("SELECT * FROM fills WHERE order_id = ?", (order_id,)).fetchall()
    assert [f["qty"] for f in fills] == [10, 11]


def test_update_order_with_nothing_to_change_is_a_no_op(storage):
    _seed_alert(storage)
    order_id = storage.record_order(
        occ_symbol=SPX_OCC, intent="ENTRY", side="BUY", qty=1, status="PENDING"
    )
    storage.update_order(order_id)
    row = storage._conn.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone()
    assert row["status"] == "PENDING"


# --- day counters ---------------------------------------------------------


def test_day_counters_start_at_zero_and_accumulate(storage):
    stats = storage.day_stats(TRADING_DAY)
    assert stats["entries"] == 0 and stats["realized_pnl"] == 0.0

    storage.bump_day(TRADING_DAY, entries=1, realized_pnl=-120.5)
    storage.bump_day(TRADING_DAY, entries=2, realized_pnl=40.0)

    stats = storage.day_stats(TRADING_DAY)
    assert stats["entries"] == 3
    assert stats["realized_pnl"] == pytest.approx(-80.5)


def test_days_are_independent(storage):
    storage.bump_day(TRADING_DAY, entries=5)
    assert storage.day_stats(date(2026, 10, 7))["entries"] == 0


def test_an_unknown_counter_is_rejected_rather_than_interpolated(storage):
    with pytest.raises(ValueError, match="unknown day_stats column"):
        storage.bump_day(TRADING_DAY, **{"entries); DROP TABLE alerts;--": 1})


def test_bumping_nothing_is_harmless(storage):
    storage.bump_day(TRADING_DAY)
    assert storage.day_stats(TRADING_DAY)["entries"] == 0


# --- the halt flag --------------------------------------------------------


def test_the_halt_flag_persists_across_a_reopen(tmp_path):
    """A bot that restarts un-halted after being halted would resume trading
    on its own, which is the one thing halting is meant to prevent."""
    path = tmp_path / "halt.sqlite3"
    first = Storage(path)
    first.set_halted(True, "daily loss limit")
    assert first.is_halted() is True
    first.close()

    second = Storage(path)
    try:
        assert second.is_halted() is True
        assert second.halt_reason() == "daily loss limit"
    finally:
        second.close()


def test_resuming_clears_the_flag(storage):
    storage.set_halted(True, "testing")
    storage.set_halted(False)
    assert storage.is_halted() is False


def test_arbitrary_state_round_trips(storage):
    assert storage.get_state("nope") is None
    storage.set_state("k", "v1")
    storage.set_state("k", "v2")
    assert storage.get_state("k") == "v2"


def test_an_average_down_survives_a_restart(storage):
    """An average-down changes the entry and the original size. Reloading the
    old entry against the new quantity would run the wrong ladder."""
    from options_scanner.rules import average_into

    _seed_alert(storage)
    state = _state(entry_fill=2.00, original_qty=1, remaining_qty=1, peak_bid=2.00)
    position_id = storage.create_position(1, state)

    average_into(state, added_qty=2, added_price=1.40)
    storage.save_position(position_id, state)

    (_, reloaded), = storage.open_positions()
    assert reloaded.entry_fill == pytest.approx(1.60)
    assert reloaded.original_qty == 3
    assert reloaded.remaining_qty == 3
    assert reloaded.stop_price is None
    assert reloaded.fired_levels == frozenset()


# --- the runner lock --------------------------------------------------------


def test_the_runner_lock_round_trips(storage):
    _seed_alert(storage)
    state = _state(
        remaining_qty=5,
        peak_bid=2.30,
        stop_price=1.50,
        stop_reason="trail",
        fired_levels=frozenset({25, 50, 75, 100}),
        lock_armed=True,
        lock_qty=2,
        lock_stop=2.00,
        lock_breaches=1,
    )
    position_id = storage.create_position(1, state)
    storage.save_position(position_id, state)

    (_, loaded), = storage.open_positions()
    assert states_equal(state, loaded)
    assert (loaded.lock_armed, loaded.lock_qty, loaded.lock_stop, loaded.lock_breaches) == (
        True, 2, 2.00, 1,
    )


def test_an_older_database_gains_the_lock_columns(tmp_path):
    """The live database predates the runner lock. Opening it must add the
    columns in place, keep its rows, and load them with the lock off."""
    import sqlite3

    path = tmp_path / "old.sqlite3"
    Storage(path).close()
    conn = sqlite3.connect(path)
    for column in ("lock_armed", "lock_qty", "lock_stop", "lock_breaches"):
        conn.execute(f"ALTER TABLE positions DROP COLUMN {column}")
    conn.commit()
    conn.close()

    old = Storage(path)
    try:
        _seed_alert(old)
        old.create_position(1, _state())
        (_, loaded), = old.open_positions()
        assert (loaded.lock_armed, loaded.lock_qty, loaded.lock_stop) == (False, 0, None)
    finally:
        old.close()
