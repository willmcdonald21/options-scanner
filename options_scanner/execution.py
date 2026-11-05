"""Order execution: buying in at market, and getting out again with limits.

Entries are one market order, so a call that passed the pipeline's checks is
bought rather than missed. The pipeline is what refuses an ask too far above
the alert; this module only sends the order and accounts for what filled.

Exits are limits, priced *through* the bid so they fill. A
synthetic stop that has triggered has already decided the position should be
gone, so the risk worth minimising there is not slippage but failing to exit.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field

from options_scanner.broker.base import Broker, BrokerError, OrderResult, OrderStatus
from options_scanner.contracts import ContractSpec
from options_scanner.rules import round_price

logger = logging.getLogger("options_scanner.execution")


@dataclass
class Attempt:
    """One order placed, and what came of it. Kept so the audit trail can show
    every order rather than only the outcome."""

    limit_price: float | None  # None is a market order
    status: str
    filled_qty: int = 0
    broker_order_id: str | None = None
    detail: str = ""


@dataclass
class Outcome:
    requested_qty: int
    filled_qty: int = 0
    avg_price: float | None = None
    attempts: list[Attempt] = field(default_factory=list)
    detail: str = ""

    @property
    def status(self) -> str:
        if self.filled_qty == 0:
            return "UNFILLED"
        return "FILLED" if self.filled_qty >= self.requested_qty else "PARTIAL"

    @property
    def is_filled(self) -> bool:
        return self.filled_qty >= self.requested_qty

    @property
    def any_fill(self) -> bool:
        return self.filled_qty > 0

    @property
    def cost(self) -> float:
        return (self.avg_price or 0.0) * self.filled_qty * 100


async def execute_entry(
    broker: Broker,
    spec: ContractSpec,
    qty: int,
    *,
    timeout_seconds: float,
) -> Outcome:
    """Buy `qty` with one market order.

    The caller has already checked the ask against its cap: once sent, a
    market order pays whatever the book asks. An order still working at the
    timeout -- an empty book, a halted contract -- is cancelled, so nothing is
    left live that nobody is watching.

    Returns rather than raises on failure. A partial fill is a legitimate
    outcome: the caller carries on with whatever actually filled, because the
    ladder has to be measured against contracts we hold, not contracts we
    hoped to hold.
    """
    outcome = Outcome(requested_qty=qty)
    try:
        result = await broker.place_market_order(spec, "BUY", qty, timeout_seconds=timeout_seconds)
    except BrokerError as exc:
        logger.warning("market entry for %s failed: %s", spec.occ_symbol, exc)
        outcome.attempts.append(Attempt(None, "ERROR", detail=str(exc)))
        outcome.detail = f"market order failed: {exc}"
        return outcome

    filled = _filled(result)
    outcome.attempts.append(
        Attempt(None, result.status.value, filled, result.broker_order_id, result.detail)
    )
    outcome.filled_qty = filled
    avg = result.avg_fill_price if filled else None

    if result.status is OrderStatus.REJECTED:
        outcome.detail = f"market order rejected: {result.detail}"
    elif not result.status.is_done:
        extra, price = await _cancel_quietly(
            broker, result.broker_order_id, outcome, already_counted=filled
        )
        # A cancel reports the order's cumulative average, so once it adds
        # contracts its price is the whole order's price.
        if extra and price:
            avg = price

    if outcome.filled_qty and avg:
        outcome.avg_price = round_price(avg)
    if not outcome.detail and not outcome.any_fill:
        outcome.detail = f"market order for {qty} did not fill within {timeout_seconds:.0f}s; cancelled"
    return outcome


async def execute_exit(
    broker: Broker,
    spec: ContractSpec,
    qty: int,
    *,
    bid: float,
    through_pct: float,
    timeout_seconds: float,
    retries: int,
) -> Outcome:
    """Sell `qty` at a marketable limit priced through the bid.

    Retries because the decision to exit has already been made; the only
    question left is whether the order fills. Each retry re-prices off the
    latest quote, so a falling market is chased *down* here -- the opposite of
    an entry, and deliberately so.
    """
    outcome = Outcome(requested_qty=qty)
    remaining = qty
    total_proceeds = 0.0
    current_bid = bid

    for attempt_number in range(1, retries + 1):
        if remaining <= 0:
            break
        limit = round_price(max(current_bid * (1 - through_pct / 100.0), 0.01))
        try:
            result = await broker.place_order(
                spec, "SELL", remaining, limit, timeout_seconds=timeout_seconds
            )
        except BrokerError as exc:
            logger.warning("exit attempt %s at %.2f failed: %s", attempt_number, limit, exc)
            outcome.attempts.append(Attempt(limit, "ERROR", detail=str(exc)))
            current_bid = await _refresh_bid(broker, spec, current_bid)
            continue

        filled = _filled(result)
        outcome.attempts.append(
            Attempt(limit, result.status.value, filled, result.broker_order_id, result.detail)
        )
        if filled:
            total_proceeds += filled * (result.avg_fill_price or limit)
            remaining -= filled
            outcome.filled_qty += filled

        if remaining <= 0:
            break

        if not result.status.is_done:
            extra, price = await _cancel_quietly(
                broker, result.broker_order_id, outcome, already_counted=filled
            )
            if extra:
                total_proceeds += extra * (price or limit)
                remaining -= extra
                if remaining <= 0:
                    break

        current_bid = await _refresh_bid(broker, spec, current_bid)

    if outcome.filled_qty:
        outcome.avg_price = round_price(total_proceeds / outcome.filled_qty)
    if remaining > 0:
        outcome.detail = (
            f"only {outcome.filled_qty} of {qty} sold after {len(outcome.attempts)} attempt(s); "
            f"{remaining} still held"
        )
    return outcome


async def safe_sell_qty(broker: Broker, spec: ContractSpec, wanted: int) -> int:
    """Clamp a sell to what the broker says we actually hold.

    Checked before every exit, not trusted from local state: selling more than
    is held would open a short position, which this bot must never do. A broker
    that cannot answer gets the benefit of the doubt only downward -- an
    unknown position means sell nothing.
    """
    if wanted <= 0:
        return 0
    try:
        positions = await broker.get_positions()
    except BrokerError as exc:
        logger.error("could not read positions before selling %s: %s", spec.occ_symbol, exc)
        return 0
    held = next((p.qty for p in positions if p.occ_symbol == spec.occ_symbol), None)
    if held is None:
        logger.error("broker reports no position in %s; refusing to sell", spec.occ_symbol)
        return 0
    if held < wanted:
        logger.warning(
            "broker holds %s %s but %s was requested; clamping", held, spec.occ_symbol, wanted
        )
    return max(0, min(wanted, held))


async def _refresh_bid(broker: Broker, spec: ContractSpec, fallback: float) -> float:
    try:
        quote = await broker.get_quote(spec)
    except BrokerError:
        return fallback
    return quote.bid if quote.bid and quote.bid > 0 else fallback


async def _cancel_quietly(
    broker: Broker,
    broker_order_id: str | None,
    outcome: Outcome,
    already_counted: int = 0,
) -> tuple[int, float]:
    """Cancel a working order, tolerating the race where it fills first.

    Returns (extra quantity filled, price). `already_counted` matters: a cancel
    reports the order's *cumulative* fill, so an order that had already partly
    filled would otherwise have those same contracts counted twice.
    """
    if not broker_order_id:
        return 0, 0.0
    try:
        result = await broker.cancel_order(broker_order_id)
    except BrokerError as exc:
        logger.warning("could not cancel %s: %s", broker_order_id, exc)
        return 0, 0.0

    extra = max(0, _filled(result) - already_counted)
    if not extra:
        return 0, 0.0

    price = result.avg_fill_price or 0.0
    logger.info("order %s filled %s more during cancellation", broker_order_id, extra)
    outcome.filled_qty += extra
    outcome.attempts.append(
        Attempt(price, "FILLED_ON_CANCEL", extra, broker_order_id, "filled while being cancelled")
    )
    return extra, price


def _filled(result: OrderResult) -> int:
    """Filled quantity, trusting `filled_qty` but treating a FILLED status with
    a zero count as a complete fill, since adapters differ."""
    if result.filled_qty:
        return int(result.filled_qty)
    return 0


async def sleep(seconds: float) -> None:
    """Indirection so the polling loop can be driven instantly in tests."""
    await asyncio.sleep(seconds)
