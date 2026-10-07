from datetime import date

import pytest

from options_scanner.contracts import build_spec, exchange_for, is_index, trading_class_for
from options_scanner.models import OptionKey


def _spy() -> OptionKey:
    return OptionKey("SPY", date(2026, 9, 22), 769.0, "C")


def _spx() -> OptionKey:
    return OptionKey("SPX", date(2026, 10, 6), 7815.0, "P")


# --- trading class / routing ----------------------------------------------


def test_an_equity_option_keeps_its_ticker_and_routes_smart():
    spec = build_spec(_spy())

    assert spec.ticker == "SPY"
    assert spec.trading_class == "SPY"
    assert spec.exchange == "SMART"
    assert spec.is_index is False


def test_spx_trades_under_spxw_on_cboe():
    """SPXW is a different contract from the monthly SPX class at the same
    strike and expiry, and a wrong root qualifies silently rather than
    failing -- so it is pinned explicitly."""
    spec = build_spec(_spx())

    assert spec.ticker == "SPX"
    assert spec.trading_class == "SPXW"
    assert spec.exchange == "CBOE"
    assert spec.is_index is True


@pytest.mark.parametrize(
    "ticker,root,exchange",
    [
        ("SPX", "SPXW", "CBOE"),
        ("NDX", "NDXP", "NASDAQ"),
        ("RUT", "RUTW", "CBOE"),
        ("XSP", "XSP", "CBOE"),
        ("VIX", "VIX", "CBOE"),
        ("SPY", "SPY", "SMART"),
        ("AAPL", "AAPL", "SMART"),
    ],
)
def test_root_and_exchange_by_ticker(ticker, root, exchange):
    assert trading_class_for(ticker) == root
    assert exchange_for(ticker) == exchange


def test_ticker_case_is_normalized():
    assert trading_class_for("spx") == "SPXW"
    assert exchange_for("spx") == "CBOE"
    assert is_index("spx") is True


def test_every_spec_carries_the_standard_contract_terms():
    for option in (_spy(), _spx()):
        spec = build_spec(option)
        assert spec.multiplier == "100"
        assert spec.currency == "USD"
        assert spec.sec_type == "OPT"


# --- OCC symbol -----------------------------------------------------------


def test_occ_symbol_uses_the_trading_class_not_the_ticker():
    assert _spx().occ_symbol == "SPXW  261006P07815000"


def test_occ_symbol_is_21_characters():
    for option in (_spy(), _spx()):
        assert len(option.occ_symbol) == 21, option.occ_symbol


def test_occ_symbol_pads_a_short_root_to_six():
    symbol = _spy().occ_symbol
    assert symbol.startswith("SPY   ")  # 3 chars + 3 spaces
    assert symbol == "SPY   260922C00769000"


@pytest.mark.parametrize(
    "strike,expected_tail",
    [
        (7815.0, "07815000"),
        (769.0, "00769000"),
        (367.5, "00367500"),
        (0.5, "00000500"),
        (1234.567, "01234567"),
    ],
)
def test_occ_strike_is_thousandths_zero_padded_to_eight(strike, expected_tail):
    option = OptionKey("SPY", date(2026, 9, 22), strike, "C")
    assert option.occ_symbol.endswith(expected_tail)


def test_occ_symbol_encodes_the_right():
    call = OptionKey("SPY", date(2026, 9, 22), 769.0, "C").occ_symbol
    put = OptionKey("SPY", date(2026, 9, 22), 769.0, "P").occ_symbol

    assert call[12] == "C"
    assert put[12] == "P"
    assert call[:12] == put[:12]
