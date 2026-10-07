"""The real IBKR adapter, against a fake ib_async session.

No network. The fakes stand in for ib_async's IB/Trade/OrderStatus objects so
the mapping, the position filtering and the cancel race can all be driven
exactly.
"""

from datetime import date, datetime
from types import SimpleNamespace

import pytest

from options_scanner.broker.base import BrokerError, OrderStatus, Quote
from options_scanner.broker.ibkr import IBKRBroker, occ_from_contract
from options_scanner.contracts import build_spec
from options_scanner.models import OptionKey

SPX = build_spec(OptionKey("SPX", date(2026, 10, 6), 7815.0, "P"))
SPX_OCC = "SPXW  261006P07815000"
SPY = build_spec(OptionKey("SPY", date(2026, 10, 9), 670.0, "C"))


def ib_contract(symbol="SPX", trading_class="SPXW", expiry="20261006", right="P", strike=7815.0,
                sec_type="OPT", multiplier="100"):
    return SimpleNamespace(
        symbol=symbol,
        tradingClass=trading_class,
        lastTradeDateOrContractMonth=expiry,
        right=right,
        strike=strike,
        secType=sec_type,
        multiplier=multiplier,
        conId=1234,
    )


class FakeTrade:
    def __init__(self, order_id=1, status="Filled", filled=10, avg=0.48, log_message=""):
        self.order = SimpleNamespace(orderId=order_id, orderType="LMT")
        self.orderStatus = SimpleNamespace(status=status, filled=filled, avgFillPrice=avg)
        self.log = [SimpleNamespace(message=log_message)] if log_message else []
        self._done_states = {"Filled", "Cancelled", "ApiCancelled", "Inactive"}

    def isDone(self):
        return self.orderStatus.status in self._done_states


class FakeIB:
    def __init__(self, trade=None, positions=(), account_values=(), confirm_cancel=True):
        self._trade = trade or FakeTrade()
        self.confirm_cancel = confirm_cancel
        self._positions = list(positions)
        self._account_values = list(account_values)
        self.placed: list[tuple[object, object]] = []
        self.cancelled: list[object] = []

    def placeOrder(self, contract, order):
        self.placed.append((contract, order))
        self._trade.order.orderId = getattr(self._trade.order, "orderId", 1)
        return self._trade

    def cancelOrder(self, order):
        self.cancelled.append(order)
        if self.confirm_cancel:
            # Realistic: the cancel is what makes IBKR report the terminal
            # state, so flipping it here rather than in the test keeps the
            # adapter's early-return path honest.
            self._trade.orderStatus.status = "Cancelled"

    def positions(self):
        return list(self._positions)

    def accountValues(self):
        return list(self._account_values)


class FakeQuotes:
    """Stands in for IBKRQuotes."""

    def __init__(self, ib, *, exists=True, contract=None):
        self._ib = ib
        self._exists = exists
        self._contract = contract or ib_contract()
        self.released: list[object] = []
        self.connected = True

    @property
    def is_connected(self):
        return self.connected

    async def connect(self):
        self.connected = True

    async def disconnect(self):
        self.connected = False

    async def exists(self, spec):
        return self._exists

    async def qualified_contract(self, spec):
        if not self._exists:
            raise BrokerError("does not exist")
        return self._contract

    async def quote(self, spec):
        return Quote(bid=0.47, ask=0.48, last=0.47, asof=datetime(2026, 10, 6, 11, 0))

    def release(self, spec_or_symbol):
        self.released.append(spec_or_symbol)


def broker(ib=None, **kwargs) -> IBKRBroker:
    ib = ib or FakeIB()
    return IBKRBroker("127.0.0.1", 4002, 12, quotes=FakeQuotes(ib, **kwargs))


# --- OCC symbol reconstruction -------------------------------------------


def test_a_position_is_keyed_by_its_trading_class_not_its_symbol():
    """An SPX weekly has symbol SPX and trading class SPXW. Keying on the
    symbol would fail to match the SPXW symbol the bot tracks."""
    assert occ_from_contract(ib_contract()) == SPX_OCC


def test_an_equity_option_round_trips():
    contract = ib_contract(symbol="SPY", trading_class="SPY", expiry="20261009", right="C", strike=670.0)
    assert occ_from_contract(contract) == SPY.occ_symbol


def test_a_fractional_strike_round_trips():
    contract = ib_contract(symbol="GOOGL", trading_class="GOOGL", strike=367.5, right="C")
    assert occ_from_contract(contract).endswith("00367500")


def test_a_contract_missing_its_trading_class_falls_back_to_the_symbol():
    contract = ib_contract(trading_class="")
    assert occ_from_contract(contract).startswith("SPX   ")


@pytest.mark.parametrize(
    "kwargs",
    [
        {"right": ""},
        {"right": "X"},
        {"strike": 0},
        {"strike": None},
        {"expiry": ""},
        {"expiry": "2026-10-06"},
        {"expiry": "202610"},
        {"symbol": "", "trading_class": ""},
    ],
)
def test_an_unidentifiable_contract_returns_none_rather_than_a_guess(kwargs):
    assert occ_from_contract(ib_contract(**kwargs)) is None


# --- placing orders -------------------------------------------------------


async def test_a_filled_order_maps_to_filled():
    ib = FakeIB(FakeTrade(status="Filled", filled=21, avg=0.48))
    result = await broker(ib).place_order(SPX, "BUY", 21, 0.48, timeout_seconds=1)

    assert result.status is OrderStatus.FILLED
    assert result.filled_qty == 21
    assert result.avg_fill_price == 0.48


async def test_the_order_is_always_a_day_limit_never_a_market_or_stop():
    ib = FakeIB()
    await broker(ib).place_order(SPX, "BUY", 10, 0.48, timeout_seconds=1)

    (_, order) = ib.placed[0]
    assert order.orderType == "LMT"
    assert order.tif == "DAY"
    assert order.outsideRth is False


async def test_the_limit_price_is_rounded_to_a_tick():
    ib = FakeIB()
    await broker(ib).place_order(SPX, "BUY", 10, 0.4789, timeout_seconds=1)

    (_, order) = ib.placed[0]
    assert order.lmtPrice == 0.48


async def test_a_partially_filled_order_maps_to_partial():
    ib = FakeIB(FakeTrade(status="Submitted", filled=5, avg=0.48))
    result = await broker(ib).place_order(SPX, "BUY", 21, 0.48, timeout_seconds=0)

    assert result.status is OrderStatus.PARTIAL
    assert result.filled_qty == 5


async def test_an_order_still_working_at_the_timeout_is_pending_not_failed():
    """It is still live at IBKR; claiming otherwise would let the caller place
    a second order."""
    ib = FakeIB(FakeTrade(status="Submitted", filled=0, avg=None))
    result = await broker(ib).place_order(SPX, "BUY", 21, 0.48, timeout_seconds=0)

    assert result.status is OrderStatus.PENDING
    assert result.status.is_done is False


async def test_a_rejected_order_maps_to_rejected_with_the_reason():
    ib = FakeIB(FakeTrade(status="Inactive", filled=0, avg=None, log_message="no trading permission"))
    result = await broker(ib).place_order(SPX, "BUY", 21, 0.48, timeout_seconds=1)

    assert result.status is OrderStatus.REJECTED
    assert "no trading permission" in result.detail


async def test_an_unknown_status_is_treated_as_still_working():
    """Never as filled, and never as safely cancelled."""
    ib = FakeIB(FakeTrade(status="SomeNewIBKRState", filled=0, avg=None))
    result = await broker(ib).place_order(SPX, "BUY", 21, 0.48, timeout_seconds=0)

    assert result.status is OrderStatus.PENDING


async def test_a_contract_that_does_not_exist_cannot_be_ordered():
    with pytest.raises(BrokerError, match="does not exist"):
        await broker(exists=False).place_order(SPX, "BUY", 1, 0.48, timeout_seconds=1)


async def test_a_place_order_failure_is_wrapped():
    class Exploding(FakeIB):
        def placeOrder(self, contract, order):
            raise RuntimeError("socket closed")

    with pytest.raises(BrokerError, match="placeOrder failed"):
        await broker(Exploding()).place_order(SPX, "BUY", 1, 0.48, timeout_seconds=1)


@pytest.mark.parametrize("side,qty", [("HOLD", 1), ("BUY", 0), ("BUY", -5)])
async def test_nonsense_orders_are_refused_before_reaching_ibkr(side, qty):
    ib = FakeIB()
    with pytest.raises(BrokerError):
        await broker(ib).place_order(SPX, side, qty, 0.48, timeout_seconds=1)
    assert ib.placed == []


async def test_ordering_while_disconnected_is_refused():
    ib = FakeIB()
    quotes = FakeQuotes(ib)
    quotes.connected = False
    with pytest.raises(BrokerError, match="not connected"):
        await IBKRBroker("h", 4002, 12, quotes=quotes).place_order(SPX, "BUY", 1, 0.48, timeout_seconds=1)


# --- cancelling -----------------------------------------------------------


async def test_cancelling_a_working_order_reports_cancelled():
    trade = FakeTrade(status="Submitted", filled=0, avg=None)
    ib = FakeIB(trade)
    adapter = broker(ib)
    placed = await adapter.place_order(SPX, "BUY", 10, 0.48, timeout_seconds=0)

    result = await adapter.cancel_order(placed.broker_order_id)

    assert result.status is OrderStatus.CANCELLED
    assert ib.cancelled


async def test_an_order_that_filled_before_the_cancel_landed_reports_the_fill():
    """The contracts are ours whether or not the cancel won the race."""
    trade = FakeTrade(status="Filled", filled=10, avg=0.48)
    adapter = broker(FakeIB(trade))
    placed = await adapter.place_order(SPX, "BUY", 10, 0.48, timeout_seconds=1)

    result = await adapter.cancel_order(placed.broker_order_id)

    assert result.status is OrderStatus.FILLED
    assert result.filled_qty == 10


async def test_a_cancel_that_fills_partially_reports_partial():
    trade = FakeTrade(status="Submitted", filled=4, avg=0.48)
    adapter = broker(FakeIB(trade))
    placed = await adapter.place_order(SPX, "BUY", 10, 0.48, timeout_seconds=0)

    result = await adapter.cancel_order(placed.broker_order_id)

    assert result.status is OrderStatus.PARTIAL
    assert result.filled_qty == 4


async def test_an_unconfirmed_cancel_is_reported_as_pending():
    """A caller that believed an unconfirmed cancel could place a second
    order against a position it still holds."""
    trade = FakeTrade(status="Submitted", filled=0, avg=None)
    # confirm_cancel=False models IBKR not answering within the window.
    adapter = broker(FakeIB(trade, confirm_cancel=False))
    placed = await adapter.place_order(SPX, "BUY", 10, 0.48, timeout_seconds=0)

    import options_scanner.broker.ibkr as module

    original = module._CANCEL_CONFIRM_SECONDS
    module._CANCEL_CONFIRM_SECONDS = 0.01
    try:
        result = await adapter.cancel_order(placed.broker_order_id)
    finally:
        module._CANCEL_CONFIRM_SECONDS = original

    assert result.status is OrderStatus.PENDING
    assert "not yet confirmed" in result.detail


async def test_cancelling_an_unknown_order_is_an_error():
    with pytest.raises(BrokerError, match="unknown order"):
        await broker().cancel_order("nope")


async def test_status_of_an_unknown_order_is_an_error():
    with pytest.raises(BrokerError, match="unknown order"):
        await broker().get_order_status("nope")


# --- positions ------------------------------------------------------------


def position(contract, qty, avg_cost):
    return SimpleNamespace(contract=contract, position=qty, avgCost=avg_cost)


async def test_stock_positions_are_filtered_out():
    """ib.positions() is account-wide and this account is shared with an
    equities bot; its stocks must never reach the sell clamp."""
    ib = FakeIB(positions=[
        position(ib_contract(symbol="NRXS", trading_class="NRXS", sec_type="STK"), 850, 2.50),
        position(ib_contract(), 8, 48.0),
    ])

    positions = await broker(ib).get_positions()

    assert [p.occ_symbol for p in positions] == [SPX_OCC]


async def test_the_average_cost_is_converted_back_to_a_per_share_price():
    """IBKR reports an option's cost per contract including the multiplier, so
    a $0.48 option arrives as 48.0."""
    ib = FakeIB(positions=[position(ib_contract(), 8, 48.0)])

    (held,) = await broker(ib).get_positions()

    assert held.avg_cost == 0.48
    assert held.qty == 8


async def test_a_flat_position_is_omitted():
    ib = FakeIB(positions=[position(ib_contract(), 0, 48.0)])
    assert await broker(ib).get_positions() == []


async def test_an_unidentifiable_option_position_is_skipped_loudly(caplog):
    ib = FakeIB(positions=[position(ib_contract(right="X"), 8, 48.0)])

    with caplog.at_level("ERROR"):
        positions = await broker(ib).get_positions()

    assert positions == []
    assert "could not build an OCC symbol" in caplog.text


async def test_a_positions_read_failure_is_wrapped():
    class Blind(FakeIB):
        def positions(self):
            raise RuntimeError("no connection")

    with pytest.raises(BrokerError, match="could not read positions"):
        await broker(Blind()).get_positions()


# --- account --------------------------------------------------------------


def value(tag, amount, currency="USD"):
    return SimpleNamespace(tag=tag, value=str(amount), currency=currency)


async def test_the_account_snapshot_reads_the_usd_values():
    ib = FakeIB(account_values=[
        value("NetLiquidation", 101_234.56),
        value("BuyingPower", 50_000.0),
        value("RealizedPnL", -120.5),
    ])

    account = await broker(ib).get_account()

    assert account.net_liquidation == pytest.approx(101_234.56)
    assert account.buying_power == pytest.approx(50_000.0)
    assert account.realized_pnl_today == pytest.approx(-120.5)


async def test_a_missing_net_liquidation_is_an_error_not_a_zero():
    ib = FakeIB(account_values=[value("BuyingPower", 1.0)])
    with pytest.raises(BrokerError, match="NetLiquidation"):
        await broker(ib).get_account()


async def test_an_unparsable_value_does_not_crash_the_snapshot():
    ib = FakeIB(account_values=[
        value("NetLiquidation", 100.0),
        value("BuyingPower", "n/a"),
    ])

    account = await broker(ib).get_account()
    assert account.buying_power == 0.0


# --- session and plumbing -------------------------------------------------


async def test_connect_and_disconnect_delegate_to_the_quote_layer():
    ib = FakeIB()
    quotes = FakeQuotes(ib)
    quotes.connected = False
    adapter = IBKRBroker("h", 4002, 12, quotes=quotes)

    await adapter.connect()
    assert adapter.is_connected is True
    await adapter.disconnect()
    assert adapter.is_connected is False


async def test_the_chain_check_and_quotes_delegate_too():
    ib = FakeIB()
    adapter = broker(ib)

    assert await adapter.get_option_chain(SPX) is True
    quote = await adapter.get_quote(SPX)
    assert quote.bid == 0.47


def test_releasing_a_subscription_reaches_the_quote_layer():
    ib = FakeIB()
    quotes = FakeQuotes(ib)
    adapter = IBKRBroker("h", 4002, 12, quotes=quotes)

    adapter.release(SPX_OCC)
    assert quotes.released == [SPX_OCC]


def test_the_adapter_names_itself_for_logs():
    assert broker().name == "ibkr"
