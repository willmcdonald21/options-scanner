from __future__ import annotations

from ib_async import Option

from bot.models import OptionKey

# Ticker-specific overrides for index options, which don't route through
# SMART like equity options do. SPX is cash-settled and trades on CBOE;
# every SPX alert seen in the channel is a 0DTE weekly, which IBKR resolves
# under the SPXW trading class (distinct from the monthly/quarterly SPX
# class at the same strike/expiry) -- getting this wrong risks silently
# qualifying the wrong contract, not just failing to qualify one.
_INDEX_OVERRIDES: dict[str, dict[str, str]] = {
    "SPX": {"exchange": "CBOE", "tradingClass": "SPXW", "currency": "USD"},
}


def resolve_contract(option: OptionKey) -> Option:
    """Pure OptionKey -> unqualified ib_async.Option. Caller is responsible
    for ib.qualifyContracts()/qualifyContractsAsync() before using it in an
    order -- this function never touches IB."""
    contract = Option(
        symbol=option.ticker,
        lastTradeDateOrContractMonth=option.expiry.strftime("%Y%m%d"),
        strike=option.strike,
        right=option.right,
        exchange="SMART",
        multiplier="100",
        currency="USD",
    )
    overrides = _INDEX_OVERRIDES.get(option.ticker)
    if overrides:
        for field_name, value in overrides.items():
            setattr(contract, field_name, value)
    return contract
