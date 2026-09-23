from datetime import date

from bot.contracts import resolve_contract
from bot.models import OptionKey


def test_standard_ticker_routes_smart_with_no_trading_class():
    contract = resolve_contract(OptionKey("SPY", date(2026, 9, 22), 769.0, "C"))
    assert contract.symbol == "SPY"
    assert contract.lastTradeDateOrContractMonth == "20260922"
    assert contract.strike == 769.0
    assert contract.right == "C"
    assert contract.exchange == "SMART"
    assert contract.multiplier == "100"
    assert contract.currency == "USD"
    assert contract.tradingClass == ""


def test_standard_ticker_put():
    contract = resolve_contract(OptionKey("AAPL", date(2026, 9, 25), 345.0, "P"))
    assert contract.right == "P"
    assert contract.exchange == "SMART"


def test_spx_routes_cboe_with_spxw_trading_class():
    contract = resolve_contract(OptionKey("SPX", date(2026, 9, 21), 7700.0, "C"))
    assert contract.symbol == "SPX"
    assert contract.exchange == "CBOE"
    assert contract.tradingClass == "SPXW"
    assert contract.multiplier == "100"
    assert contract.currency == "USD"


def test_spx_put_also_routes_cboe():
    contract = resolve_contract(OptionKey("SPX", date(2026, 9, 22), 7765.0, "P"))
    assert contract.right == "P"
    assert contract.exchange == "CBOE"
    assert contract.tradingClass == "SPXW"
