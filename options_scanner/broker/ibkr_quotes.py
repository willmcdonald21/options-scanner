"""Real IBKR market data: contract verification and quotes.

Split out from the full IBKR order adapter (phase 5) so `PaperBroker` can run
on genuine prices while sending nothing to the broker. That combination is the
whole point of phase 4: a paper run against a frozen or synthetic price path
tells you almost nothing about how a 0DTE trim ladder behaves.

Two things here are easy to get wrong and expensive to get wrong silently:

* **The trading class.** An SPX weekly is SPXW, a different contract from the
  monthly SPX at the same strike and expiry. Left blank, IBKR will happily
  qualify *a* contract -- just not necessarily the one in the alert. The spec
  is explicit about this, so the class is always pinned and a lookup that
  resolves to more than one contract is treated as a failure, not a guess.
* **Market data entitlements.** Without an OPRA subscription, requesting live
  data returns an error rather than ticks, and a synthetic stop with no quotes
  does not fail -- it silently stops working. So the adapter detects that case,
  falls back to delayed data, and says so loudly.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

from options_scanner.broker.base import BrokerError, Quote
from options_scanner.contracts import ContractSpec

logger = logging.getLogger("options_scanner.broker.ibkr_quotes")

# IBKR error codes meaning "you are not subscribed to this market data".
NO_MARKET_DATA_CODES = {354, 10167, 10168, 10197}

DELAYED_MARKET_DATA = 3

# How long to wait for a snapshot to populate before giving up on it.
_QUOTE_TIMEOUT_SECONDS = 6.0
_POLL_INTERVAL = 0.1


class IBKRQuotes:
    """Quote source backed by a real IB Gateway / TWS session."""

    def __init__(
        self,
        host: str,
        port: int,
        client_id: int,
        *,
        market_data_type: int = 1,
        on_no_market_data=None,
    ):
        self.host = host
        self.port = port
        self.client_id = client_id
        self.market_data_type = market_data_type
        self._on_no_market_data = on_no_market_data
        self._ib = None
        self._qualified: dict[str, object] = {}
        self._tickers: dict[str, object] = {}
        self._warned_no_data = False
        self.using_delayed_data = False

    # --- session -----------------------------------------------------------

    async def connect(self) -> None:
        from ib_async import IB

        if self._ib is not None and self._ib.isConnected():
            return
        self._ib = IB()
        self._ib.errorEvent += self._on_error
        try:
            await self._ib.connectAsync(self.host, self.port, clientId=self.client_id, timeout=20)
        except Exception as exc:
            raise BrokerError(
                f"could not connect to IB at {self.host}:{self.port} as client {self.client_id}: "
                f"{exc}. Is Gateway running, and is this client id free? "
                "(warrior_bot holds 11 on this account.)"
            ) from exc
        self._ib.reqMarketDataType(self.market_data_type)
        logger.info(
            "connected to IB %s:%s as client %s (market data type %s)",
            self.host, self.port, self.client_id, self.market_data_type,
        )

    async def disconnect(self) -> None:
        if self._ib is not None and self._ib.isConnected():
            for ticker in self._tickers.values():
                try:
                    self._ib.cancelMktData(ticker.contract)
                except Exception:
                    logger.debug("cancelMktData failed during shutdown", exc_info=True)
            self._ib.disconnect()
        self._tickers.clear()
        self._ib = None

    @property
    def is_connected(self) -> bool:
        return self._ib is not None and self._ib.isConnected()

    def _require(self):
        if self._ib is None or not self._ib.isConnected():
            raise BrokerError("not connected to IB")
        return self._ib

    def _on_error(self, reqId, errorCode, errorString, contract) -> None:
        if errorCode in NO_MARKET_DATA_CODES and not self._warned_no_data:
            self._warned_no_data = True
            self.using_delayed_data = True
            try:
                self._require().reqMarketDataType(DELAYED_MARKET_DATA)
            except BrokerError:
                pass
            message = (
                f"No real-time option quotes (IBKR {errorCode}: {errorString}). Falling back to "
                "delayed data. Synthetic stops still work but will trigger late, which on 0DTE "
                "is a real cost. Add an OPRA subscription to this account."
            )
            logger.error(message)
            if self._on_no_market_data is not None:
                self._on_no_market_data(message)

    # --- contracts ---------------------------------------------------------

    async def _qualify(self, spec: ContractSpec):
        """Resolve a ContractSpec to exactly one IBKR contract, or fail."""
        cached = self._qualified.get(spec.occ_symbol)
        if cached is not None:
            return cached

        from ib_async import Option

        ib = self._require()
        contract = Option(
            symbol=spec.ticker,
            lastTradeDateOrContractMonth=spec.expiry.strftime("%Y%m%d"),
            strike=spec.strike,
            right=spec.right,
            exchange=spec.exchange,
            multiplier=spec.multiplier,
            currency=spec.currency,
            tradingClass=spec.trading_class,
        )
        try:
            matches = await ib.qualifyContractsAsync(contract)
        except Exception as exc:
            raise BrokerError(f"contract lookup failed for {spec.occ_symbol}: {exc}") from exc

        usable = [c for c in matches if getattr(c, "conId", 0)]
        if not usable:
            return None
        if len(usable) > 1:
            # Ambiguity here means the strike/expiry/class triple matched more
            # than one listing. Picking one would be a guess about which
            # contract the alert meant, so refuse instead.
            raise BrokerError(
                f"{spec.occ_symbol} matched {len(usable)} contracts under trading class "
                f"{spec.trading_class}; refusing to guess which one the alert meant"
            )
        self._qualified[spec.occ_symbol] = usable[0]
        return usable[0]

    async def exists(self, spec: ContractSpec) -> bool:
        return await self._qualify(spec) is not None

    async def qualified_contract(self, spec: ContractSpec):
        """The resolved IBKR contract, for the order adapter in phase 5."""
        contract = await self._qualify(spec)
        if contract is None:
            raise BrokerError(f"{spec.occ_symbol} does not exist at IBKR")
        return contract

    # --- quotes ------------------------------------------------------------

    async def quote(self, spec: ContractSpec) -> Quote:
        ib = self._require()
        contract = await self._qualify(spec)
        if contract is None:
            raise BrokerError(f"{spec.occ_symbol} does not exist at IBKR")

        ticker = self._tickers.get(spec.occ_symbol)
        if ticker is None:
            # A streaming subscription, not a snapshot: the stop loop polls
            # every second or two, and a snapshot per poll would burn through
            # the account's request limits.
            ticker = ib.reqMktData(contract, "", False, False)
            self._tickers[spec.occ_symbol] = ticker
            await self._await_first_tick(ticker)

        return Quote(
            bid=_clean(getattr(ticker, "bid", None)),
            ask=_clean(getattr(ticker, "ask", None)),
            last=_clean(getattr(ticker, "last", None)) or _clean(getattr(ticker, "close", None)),
            asof=_ticker_time(ticker),
        )

    async def _await_first_tick(self, ticker) -> None:
        """Wait briefly for a new subscription to populate, so the first poll
        after an entry is not uselessly empty."""
        deadline = asyncio.get_running_loop().time() + _QUOTE_TIMEOUT_SECONDS
        while asyncio.get_running_loop().time() < deadline:
            if _clean(getattr(ticker, "bid", None)) or _clean(getattr(ticker, "ask", None)):
                return
            await asyncio.sleep(_POLL_INTERVAL)
        logger.warning("no quote arrived for %s within %.0fs", ticker.contract, _QUOTE_TIMEOUT_SECONDS)

    def release(self, spec_or_symbol) -> None:
        """Drop a market-data subscription once a position is closed. Lines are
        a shared, limited account resource.

        Accepts either a ContractSpec or a bare OCC symbol, because the
        position manager only has the symbol by the time it is cleaning up.
        """
        symbol = getattr(spec_or_symbol, "occ_symbol", spec_or_symbol)
        ticker = self._tickers.pop(symbol, None)
        if ticker is None or self._ib is None:
            return
        try:
            self._ib.cancelMktData(ticker.contract)
        except Exception:
            logger.debug("cancelMktData failed for %s", symbol, exc_info=True)


def _clean(value) -> float | None:
    """IBKR reports an absent price as nan or -1, both of which would be
    disastrous if treated as a real quote."""
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number != number or number <= 0:  # nan or non-positive
        return None
    return number


def _ticker_time(ticker) -> datetime:
    stamp = getattr(ticker, "time", None)
    if isinstance(stamp, datetime):
        return stamp
    return datetime.now(timezone.utc)
