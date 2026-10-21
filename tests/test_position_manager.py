"""The managed-position loop: entry through trims, trail, and exit.

Runs the real pipeline, the real rules engine and the real PaperBroker over a
scripted price path. Nothing is mocked except the prices themselves.
"""

from datetime import date, datetime

import pytest

from conftest import SPX_OCC, TRADING_DAY, make_settings, paste

from options_scanner.broker.base import BrokerError, Quote
from options_scanner.broker.paper import PaperBroker
from options_scanner.contracts import build_spec
from options_scanner.models import OptionKey
from options_scanner.pipeline import AlertPipeline
from options_scanner.position_manager import PositionManager
from options_scanner.risk import RiskGate
from options_scanner.storage import Storage

OPTION = OptionKey("SPX", date(2026, 10, 6), 7815.0, "P")
SPEC = build_spec(OPTION)


class Feed:
    """A quote feed under test control: the price is set, not scripted, so a
    test reads like the price path it describes."""

    def __init__(self, bid=0.47, ask=0.48, exists=True):
        self.bid, self.ask = bid, ask
        self._exists = exists
        self.released: list[str] = []

    def set(self, bid, ask=None):
        self.bid = bid
        self.ask = ask if ask is not None else round(bid + 0.01, 2)

    def blank(self):
        self.bid = self.ask = None

    async def quote(self, spec):
        return Quote(bid=self.bid, ask=self.ask, last=self.bid, asof=datetime(2026, 10, 6, 11, 0))

    async def exists(self, spec):
        return self._exists

    def release(self, symbol):
        self.released.append(symbol)


def build(storage: Storage, *, feed: Feed | None = None, **overrides):
    """A full paper stack over a controllable feed."""
    feed = feed or Feed()
    settings = make_settings(mode="paper", **overrides)
    broker = PaperBroker(feed)
    broker._connected = True
    risk = RiskGate(settings, storage)
    manager = PositionManager(settings, storage, risk, broker)
    # Pin the booking day so the suite does not pass only on the date it was
    # written -- the real clock is the production default.
    manager.today = lambda: TRADING_DAY
    pipeline = AlertPipeline(settings, storage, risk, broker, manager)
    return feed, broker, manager, pipeline, risk


def titles(notes):
    return [n.title for n in notes]


def has(notes, needle):
    return any(needle.lower() in n.title.lower() for n in notes)


async def enter(pipeline, feed, *, bid=0.47, ask=0.48, message_id=700_001):
    feed.set(bid, ask)
    return await paste(pipeline, message_id=message_id)


# --- entry ----------------------------------------------------------------


async def test_an_alert_fills_and_becomes_a_managed_position(storage):
    feed, broker, manager, pipeline, _ = build(storage)

    result = await enter(pipeline, feed)

    assert has(result.notifications, "BUY")
    assert SPX_OCC in manager.managed
    (position,) = await broker.get_positions()
    assert position.qty == 21
    assert storage.open_position_count() == 1


async def test_the_ladder_measures_from_our_fill_not_the_advisor_entry(storage):
    """We paid the 0.48 ask, not the 0.475 the card advertised."""
    feed, _, manager, pipeline, _ = build(storage)

    await enter(pipeline, feed, bid=0.47, ask=0.48)

    state = manager.managed[SPX_OCC].state
    assert state.entry_fill == 0.48
    assert state.peak_bid == 0.48


async def test_an_unfillable_entry_opens_no_position(storage):
    """The ask is above the +10% slippage cap, so nothing should be bought."""
    feed, broker, manager, pipeline, _ = build(storage)

    result = await enter(pipeline, feed, bid=0.60, ask=0.62)

    assert manager.managed == {}
    assert await broker.get_positions() == []
    assert has(result.notifications, "skipped")


# --- the ladder, live -----------------------------------------------------


async def test_the_first_trim_sells_and_promotes_the_stop_to_breakeven(storage):
    feed, broker, manager, pipeline, _ = build(storage)
    await enter(pipeline, feed)

    feed.set(0.62)  # 0.48 * 1.25 = 0.60, so this clears the +25% rung
    notes = await manager.poll_once()

    assert has(notes, "TRIM +25%")
    assert has(notes, "STOP \u2192")
    state = manager.managed[SPX_OCC].state
    assert state.remaining_qty == 10  # sold half: ceil(21*0.50) = 11
    assert state.stop_price == 0.48
    assert state.stop_reason == "breakeven"
    (position,) = await broker.get_positions()
    assert position.qty == 10  # the broker agrees


async def test_climbing_the_whole_ladder_arms_the_trail_and_keeps_a_runner(storage):
    feed, broker, manager, pipeline, _ = build(storage)
    await enter(pipeline, feed)

    for bid in (0.62, 0.74, 0.86, 1.00):
        await manager.poll_once()
        feed.set(bid)
    notes = await manager.poll_once()

    state = manager.managed[SPX_OCC].state
    assert state.trail_armed is True
    assert state.fired_levels == {25, 50, 75, 100}
    assert state.remaining_qty >= 1
    # Armed, but break-even is still the stop: peak 1.00 is +108%, and
    # peak * 0.40 does not clear the entry fill until +150%. The trail having
    # armed is not the same thing as the trail protecting anything.
    assert state.stop_reason == "breakeven"
    assert state.stop_price == 0.48


async def test_the_runner_levels_sell_nothing_and_are_each_reported(storage):
    feed, _, manager, pipeline, _ = build(storage)
    await enter(pipeline, feed)

    feed.set(0.86)  # clears the +25/+50 ladder and the +75 runner level
    await manager.poll_once()
    before = manager.managed[SPX_OCC].state.remaining_qty

    feed.set(1.00)  # clears +100%
    notes = await manager.poll_once()

    state = manager.managed[SPX_OCC].state
    assert 100 in state.fired_levels
    assert state.remaining_qty == before
    assert has(notes, "RUNNER +100%")


async def test_a_runner_level_is_announced_once_not_on_every_quote(storage):
    feed, _, manager, pipeline, _ = build(storage)
    await enter(pipeline, feed)

    feed.set(0.86)
    first = await manager.poll_once()
    feed.set(0.88)
    second = await manager.poll_once()

    assert has(first, "RUNNER +75%")
    assert not has(second, "RUNNER +75%")


async def test_a_gap_through_several_rungs_fires_each_one(storage):
    feed, _, manager, pipeline, _ = build(storage)
    await enter(pipeline, feed)

    feed.set(0.90)
    notes = await manager.poll_once()

    assert has(notes, "TRIM +25%")
    assert has(notes, "TRIM +50%")
    assert has(notes, "RUNNER +75%")  # above the ladder: reported, never sold
    assert manager.managed[SPX_OCC].state.remaining_qty == 4


async def test_a_rung_cannot_fire_twice(storage):
    feed, _, manager, pipeline, _ = build(storage)
    await enter(pipeline, feed)

    feed.set(0.62)
    await manager.poll_once()
    after_first = manager.managed[SPX_OCC].state.remaining_qty
    await manager.poll_once()
    await manager.poll_once()

    assert manager.managed[SPX_OCC].state.remaining_qty == after_first


# --- stops ----------------------------------------------------------------


async def test_a_breach_needs_confirming_before_it_exits(storage):
    feed, _, manager, pipeline, _ = build(storage)
    await enter(pipeline, feed)
    feed.set(0.62)
    await manager.poll_once()  # breakeven stop at 0.48

    feed.set(0.45)
    first = await manager.poll_once()
    assert not has(first, "SOLD ALL")

    feed.set(0.44)
    second = await manager.poll_once()
    assert has(second, "SOLD ALL")


async def test_a_single_bad_tick_does_not_stop_the_position_out(storage):
    feed, _, manager, pipeline, _ = build(storage)
    await enter(pipeline, feed)
    feed.set(0.62)
    await manager.poll_once()

    feed.set(0.45)
    await manager.poll_once()
    feed.set(0.70)  # recovered
    await manager.poll_once()
    feed.set(0.45)
    notes = await manager.poll_once()

    assert not has(notes, "SOLD ALL")
    assert SPX_OCC in manager.managed


async def test_stopping_out_closes_the_position_everywhere(storage):
    feed, broker, manager, pipeline, _ = build(storage)
    await enter(pipeline, feed)
    feed.set(0.62)
    await manager.poll_once()

    feed.set(0.45)
    await manager.poll_once()
    feed.set(0.44)
    notes = await manager.poll_once()

    assert has(notes, "SOLD ALL")
    assert manager.managed == {}
    assert await broker.get_positions() == []
    assert storage.open_position_count() == 0
    assert feed.released == [SPX_OCC]  # market-data line handed back


async def test_a_trailed_stop_exits_above_breakeven(storage):
    feed, _, manager, pipeline, _ = build(storage)
    await enter(pipeline, feed)

    # 2.00 is +317% off the 0.48 fill, which is where the trail finally beats
    # break-even: 2.00 * 0.55 = 1.10. At 0.90 it would still be 0.48.
    feed.set(2.00)
    await manager.poll_once()      # trims, arms and ratchets the trail
    state = manager.managed[SPX_OCC].state
    stop = state.stop_price
    assert stop == 1.10
    assert state.stop_reason == "trail"

    feed.set(stop - 0.02)
    await manager.poll_once()
    notes = await manager.poll_once()

    assert has(notes, "SOLD ALL")


async def test_the_realized_pnl_reaches_the_day_counters(storage):
    feed, _, manager, pipeline, risk = build(storage)
    await enter(pipeline, feed)

    feed.set(0.90)
    await manager.poll_once()

    assert risk.day_summary(TRADING_DAY)["realized_pnl"] > 0


# --- restart and reconciliation ------------------------------------------


async def test_a_restart_restores_the_whole_rules_state(storage):
    """The peak and the fired-level set *are* the stop. Losing either would
    quietly loosen it rather than fail."""
    feed, broker, manager, pipeline, _ = build(storage)
    await enter(pipeline, feed)
    feed.set(0.90)
    await manager.poll_once()
    before = manager.managed[SPX_OCC].state

    # A fresh process over the same database and the same broker state.
    settings = make_settings(mode="paper")
    reborn = PositionManager(settings, storage, RiskGate(settings, storage), broker)
    reborn.today = lambda: TRADING_DAY
    lines, mismatched = await reborn.reconcile()

    after = reborn.managed[SPX_OCC].state
    assert mismatched is False
    assert "matches the broker" in lines[0]
    assert after.entry_fill == before.entry_fill
    assert after.peak_bid == before.peak_bid
    assert after.stop_price == before.stop_price
    assert after.fired_levels == before.fired_levels
    assert after.trail_armed == before.trail_armed
    assert after.remaining_qty == before.remaining_qty


async def test_a_restored_position_keeps_laddering(storage):
    feed, broker, manager, pipeline, _ = build(storage)
    await enter(pipeline, feed)
    feed.set(0.62)
    await manager.poll_once()

    settings = make_settings(mode="paper")
    reborn = PositionManager(settings, storage, RiskGate(settings, storage), broker)
    reborn.today = lambda: TRADING_DAY
    await reborn.reconcile()

    feed.set(0.74)
    notes = await reborn.poll_once()

    assert has(notes, "TRIM +50%")
    assert not has(notes, "TRIM +25%"), "the rung fired before the restart must not re-fire"
    assert reborn.managed[SPX_OCC].state.fired_levels >= {25, 50}


async def test_reconciliation_reports_a_position_the_broker_does_not_have(storage):
    feed, broker, manager, pipeline, _ = build(storage)
    await enter(pipeline, feed)

    broker.drop_position(SPX_OCC)  # as an account-wide flatten would
    settings = make_settings(mode="paper")
    reborn = PositionManager(settings, storage, RiskGate(settings, storage), broker)
    reborn.today = lambda: TRADING_DAY
    lines, mismatched = await reborn.reconcile()

    assert mismatched is True
    assert "broker reports no position" in lines[0]
    assert reborn.managed == {}
    assert storage.open_position_count() == 0


async def test_reconciliation_corrects_a_quantity_down_to_the_brokers(storage):
    """Corrected downward, never upward: selling against a quantity we do not
    hold is the one error that can open a short."""
    feed, broker, manager, pipeline, _ = build(storage)
    await enter(pipeline, feed)
    broker.seed_position(SPX_OCC, 5, 0.48)

    settings = make_settings(mode="paper")
    reborn = PositionManager(settings, storage, RiskGate(settings, storage), broker)
    reborn.today = lambda: TRADING_DAY
    lines, mismatched = await reborn.reconcile()

    assert mismatched is True
    assert "broker reports 5" in lines[0]
    assert reborn.managed[SPX_OCC].state.remaining_qty == 5


async def test_reconciliation_is_quiet_with_nothing_open(storage):
    feed, broker, _, _, _ = build(storage)
    settings = make_settings(mode="paper")
    manager = PositionManager(settings, storage, RiskGate(settings, storage), broker)

    lines, mismatched = await manager.reconcile()

    assert lines == []
    assert mismatched is False


# --- interference, staleness, failure ------------------------------------


async def test_a_position_vanishing_mid_session_is_escalated(storage):
    """warrior_bot shares this account and its panic path flattens
    account-wide, so this is a real possibility rather than a theoretical."""
    feed, broker, manager, pipeline, _ = build(storage)
    await enter(pipeline, feed)

    broker.drop_position(SPX_OCC)
    feed.set(0.62)
    notes = await manager.poll_once()

    assert has(notes, "Position changed without our order")
    assert notes[0].mentions_owner is True
    assert manager.managed == {}


async def test_a_partial_disappearance_corrects_down_and_keeps_going(storage):
    feed, broker, manager, pipeline, _ = build(storage)
    await enter(pipeline, feed)

    broker.seed_position(SPX_OCC, 10, 0.48)
    feed.set(0.50)
    notes = await manager.poll_once()

    assert has(notes, "Position changed without our order")
    assert manager.managed[SPX_OCC].state.remaining_qty == 10


async def test_a_blank_quote_eventually_raises_a_stale_alarm(storage):
    """No quotes means no stop, so the failure mode is silence."""
    feed, _, manager, pipeline, _ = build(storage, stops={"stale_quote_seconds": 0.0001})
    await enter(pipeline, feed)

    feed.blank()
    notes = await manager.poll_once()

    assert has(notes, "stale")
    assert notes[0].mentions_owner is True


async def test_the_stale_alarm_is_raised_once_per_episode(storage):
    feed, _, manager, pipeline, _ = build(storage, stops={"stale_quote_seconds": 0.0001})
    await enter(pipeline, feed)
    feed.blank()

    first = await manager.poll_once()
    second = await manager.poll_once()

    assert has(first, "stale")
    assert not has(second, "stale")


async def test_a_disconnected_broker_is_reported_and_nothing_is_polled(storage):
    feed, broker, manager, pipeline, _ = build(storage)
    await enter(pipeline, feed)
    broker._connected = False

    notes = await manager.poll_once()

    assert has(notes, "Broker disconnected")
    assert notes[0].mentions_owner is True


# --- flatten --------------------------------------------------------------


async def test_flatten_closes_everything_at_a_marketable_limit(storage):
    feed, broker, manager, pipeline, _ = build(storage)
    await enter(pipeline, feed)

    notes = await manager.flatten_all()

    assert has(notes, "SOLD ALL")
    assert manager.managed == {}
    assert await broker.get_positions() == []
    assert storage.open_position_count() == 0


async def test_flatten_with_nothing_open_does_nothing(storage):
    _, _, manager, _, _ = build(storage)
    assert await manager.flatten_all() == []


# --- near-close warning ---------------------------------------------------


async def test_the_near_close_warning_fires_once_while_forced_exit_is_off(storage, monkeypatch):
    feed, _, manager, pipeline, _ = build(storage)
    await enter(pipeline, feed)

    import options_scanner.position_manager as pm

    monkeypatch.setattr(pm, "minutes_to_close", lambda *a, **k: 5.0)
    first = await manager.poll_once()
    second = await manager.poll_once()

    assert has(first, "near the close")
    assert not has(second, "near the close")


async def test_no_near_close_warning_when_forced_exit_is_enabled(storage, monkeypatch):
    feed, _, manager, pipeline, _ = build(storage, market={"force_exit_enabled": True})
    await enter(pipeline, feed)

    import options_scanner.position_manager as pm

    monkeypatch.setattr(pm, "minutes_to_close", lambda *a, **k: 5.0)
    notes = await manager.poll_once()

    assert not has(notes, "near the close")


async def test_the_trim_card_comes_before_the_stop_it_caused(storage):
    """The break-even stop exists because the trim filled. Posted the other
    way round, #updates announced the stop before the trim that earned it."""
    feed, _, manager, pipeline, _ = build(storage)
    await enter(pipeline, feed)

    feed.set(0.62)  # clears +25%: sells, then promotes the stop to break-even
    notes = await manager.poll_once()

    order = titles(notes)
    trim = next(i for i, t in enumerate(order) if t.startswith("TRIM +25%"))
    stop = next(i for i, t in enumerate(order) if t.startswith("STOP"))
    assert trim < stop


# --- broker outages ---------------------------------------------------------
#
# 2026-10-16: Gateway logged off at 23:37 and the bot posted a CRITICAL
# "Broker disconnected" on every 1.5s poll -- 2,379 pings in 78 minutes -- and
# never tried to reconnect.

from options_scanner.position_manager import (  # noqa: E402
    OUTAGE_REMINDER_SECONDS,
    RECONNECT_FIRST_SECONDS,
    RECONNECT_MAX_IN_SESSION_SECONDS,
    RECONNECT_MAX_SECONDS,
)
from options_scanner.notifier import Level  # noqa: E402


class Clock:
    def __init__(self):
        self.now = 1_000.0

    def __call__(self):
        return self.now


def _outage(storage, *, reconnects: bool):
    """A manager whose broker has just dropped. `reconnects` decides whether
    connect() brings it back."""
    feed, broker, manager, pipeline, risk = build(storage)
    clock = Clock()
    manager._monotonic = clock
    # Pinned, so these do not depend on whether the real market is open.
    manager._is_market_open = lambda: False
    calls = []

    async def connect():
        calls.append(clock.now)
        if not reconnects:
            raise BrokerError("connection refused")
        broker._connected = True

    broker.connect = connect
    return feed, broker, manager, pipeline, clock, calls


async def _drop(broker):
    broker._connected = False


async def test_an_outage_is_announced_once_not_every_poll(storage):
    _, broker, manager, _, clock, _ = _outage(storage, reconnects=False)
    await _drop(broker)

    first = await manager.poll_once()
    later = []
    for _ in range(200):  # five minutes of 1.5s polls
        clock.now += 1.5
        later += await manager.poll_once()

    assert titles(first) == ["Broker disconnected"]
    assert not any(n.title == "Broker disconnected" for n in later)


async def test_with_nothing_open_an_outage_is_a_warning_not_a_ping(storage):
    _, broker, manager, _, _, _ = _outage(storage, reconnects=False)
    await _drop(broker)

    (note,) = await manager.poll_once()

    assert note.level is Level.WARNING
    assert not note.mentions_owner
    assert "nothing is at risk" in note.description


async def test_with_a_position_open_an_outage_is_critical(storage):
    feed, broker, manager, pipeline, _, _ = _outage(storage, reconnects=False)
    await enter(pipeline, feed)
    await _drop(broker)

    (note,) = await manager.poll_once()

    assert note.level is Level.CRITICAL
    assert note.mentions_owner
    assert "1 open position" in note.description


async def test_open_positions_in_storage_count_even_before_reconcile(storage):
    """A restart with Gateway down: nothing is tracked in memory yet, but the
    position is open and its stop is blind."""
    feed, broker, manager, pipeline, _, _ = _outage(storage, reconnects=False)
    await enter(pipeline, feed)
    manager.managed.clear()  # as if freshly restarted, reconcile not yet run
    await _drop(broker)

    (note,) = await manager.poll_once()

    assert note.level is Level.CRITICAL


async def test_a_long_outage_with_positions_open_is_restated_every_15_minutes(storage):
    feed, broker, manager, pipeline, clock, _ = _outage(storage, reconnects=False)
    await enter(pipeline, feed)
    await _drop(broker)
    await manager.poll_once()

    reminders = []
    for _ in range(int(2 * OUTAGE_REMINDER_SECONDS / 30) + 1):  # just over 30 minutes
        clock.now += 30
        reminders += [n for n in await manager.poll_once() if n.title == "Broker disconnected"]

    assert len(reminders) == 2
    assert "Still not connected" in reminders[0].description


async def test_reconnection_is_retried_with_a_growing_gap(storage):
    _, broker, manager, _, clock, calls = _outage(storage, reconnects=False)
    await _drop(broker)
    await manager.poll_once()

    for _ in range(int(400 / 1.5)):
        clock.now += 1.5
        await manager.poll_once()

    gaps = [round(b - a) for a, b in zip(calls, calls[1:])]
    assert calls[0] - 1_000.0 >= RECONNECT_FIRST_SECONDS
    assert gaps == sorted(gaps)        # never retries faster than before
    assert gaps[0] < gaps[-1]          # and backs off


async def test_the_bot_reconnects_and_says_so(storage):
    _, broker, manager, _, clock, calls = _outage(storage, reconnects=True)
    await _drop(broker)
    await manager.poll_once()

    clock.now += RECONNECT_FIRST_SECONDS
    notes = await manager.poll_once()

    assert calls
    assert broker.is_connected
    assert "Broker reconnected" in titles(notes)


async def test_after_reconnecting_open_positions_are_rechecked_and_managed(storage):
    """It could not see the account during the outage, so it re-checks before
    acting -- then carries on laddering as normal."""
    feed, broker, manager, pipeline, clock, _ = _outage(storage, reconnects=True)
    await enter(pipeline, feed)
    await _drop(broker)
    await manager.poll_once()

    clock.now += RECONNECT_FIRST_SECONDS
    notes = await manager.poll_once()
    assert "Broker reconnected" in titles(notes)
    assert any("Reconcil" in t or "reconcil" in t.lower() for t in titles(notes))

    feed.set(0.62)  # +25%
    assert has(await manager.poll_once(), "TRIM +25%")


async def test_a_second_outage_is_announced_again(storage):
    _, broker, manager, _, clock, _ = _outage(storage, reconnects=True)
    await _drop(broker)
    await manager.poll_once()
    clock.now += RECONNECT_FIRST_SECONDS
    await manager.poll_once()  # back

    await _drop(broker)
    clock.now += 5
    notes = await manager.poll_once()

    assert titles(notes) == ["Broker disconnected"]


async def test_re_tracking_keeps_the_link_to_the_alert(storage):
    feed, broker, manager, pipeline, _, _ = _outage(storage, reconnects=True)
    await enter(pipeline, feed)
    before = manager.managed[SPX_OCC].jump_url

    await manager.reconcile()

    assert before and manager.managed[SPX_OCC].jump_url == before



async def test_during_the_session_retries_never_wait_more_than_30s(storage):
    """2026-10-19: Gateway was back at 10:46:59, but the bot had backed off to
    five minutes and did not retry until 10:50:04."""
    _, broker, manager, _, clock, calls = _outage(storage, reconnects=False)
    manager._is_market_open = lambda: True
    await _drop(broker)
    await manager.poll_once()

    for _ in range(int(600 / 1.5)):
        clock.now += 1.5
        await manager.poll_once()

    gaps = [b - a for a, b in zip(calls, calls[1:])]
    assert max(gaps) <= RECONNECT_MAX_IN_SESSION_SECONDS + 1.5


async def test_outside_the_session_the_gap_grows_to_five_minutes(storage):
    _, broker, manager, _, clock, calls = _outage(storage, reconnects=False)
    await _drop(broker)
    await manager.poll_once()

    for _ in range(int(1800 / 1.5)):
        clock.now += 1.5
        await manager.poll_once()

    gaps = [b - a for a, b in zip(calls, calls[1:])]
    assert max(gaps) == pytest.approx(RECONNECT_MAX_SECONDS, abs=1.5)


async def test_the_open_bell_cuts_an_overnight_backoff_short(storage):
    _, broker, manager, _, clock, calls = _outage(storage, reconnects=False)
    await _drop(broker)
    await manager.poll_once()
    for _ in range(int(900 / 1.5)):  # backed off to the 5-minute gap
        clock.now += 1.5
        await manager.poll_once()

    manager._is_market_open = lambda: True
    opened_at, before = clock.now, len(calls)
    while len(calls) == before:
        clock.now += 1.5
        await manager.poll_once()

    assert calls[-1] - opened_at <= RECONNECT_MAX_IN_SESSION_SECONDS + 1.5


async def test_a_pasted_alert_connects_on_demand_instead_of_waiting(storage):
    """The trader is waiting on a paste, so it must not wait out the retry
    schedule when Gateway may already be back."""
    feed, broker, manager, pipeline, clock, calls = _outage(storage, reconnects=True)
    posted = []

    async def notify(note):
        posted.append(note)

    manager._notify = notify
    await _drop(broker)
    await manager.poll_once()  # outage begins; next scheduled retry is 10s away

    result = await enter(pipeline, feed)  # pasted 0s later

    assert calls                                   # connected right away
    assert has(result.notifications, "BUY")        # and traded
    assert "Broker reconnected" in titles(posted)  # and said so
    assert SPX_OCC in manager.managed


async def test_a_paste_while_gateway_is_still_down_explains_itself(storage):
    feed, broker, manager, pipeline, _, _ = _outage(storage, reconnects=False)

    async def refuse(spec):
        # What the real IBKR session does with no connection (2026-10-19 10:47).
        raise BrokerError("not connected to IB")

    feed.exists = refuse
    await _drop(broker)

    result = await enter(pipeline, feed)

    assert not has(result.notifications, "BUY —")
    assert any("not connected to IB" in n.description for n in result.notifications)


async def test_a_connection_restored_elsewhere_still_closes_the_outage(storage):
    _, broker, manager, _, clock, _ = _outage(storage, reconnects=False)
    await _drop(broker)
    await manager.poll_once()

    broker._connected = True  # e.g. the pipeline reconnected it
    notes = await manager.poll_once()

    assert "Broker reconnected" in titles(notes)


# --- averaging down, end to end -----------------------------------------
#
# enter() fills 21 SPX 7815P at the 0.48 ask. An average-down at 0.40 then
# buys 25 more ($1,000 / $40) and merges: 46 @ (21 x 0.48 + 25 x 0.40) / 46.

AVG_ONE_LINE = "avg SPX 7815P 0.40"
AVG_CARD = """AVERAGING DOWN — SPX 7815P · 0DTE
Averaging down on SPX Oct06 '26 7815 Put
Added 25 @ $0.40
Open Live Dashboard →

New Avg
$0.44
Total Contracts
50
Cost
$2,200
Trim Targets (new avg)
25%   $0.550
50%   $0.660
75%   $0.770
100%  $0.880

Not financial advice"""
MERGED_ENTRY = (21 * 0.48 + 25 * 0.40) / 46


async def _average_down(pipeline, feed, text=AVG_ONE_LINE, message_id=700_002):
    feed.set(0.39, 0.40)
    return await paste(pipeline, text=text, message_id=message_id)


async def test_an_average_down_buys_more_and_merges_into_the_position(storage):
    feed, broker, manager, pipeline, _ = build(storage)
    await enter(pipeline, feed)

    result = await _average_down(pipeline, feed)

    assert has(result.notifications, "AVERAGED DOWN")
    state = manager.managed[SPX_OCC].state
    assert state.remaining_qty == state.original_qty == 46
    assert state.entry_fill == pytest.approx(MERGED_ENTRY)
    (position,) = await broker.get_positions()
    assert position.qty == 46  # the broker agrees: one position, 46 contracts
    assert storage.open_position_count() == 1


async def test_the_full_averaging_down_card_works_the_same(storage):
    feed, _, manager, pipeline, _ = build(storage)
    await enter(pipeline, feed)

    result = await _average_down(pipeline, feed, text=AVG_CARD)

    assert has(result.notifications, "AVERAGED DOWN")
    assert has(result.notifications, "PARSED (AVG DOWN)")
    assert manager.managed[SPX_OCC].state.remaining_qty == 46


async def test_after_averaging_down_the_ladder_trims_off_the_new_average(storage):
    feed, broker, manager, pipeline, _ = build(storage)
    await enter(pipeline, feed)
    await _average_down(pipeline, feed)

    feed.set(round(MERGED_ENTRY * 1.26, 2))
    notes = await manager.poll_once()

    assert has(notes, "TRIM +25%")
    assert manager.managed[SPX_OCC].state.remaining_qty == 46 - 23  # half of the new 46


async def test_averaging_down_clears_a_break_even_stop_and_says_so(storage):
    """Held with a break-even stop at 0.48, the price falls to 0.40, and an
    average-down buys there. Kept, the stop would sell all 46 on the next
    quotes -- the opposite of what the trader asked for."""
    feed, _, manager, pipeline, _ = build(storage)
    await enter(pipeline, feed)
    feed.set(0.62)
    await manager.poll_once()  # +25% trim: 11 sold, stop to break-even 0.48
    held = manager.managed[SPX_OCC].state.remaining_qty
    assert manager.managed[SPX_OCC].state.stop_price == 0.48

    result = await _average_down(pipeline, feed)

    card = next(n for n in result.notifications if n.title.startswith("AVERAGED DOWN"))
    assert "Stop $0.48 cleared" in card.description
    state = manager.managed[SPX_OCC].state
    assert state.stop_price is None
    assert state.remaining_qty == held + 25

    for _ in range(3):
        assert not has(await manager.poll_once(), "SOLD ALL")


async def test_an_average_down_with_nothing_held_is_a_plain_buy(storage):
    feed, broker, manager, pipeline, _ = build(storage)

    result = await _average_down(pipeline, feed)

    assert has(result.notifications, "BUY —")
    assert not has(result.notifications, "AVERAGED DOWN")
    assert manager.managed[SPX_OCC].state.remaining_qty == 25


async def test_a_plain_buy_for_a_held_contract_is_still_skipped(storage):
    feed, broker, manager, pipeline, _ = build(storage)
    await enter(pipeline, feed)

    feed.set(0.39, 0.40)
    result = await paste(pipeline, text="SPX 7815P 0.40", message_id=700_003)

    assert has(result.notifications, "skipped")
    assert manager.managed[SPX_OCC].state.remaining_qty == 21


async def test_the_merged_position_survives_a_restart(storage):
    feed, broker, manager, pipeline, _ = build(storage)
    await enter(pipeline, feed)
    await _average_down(pipeline, feed)

    settings = make_settings(mode="paper")
    reborn = PositionManager(settings, storage, RiskGate(settings, storage), broker)
    reborn.today = lambda: TRADING_DAY
    _, mismatched = await reborn.reconcile()

    state = reborn.managed[SPX_OCC].state
    assert mismatched is False
    assert state.remaining_qty == state.original_qty == 46
    assert state.entry_fill == pytest.approx(MERGED_ENTRY)


# --- the P&L channel --------------------------------------------------------


def _with_pnl_channel(storage):
    feed, broker, manager, pipeline, risk = build(storage)
    cards = []

    async def pnl(note):
        cards.append(note)

    manager._notify_pnl = pnl
    return feed, broker, manager, pipeline, risk, cards


def _fields(card):
    return {name: value for name, value, _ in card.fields}


async def test_every_trim_posts_one_pnl_card(storage):
    feed, _, manager, pipeline, _, cards = _with_pnl_channel(storage)
    await enter(pipeline, feed)  # 21 @ 0.48

    feed.set(0.62)  # +25%: sells 11
    await manager.poll_once()

    (card,) = cards
    assert card.title.startswith("TRIM +25%")
    assert _fields(card) == {"Trade P&L": "+$154.00", "Day P&L": "+$154.00"}  # 11 x 0.14


async def test_the_day_total_accumulates_across_sales(storage):
    feed, _, manager, pipeline, risk, cards = _with_pnl_channel(storage)
    await enter(pipeline, feed)

    feed.set(0.62)
    await manager.poll_once()   # +25%: 11 x 0.14 = +154
    feed.set(0.74)
    await manager.poll_once()   # +50%: 6 x 0.26 = +156

    assert [c.title.split(" —")[0] for c in cards] == ["TRIM +25%", "TRIM +50%"]
    assert _fields(cards[1]) == {"Trade P&L": "+$156.00", "Day P&L": "+$310.00"}
    assert risk.day_summary(TRADING_DAY)["realized_pnl"] == pytest.approx(310.0)


async def test_a_stop_out_posts_its_pnl(storage):
    feed, _, manager, pipeline, _, cards = _with_pnl_channel(storage)
    await enter(pipeline, feed)
    feed.set(0.62)
    await manager.poll_once()   # trim, stop to break-even 0.48
    feed.set(0.45)
    await manager.poll_once()
    feed.set(0.44)
    await manager.poll_once()   # stopped out

    stop = cards[-1]
    assert stop.title.startswith("STOPPED OUT")
    assert _fields(stop)["Trade P&L"].startswith("-$")  # 10 sold under the 0.48 entry


async def test_a_flatten_posts_its_pnl(storage):
    feed, _, manager, pipeline, _, cards = _with_pnl_channel(storage)
    await enter(pipeline, feed)
    feed.set(0.50)

    await manager.flatten_all()

    (card,) = cards
    assert card.title.startswith("FLATTENED")


async def test_without_a_pnl_channel_nothing_extra_is_posted(storage):
    feed, _, manager, pipeline, _ = build(storage)
    await enter(pipeline, feed)

    feed.set(0.62)
    notes = await manager.poll_once()

    assert not any(n.title.startswith("TRIM +25%") and "Day P&L" in _fields(n) for n in notes)


async def test_a_failing_pnl_post_never_stops_the_exit(storage):
    feed, broker, manager, pipeline, _ = build(storage)

    async def broken(note):
        raise RuntimeError("discord is down")

    manager._notify_pnl = broken
    await enter(pipeline, feed)
    feed.set(0.62)

    await manager.poll_once()

    assert manager.managed[SPX_OCC].state.remaining_qty == 10  # the trim still happened


# --- a runner card at every +25% -------------------------------------------
#
# enter() fills at 0.48, so +125% is 1.08, +150% is 1.20, +175% is 1.32.


def _stepped(storage):
    return build(storage, trim={"runner_levels": [], "runner_step_pct": 25})


def _runner_cards(notes):
    return [n for n in notes if "RUNNER +" in (n.title + n.description)]


async def test_every_25_percent_gets_its_own_card(storage):
    feed, _, manager, pipeline, _ = _stepped(storage)
    await enter(pipeline, feed)

    seen = []
    for bid in (0.60, 0.72, 0.84, 0.96, 1.08, 1.20, 1.32):
        feed.set(bid)
        seen += _runner_cards(await manager.poll_once())

    assert len(seen) == 5  # +75, +100, +125, +150, +175
    assert "RUNNER +125%" in (seen[2].title + seen[2].description)


async def test_a_jump_through_several_steps_is_one_card(storage):
    feed, _, manager, pipeline, _ = _stepped(storage)
    await enter(pipeline, feed)
    feed.set(0.72)
    await manager.poll_once()   # the two trims

    feed.set(1.32)              # +175% in one quote: crosses 75 .. 175
    cards = _runner_cards(await manager.poll_once())

    assert len(cards) == 1
    assert "RUNNER +175%" in (cards[0].title + cards[0].description)
    assert {75, 100, 125, 150, 175} <= manager.managed[SPX_OCC].state.fired_levels


async def test_the_skipped_steps_are_not_announced_later(storage):
    feed, _, manager, pipeline, _ = _stepped(storage)
    await enter(pipeline, feed)
    feed.set(1.32)
    await manager.poll_once()

    feed.set(1.10)              # back down through +125%
    feed.set(1.33)
    assert _runner_cards(await manager.poll_once()) == []
