"""PaperBroker: simulated fills over a quote feed.

The fill model is deliberately pessimistic -- an optimistic simulator would
make every trim look like it filled exactly on its rung, which is the most
misleading thing a paper run could report.
"""

from datetime import date

import pytest

from options_scanner.broker.base import BrokerError, OrderStatus, Quote
from options_scanner.broker.paper import PaperBroker, ScriptedQuotes
from options_scanner.contracts import build_spec
from options_scanner.models import OptionKey

SPEC = build_spec(OptionKey("SPX", date(2026, 10, 6), 7815.0, "P"))


def broker(path, **kwargs) -> PaperBroker:
    return PaperBroker(ScriptedQuotes(path=path), **kwargs)


# --- session and chain ----------------------------------------------------


async def test_connecting_marks_the_broker_connected():
    paper = broker([(0.47, 0.48)])
    assert paper.is_connected is False
    await paper.connect()
    assert paper.is_connected is True
    await paper.disconnect()
    assert paper.is_connected is False


async def test_the_chain_check_can_report_a_missing_contract():
    quotes = ScriptedQuotes(path=[(0.47, 0.48)], contracts={"SOMETHING ELSE"})
    paper = PaperBroker(quotes)
    assert await paper.get_option_chain(SPEC) is False


# --- buying ---------------------------------------------------------------


async def test_a_buy_fills_at_the_ask_not_at_the_limit():
    """You do not get a better price than the book offers."""
    paper = broker([(0.47, 0.48)] * 4)

    result = await paper.place_order(SPEC, "BUY", 10, 0.52, timeout_seconds=5)

    assert result.status is OrderStatus.FILLED
    assert result.avg_fill_price == 0.48  # the ask, not the 0.52 limit


async def test_a_buy_below_the_ask_does_not_cross():
    paper = broker([(0.47, 0.48)] * 4)

    result = await paper.place_order(SPEC, "BUY", 10, 0.45, timeout_seconds=5)

    assert result.status is OrderStatus.PENDING
    assert result.filled_qty == 0
    assert "does not cross" in result.detail


async def test_a_one_sided_book_fills_nothing():
    paper = PaperBroker(_FixedQuotes(bid=0.47, ask=None))

    result = await paper.place_order(SPEC, "BUY", 10, 0.60, timeout_seconds=5)
    assert result.filled_qty == 0


async def test_a_fill_creates_a_position_and_spends_cash():
    paper = broker([(0.47, 0.48)] * 4)

    await paper.place_order(SPEC, "BUY", 10, 0.52, timeout_seconds=5)

    (position,) = await paper.get_positions()
    assert (position.occ_symbol, position.qty, position.avg_cost) == (SPEC.occ_symbol, 10, 0.48)
    account = await paper.get_account()
    assert account.net_liquidation == pytest.approx(100_000 - 10 * 0.48 * 100)


async def test_two_buys_blend_the_average_cost():
    paper = PaperBroker(_StepQuotes([(0.47, 0.48), (0.57, 0.58)]))

    await paper.place_order(SPEC, "BUY", 10, 0.60, timeout_seconds=5)
    await paper.place_order(SPEC, "BUY", 10, 0.60, timeout_seconds=5)

    (position,) = await paper.get_positions()
    assert position.qty == 20
    assert position.avg_cost == pytest.approx(0.53, abs=0.01)


async def test_a_partial_fill_fraction_fills_some_and_reports_partial():
    paper = broker([(0.47, 0.48)] * 4, fill_fraction=0.5)

    result = await paper.place_order(SPEC, "BUY", 10, 0.52, timeout_seconds=5)

    assert result.status is OrderStatus.PARTIAL
    assert result.filled_qty == 5


# --- selling --------------------------------------------------------------


async def test_a_sell_fills_at_the_bid_not_at_the_limit():
    paper = broker([(0.47, 0.48)] * 8)
    await paper.place_order(SPEC, "BUY", 10, 0.52, timeout_seconds=5)

    result = await paper.place_order(SPEC, "SELL", 10, 0.40, timeout_seconds=5)

    assert result.status is OrderStatus.FILLED
    assert result.avg_fill_price == 0.47  # the bid, not the 0.40 limit


async def test_a_sell_above_the_bid_does_not_cross():
    paper = broker([(0.47, 0.48)] * 8)
    await paper.place_order(SPEC, "BUY", 10, 0.52, timeout_seconds=5)

    result = await paper.place_order(SPEC, "SELL", 10, 0.60, timeout_seconds=5)

    assert result.status is OrderStatus.PENDING
    assert result.filled_qty == 0


async def test_the_simulator_refuses_to_sell_more_than_is_held():
    """The simulator enforces the rule the real bot must obey: never short."""
    paper = broker([(0.47, 0.48)] * 8)
    await paper.place_order(SPEC, "BUY", 5, 0.52, timeout_seconds=5)

    with pytest.raises(BrokerError, match="cannot sell 10"):
        await paper.place_order(SPEC, "SELL", 10, 0.40, timeout_seconds=5)


async def test_selling_with_no_position_at_all_is_refused():
    paper = broker([(0.47, 0.48)] * 4)
    with pytest.raises(BrokerError, match="cannot sell"):
        await paper.place_order(SPEC, "SELL", 1, 0.40, timeout_seconds=5)


async def test_a_full_sell_closes_the_position_and_books_the_pnl():
    paper = PaperBroker(_StepQuotes([(0.47, 0.48), (0.97, 0.98)]))
    await paper.place_order(SPEC, "BUY", 10, 0.52, timeout_seconds=5)

    await paper.place_order(SPEC, "SELL", 10, 0.50, timeout_seconds=5)

    assert await paper.get_positions() == []
    # Bought at the 0.48 ask, sold at the 0.97 bid.
    assert paper.realized_pnl == pytest.approx((0.97 - 0.48) * 10 * 100)


async def test_a_partial_sell_leaves_the_rest_at_the_original_cost():
    paper = broker([(0.47, 0.48)] * 8)
    await paper.place_order(SPEC, "BUY", 10, 0.52, timeout_seconds=5)

    await paper.place_order(SPEC, "SELL", 4, 0.40, timeout_seconds=5)

    (position,) = await paper.get_positions()
    assert position.qty == 6
    assert position.avg_cost == 0.48


# --- order lifecycle ------------------------------------------------------


async def test_a_resting_order_can_be_cancelled():
    paper = broker([(0.47, 0.48)] * 4)
    result = await paper.place_order(SPEC, "BUY", 10, 0.40, timeout_seconds=5)

    cancelled = await paper.cancel_order(result.broker_order_id)
    assert cancelled.status is OrderStatus.CANCELLED


async def test_cancelling_a_filled_order_reports_it_as_already_done():
    paper = broker([(0.47, 0.48)] * 4)
    result = await paper.place_order(SPEC, "BUY", 10, 0.52, timeout_seconds=5)

    cancelled = await paper.cancel_order(result.broker_order_id)
    assert cancelled.status is OrderStatus.FILLED
    assert cancelled.detail == "already done"


async def test_an_unknown_order_id_is_an_error():
    paper = broker([(0.47, 0.48)])
    with pytest.raises(BrokerError, match="unknown order"):
        await paper.cancel_order("nope")
    with pytest.raises(BrokerError, match="unknown order"):
        await paper.get_order_status("nope")


@pytest.mark.parametrize("side,qty", [("HOLD", 1), ("BUY", 0), ("BUY", -1)])
async def test_nonsense_orders_are_refused(side, qty):
    paper = broker([(0.47, 0.48)])
    with pytest.raises(BrokerError):
        await paper.place_order(SPEC, side, qty, 0.50, timeout_seconds=5)


# --- restart and interference helpers -------------------------------------


async def test_a_position_can_be_seeded_for_a_restart_test():
    paper = broker([(0.47, 0.48)] * 4)
    paper.seed_position(SPEC.occ_symbol, 8, 0.48)

    (position,) = await paper.get_positions()
    assert position.qty == 8


async def test_a_position_can_be_made_to_vanish_without_an_order():
    """Simulates an account-wide flatten from another process."""
    paper = broker([(0.47, 0.48)] * 4)
    paper.seed_position(SPEC.occ_symbol, 8, 0.48)

    paper.drop_position(SPEC.occ_symbol)

    assert await paper.get_positions() == []


# --- scripted quotes ------------------------------------------------------


async def test_the_scripted_path_advances_then_repeats_its_last_quote():
    quotes = ScriptedQuotes(path=[(0.50, 0.51), (0.60, 0.61)])

    first = await quotes.quote(SPEC)
    second = await quotes.quote(SPEC)
    third = await quotes.quote(SPEC)

    assert (first.bid, second.bid, third.bid) == (0.50, 0.60, 0.60)


async def test_an_empty_path_yields_an_empty_quote():
    quote = await ScriptedQuotes(path=[]).quote(SPEC)
    assert quote.bid is None and quote.is_two_sided is False


# --- quote helpers --------------------------------------------------------


def test_the_exit_price_is_the_bid_by_default():
    from datetime import datetime

    quote = Quote(bid=0.45, ask=0.55, last=0.50, asof=datetime(2026, 10, 6))
    assert quote.exit_price(0.0) == 0.45
    assert quote.mid == 0.50
    assert quote.exit_price(1.0) == 0.50
    assert quote.exit_price(0.5) == pytest.approx(0.475)


def test_a_missing_bid_has_no_exit_price():
    from datetime import datetime

    quote = Quote(bid=None, ask=0.55, last=0.50, asof=datetime(2026, 10, 6))
    assert quote.exit_price(0.0) is None


class _FixedQuotes:
    def __init__(self, bid, ask):
        self.bid, self.ask = bid, ask

    async def quote(self, spec):
        from datetime import datetime

        return Quote(bid=self.bid, ask=self.ask, last=self.bid, asof=datetime(2026, 10, 6))

    async def exists(self, spec):
        return True


class _StepQuotes:
    """One quote per *order*, not per call -- place_order reads the quote once,
    so a path indexed per call would skip steps."""

    def __init__(self, steps):
        self.steps = steps
        self.index = 0

    async def quote(self, spec):
        from datetime import datetime

        bid, ask = self.steps[min(self.index, len(self.steps) - 1)]
        self.index += 1
        return Quote(bid=bid, ask=ask, last=bid, asof=datetime(2026, 10, 6))

    async def exists(self, spec):
        return True
