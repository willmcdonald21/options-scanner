"""The broker interface every adapter implements.

Defined now, ahead of any implementation, so the rest of the bot is written
against an abstraction rather than against `ib_async`. The concrete adapters
arrive in later phases: `PaperBroker` (real quotes, simulated fills) in
phase 4 and `IBKRBroker` in phase 5.

Deliberately narrow. No adapter is allowed to place a stop order, because the
bot's stops are synthetic -- a broker-side stop on a 0DTE option gets
triggered by a bad quote on a wide spread, which is the whole reason for
managing them here instead.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime
from enum import Enum

from options_scanner.contracts import ContractSpec


class OrderStatus(str, Enum):
    PENDING = "PENDING"
    PARTIAL = "PARTIAL"
    FILLED = "FILLED"
    CANCELLED = "CANCELLED"
    REJECTED = "REJECTED"

    @property
    def is_done(self) -> bool:
        return self in (OrderStatus.FILLED, OrderStatus.CANCELLED, OrderStatus.REJECTED)


@dataclass(frozen=True)
class Quote:
    """A point-in-time quote. `asof` is carried so the caller can tell a live
    quote from a stale one -- with synthetic stops, acting on a stale quote is
    worse than not acting."""

    bid: float | None
    ask: float | None
    last: float | None
    asof: datetime

    @property
    def mid(self) -> float | None:
        if self.bid is None or self.ask is None:
            return None
        return (self.bid + self.ask) / 2.0

    @property
    def is_two_sided(self) -> bool:
        return self.bid is not None and self.ask is not None and self.bid > 0 and self.ask > 0

    def exit_price(self, blend: float = 0.0) -> float | None:
        """The price to judge a stop against. `blend` 0 is the pure bid -- what
        we could actually sell into -- and 1 is the mid. Anything in between
        trades a little honesty for a little less spread noise."""
        if self.bid is None or self.bid <= 0:
            return None
        if blend <= 0.0 or self.mid is None:
            return self.bid
        return self.bid + blend * (self.mid - self.bid)


@dataclass(frozen=True)
class OrderResult:
    """What came back from submitting an order."""

    broker_order_id: str
    status: OrderStatus
    filled_qty: int = 0
    avg_fill_price: float | None = None
    detail: str = ""

    @property
    def is_filled(self) -> bool:
        return self.status is OrderStatus.FILLED

    @property
    def is_partial(self) -> bool:
        return self.status is OrderStatus.PARTIAL and self.filled_qty > 0


@dataclass(frozen=True)
class BrokerPosition:
    occ_symbol: str
    qty: int
    avg_cost: float


@dataclass(frozen=True)
class AccountSnapshot:
    net_liquidation: float
    buying_power: float
    realized_pnl_today: float | None = None


class BrokerError(RuntimeError):
    """Any adapter-level failure. Raised rather than returned so a caller
    cannot accidentally proceed as though an order had been placed."""


class Broker(ABC):
    """Abstract broker. Implementations must be safe to call from asyncio."""

    @property
    @abstractmethod
    def name(self) -> str:
        """Short identifier for logs and notifications, e.g. "paper", "ibkr"."""

    @abstractmethod
    async def connect(self) -> None:
        """Establish the session. Must raise BrokerError on failure rather
        than leaving a half-connected adapter behind."""

    @abstractmethod
    async def disconnect(self) -> None: ...

    @property
    @abstractmethod
    def is_connected(self) -> bool: ...

    @abstractmethod
    async def get_option_chain(self, spec: ContractSpec) -> bool:
        """Confirm the contract actually exists and is tradeable.

        Returns True when found. The spec requires this before any order, so a
        mis-parsed strike or a wrong trading class fails here instead of
        becoming a rejected order -- or worse, a silently different contract.
        """

    @abstractmethod
    async def get_quote(self, spec: ContractSpec) -> Quote:
        """Current quote. Implementations should return a Quote with None
        fields rather than raising when the book is simply empty."""

    @abstractmethod
    async def place_order(
        self,
        spec: ContractSpec,
        side: str,
        qty: int,
        limit_price: float,
        *,
        timeout_seconds: float,
    ) -> OrderResult:
        """Submit a limit order and wait up to `timeout_seconds` for a
        terminal state.

        Limit only, by design: there is no market-order path anywhere in this
        interface. A market order on a wide 0DTE book is an open-ended cost.
        """

    @abstractmethod
    async def cancel_order(self, broker_order_id: str) -> OrderResult:
        """Cancel, and report the state it ended in -- an order can fill in
        the moment between deciding to cancel and the cancel arriving."""

    @abstractmethod
    async def get_order_status(self, broker_order_id: str) -> OrderResult: ...

    @abstractmethod
    async def get_positions(self) -> list[BrokerPosition]:
        """Positions as the broker sees them. The authority for "how many do
        we actually hold" -- checked before every sell so the bot can never
        sell more than it holds or flip short."""

    @abstractmethod
    async def get_account(self) -> AccountSnapshot: ...
