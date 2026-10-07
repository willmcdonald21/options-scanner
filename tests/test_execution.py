"""Order execution: walking a limit into a fill, and getting back out.

Limit orders only -- there is no market-order path to test, by design.
"""

from datetime import date, datetime

import pytest

from options_scanner.broker.base import BrokerError, OrderResult, OrderStatus, Quote
from options_scanner.contracts import build_spec
from options_scanner.execution import (
    execute_entry,
    execute_exit,
    safe_sell_qty,
    walk_prices,
)
from options_scanner.models import OptionKey

SPEC = build_spec(OptionKey("SPX", date(2026, 10, 6), 7815.0, "P"))
NOW = datetime(2026, 10, 6, 11, 0)


class StubBroker:
    """Scripted order results, so the walking logic can be driven exactly."""

    def __init__(self, results, *, bid=0.47, ask=0.48, positions=None, raise_on=()):
        self._results = list(results)
        self.bid, self.ask = bid, ask
        self.placed: list[tuple[str, int, float]] = []
        self.cancelled: list[str] = []
        self._positions = positions if positions is not None else []
        self._raise_on = set(raise_on)
        self._n = 0

    @property
    def is_connected(self):
        return True

    async def place_order(self, spec, side, qty, limit_price, *, timeout_seconds):
        self._n += 1
        self.placed.append((side, qty, limit_price))
        if self._n in self._raise_on:
            raise BrokerError("broker said no")
        if not self._results:
            return OrderResult(f"id-{self._n}", OrderStatus.PENDING, detail="no cross")
        return self._results.pop(0)

    async def cancel_order(self, broker_order_id):
        self.cancelled.append(broker_order_id)
        return OrderResult(broker_order_id, OrderStatus.CANCELLED)

    async def get_quote(self, spec):
        return Quote(bid=self.bid, ask=self.ask, last=self.bid, asof=NOW)

    async def get_positions(self):
        return list(self._positions)


def filled(qty, price, order_id="id-1"):
    return OrderResult(order_id, OrderStatus.FILLED, filled_qty=qty, avg_fill_price=price)


def partial(qty, price, order_id="id-1"):
    return OrderResult(order_id, OrderStatus.PARTIAL, filled_qty=qty, avg_fill_price=price)


def pending(order_id="id-1"):
    return OrderResult(order_id, OrderStatus.PENDING, detail="resting")


def rejected(detail="no permission", order_id="id-1"):
    return OrderResult(order_id, OrderStatus.REJECTED, detail=detail)


# --- the price walk -------------------------------------------------------


def test_the_walk_starts_at_the_alert_price_and_ends_at_the_cap():
    prices = walk_prices(0.475, 0.523, 3)
    assert prices[0] == 0.48  # rounded to a tick
    assert prices[-1] == 0.52


def test_a_single_step_only_tries_the_start_price():
    assert walk_prices(0.475, 0.523, 1) == [0.48]


def test_a_cap_at_or_below_the_start_collapses_to_one_price():
    assert walk_prices(0.50, 0.50, 3) == [0.50]
    assert walk_prices(0.50, 0.40, 3) == [0.50]


def test_steps_that_round_onto_the_same_cent_are_collapsed():
    """Re-placing an identical limit achieves nothing but audit-trail noise."""
    prices = walk_prices(0.20, 0.21, 5)
    assert prices == sorted(set(prices))
    assert len(prices) <= 2


def test_the_walk_is_monotonic_and_never_exceeds_the_cap():
    prices = walk_prices(1.00, 1.10, 6)
    assert prices == sorted(prices)
    assert max(prices) == 1.10


def test_zero_steps_is_rejected():
    with pytest.raises(ValueError, match="at least 1"):
        walk_prices(0.5, 0.6, 0)


# --- entries --------------------------------------------------------------


async def test_a_fill_at_the_first_price_stops_walking():
    broker = StubBroker([filled(21, 0.475)])

    outcome = await execute_entry(
        broker, SPEC, 21, start_price=0.475, cap_price=0.523, steps=3, timeout_seconds=30
    )

    assert outcome.is_filled
    assert outcome.filled_qty == 21
    assert outcome.avg_price == 0.48
    assert len(broker.placed) == 1  # never walked past the advertised price


async def test_an_unfilled_attempt_is_cancelled_before_the_price_moves():
    """Two live limits at different prices could both fill."""
    broker = StubBroker([pending("a"), filled(21, 0.50, "b")])

    outcome = await execute_entry(
        broker, SPEC, 21, start_price=0.475, cap_price=0.523, steps=3, timeout_seconds=3
    )

    assert outcome.is_filled
    assert broker.cancelled == ["a"]
    assert [p[2] for p in broker.placed] == [0.48, 0.50]


async def test_the_walk_gives_up_at_the_cap_without_chasing():
    broker = StubBroker([pending("a"), pending("b"), pending("c"), pending("d")])

    outcome = await execute_entry(
        broker, SPEC, 21, start_price=0.475, cap_price=0.523, steps=3, timeout_seconds=3
    )

    assert outcome.status == "UNFILLED"
    assert outcome.filled_qty == 0
    assert "not chasing" in outcome.detail
    assert max(p[2] for p in broker.placed) <= 0.53


async def test_a_rejection_stops_the_walk_immediately():
    """A rejection is about the order, not the price, so walking will not help."""
    broker = StubBroker([rejected("contract closed")])

    outcome = await execute_entry(
        broker, SPEC, 21, start_price=0.475, cap_price=0.523, steps=3, timeout_seconds=3
    )

    assert outcome.filled_qty == 0
    assert "contract closed" in outcome.detail
    assert len(broker.placed) == 1


async def test_a_partial_fill_continues_with_only_the_remainder():
    broker = StubBroker([partial(10, 0.48, "a"), filled(11, 0.50, "b")])

    outcome = await execute_entry(
        broker, SPEC, 21, start_price=0.475, cap_price=0.523, steps=3, timeout_seconds=3
    )

    assert outcome.filled_qty == 21
    assert broker.placed[1][1] == 11  # asked for the remainder, not 21 again
    # Blended average of 10 @ 0.48 and 11 @ 0.50.
    assert outcome.avg_price == pytest.approx(0.49, abs=0.01)


async def test_a_partial_fill_that_never_completes_is_reported_as_partial():
    broker = StubBroker([partial(10, 0.48, "a"), pending("b"), pending("c"), pending("d")])

    outcome = await execute_entry(
        broker, SPEC, 21, start_price=0.475, cap_price=0.523, steps=3, timeout_seconds=3
    )

    assert outcome.status == "PARTIAL"
    assert outcome.filled_qty == 10
    assert outcome.any_fill is True
    assert outcome.is_filled is False


async def test_a_broker_error_on_one_attempt_does_not_abort_the_walk():
    broker = StubBroker([pending("a"), filled(21, 0.50, "b")], raise_on={1})

    outcome = await execute_entry(
        broker, SPEC, 21, start_price=0.475, cap_price=0.523, steps=3, timeout_seconds=3
    )

    assert outcome.is_filled
    assert any(a.status == "ERROR" for a in outcome.attempts)


async def test_an_order_that_fills_while_being_cancelled_is_still_counted():
    """The contracts are ours whether or not the cancel won the race."""

    class RacyBroker(StubBroker):
        async def cancel_order(self, broker_order_id):
            self.cancelled.append(broker_order_id)
            return OrderResult(broker_order_id, OrderStatus.FILLED, filled_qty=21, avg_fill_price=0.49)

    broker = RacyBroker([pending("a")])
    outcome = await execute_entry(
        broker, SPEC, 21, start_price=0.475, cap_price=0.475, steps=1, timeout_seconds=3
    )

    assert outcome.filled_qty == 21
    assert any(a.status == "FILLED_ON_CANCEL" for a in outcome.attempts)


async def test_every_attempt_is_recorded_for_the_audit_trail():
    broker = StubBroker([pending("a"), pending("b"), filled(21, 0.52, "c")])

    outcome = await execute_entry(
        broker, SPEC, 21, start_price=0.475, cap_price=0.523, steps=3, timeout_seconds=3
    )

    assert len(outcome.attempts) == 3
    assert [a.limit_price for a in outcome.attempts] == [0.48, 0.50, 0.52]


# --- exits ----------------------------------------------------------------


async def test_an_exit_prices_through_the_bid_so_it_fills():
    broker = StubBroker([filled(8, 1.07)])

    outcome = await execute_exit(
        broker, SPEC, 8, bid=1.09, through_pct=2.0, timeout_seconds=5, retries=3
    )

    assert outcome.is_filled
    # 1.09 less 2% = 1.0682 -> 1.07, below the bid rather than on it.
    assert broker.placed[0][2] == 1.07
    assert broker.placed[0][2] < 1.09


async def test_an_exit_retries_and_reprices_off_the_falling_bid():
    """The decision to exit is already made, so a falling market is chased
    down here -- the opposite of an entry, deliberately."""
    broker = StubBroker([pending("a"), filled(8, 0.90, "b")], bid=0.92)

    outcome = await execute_exit(
        broker, SPEC, 8, bid=1.09, through_pct=2.0, timeout_seconds=1, retries=3
    )

    assert outcome.is_filled
    assert broker.placed[1][2] < broker.placed[0][2]


async def test_an_exit_that_never_fills_says_how_much_is_still_held():
    broker = StubBroker([pending("a"), pending("b"), pending("c")])

    outcome = await execute_exit(
        broker, SPEC, 8, bid=1.09, through_pct=2.0, timeout_seconds=1, retries=3
    )

    assert outcome.filled_qty == 0
    assert "8 still held" in outcome.detail
    assert len(broker.placed) == 3


async def test_a_partially_filled_exit_reports_the_remainder():
    broker = StubBroker([partial(5, 1.07, "a"), pending("b"), pending("c")], bid=1.05)

    outcome = await execute_exit(
        broker, SPEC, 8, bid=1.09, through_pct=2.0, timeout_seconds=1, retries=3
    )

    assert outcome.filled_qty == 5
    assert "3 still held" in outcome.detail


async def test_an_exit_limit_never_goes_to_zero_or_below():
    broker = StubBroker([filled(1, 0.01)], bid=0.01)

    await execute_exit(broker, SPEC, 1, bid=0.01, through_pct=50.0, timeout_seconds=1, retries=1)

    assert broker.placed[0][2] >= 0.01


# --- the sell clamp -------------------------------------------------------


async def test_a_sell_is_clamped_to_what_the_broker_reports():
    from options_scanner.broker.base import BrokerPosition

    broker = StubBroker([], positions=[BrokerPosition(SPEC.occ_symbol, 5, 0.48)])
    assert await safe_sell_qty(broker, SPEC, 8) == 5
    assert await safe_sell_qty(broker, SPEC, 3) == 3


async def test_selling_is_refused_when_the_broker_reports_no_position():
    """Selling what we do not hold would open a short, which this bot must
    never do."""
    broker = StubBroker([], positions=[])
    assert await safe_sell_qty(broker, SPEC, 8) == 0


async def test_selling_is_refused_when_positions_cannot_be_read():
    class BlindBroker(StubBroker):
        async def get_positions(self):
            raise BrokerError("no connection")

    assert await safe_sell_qty(BlindBroker([]), SPEC, 8) == 0


async def test_a_non_positive_request_sells_nothing():
    broker = StubBroker([], positions=[])
    assert await safe_sell_qty(broker, SPEC, 0) == 0
    assert await safe_sell_qty(broker, SPEC, -1) == 0


# --- the cancel race, counted exactly once --------------------------------


async def test_a_partial_fill_is_not_counted_twice_by_the_cancel():
    """A cancel reports the order's *cumulative* fill. Folding that in on top of
    the partial already counted would double-count the same contracts -- and
    would do so silently, inflating the position the ladder then works from."""

    class PartialThenCancel(StubBroker):
        async def cancel_order(self, broker_order_id):
            self.cancelled.append(broker_order_id)
            # Same 4 contracts, reported again as the order's total.
            return OrderResult(broker_order_id, OrderStatus.CANCELLED, filled_qty=4,
                               avg_fill_price=0.48)

    broker = PartialThenCancel([partial(4, 0.48, "a")])

    outcome = await execute_entry(
        broker, SPEC, 10, start_price=0.475, cap_price=0.475, steps=1, timeout_seconds=1
    )

    assert outcome.filled_qty == 4
    assert outcome.status == "PARTIAL"


async def test_only_the_incremental_fill_from_a_cancel_is_added():
    class FilledMoreOnCancel(StubBroker):
        async def cancel_order(self, broker_order_id):
            self.cancelled.append(broker_order_id)
            # 4 were already counted; the order is now reported at 7.
            return OrderResult(broker_order_id, OrderStatus.CANCELLED, filled_qty=7,
                               avg_fill_price=0.49)

    broker = FilledMoreOnCancel([partial(4, 0.48, "a")])

    outcome = await execute_entry(
        broker, SPEC, 10, start_price=0.475, cap_price=0.475, steps=1, timeout_seconds=1
    )

    assert outcome.filled_qty == 7
    assert any(a.status == "FILLED_ON_CANCEL" and a.filled_qty == 3 for a in outcome.attempts)


async def test_an_exit_does_not_double_count_a_cancelled_partial():
    class PartialThenCancel(StubBroker):
        async def cancel_order(self, broker_order_id):
            self.cancelled.append(broker_order_id)
            return OrderResult(broker_order_id, OrderStatus.CANCELLED, filled_qty=3,
                               avg_fill_price=1.07)

    broker = PartialThenCancel([partial(3, 1.07, "a")], bid=1.05)

    outcome = await execute_exit(
        broker, SPEC, 8, bid=1.09, through_pct=2.0, timeout_seconds=1, retries=1
    )

    assert outcome.filled_qty == 3
    assert "5 still held" in outcome.detail
