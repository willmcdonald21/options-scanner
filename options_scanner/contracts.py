"""Contract identity: trading class, routing exchange, OCC symbol.

Deliberately free of any broker SDK import so the parser and its tests stay
pure. Turning a ContractSpec into a broker-native order object is the
adapter's job (options_scanner/broker/ibkr.py).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from options_scanner.models import OptionKey

# Index options don't route like equity options. SPX is cash-settled on
# CBOE, and every SPX alert seen in this channel is a weekly/0DTE, which
# resolves under the SPXW trading class -- a *different* contract from the
# monthly SPX class at the same strike and expiry. Getting this wrong
# silently qualifies the wrong contract rather than failing outright, which
# is why the trading class is pinned explicitly instead of left blank.
_INDEX_ROOTS: dict[str, str] = {
    "SPX": "SPXW",
    "NDX": "NDXP",
    "RUT": "RUTW",
    "XSP": "XSP",
    "VIX": "VIX",
}

_INDEX_EXCHANGES: dict[str, str] = {
    "SPX": "CBOE",
    "SPXW": "CBOE",
    "NDX": "NASDAQ",
    "NDXP": "NASDAQ",
    "RUT": "CBOE",
    "RUTW": "CBOE",
    "XSP": "CBOE",
    "VIX": "CBOE",
}

_EQUITY_EXCHANGE = "SMART"
_MULTIPLIER = "100"
_CURRENCY = "USD"


def is_index(ticker: str) -> bool:
    return ticker.upper() in _INDEX_ROOTS


def trading_class_for(ticker: str, expiry: date | None = None) -> str:
    """The root/trading class to trade under.

    `expiry` is accepted for future use (an index whose monthly series needs
    the non-weekly root) but is not consulted today: every index alert this
    bot has seen is a weekly, and guessing "monthly" from a third-Friday
    date would be a silent mis-route on a week where both exist.
    """
    return _INDEX_ROOTS.get(ticker.upper(), ticker.upper())


def exchange_for(ticker: str) -> str:
    return _INDEX_EXCHANGES.get(ticker.upper(), _EQUITY_EXCHANGE)


@dataclass(frozen=True)
class ContractSpec:
    """Broker-neutral description of the contract to trade. The adapter maps
    this onto whatever its SDK wants."""

    ticker: str
    trading_class: str
    expiry: date
    strike: float
    right: str
    exchange: str
    occ_symbol: str
    multiplier: str = _MULTIPLIER
    currency: str = _CURRENCY
    sec_type: str = "OPT"

    @property
    def is_index(self) -> bool:
        return is_index(self.ticker)


def build_spec(option: OptionKey) -> ContractSpec:
    """OptionKey -> ContractSpec. Pure; touches no broker."""
    return ContractSpec(
        ticker=option.ticker.upper(),
        trading_class=trading_class_for(option.ticker, option.expiry),
        expiry=option.expiry,
        strike=option.strike,
        right=option.right,
        exchange=exchange_for(option.ticker),
        occ_symbol=option.occ_symbol,
    )
