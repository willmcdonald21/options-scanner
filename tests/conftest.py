"""Shared fakes and fixtures for the executor / watcher tests.

Everything here stands in for IBKR and Discord so the order-placement and
stop-laddering logic can be exercised with no network and no event loop.
"""

import pytest
from ib_async import Option, Order, OrderStatus, Trade

from bot.position_store import PositionStore
from config.settings import RiskConfig


class FakeIB:
    """Records qualifyContracts/placeOrder calls; scripted to return a
    Trade already in a chosen terminal state (or "Submitted" to simulate a
    fill that never confirms), so handle_event's branching is testable
    without any real network or event-loop I/O."""

    def __init__(self, fill_status: str = "Filled", avg_fill_price: float = 1.0, qualify_ok: bool = True):
        self.placed_orders: list[tuple[Option, Order]] = []
        self.cancelled_order_ids: list[int] = []
        self._fill_status = fill_status
        self._avg_fill_price = avg_fill_price
        self._qualify_ok = qualify_ok
        self._next_order_id = 1
        self._book: dict[int, Order] = {}

    def qualifyContracts(self, contract: Option) -> list[Option]:
        if not self._qualify_ok:
            return []
        contract.conId = 99999
        return [contract]

    def placeOrder(self, contract: Option, order: Order) -> Trade:
        # An order arriving with an id already set is a modification (the
        # stop ratchet re-places under the same id); only a new order gets
        # the next id.
        if not order.orderId:
            order.orderId = self._next_order_id
            self._next_order_id += 1
        self._book[order.orderId] = order
        self.placed_orders.append((contract, order))
        status = OrderStatus(status=self._fill_status, avgFillPrice=self._avg_fill_price)
        return Trade(contract=contract, order=order, orderStatus=status)

    def orders(self) -> list[Order]:
        return list(self._book.values())

    def cancelOrder(self, order: Order) -> None:
        self.cancelled_order_ids.append(order.orderId)
        self._book.pop(order.orderId, None)

    def reqAllOpenOrders(self) -> list[Order]:
        return self.orders()

    def reqMktData(self, contract, *args, **kwargs) -> None:
        pass

    def cancelMktData(self, contract) -> None:
        pass

    def reqMarketDataType(self, data_type: int) -> None:
        pass

    def sleep(self, secs: float = 0.02) -> bool:
        return True

    # --- convenience views used by assertions -----------------------------

    def orders_of(self, action: str, order_type: str | None = None) -> list[Order]:
        return [
            o
            for _, o in self.placed_orders
            if o.action == action and (order_type is None or o.orderType == order_type)
        ]


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


