"""Shared fixtures and fakes.

Nothing here touches Discord, IBKR or the network. The Discord layer is a thin
adapter by design (see options_scanner/discord_bot.py), so the whole alert path
is exercised through `AlertPipeline` with a fake broker.
"""

from __future__ import annotations

from datetime import date, datetime

import pytest

from options_scanner.broker.base import (
    AccountSnapshot,
    Broker,
    BrokerError,
    BrokerPosition,
    OrderResult,
    OrderStatus,
    Quote,
)
from options_scanner.config import Settings
from options_scanner.market_hours import EASTERN
from options_scanner.pipeline import AlertPipeline
from options_scanner.risk import RiskGate
from options_scanner.storage import Storage

# A regular Wednesday session, mid-morning: open, well before the cutoff.
TRADING_DAY = date(2026, 10, 6)
MID_SESSION = datetime(2026, 10, 6, 11, 0, tzinfo=EASTERN)

# The spec's canonical alert. 0DTE on TRADING_DAY.
SPEC_ALERT = """BUY — SPX 7815P · 0DTE
Entered SPX Oct06 '26 7815 Put
Open Live Dashboard →
Entry
$0.475
Contracts
25
Cost
$1,188
Trim Targets
25%   $0.594
50%   $0.713
75%   $0.831
100%  $0.950
News backdrop
 Broad Market leaning up — 2 up / 1 down in the last 6h

Not financial advice"""

SPX_OCC = "SPXW  261006P07815000"


def env(monkeypatch) -> None:
    for name, value in (
        ("DISCORD_BOT_TOKEN", "test-token-not-a-real-secret"),
        ("ALERTS_CHANNEL_ID", "1001"),
        ("UPDATES_CHANNEL_ID", "2002"),
        ("OWNER_USER_ID", "3003"),
    ):
        monkeypatch.setenv(name, value)


def make_settings(**overrides) -> Settings:
    """Settings built directly, bypassing config.yaml so a test never depends
    on whatever the committed config happens to say today."""
    base = {
        "mode": "dry_run",
        "discord": {
            "bot_token": "test-token-not-a-real-secret",
            "alerts_channel_id": 1001,
            "updates_channel_id": 2002,
            "owner_user_id": 3003,
        },
    }
    base.update(overrides)
    return Settings.model_validate(base)


class FakeBroker(Broker):
    """Records calls; scripted responses. Quotes default to a two-sided book
    around the alert's entry price."""

    def __init__(
        self,
        *,
        chain_has_contract: bool = True,
        bid: float | None = 0.47,
        ask: float | None = 0.48,
        chain_raises: bool = False,
        quote_raises: bool = False,
    ):
        self.chain_has_contract = chain_has_contract
        self.chain_raises = chain_raises
        self.quote_raises = quote_raises
        self._bid = bid
        self._ask = ask
        self.connected = False
        self.chain_lookups: list[str] = []
        self.quote_lookups: list[str] = []
        self.orders: list[tuple[str, str, int, float]] = []
        self.cancelled: list[str] = []
        self.positions: list[BrokerPosition] = []

    @property
    def name(self) -> str:
        return "fake"

    async def connect(self) -> None:
        self.connected = True

    async def disconnect(self) -> None:
        self.connected = False

    @property
    def is_connected(self) -> bool:
        return self.connected

    async def get_option_chain(self, spec) -> bool:
        self.chain_lookups.append(spec.occ_symbol)
        if self.chain_raises:
            raise BrokerError("chain service unavailable")
        return self.chain_has_contract

    async def get_quote(self, spec) -> Quote:
        self.quote_lookups.append(spec.occ_symbol)
        if self.quote_raises:
            raise BrokerError("no market data subscription")
        return Quote(bid=self._bid, ask=self._ask, last=self._bid, asof=MID_SESSION)

    def set_quote(self, bid: float | None, ask: float | None) -> None:
        self._bid, self._ask = bid, ask

    async def place_order(self, spec, side, qty, limit_price, *, timeout_seconds) -> OrderResult:
        self.orders.append((spec.occ_symbol, side, qty, limit_price))
        return OrderResult(
            broker_order_id=f"fake-{len(self.orders)}",
            status=OrderStatus.FILLED,
            filled_qty=qty,
            avg_fill_price=limit_price,
        )

    async def cancel_order(self, broker_order_id: str) -> OrderResult:
        self.cancelled.append(broker_order_id)
        return OrderResult(broker_order_id=broker_order_id, status=OrderStatus.CANCELLED)

    async def get_order_status(self, broker_order_id: str) -> OrderResult:
        return OrderResult(broker_order_id=broker_order_id, status=OrderStatus.FILLED)

    async def get_positions(self) -> list[BrokerPosition]:
        return list(self.positions)

    async def get_account(self) -> AccountSnapshot:
        return AccountSnapshot(net_liquidation=100_000.0, buying_power=50_000.0)


@pytest.fixture
def settings() -> Settings:
    return make_settings()


@pytest.fixture
def storage(tmp_path) -> Storage:
    store = Storage(tmp_path / "test.sqlite3")
    yield store
    store.close()


@pytest.fixture
def risk(settings, storage) -> RiskGate:
    return RiskGate(settings, storage)


@pytest.fixture
def broker() -> FakeBroker:
    return FakeBroker()


@pytest.fixture
def pipeline(settings, storage, risk, broker) -> AlertPipeline:
    return AlertPipeline(settings, storage, risk, broker)


async def paste(pipeline: AlertPipeline, text: str = SPEC_ALERT, *, message_id: int = 500_001, **kwargs):
    """Push one paste through the pipeline with sensible defaults."""
    params = {
        "message_id": message_id,
        "channel_id": 1001,
        "author_id": 3003,
        "text": text,
        "jump_url": f"https://discord.com/channels/1/1001/{message_id}",
        "moment": MID_SESSION,
        "trading_day": TRADING_DAY,
    }
    params.update(kwargs)
    return await pipeline.handle_paste(**params)


def titles(result) -> list[str]:
    return [n.title for n in result.notifications]


def find(result, needle: str):
    """The first notification whose title contains `needle`."""
    for note in result.notifications:
        if needle.lower() in note.title.lower():
            return note
    raise AssertionError(f"no notification matching {needle!r} in {titles(result)}")
