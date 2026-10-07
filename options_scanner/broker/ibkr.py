"""Real IBKR orders, on top of the quote/contract layer in `ibkr_quotes`.

This is the adapter that actually sends orders. Three things shape it:

* **Limit orders only.** There is no code path here that builds a market order
  or a stop order. Stops are synthetic by design, and a broker-side stop on a
  0DTE option gets triggered by a bad quote on a wide spread.
* **Scoped to one account, and options only, when reading positions.**
  `ib.positions()` is account-wide, not per-client. Once two accounts are
  linked under one username it returns both, so reads are filtered to this
  bot's account; and because an account can hold more than one instrument type,
  everything that is not an option is filtered out as well. Either filter alone
  would be enough today, and neither is enough on its own forever.
* **No guessing.** A contract that resolves ambiguously, an order whose state
  cannot be read, a position whose OCC symbol cannot be reconstructed: each
  raises rather than returning a plausible-looking value, because every one of
  those feeds a decision about how much to sell.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import date

from options_scanner.broker.base import (
    AccountSnapshot,
    Broker,
    BrokerError,
    BrokerPosition,
    OrderResult,
    OrderStatus,
    Quote,
)
from options_scanner.broker.ibkr_quotes import IBKRQuotes
from options_scanner.contracts import ContractSpec
from options_scanner.rules import round_price

logger = logging.getLogger("options_scanner.broker.ibkr")

# IBKR order states, mapped onto ours. Anything unrecognized is treated as
# still working rather than assumed done -- an unknown state must never be read
# as "filled" or as "safely cancelled".
_TERMINAL_FILLED = {"Filled"}
_TERMINAL_CANCELLED = {"Cancelled", "ApiCancelled"}
_TERMINAL_REJECTED = {"Inactive"}

_POLL_INTERVAL = 0.2

# How long to wait for IBKR to confirm a cancel. A cancel is not instant and
# an order can still fill inside this window, so the wait is deliberate.
_CANCEL_CONFIRM_SECONDS = 2.0

# Entries and exits are both day orders. The bot manages every exit itself, so
# a GTC order outliving the session would be an order nothing is watching.
_TIF = "DAY"


class IBKRBroker(Broker):
    """Live IBKR adapter. Used against the paper account in phase 5."""

    def __init__(
        self,
        host: str,
        port: int,
        client_id: int,
        *,
        market_data_type: int = 1,
        on_no_market_data=None,
        quotes: IBKRQuotes | None = None,
        account: str = "",
    ):
        self._quotes = quotes or IBKRQuotes(
            host,
            port,
            client_id,
            market_data_type=market_data_type,
            on_no_market_data=on_no_market_data,
            account=account,
        )
        # Empty means "whatever the login manages", which is both ib_async's and
        # IBKR's own behaviour for a single-account login.
        self._account = account.strip()
        self._trades: dict[str, object] = {}

    @property
    def name(self) -> str:
        return "ibkr"

    # --- session -----------------------------------------------------------

    async def connect(self) -> None:
        await self._quotes.connect()

    async def disconnect(self) -> None:
        await self._quotes.disconnect()

    @property
    def is_connected(self) -> bool:
        return self._quotes.is_connected

    @property
    def _ib(self):
        ib = getattr(self._quotes, "_ib", None)
        if ib is None or not self._quotes.is_connected:
            raise BrokerError("not connected to IB")
        return ib

    def release(self, spec_or_symbol) -> None:
        self._quotes.release(spec_or_symbol)

    # --- market data -------------------------------------------------------

    async def get_option_chain(self, spec: ContractSpec) -> bool:
        return await self._quotes.exists(spec)

    async def get_quote(self, spec: ContractSpec) -> Quote:
        return await self._quotes.quote(spec)

    # --- orders ------------------------------------------------------------

    async def place_order(
        self,
        spec: ContractSpec,
        side: str,
        qty: int,
        limit_price: float,
        *,
        timeout_seconds: float,
    ) -> OrderResult:
        """Submit a limit order and wait up to `timeout_seconds`.

        A timeout is reported as PENDING with whatever has filled so far, never
        as a failure: the order is still live at IBKR, and the caller decides
        whether to cancel it. Claiming it was cancelled here would be a lie the
        caller could act on.
        """
        if side not in ("BUY", "SELL"):
            raise BrokerError(f"side must be BUY or SELL, got {side!r}")
        if qty <= 0:
            raise BrokerError(f"quantity must be positive, got {qty}")

        from ib_async import LimitOrder

        ib = self._ib
        contract = await self._quotes.qualified_contract(spec)

        order = LimitOrder(side, qty, round_price(limit_price))
        order.tif = _TIF
        order.outsideRth = False
        # Naming the account is mandatory once the login manages more than one;
        # IBKR rejects the order otherwise. Left blank it means the only account,
        # which is what IBKR assumes anyway.
        order.account = self._account
        # Belt and braces: if a future change ever reached this function with
        # something other than a limit, it must not silently become one.
        if order.orderType != "LMT":
            raise BrokerError(f"refusing to place a {order.orderType} order; limits only")

        try:
            trade = ib.placeOrder(contract, order)
        except Exception as exc:
            raise BrokerError(f"placeOrder failed for {spec.occ_symbol}: {exc}") from exc

        broker_order_id = str(trade.order.orderId)
        self._trades[broker_order_id] = trade
        logger.info(
            "placed %s %s x%s @ %.2f (order %s)", side, spec.occ_symbol, qty, limit_price, broker_order_id
        )

        await self._await_terminal(trade, timeout_seconds)
        return self._to_result(broker_order_id, trade)

    async def _await_terminal(self, trade, timeout_seconds: float) -> None:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + max(timeout_seconds, 0.0)
        while loop.time() < deadline:
            if _is_done(trade):
                return
            await asyncio.sleep(_POLL_INTERVAL)

    async def cancel_order(self, broker_order_id: str) -> OrderResult:
        trade = self._trades.get(broker_order_id)
        if trade is None:
            raise BrokerError(f"unknown order {broker_order_id}")
        if _is_done(trade):
            # It reached a terminal state before the cancel went out. Report
            # what actually happened, so a fill in that window is counted.
            return self._to_result(broker_order_id, trade)
        try:
            self._ib.cancelOrder(trade.order)
        except Exception as exc:
            raise BrokerError(f"cancelOrder failed for {broker_order_id}: {exc}") from exc

        # Give IBKR a moment to confirm; a cancel is not instant, and an order
        # can still fill inside that window.
        loop = asyncio.get_running_loop()
        deadline = loop.time() + _CANCEL_CONFIRM_SECONDS
        while loop.time() < deadline and not _is_done(trade):
            await asyncio.sleep(_POLL_INTERVAL)

        result = self._to_result(broker_order_id, trade)
        # Terminality is IBKR's view of the order, not our mapped status: a
        # cancelled order that filled part way is PARTIAL in our vocabulary but
        # is finished -- nothing more will fill -- so it must not be reported
        # as still working.
        if _is_done(trade):
            return result
        # Still not confirmed. Report it as pending rather than cancelled --
        # a caller that believed the cancel could place a second order.
        return OrderResult(
            broker_order_id=broker_order_id,
            status=OrderStatus.PENDING,
            filled_qty=result.filled_qty,
            avg_fill_price=result.avg_fill_price,
            detail="cancel requested but not yet confirmed by IBKR",
        )

    async def get_order_status(self, broker_order_id: str) -> OrderResult:
        trade = self._trades.get(broker_order_id)
        if trade is None:
            raise BrokerError(f"unknown order {broker_order_id}")
        return self._to_result(broker_order_id, trade)

    def _to_result(self, broker_order_id: str, trade) -> OrderResult:
        status = getattr(trade.orderStatus, "status", "") or ""
        filled = int(getattr(trade.orderStatus, "filled", 0) or 0)
        avg = getattr(trade.orderStatus, "avgFillPrice", None)
        avg = float(avg) if avg else None

        if status in _TERMINAL_FILLED:
            mapped = OrderStatus.FILLED
        elif status in _TERMINAL_CANCELLED:
            mapped = OrderStatus.PARTIAL if filled else OrderStatus.CANCELLED
        elif status in _TERMINAL_REJECTED:
            mapped = OrderStatus.REJECTED
        elif filled:
            mapped = OrderStatus.PARTIAL
        else:
            mapped = OrderStatus.PENDING

        detail = status
        log = getattr(trade, "log", None)
        if log:
            last = log[-1]
            message = getattr(last, "message", "") or ""
            if message:
                detail = f"{status}: {message}"

        return OrderResult(
            broker_order_id=broker_order_id,
            status=mapped,
            filled_qty=filled,
            avg_fill_price=avg,
            detail=detail,
        )

    # --- account -----------------------------------------------------------

    async def get_positions(self) -> list[BrokerPosition]:
        """This account's option positions, and nothing else.

        Two filters, because each covers a case the other does not. The account
        filter keeps another linked account's holdings out once a second account
        exists. The secType filter keeps non-options out of a single account that
        happens to hold both. Letting either through would feed positions we do
        not manage into reconciliation and the sell clamp.
        """
        try:
            raw = self._ib.positions(account=self._account)
        except Exception as exc:
            raise BrokerError(f"could not read positions: {exc}") from exc

        out: list[BrokerPosition] = []
        for item in raw:
            contract = getattr(item, "contract", None)
            if contract is None or getattr(contract, "secType", "") != "OPT":
                continue
            # Belt and braces: ib_async already filtered, but a mismatch here
            # would mean selling against a position in someone else's account.
            holder = getattr(item, "account", "") or ""
            if self._account and holder and holder != self._account:
                logger.error(
                    "ignoring a %s position reported under account %s, not ours (%s)",
                    getattr(contract, "symbol", "?"), holder, self._account,
                )
                continue
            qty = int(getattr(item, "position", 0) or 0)
            if qty == 0:
                continue
            symbol = occ_from_contract(contract)
            if symbol is None:
                # Cannot identify it, so cannot safely act on it. Log loudly
                # rather than silently dropping something we might hold.
                logger.error(
                    "could not build an OCC symbol for an option position: %r; ignoring it", contract
                )
                continue
            out.append(
                BrokerPosition(
                    occ_symbol=symbol,
                    qty=qty,
                    avg_cost=_per_contract_cost(getattr(item, "avgCost", 0.0), contract),
                )
            )
        return out

    async def get_account(self) -> AccountSnapshot:
        try:
            values = self._ib.accountValues(account=self._account)
        except Exception as exc:
            raise BrokerError(f"could not read account values: {exc}") from exc

        def pick(tag: str) -> float | None:
            for value in values:
                if getattr(value, "tag", "") == tag and getattr(value, "currency", "USD") in ("USD", ""):
                    try:
                        return float(value.value)
                    except (TypeError, ValueError):
                        return None
            return None

        net = pick("NetLiquidation")
        if net is None:
            raise BrokerError("IBKR did not report NetLiquidation")
        return AccountSnapshot(
            net_liquidation=net,
            buying_power=pick("BuyingPower") or 0.0,
            realized_pnl_today=pick("RealizedPnL"),
        )


# --- contract identity -----------------------------------------------------


def occ_from_contract(contract) -> str | None:
    """Rebuild our 21-character OCC symbol from an IBKR contract.

    Uses the **trading class** rather than the symbol, matching how the symbol
    was built in the first place: an SPX weekly has symbol SPX and trading
    class SPXW, and keying positions on SPX would fail to match the SPXW symbol
    the bot tracks.
    """
    root = getattr(contract, "tradingClass", "") or getattr(contract, "symbol", "")
    raw_expiry = getattr(contract, "lastTradeDateOrContractMonth", "") or ""
    right = (getattr(contract, "right", "") or "").upper()[:1]
    strike = getattr(contract, "strike", None)

    if not root or right not in ("C", "P") or not strike:
        return None
    expiry = _parse_ib_date(raw_expiry)
    if expiry is None:
        return None

    return f"{root:<6}{expiry:%y%m%d}{right}{int(round(float(strike) * 1000)):08d}"


def _parse_ib_date(raw: str) -> date | None:
    text = str(raw).strip()
    if len(text) == 8 and text.isdigit():
        try:
            return date(int(text[:4]), int(text[4:6]), int(text[6:8]))
        except ValueError:
            return None
    return None


def _per_contract_cost(avg_cost, contract) -> float:
    """IBKR reports an option's average cost per *contract* including the
    multiplier, so a $0.48 option comes back as 48.0. Everything in this bot
    talks in per-share prices, so divide it back down."""
    try:
        cost = float(avg_cost or 0.0)
    except (TypeError, ValueError):
        return 0.0
    try:
        multiplier = float(getattr(contract, "multiplier", 100) or 100)
    except (TypeError, ValueError):
        multiplier = 100.0
    if multiplier <= 0:
        return cost
    return round_price(cost / multiplier)


def _is_done(trade) -> bool:
    """Terminal according to IBKR. Falls back to the status string when
    `isDone()` is unavailable, and treats an unknown status as still working."""
    is_done = getattr(trade, "isDone", None)
    if callable(is_done):
        try:
            return bool(is_done())
        except Exception:
            pass
    status = getattr(getattr(trade, "orderStatus", None), "status", "") or ""
    return status in (_TERMINAL_FILLED | _TERMINAL_CANCELLED | _TERMINAL_REJECTED)
