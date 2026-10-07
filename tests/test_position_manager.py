"""The managed-position loop: entry through trims, trail, and exit.

Runs the real pipeline, the real rules engine and the real PaperBroker over a
scripted price path. Nothing is mocked except the prices themselves.
"""

from datetime import date, datetime

from conftest import SPX_OCC, TRADING_DAY, make_settings, paste

from options_scanner.broker.base import Quote
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

    assert has(result.notifications, "Entry filled")
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

    assert has(notes, "Trim +25%")
    assert has(notes, "Stop moved up")
    state = manager.managed[SPX_OCC].state
    assert state.remaining_qty == 15  # sold ceil(21*0.25) = 6
    assert state.stop_price == 0.48
    assert state.stop_reason == "breakeven"
    (position,) = await broker.get_positions()
    assert position.qty == 15  # the broker agrees


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
    assert state.stop_reason == "trail"


async def test_the_hundred_percent_rung_sells_nothing(storage):
    feed, _, manager, pipeline, _ = build(storage)
    await enter(pipeline, feed)

    feed.set(0.86)  # clears +25/+50/+75 in one gap
    await manager.poll_once()
    before = manager.managed[SPX_OCC].state.remaining_qty

    feed.set(1.00)  # clears +100%
    await manager.poll_once()

    state = manager.managed[SPX_OCC].state
    assert 100 in state.fired_levels
    assert state.remaining_qty == before


async def test_a_gap_through_several_rungs_fires_each_one(storage):
    feed, _, manager, pipeline, _ = build(storage)
    await enter(pipeline, feed)

    feed.set(0.90)
    notes = await manager.poll_once()

    assert has(notes, "Trim +25%")
    assert has(notes, "Trim +50%")
    assert has(notes, "Trim +75%")
    assert manager.managed[SPX_OCC].state.remaining_qty == 8


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
    assert not has(first, "Stopped out")

    feed.set(0.44)
    second = await manager.poll_once()
    assert has(second, "Stopped out")


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

    assert not has(notes, "Stopped out")
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

    assert has(notes, "Stopped out")
    assert manager.managed == {}
    assert await broker.get_positions() == []
    assert storage.open_position_count() == 0
    assert feed.released == [SPX_OCC]  # market-data line handed back


async def test_a_trailed_stop_exits_above_breakeven(storage):
    feed, _, manager, pipeline, _ = build(storage)
    await enter(pipeline, feed)

    feed.set(0.90)
    await manager.poll_once()      # trims, arms the trail
    stop = manager.managed[SPX_OCC].state.stop_price
    assert stop > 0.48

    feed.set(stop - 0.02)
    await manager.poll_once()
    notes = await manager.poll_once()

    assert has(notes, "Stopped out")


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
    await reborn.reconcile()

    feed.set(0.74)
    notes = await reborn.poll_once()

    assert has(notes, "Trim +50%")
    assert not has(notes, "Trim +25%"), "the rung fired before the restart must not re-fire"
    assert reborn.managed[SPX_OCC].state.fired_levels >= {25, 50}


async def test_reconciliation_reports_a_position_the_broker_does_not_have(storage):
    feed, broker, manager, pipeline, _ = build(storage)
    await enter(pipeline, feed)

    broker.drop_position(SPX_OCC)  # as an account-wide flatten would
    settings = make_settings(mode="paper")
    reborn = PositionManager(settings, storage, RiskGate(settings, storage), broker)
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

    assert has(notes, "position closed")
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
