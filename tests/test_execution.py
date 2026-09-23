from datetime import date

import pytest
from ib_async import Option, Order, OrderStatus, Trade

from bot.execution import handle_event
from bot.models import (
    BuyEvent,
    ExpiredEvent,
    InfoEvent,
    OpenPosition,
    OptionKey,
    SoldAllEvent,
    TrimEvent,
    UnderlyingKey,
    UnknownEvent,
)
from bot.position_store import PositionStore
from config.settings import RiskConfig


class FakeIB:
    """Records qualifyContracts/placeOrder calls; scripted to return a
    Trade already in a chosen terminal state (or "Submitted" to simulate a
    fill that never confirms), so handle_event's branching is testable
    without any real network or event-loop I/O."""

    def __init__(self, fill_status: str = "Filled", avg_fill_price: float = 1.0, qualify_ok: bool = True):
        self.placed_orders: list[tuple[Option, Order]] = []
        self._fill_status = fill_status
        self._avg_fill_price = avg_fill_price
        self._qualify_ok = qualify_ok
        self._next_order_id = 1

    def qualifyContracts(self, contract: Option) -> list[Option]:
        if not self._qualify_ok:
            return []
        contract.conId = 99999
        return [contract]

    def placeOrder(self, contract: Option, order: Order) -> Trade:
        order.orderId = self._next_order_id
        self._next_order_id += 1
        self.placed_orders.append((contract, order))
        status = OrderStatus(status=self._fill_status, avgFillPrice=self._avg_fill_price)
        return Trade(contract=contract, order=order, orderStatus=status)

    def sleep(self, secs: float = 0.02) -> bool:
        return True


class FakeNotifier:
    def __init__(self):
        self.alerts: list[str] = []

    def alert(self, message: str) -> None:
        self.alerts.append(message)


@pytest.fixture
def store(tmp_path):
    s = PositionStore(tmp_path / "positions.sqlite3")
    yield s
    s.close()


@pytest.fixture
def notifier():
    return FakeNotifier()


@pytest.fixture
def risk():
    return RiskConfig(max_usd_per_trade=1000.0)


def _qqq_option() -> OptionKey:
    return OptionKey("QQQ", date(2026, 9, 23), 740.0, "C")


def _seeded_position(store, *, channel_total=20, channel_remaining=20, user_total=4, user_remaining=4):
    option = _qqq_option()
    store.create_open(
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
    assert len(ib.placed_orders) == 1
    _, order = ib.placed_orders[0]
    assert order.action == "BUY"
    assert order.totalQuantity == expected_contracts

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


def test_trim_event_mirrors_channel_rate(store, notifier):
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

    assert len(ib.placed_orders) == 1
    _, order = ib.placed_orders[0]
    assert order.action == "SELL"
    assert order.totalQuantity == 2  # floor(4 * 12 / 20)

    position = store.get_open(_qqq_option())
    assert position.user_remaining_qty == 2
    assert position.channel_remaining_qty == 8
    assert store.already_processed(5) is True
    assert notifier.alerts == []
    assert len(trade_notifier.alerts) == 1
    assert "TRIMMED 2x" in trade_notifier.alerts[0]
    assert "2 remaining" in trade_notifier.alerts[0]


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


def test_trim_event_no_matching_position_alerts_and_marks_processed(store, notifier):
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

    assert ib.placed_orders == []
    assert len(notifier.alerts) == 1
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
    assert len(ib.placed_orders) == 1

    handle_event(event, ib, store, notifier, risk)
    assert len(ib.placed_orders) == 1
