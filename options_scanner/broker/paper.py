"""PaperBroker: real quotes, simulated fills.

The point of this adapter is to exercise the whole bot -- entry walking,
the trim ladder, the trailing stop, restart reconciliation -- against prices
that actually move, without sending anything to a broker. Quotes come from a
pluggable source so the same adapter can run on a live IBKR feed or on a
scripted price path in a test.

Fill simulation is deliberately pessimistic:

* a BUY fills only if the **ask** is at or below the limit, and fills at the
  ask, not at the limit -- you do not get a better price than the book offers
* a SELL fills only if the **bid** is at or above the limit, and fills at the
  bid
* a one-sided or missing quote fills nothing

Being pessimistic matters because an optimistic simulator would make the trim
ladder look like it fills at exactly its rung every time, which is the single
most misleading thing a paper run could tell you.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Awaitable, Callable, Protocol

from options_scanner.broker.base import (
    AccountSnapshot,
    Broker,
    BrokerError,
    BrokerPosition,
    OrderResult,
    OrderStatus,
    Quote,
)
from options_scanner.contracts import ContractSpec
from options_scanner.rules import round_price

logger = logging.getLogger("options_scanner.broker.paper")


class QuoteSource(Protocol):
    """Where a PaperBroker gets its prices."""

    async def quote(self, spec: ContractSpec) -> Quote: ...

    async def exists(self, spec: ContractSpec) -> bool: ...


@dataclass
class ScriptedQuotes:
    """A fixed price path, for tests. Each call advances one step; the last
    quote repeats once the path is exhausted so a loop can keep polling."""

    path: list[tuple[float, float]]  # (bid, ask)
    contracts: set[str] = field(default_factory=set)
    index: int = 0
    asof: datetime = field(default_factory=lambda: datetime(2026, 10, 6, 11, 0))

    async def quote(self, spec: ContractSpec) -> Quote:
        if not self.path:
            return Quote(bid=None, ask=None, last=None, asof=self.asof)
        step = min(self.index, len(self.path) - 1)
        bid, ask = self.path[step]
        self.index += 1
        return Quote(bid=bid, ask=ask, last=bid, asof=self.asof)

    async def exists(self, spec: ContractSpec) -> bool:
        return not self.contracts or spec.occ_symbol in self.contracts


@dataclass
class _SimOrder:
    broker_order_id: str
    spec: ContractSpec
    side: str
    qty: int
    limit_price: float
    status: OrderStatus = OrderStatus.PENDING
    filled_qty: int = 0
    avg_fill_price: float | None = None


class PaperBroker(Broker):
    """Simulated fills over a real (or scripted) quote feed."""

    def __init__(
        self,
        quotes: QuoteSource,
        *,
        starting_cash: float = 100_000.0,
        fill_fraction: float = 1.0,
        on_fill: Callable[[_SimOrder], Awaitable[None]] | None = None,
    ):
        self._quotes = quotes
        self._cash = starting_cash
        self._starting_cash = starting_cash
        # Fraction of a crossing order that fills at once. 1.0 fills whole;
        # lower values exercise the partial-fill paths the spec requires.
        self._fill_fraction = fill_fraction
        self._on_fill = on_fill
        self._connected = False
        self._orders: dict[str, _SimOrder] = {}
        self._positions: dict[str, BrokerPosition] = {}
        self._next_id = 1
        self.realized_pnl = 0.0

    @property
    def name(self) -> str:
        return "paper"

    # --- session -----------------------------------------------------------

    async def connect(self) -> None:
        connect = getattr(self._quotes, "connect", None)
        if connect is not None:
            await connect()
        self._connected = True

    async def disconnect(self) -> None:
        disconnect = getattr(self._quotes, "disconnect", None)
        if disconnect is not None:
            await disconnect()
        self._connected = False

    @property
    def is_connected(self) -> bool:
        return self._connected

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
        if side not in ("BUY", "SELL"):
            raise BrokerError(f"side must be BUY or SELL, got {side!r}")
        if qty <= 0:
            raise BrokerError(f"quantity must be positive, got {qty}")

        if side == "SELL":
            # The simulator enforces the same rule the real bot must obey: it
            # cannot sell what it does not hold, and must never go short.
            held = self._positions.get(spec.occ_symbol)
            if held is None or held.qty < qty:
                raise BrokerError(
                    f"cannot sell {qty} {spec.occ_symbol}; simulated position is "
                    f"{held.qty if held else 0}"
                )

        order = _SimOrder(
            broker_order_id=f"paper-{self._next_id}",
            spec=spec,
            side=side,
            qty=qty,
            limit_price=round_price(limit_price),
        )
        self._next_id += 1
        self._orders[order.broker_order_id] = order

        quote = await self._quotes.quote(spec)
        fill_price = self._crossing_price(order, quote)

        if fill_price is None:
            # Does not cross. A real limit would sit on the book; the simulator
            # reports it still working so the caller's timeout logic runs.
            order.status = OrderStatus.PENDING
            return OrderResult(
                broker_order_id=order.broker_order_id,
                status=OrderStatus.PENDING,
                detail=self._no_cross_detail(order, quote),
            )

        fill_qty = max(1, int(order.qty * self._fill_fraction))
        fill_qty = min(fill_qty, order.qty)
        self._apply_fill(order, fill_qty, fill_price)

        if self._on_fill is not None:
            await self._on_fill(order)

        return OrderResult(
            broker_order_id=order.broker_order_id,
            status=order.status,
            filled_qty=order.filled_qty,
            avg_fill_price=order.avg_fill_price,
            detail="simulated fill",
        )

    def _crossing_price(self, order: _SimOrder, quote: Quote) -> float | None:
        """The price a crossing order fills at, or None if it does not cross.

        A BUY needs the ask at or below its limit and pays the ask; a SELL needs
        the bid at or above its limit and receives the bid. Never better than
        the book -- an optimistic simulator would make every trim look like it
        filled exactly on its rung.
        """
        if order.side == "BUY":
            if quote.ask is None or quote.ask <= 0:
                return None
            return quote.ask if quote.ask <= order.limit_price else None
        if quote.bid is None or quote.bid <= 0:
            return None
        return quote.bid if quote.bid >= order.limit_price else None

    def _no_cross_detail(self, order: _SimOrder, quote: Quote) -> str:
        book = f"bid {quote.bid} / ask {quote.ask}"
        return f"limit {order.limit_price} does not cross ({book})"

    def _apply_fill(self, order: _SimOrder, qty: int, price: float) -> None:
        order.filled_qty += qty
        order.avg_fill_price = price
        order.status = OrderStatus.FILLED if order.filled_qty >= order.qty else OrderStatus.PARTIAL

        symbol = order.spec.occ_symbol
        existing = self._positions.get(symbol)
        if order.side == "BUY":
            self._cash -= qty * price * 100
            if existing is None:
                self._positions[symbol] = BrokerPosition(symbol, qty, price)
            else:
                total = existing.qty + qty
                blended = (existing.avg_cost * existing.qty + price * qty) / total
                self._positions[symbol] = BrokerPosition(symbol, total, round_price(blended))
        else:
            self._cash += qty * price * 100
            assert existing is not None  # guarded in place_order
            self.realized_pnl += (price - existing.avg_cost) * qty * 100
            remaining = existing.qty - qty
            if remaining <= 0:
                del self._positions[symbol]
            else:
                self._positions[symbol] = BrokerPosition(symbol, remaining, existing.avg_cost)

        logger.info(
            "paper fill: %s %s x%s @ %.2f (position now %s)",
            order.side,
            symbol,
            qty,
            price,
            self._positions.get(symbol).qty if symbol in self._positions else 0,
        )

    async def cancel_order(self, broker_order_id: str) -> OrderResult:
        order = self._orders.get(broker_order_id)
        if order is None:
            raise BrokerError(f"unknown order {broker_order_id}")
        if order.status.is_done:
            return OrderResult(
                broker_order_id=broker_order_id,
                status=order.status,
                filled_qty=order.filled_qty,
                avg_fill_price=order.avg_fill_price,
                detail="already done",
            )
        order.status = OrderStatus.CANCELLED
        return OrderResult(
            broker_order_id=broker_order_id,
            status=OrderStatus.CANCELLED,
            filled_qty=order.filled_qty,
            avg_fill_price=order.avg_fill_price,
        )

    async def get_order_status(self, broker_order_id: str) -> OrderResult:
        order = self._orders.get(broker_order_id)
        if order is None:
            raise BrokerError(f"unknown order {broker_order_id}")
        return OrderResult(
            broker_order_id=broker_order_id,
            status=order.status,
            filled_qty=order.filled_qty,
            avg_fill_price=order.avg_fill_price,
        )

    # --- account -----------------------------------------------------------

    async def get_positions(self) -> list[BrokerPosition]:
        return list(self._positions.values())

    async def get_account(self) -> AccountSnapshot:
        return AccountSnapshot(
            net_liquidation=self._cash,
            buying_power=self._cash,
            realized_pnl_today=self.realized_pnl,
        )

    def release(self, occ_symbol: str) -> None:
        """Drop the quote subscription for a closed position. Market-data lines
        are a limited, shared account resource."""
        release = getattr(self._quotes, "release", None)
        if release is not None:
            release(occ_symbol)

    # --- test and reconciliation helpers ----------------------------------

    def seed_position(self, occ_symbol: str, qty: int, avg_cost: float) -> None:
        """Pre-load a position, for simulating a restart into an existing
        holding."""
        self._positions[occ_symbol] = BrokerPosition(occ_symbol, qty, avg_cost)

    def drop_position(self, occ_symbol: str) -> None:
        """Make a position vanish without an order, as an account-wide flatten
        from another process would. Used to test phantom-exit detection."""
        self._positions.pop(occ_symbol, None)
