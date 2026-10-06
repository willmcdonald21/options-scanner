"""The stop ladder, end to end against a fake IBKR.

Two ways a target gets reached: one of our own limit orders fills (the
normal case), or a market print crosses a rung with no limit resting
above it (a runner). Both must leave every remaining stop at the rung
below the one reached, and never lower than it already was.
"""

from datetime import date

import pytest
from conftest import FakeIB, FakeNotifier

from bot.execution import handle_event
from bot.fill_watcher import FillWatcher
from bot.models import BuyEvent, OptionKey, TrimTarget
from config.settings import RiskConfig

ENTRY = 1.905
TARGETS = (
    TrimTarget(0.25, 2.381),
    TrimTarget(0.50, 2.858),
    TrimTarget(0.75, 3.334),
    TrimTarget(1.00, 3.810),
)


def _option() -> OptionKey:
    return OptionKey("SPY", date(2026, 9, 30), 764.0, "C")


def _buy(message_id: int = 1) -> BuyEvent:
    return BuyEvent(
        message_id=message_id,
        option=_option(),
        entry_price=ENTRY,
        contracts=25,
        cost=4763.0,
        trim_targets=TARGETS,
    )


@pytest.fixture
def trade_notifier():
    return FakeNotifier()


def _bracketed(store, notifier, cap: float):
    """Fills an entry and rests its exit structure, returning the pieces a
    test needs to drive fills afterwards."""
    risk = RiskConfig(max_usd_per_trade=cap, stop_loss_pct=0.30)
    ib = FakeIB(fill_status="Filled", avg_fill_price=ENTRY)
    handle_event(_buy(), ib, store, notifier, risk)
    position = store.get_open(_option())
    return ib, risk, position


def _watcher(ib, store, risk, notifier, trade_notifier):
    return FillWatcher(ib=ib, store=store, risk=risk, notifier=notifier, trade_notifier=trade_notifier)


def _live_stops(ib, store, position_id):
    """Current trigger price of every stop still working at IBKR."""
    book = {o.orderId: o for o in ib.orders()}
    return [
        book[leg.stp_order_id].auxPrice
        for leg in store.list_legs(position_id, only_live=True)
        if leg.stp_order_id in book
    ]


# --- a target filling ------------------------------------------------------


def test_first_target_filling_moves_every_remaining_stop_to_breakeven(store, notifier, trade_notifier):
    ib, risk, position = _bracketed(store, notifier, cap=1000.0)  # 5 contracts, 2/1/1/1
    watcher = _watcher(ib, store, risk, notifier, trade_notifier)
    assert _live_stops(ib, store, position.id) == [1.33, 1.33, 1.33, 1.33]

    tier0 = store.list_legs(position.id)[0]
    assert watcher.handle_fill(tier0.lmt_order_id, 2.38) is True

    # Three tranches left, all now stopped at entry -- the position can no
    # longer lose money, which is the whole point of the ladder.
    assert _live_stops(ib, store, position.id) == [1.90, 1.90, 1.90]
    assert store.get_open(_option()).user_remaining_qty == 3
    assert store.get_open(_option()).current_stop_price == 1.90


def test_each_further_target_locks_in_the_rung_below_it(store, notifier, trade_notifier):
    ib, risk, position = _bracketed(store, notifier, cap=1000.0)
    watcher = _watcher(ib, store, risk, notifier, trade_notifier)
    legs = store.list_legs(position.id)

    watcher.handle_fill(legs[0].lmt_order_id, 2.38)
    assert _live_stops(ib, store, position.id) == [1.90] * 3  # breakeven

    watcher.handle_fill(legs[1].lmt_order_id, 2.86)
    assert _live_stops(ib, store, position.id) == [2.38] * 2  # +25% locked

    watcher.handle_fill(legs[2].lmt_order_id, 3.33)
    assert _live_stops(ib, store, position.id) == [2.86]  # +50% locked


def test_a_stop_is_modified_in_place_never_cancelled_and_replaced(store, notifier, trade_notifier):
    """Cancelling a stop to re-place it higher would leave the position
    briefly naked, so the ratchet reuses the same IBKR order id."""
    ib, risk, position = _bracketed(store, notifier, cap=1000.0)
    watcher = _watcher(ib, store, risk, notifier, trade_notifier)
    legs = store.list_legs(position.id)
    stop_ids_before = {leg.stp_order_id for leg in legs if leg.tier_index > 0}

    watcher.handle_fill(legs[0].lmt_order_id, 2.38)

    still_live = {leg.stp_order_id for leg in store.list_legs(position.id, only_live=True)}
    assert still_live == stop_ids_before
    assert ib.cancelled_order_ids == []


def test_the_whole_position_exits_once_the_last_target_fills(store, notifier, trade_notifier):
    ib, risk, position = _bracketed(store, notifier, cap=1000.0)
    watcher = _watcher(ib, store, risk, notifier, trade_notifier)

    for leg in store.list_legs(position.id):
        watcher.handle_fill(leg.lmt_order_id, leg.tp_price)

    assert store.get_open(_option()) is None
    assert store.get_position_by_id(position.id).user_remaining_qty == 0


def test_a_stop_filling_reduces_the_position_and_reports_it(store, notifier, trade_notifier):
    ib, risk, position = _bracketed(store, notifier, cap=1000.0)
    watcher = _watcher(ib, store, risk, notifier, trade_notifier)
    leg = store.list_legs(position.id)[0]

    watcher.handle_fill(leg.stp_order_id, 1.33)

    assert store.get_open(_option()).user_remaining_qty == 3
    assert any("STOPPED OUT" in a for a in trade_notifier.alerts)


def test_a_repeated_fill_status_for_the_same_order_changes_nothing(store, notifier, trade_notifier):
    """IBKR can report a terminal status more than once."""
    ib, risk, position = _bracketed(store, notifier, cap=1000.0)
    watcher = _watcher(ib, store, risk, notifier, trade_notifier)
    leg = store.list_legs(position.id)[0]

    watcher.handle_fill(leg.lmt_order_id, 2.38)
    remaining_after_first = store.get_open(_option()).user_remaining_qty
    watcher.handle_fill(leg.lmt_order_id, 2.38)

    assert store.get_open(_option()).user_remaining_qty == remaining_after_first


def test_a_fill_on_an_order_we_do_not_own_is_ignored(store, notifier, trade_notifier):
    ib, risk, position = _bracketed(store, notifier, cap=1000.0)
    watcher = _watcher(ib, store, risk, notifier, trade_notifier)

    assert watcher.handle_fill(999_999, 5.0) is False


# --- a runner, which has no limit to fill ---------------------------------


def test_a_runners_stop_climbs_as_the_price_crosses_each_rung(store, notifier, trade_notifier):
    ib, risk, position = _bracketed(store, notifier, cap=200.0)  # 1 contract, runner only
    watcher = _watcher(ib, store, risk, notifier, trade_notifier)
    assert _live_stops(ib, store, position.id) == [1.33]

    watcher.handle_price(position.id, 2.40)  # through +25%
    assert _live_stops(ib, store, position.id) == [1.90]  # breakeven

    watcher.handle_price(position.id, 2.90)  # through +50%
    assert _live_stops(ib, store, position.id) == [2.38]

    watcher.handle_price(position.id, 3.85)  # through +100%
    assert _live_stops(ib, store, position.id) == [3.33]


def test_a_runner_keeps_ratcheting_past_the_last_published_target(store, notifier, trade_notifier):
    ib, risk, position = _bracketed(store, notifier, cap=200.0)
    watcher = _watcher(ib, store, risk, notifier, trade_notifier)

    watcher.handle_price(position.id, ENTRY * 2.30)  # past +125%

    assert _live_stops(ib, store, position.id) == [round(ENTRY * 2.0, 2)]


def test_freezing_the_ladder_caps_a_runner_at_the_last_target(store, notifier, trade_notifier):
    ib, _, position = _bracketed(store, notifier, cap=200.0)
    frozen = RiskConfig(max_usd_per_trade=200.0, stop_loss_pct=0.30, runner_ladder_extends=False)
    watcher = _watcher(ib, store, frozen, notifier, trade_notifier)

    watcher.handle_price(position.id, ENTRY * 3)  # way past the ladder

    assert _live_stops(ib, store, position.id) == [3.33]  # the +75% rung, not beyond


def test_a_price_dip_never_walks_the_stop_back_down(store, notifier, trade_notifier):
    ib, risk, position = _bracketed(store, notifier, cap=200.0)
    watcher = _watcher(ib, store, risk, notifier, trade_notifier)

    watcher.handle_price(position.id, 3.40)  # up through +75%
    high_water = _live_stops(ib, store, position.id)
    assert watcher.handle_price(position.id, 1.95) is None  # all the way back down
    assert _live_stops(ib, store, position.id) == high_water


def test_price_below_the_first_rung_leaves_the_initial_stop_alone(store, notifier, trade_notifier):
    ib, risk, position = _bracketed(store, notifier, cap=200.0)
    watcher = _watcher(ib, store, risk, notifier, trade_notifier)

    assert watcher.handle_price(position.id, 2.10) is None
    assert _live_stops(ib, store, position.id) == [1.33]


def test_a_closed_position_stops_reacting_to_prices(store, notifier, trade_notifier):
    ib, risk, position = _bracketed(store, notifier, cap=200.0)
    watcher = _watcher(ib, store, risk, notifier, trade_notifier)
    leg = store.list_legs(position.id)[0]
    watcher.handle_fill(leg.stp_order_id, 1.33)

    assert watcher.handle_price(position.id, 4.00) is None


def test_two_contracts_ratchet_the_runner_after_the_first_target_fills(store, notifier, trade_notifier):
    """The user's rule for 2 contracts: one sells at the first target, the
    other then behaves exactly as a lone contract would."""
    ib, risk, position = _bracketed(store, notifier, cap=400.0)
    watcher = _watcher(ib, store, risk, notifier, trade_notifier)
    target_leg = next(leg for leg in store.list_legs(position.id) if leg.tp_price is not None)

    watcher.handle_fill(target_leg.lmt_order_id, 2.38)
    assert _live_stops(ib, store, position.id) == [1.90]  # runner at breakeven
    assert store.get_open(_option()).user_remaining_qty == 1

    watcher.handle_price(position.id, 2.90)  # runner carries on up the ladder
    assert _live_stops(ib, store, position.id) == [2.38]


# --- restart ---------------------------------------------------------------


def test_reconciliation_is_quiet_when_every_leg_is_still_at_ibkr(store, notifier, trade_notifier):
    ib, risk, position = _bracketed(store, notifier, cap=1000.0)
    watcher = _watcher(ib, store, risk, notifier, trade_notifier)

    assert watcher.reconcile() == []
    assert notifier.alerts == []


def test_reconciliation_names_a_position_whose_brackets_vanished(store, notifier, trade_notifier):
    ib, risk, position = _bracketed(store, notifier, cap=1000.0)
    watcher = _watcher(ib, store, risk, notifier, trade_notifier)
    for order in ib.orders_of("SELL"):
        ib.cancelOrder(order)  # simulate brackets lost while the bot was down

    problems = watcher.reconcile()

    assert len(problems) == 4
    assert len(notifier.alerts) == 1
    assert "may be unprotected" in notifier.alerts[0]
    assert "SPY" in notifier.alerts[0]


def test_missing_market_data_downgrades_and_alerts_once(store, notifier, trade_notifier):
    ib, risk, _ = _bracketed(store, notifier, cap=200.0)
    watcher = _watcher(ib, store, risk, notifier, trade_notifier)

    watcher._on_error(1, 354, "Requested market data is not subscribed.", None)
    watcher._on_error(2, 354, "Requested market data is not subscribed.", None)

    assert len(notifier.alerts) == 1
    assert "delayed data" in notifier.alerts[0]


# --- STP_LMT, the opt-in stop variant -------------------------------------


def test_stop_limit_mode_prices_its_limit_below_the_trigger(store, notifier, trade_notifier):
    risk = RiskConfig(max_usd_per_trade=1000.0, stop_loss_pct=0.30, stop_order_type="STP_LMT")
    ib = FakeIB(fill_status="Filled", avg_fill_price=ENTRY)
    handle_event(_buy(), ib, store, notifier, risk)

    stops = [o for o in ib.placed_orders if o[1].orderType == "STP LMT"]
    assert stops, "STP_LMT mode should place stop-limit orders"
    for _, order in stops:
        assert order.auxPrice == 1.33  # trigger
        assert order.lmtPrice == 1.20  # 10% below it, so it can still fill


def test_stop_limit_orders_keep_their_offset_as_the_ladder_ratchets(store, notifier, trade_notifier):
    risk = RiskConfig(max_usd_per_trade=1000.0, stop_loss_pct=0.30, stop_order_type="STP_LMT")
    ib = FakeIB(fill_status="Filled", avg_fill_price=ENTRY)
    handle_event(_buy(), ib, store, notifier, risk)
    position = store.get_open(_option())
    watcher = _watcher(ib, store, risk, notifier, trade_notifier)

    watcher.handle_fill(store.list_legs(position.id)[0].lmt_order_id, 2.38)

    book = {o.orderId: o for o in ib.orders()}
    live = [book[leg.stp_order_id] for leg in store.list_legs(position.id, only_live=True)]
    assert all(o.auxPrice == 1.90 for o in live)  # breakeven trigger
    assert all(o.lmtPrice == 1.71 for o in live)  # offset carried along
