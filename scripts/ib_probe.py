"""Read-only IBKR probe, for `ops.sh status` and `ops.sh doctor`.

Places no orders and cancels nothing. It connects on its own client id so it
can run while both bots are live without disturbing either.

Output is one `key=value` line per fact, so the shell can parse it without
needing a JSON tool installed.

Usage:
    python scripts/ib_probe.py                 # account, positions, open orders
    python scripts/ib_probe.py --quote-check   # also prove option data arrives
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import sys
from datetime import date, timedelta

# IBKR codes meaning "you are not entitled to this market data". This is the
# single most important thing this probe can detect: without option quotes the
# bot's trim ladder and synthetic stop simply never fire.
NO_DATA_CODES = {354, 10167, 10168, 10197}

# Codes that are informational noise on every connection.
BENIGN_CODES = {2104, 2106, 2107, 2158, 2119}


def emit(key: str, value) -> None:
    print(f"{key}={value}")


async def probe(host: str, port: int, client_id: int, quote_check: bool) -> int:
    try:
        from ib_async import IB, Option, Stock
    except ImportError:
        emit("error", "ib_async not installed")
        return 2

    ib = IB()
    errors: list[tuple[int, str]] = []
    ib.errorEvent += lambda reqId, code, msg, contract: (
        errors.append((code, msg)) if code not in BENIGN_CODES else None
    )

    try:
        await ib.connectAsync(host, port, clientId=client_id, timeout=15)
    except Exception as exc:
        emit("connected", "no")
        emit("connect_error", str(exc).replace("\n", " ")[:160])
        return 1

    emit("connected", "yes")
    try:
        accounts = ib.managedAccounts()
        emit("account", ",".join(accounts) or "unknown")
        # DU/DF prefixes are IBKR's paper accounts. Worth asserting rather than
        # assuming the port implies it.
        emit("is_paper", "yes" if accounts and accounts[0].startswith(("DU", "DF")) else "no")

        options = stocks = 0
        for item in ib.positions():
            if item.position == 0:
                continue
            if getattr(item.contract, "secType", "") == "OPT":
                options += 1
            else:
                stocks += 1
        emit("positions_total", options + stocks)
        emit("positions_opt", options)
        emit("positions_stk", stocks)
        emit("flat", "yes" if options + stocks == 0 else "no")

        trades = [t for t in ib.openTrades() if not t.orderStatus.status.startswith("Cancel")]
        emit("open_orders", len(trades))
        emit("client_ids_with_orders", ",".join(sorted({str(t.order.clientId) for t in trades})) or "-")

        for value in ib.accountValues():
            if value.tag == "NetLiquidation" and value.currency in ("USD", ""):
                emit("net_liquidation", value.value)
                break

        if quote_check:
            await _quote_check(ib, Option, Stock, errors)
    finally:
        with contextlib.suppress(Exception):
            ib.disconnect()

    unentitled = sorted({code for code, _ in errors if code in NO_DATA_CODES})
    if unentitled:
        emit("market_data_entitled", "no")
        emit("market_data_errors", ",".join(str(c) for c in unentitled))
    return 0


async def _quote_check(ib, Option, Stock, errors: list) -> None:
    """Prove real option quotes arrive, using a liquid SPY contract.

    SPY rather than SPX: it is the most liquid option chain there is, so an
    empty book means an entitlement problem rather than a thin contract. The
    expiry and strike come from IBKR's own chain so this never guesses at a
    contract that does not exist.
    """
    ib.reqMarketDataType(1)

    spy = Stock("SPY", "SMART", "USD")
    qualified = await ib.qualifyContractsAsync(spy)
    if not qualified:
        emit("quote_check", "failed")
        emit("quote_check_detail", "could not qualify SPY")
        return

    ticker = ib.reqMktData(qualified[0], "", True, False)
    for _ in range(40):
        await asyncio.sleep(0.1)
        if _clean(ticker.last) or _clean(ticker.close):
            break
    underlying = _clean(ticker.last) or _clean(ticker.close)
    emit("spy_price", underlying if underlying else "none")
    emit("stock_data", "yes" if underlying else "no")
    with contextlib.suppress(Exception):
        ib.cancelMktData(qualified[0])

    chains = await ib.reqSecDefOptParamsAsync("SPY", "", "STK", qualified[0].conId)
    chain = next((c for c in chains if c.exchange == "SMART"), None)
    if chain is None or not chain.expirations:
        emit("quote_check", "failed")
        emit("quote_check_detail", "no SMART option chain for SPY")
        return

    today = date.today()
    horizon = today + timedelta(days=45)
    expiries = sorted(
        e for e in chain.expirations
        if today <= date(int(e[:4]), int(e[4:6]), int(e[6:8])) <= horizon
    )
    if not expiries:
        expiries = sorted(chain.expirations)[:1]

    strikes = sorted(chain.strikes)
    target = underlying or (strikes[len(strikes) // 2] if strikes else 0)
    strike = min(strikes, key=lambda s: abs(s - target)) if strikes else 0
    if not strike:
        emit("quote_check", "failed")
        emit("quote_check_detail", "no strikes in the SPY chain")
        return

    expiry = expiries[0]
    emit("quote_contract", f"SPY {expiry} {strike:g}C")

    contract = Option("SPY", expiry, strike, "C", "SMART", multiplier="100", currency="USD")
    qualified_opt = await ib.qualifyContractsAsync(contract)
    if not qualified_opt:
        emit("quote_check", "failed")
        emit("quote_check_detail", "could not qualify the SPY option")
        return

    opt_ticker = ib.reqMktData(qualified_opt[0], "", False, False)
    for _ in range(60):
        await asyncio.sleep(0.1)
        if _clean(opt_ticker.bid) and _clean(opt_ticker.ask):
            break
    bid, ask = _clean(opt_ticker.bid), _clean(opt_ticker.ask)
    with contextlib.suppress(Exception):
        ib.cancelMktData(qualified_opt[0])

    emit("option_bid", bid if bid else "none")
    emit("option_ask", ask if ask else "none")
    # The bid is what the bot actually reads: the trim ladder and the synthetic
    # stop both trigger off it, so a quote with no bid is unusable regardless of
    # what else arrives.
    emit("quote_check", "ok" if bid else "failed")
    if not bid:
        unentitled = [c for c, _ in errors if c in NO_DATA_CODES]
        emit(
            "quote_check_detail",
            f"no bid; market-data error(s) {sorted(set(unentitled))}" if unentitled
            else "no bid arrived (market may be closed, or the book is empty)",
        )


def _clean(value) -> float | None:
    """IBKR reports an absent price as nan or -1; either would be disastrous
    read as a real quote."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number == number and number > 0 else None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=4002)
    parser.add_argument(
        "--client-id",
        type=int,
        default=19,
        help="must not collide with either bot (warrior_bot 11, options-scanner 12)",
    )
    parser.add_argument("--quote-check", action="store_true")
    args = parser.parse_args()
    return asyncio.run(probe(args.host, args.port, args.client_id, args.quote_check))


if __name__ == "__main__":
    sys.exit(main())
