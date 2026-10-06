from datetime import date

import pytest
from conftest import FakeIB, FakeNotifier

from bot.execution import handle_event
from bot.models import (
    BuyEvent,
    ExpiredEvent,
    InfoEvent,
    OpenPosition,
    OptionKey,
    SoldAllEvent,
    TrimEvent,
    TrimTarget,
    UnderlyingKey,
    UnknownEvent,
)
from config.settings import RiskConfig


def _qqq_option() -> OptionKey:
    return OptionKey("QQQ", date(2026, 9, 23), 740.0, "C")


def _seeded_position(store, *, channel_total=20, channel_remaining=20, user_total=4, user_remaining=4):
    option = _qqq_option()
    _seeded_position.last_id = store.create_open(
        OpenPosition(
            option=option,
            channel_total_qty=channel_total,
            channel_remaining_qty=channel_remaining,
            user_original_qty=user_total,
            user_remaining_qty=user_remaining,
            entry_price=0.885,
            ibkr_order_id_entry=1,
        )
    )
    return option


# --- BuyEvent -----------------------------------------------------------


def test_buy_event_sizes_off_risk_cap_not_channel_contracts(store, notifier, risk):
    ib = FakeIB(fill_status="Filled", avg_fill_price=1.215)
    event = BuyEvent(message_id=1, option=_qqq_option(), entry_price=1.215, contracts=999, cost=999 * 121.5)

    handle_event(event, ib, store, notifier, risk)

    expected_contracts = int(1000.0 // (1.215 * 100))  # compute_contracts, not event.contracts (999)
    assert expected_contracts != 999
    buys = ib.orders_of("BUY")
    assert len(buys) == 1
    assert buys[0].totalQuantity == expected_contracts

    position = store.get_open(_qqq_option())
    assert position is not None
    assert position.user_remaining_qty == expected_contracts
    assert position.channel_total_qty == 999  # channel's own size, kept as the trim baseline
    assert store.already_processed(1) is True
    assert notifier.alerts == []


def test_buy_event_qualify_failure_alerts_and_skips_order(store, notifier, risk):
    ib = FakeIB(qualify_ok=False)
    event = BuyEvent(message_id=2, option=_qqq_option(), entry_price=1.0, contracts=10, cost=1000.0)

    handle_event(event, ib, store, notifier, risk)

    assert ib.placed_orders == []
    assert store.get_open(_qqq_option()) is None
    assert len(notifier.alerts) == 1
    assert store.already_processed(2) is True


def test_buy_event_rejected_does_not_open_position(store, notifier, risk):
    ib = FakeIB(fill_status="Cancelled")
    event = BuyEvent(message_id=3, option=_qqq_option(), entry_price=1.0, contracts=10, cost=1000.0)

    handle_event(event, ib, store, notifier, risk)

    assert store.get_open(_qqq_option()) is None
    assert len(notifier.alerts) == 1
    assert store.already_processed(3) is True


def test_buy_event_timeout_does_not_open_position(store, notifier, risk):
    ib = FakeIB(fill_status="Submitted")
    event = BuyEvent(message_id=4, option=_qqq_option(), entry_price=1.0, contracts=10, cost=1000.0)

    handle_event(event, ib, store, notifier, risk, fill_timeout_s=0.05)

    assert store.get_open(_qqq_option()) is None
    assert len(notifier.alerts) == 1
    assert "did not confirm" in notifier.alerts[0]
    assert store.already_processed(4) is True


# --- TrimEvent ------------------------------------------------------------


def test_trim_event_places_no_orders_and_leaves_our_size_alone(store, notifier):
    """Our own targets are already resting at IBKR, so mirroring the
    channel's trim here would sell on top of them."""
    ib = FakeIB(fill_status="Filled", avg_fill_price=1.328)
    _seeded_position(store, channel_total=20, channel_remaining=20, user_total=4, user_remaining=4)
    event = TrimEvent(
        message_id=5,
        underlying=UnderlyingKey("QQQ", 740.0, "C"),
        tier_pct=0.75,
        sold_this_event=12,
        channel_total_before=20,
        channel_remaining_after=8,
        avg_exit_price=1.328,
    )

    trade_notifier = FakeNotifier()
    handle_event(event, ib, store, notifier, RiskConfig(max_usd_per_trade=1000.0), trade_notifier=trade_notifier)

    assert ib.placed_orders == []
    position = store.get_open(_qqq_option())
    assert position.user_remaining_qty == 4  # untouched
    assert position.channel_remaining_qty == 8  # channel bookkeeping still current
    assert store.already_processed(5) is True
    assert notifier.alerts == []


def test_trim_event_zero_qty_still_updates_channel_bookkeeping_no_order(store, notifier):
    ib = FakeIB()
    _seeded_position(store, channel_total=50, channel_remaining=50, user_total=1, user_remaining=1)
    event = TrimEvent(
        message_id=6,
        underlying=UnderlyingKey("QQQ", 740.0, "C"),
        tier_pct=0.25,
        sold_this_event=1,
        channel_total_before=50,
        channel_remaining_after=49,
        avg_exit_price=1.0,
    )

    handle_event(event, ib, store, notifier, RiskConfig(max_usd_per_trade=1000.0))

    assert ib.placed_orders == []
    position = store.get_open(_qqq_option())
    assert position.user_remaining_qty == 1  # unchanged
    assert position.channel_remaining_qty == 49  # still updated
    assert notifier.alerts == []
    assert store.already_processed(6) is True


def test_trim_event_no_matching_position_is_not_an_alert(store, notifier):
    ib = FakeIB()
    event = TrimEvent(
        message_id=7,
        underlying=UnderlyingKey("QQQ", 740.0, "C"),
        tier_pct=0.25,
        sold_this_event=1,
        channel_total_before=10,
        channel_remaining_after=9,
        avg_exit_price=1.0,
    )

    handle_event(event, ib, store, notifier, RiskConfig(max_usd_per_trade=1000.0))

    # Our own stop or targets may well have closed the position before the
    # channel got around to trimming it -- normal, not worth an alert.
    assert ib.placed_orders == []
    assert notifier.alerts == []
    assert store.already_processed(7) is True


def test_trim_event_ambiguous_match_alerts_and_stays_unprocessed(store, notifier):
    ib = FakeIB()
    store.create_open(OpenPosition(OptionKey("QQQ", date(2026, 9, 23), 740.0, "C"), 10, 10, 4, 4, 1.0, 1))
    store.create_open(OpenPosition(OptionKey("QQQ", date(2026, 9, 25), 740.0, "C"), 5, 5, 2, 2, 1.0, 2))
    event = TrimEvent(
        message_id=8,
        underlying=UnderlyingKey("QQQ", 740.0, "C"),
        tier_pct=0.25,
        sold_this_event=1,
        channel_total_before=10,
        channel_remaining_after=9,
        avg_exit_price=1.0,
    )

    handle_event(event, ib, store, notifier, RiskConfig(max_usd_per_trade=1000.0))

    assert ib.placed_orders == []
    assert len(notifier.alerts) == 1
    assert store.already_processed(8) is False


# --- SoldAllEvent ---------------------------------------------------------


def test_sold_all_sells_remaining_and_closes(store, notifier):
    ib = FakeIB(fill_status="Filled", avg_fill_price=0.9275)
    _seeded_position(store, channel_total=15, channel_remaining=0, user_total=3, user_remaining=3)
    event = SoldAllEvent(message_id=9, underlying=UnderlyingKey("QQQ", 740.0, "C"), realized_pct=0.048, avg_exit_price=0.9275)

    trade_notifier = FakeNotifier()
    handle_event(event, ib, store, notifier, RiskConfig(max_usd_per_trade=1000.0), trade_notifier=trade_notifier)

    _, order = ib.placed_orders[0]
    assert order.action == "SELL"
    assert order.totalQuantity == 3
    assert store.get_open(_qqq_option()) is None
    assert notifier.alerts == []
    assert store.already_processed(9) is True
    assert len(trade_notifier.alerts) == 1
    assert "SOLD ALL 3x" in trade_notifier.alerts[0]


def test_sold_all_already_flat_closes_without_order(store, notifier):
    ib = FakeIB()
    _seeded_position(store, channel_total=15, channel_remaining=0, user_total=3, user_remaining=0)
    event = SoldAllEvent(message_id=10, underlying=UnderlyingKey("QQQ", 740.0, "C"), realized_pct=0.05, avg_exit_price=0.9)

    handle_event(event, ib, store, notifier, RiskConfig(max_usd_per_trade=1000.0))

    assert ib.placed_orders == []
    assert store.get_open(_qqq_option()) is None
    assert notifier.alerts == []


def test_sold_all_rejected_leaves_position_open(store, notifier):
    ib = FakeIB(fill_status="Cancelled")
    _seeded_position(store, channel_total=15, channel_remaining=0, user_total=3, user_remaining=3)
    event = SoldAllEvent(message_id=11, underlying=UnderlyingKey("QQQ", 740.0, "C"), realized_pct=0.05, avg_exit_price=0.9)

    handle_event(event, ib, store, notifier, RiskConfig(max_usd_per_trade=1000.0))

    position = store.get_open(_qqq_option())
    assert position is not None
    assert position.status == "OPEN"
    assert len(notifier.alerts) == 1
    assert store.already_processed(11) is True


# --- ExpiredEvent ----------------------------------------------------------


def test_expired_closes_without_ever_placing_an_order(store, notifier):
    ib = FakeIB()
    _seeded_position(store, channel_total=15, channel_remaining=0, user_total=3, user_remaining=3)
    event = ExpiredEvent(message_id=12, underlying=UnderlyingKey("QQQ", 740.0, "C"), won=True)

    handle_event(event, ib, store, notifier, RiskConfig(max_usd_per_trade=1000.0))

    assert ib.placed_orders == []
    assert store.get_open(_qqq_option()) is None
    assert store.already_processed(12) is True


# --- InfoEvent / UnknownEvent ----------------------------------------------


@pytest.mark.parametrize("kind", ["milestone", "averaging_down", "new_alert"])
def test_info_event_never_mutates_or_alerts(store, notifier, kind):
    ib = FakeIB()
    event = InfoEvent(message_id=13, kind=kind, raw_title="whatever")

    handle_event(event, ib, store, notifier, RiskConfig(max_usd_per_trade=1000.0))

    assert ib.placed_orders == []
    assert notifier.alerts == []
    assert store.already_processed(13) is True


def test_unknown_event_alerts_and_marks_processed(store, notifier):
    ib = FakeIB()
    event = UnknownEvent(message_id=14, raw_embed={}, reason="new title format")

    handle_event(event, ib, store, notifier, RiskConfig(max_usd_per_trade=1000.0))

    assert ib.placed_orders == []
    assert len(notifier.alerts) == 1
    assert store.already_processed(14) is True


# --- Idempotency ------------------------------------------------------------


def test_second_call_with_same_message_id_is_a_no_op(store, notifier, risk):
    ib = FakeIB(fill_status="Filled", avg_fill_price=1.0)
    event = BuyEvent(message_id=15, option=_qqq_option(), entry_price=1.0, contracts=10, cost=1000.0)

    handle_event(event, ib, store, notifier, risk)
    first_pass = len(ib.placed_orders)
    assert len(ib.orders_of("BUY")) == 1

    handle_event(event, ib, store, notifier, risk)
    assert len(ib.placed_orders) == first_pass  # no second entry, no duplicate brackets
    assert len(ib.orders_of("BUY")) == 1


# --- the exit structure placed at entry -----------------------------------


def _spy_option() -> OptionKey:
    return OptionKey("SPY", date(2026, 9, 30), 764.0, "C")


def _spy_buy(message_id: int = 100, contracts: int = 25) -> BuyEvent:
    """The SPY 764C card from the live desk, ladder included."""
    return BuyEvent(
        message_id=message_id,
        option=_spy_option(),
        entry_price=1.905,
        contracts=contracts,
        cost=4763.0,
        trim_targets=(
            TrimTarget(0.25, 2.381),
            TrimTarget(0.50, 2.858),
            TrimTarget(0.75, 3.334),
            TrimTarget(1.00, 3.810),
        ),
    )


def test_buy_rests_a_limit_and_a_stop_for_every_tranche(store, notifier):
    # $1000 cap / $190.50 per contract = 5 contracts -> 2/1/1/1 across the targets.
    risk = RiskConfig(max_usd_per_trade=1000.0, stop_loss_pct=0.30)
    ib = FakeIB(fill_status="Filled", avg_fill_price=1.905)

    handle_event(_spy_buy(), ib, store, notifier, risk)

    limits = ib.orders_of("SELL", "LMT")
    stops = ib.orders_of("SELL", "STP")
    assert [(o.totalQuantity, o.lmtPrice) for o in limits] == [(2, 2.38), (1, 2.86), (1, 3.33), (1, 3.81)]
    assert [(o.totalQuantity, o.auxPrice) for o in stops] == [(2, 1.33), (1, 1.33), (1, 1.33), (1, 1.33)]
    assert notifier.alerts == []


def test_each_limit_is_oca_paired_with_exactly_one_stop(store, notifier):
    """A filled target must take its own stop off the book and nothing
    else -- that is what keeps every tranche quantity immutable."""
    risk = RiskConfig(max_usd_per_trade=1000.0)
    ib = FakeIB(fill_status="Filled", avg_fill_price=1.905)

    handle_event(_spy_buy(), ib, store, notifier, risk)

    groups = {}
    for order in ib.orders_of("SELL"):
        groups.setdefault(order.ocaGroup, []).append(order.orderType)
    assert all(sorted(types) == ["LMT", "STP"] for types in groups.values())
    assert all(o.ocaType == 1 for o in ib.orders_of("SELL"))  # cancel-all


def test_exit_orders_outlive_the_session_that_placed_them(store, notifier):
    """A DAY bracket is purged at the close, which would leave an
    overnight position unprotected the next morning."""
    ib = FakeIB(fill_status="Filled", avg_fill_price=1.905)

    handle_event(_spy_buy(), ib, store, notifier, RiskConfig(max_usd_per_trade=10_000.0))

    assert all(o.tif == "GTC" for o in ib.orders_of("SELL"))


def test_a_single_contract_gets_a_stop_and_no_limit(store, notifier):
    # $200 cap / $1.905 -> 1 contract, so it rides as a runner.
    ib = FakeIB(fill_status="Filled", avg_fill_price=1.905)

    handle_event(_spy_buy(), ib, store, notifier, RiskConfig(max_usd_per_trade=200.0))

    assert ib.orders_of("SELL", "LMT") == []
    assert [o.totalQuantity for o in ib.orders_of("SELL", "STP")] == [1]
    position = store.get_open(_spy_option())
    assert position.runner_qty == 1


def test_two_contracts_rest_one_target_and_hold_one_runner(store, notifier):
    ib = FakeIB(fill_status="Filled", avg_fill_price=1.905)

    handle_event(_spy_buy(), ib, store, notifier, RiskConfig(max_usd_per_trade=400.0))

    assert [(o.totalQuantity, o.lmtPrice) for o in ib.orders_of("SELL", "LMT")] == [(1, 2.38)]
    assert [o.totalQuantity for o in ib.orders_of("SELL", "STP")] == [1, 1]
    assert store.get_open(_spy_option()).runner_qty == 1


def test_the_ladder_is_rebased_on_our_own_fill_not_the_channels_entry(store, notifier):
    """We get our own fill price on a market order. Keeping the channel's
    percentages but re-pricing off our fill makes "+25%" mean +25% for us."""
    ib = FakeIB(fill_status="Filled", avg_fill_price=2.000)  # we paid more than the card's 1.905

    handle_event(_spy_buy(), ib, store, notifier, RiskConfig(max_usd_per_trade=10_000.0))

    limits = sorted(o.lmtPrice for o in ib.orders_of("SELL", "LMT"))
    assert limits == [2.50, 3.00, 3.50, 4.00]  # 2.00 * 1.25 / 1.5 / 1.75 / 2.0


def test_buy_with_no_published_ladder_is_still_bracketed(store, notifier):
    event = BuyEvent(message_id=101, option=_spy_option(), entry_price=2.0, contracts=10, cost=2000.0)
    ib = FakeIB(fill_status="Filled", avg_fill_price=2.0)

    handle_event(event, ib, store, notifier, RiskConfig(max_usd_per_trade=10_000.0))

    assert len(ib.orders_of("SELL", "LMT")) == 4
    assert ib.orders_of("SELL", "STP")
    assert notifier.alerts == []


def test_the_ladder_is_persisted_for_a_later_restart(store, notifier):
    ib = FakeIB(fill_status="Filled", avg_fill_price=1.905)

    handle_event(_spy_buy(), ib, store, notifier, RiskConfig(max_usd_per_trade=200.0))

    # A 1-contract runner has no limit orders to read rungs off, so the
    # ladder has to come back from the DB after a restart.
    position = store.get_open(_spy_option())
    assert [t.pct for t in position.trim_targets] == [0.25, 0.50, 0.75, 1.00]
    assert position.current_stop_price == 1.33
    assert position.stop_tier_index == -1


def test_a_bracket_failure_after_a_fill_raises_a_loud_alert(store, notifier, monkeypatch):
    import bot.execution as execution

    def boom(*args, **kwargs):
        raise RuntimeError("IBKR said no")

    monkeypatch.setattr(execution, "place_exit_structure", boom)
    ib = FakeIB(fill_status="Filled", avg_fill_price=1.905)

    handle_event(_spy_buy(), ib, store, notifier, RiskConfig(max_usd_per_trade=10_000.0))

    assert store.get_open(_spy_option()) is not None  # we do hold the contracts
    assert len(notifier.alerts) == 1
    assert "UNPROTECTED" in notifier.alerts[0]


def test_sold_all_cancels_the_resting_brackets_before_selling(store, notifier):
    risk = RiskConfig(max_usd_per_trade=1000.0)  # 5 contracts
    ib = FakeIB(fill_status="Filled", avg_fill_price=1.905)
    handle_event(_spy_buy(), ib, store, notifier, risk)
    resting = {o.orderId for o in ib.orders_of("SELL")}

    sold_all = SoldAllEvent(
        message_id=102,
        underlying=UnderlyingKey("SPY", 764.0, "C"),
        realized_pct=0.30,
        avg_exit_price=2.5,
    )
    handle_event(sold_all, ib, store, notifier, risk)

    assert resting.issubset(set(ib.cancelled_order_ids)), "every bracket leg must come off the book"
    # The flattening market order is the last thing placed, after the cancels.
    final = ib.placed_orders[-1][1]
    assert (final.action, final.orderType, final.totalQuantity) == ("SELL", "MKT", 5)
    assert store.get_open(_spy_option()) is None


def test_expired_cancels_the_brackets_without_placing_an_order(store, notifier):
    risk = RiskConfig(max_usd_per_trade=10_000.0)
    ib = FakeIB(fill_status="Filled", avg_fill_price=1.905)
    handle_event(_spy_buy(), ib, store, notifier, risk)
    resting = {o.orderId for o in ib.orders_of("SELL")}
    placed_before = len(ib.placed_orders)

    expired = ExpiredEvent(message_id=103, underlying=UnderlyingKey("SPY", 764.0, "C"), won=True)
    handle_event(expired, ib, store, notifier, risk)

    assert resting.issubset(set(ib.cancelled_order_ids))
    assert len(ib.placed_orders) == placed_before  # the market is closed
    assert store.get_open(_spy_option()) is None
