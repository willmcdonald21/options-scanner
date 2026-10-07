"""The risk gate. Every check here can only ever refuse to OPEN a position;
nothing in risk.py touches an existing one, because halting and the daily loss
limit stop new entries while open positions keep being managed.
"""

from datetime import date, datetime

from conftest import MID_SESSION, SPX_OCC, TRADING_DAY, make_settings

from options_scanner.market_hours import EASTERN
from options_scanner.models import OptionKey, PositionState
from options_scanner.risk import BlockReason, RiskGate
from options_scanner.storage import STATUS_ACCEPTED

ENTRY = 0.475


def gate(storage, **overrides) -> RiskGate:
    return RiskGate(make_settings(**overrides), storage)


def check(g: RiskGate, *, symbol: str = SPX_OCC, entry: float = ENTRY, moment=MID_SESSION):
    return g.check_entry(symbol, entry, moment=moment, trading_day=TRADING_DAY)


def _seed_position(storage, ticker="QQQ", strike=740.0) -> None:
    storage.record_alert(
        message_id=1, channel_id=1001, author_id=3003, raw_text="seed", status=STATUS_ACCEPTED
    )
    storage.create_position(
        1,
        PositionState(
            option=OptionKey(ticker, date(2026, 10, 6), strike, "C"),
            entry_fill=1.0,
            original_qty=5,
            remaining_qty=5,
            peak_bid=1.0,
        ),
    )


# --- the happy path -------------------------------------------------------


def test_a_clean_entry_is_allowed(storage):
    decision = check(gate(storage))
    assert decision.allowed is True
    assert decision.reason is None


# --- halting --------------------------------------------------------------


def test_halting_blocks_entries_and_says_how_to_undo_it(storage):
    g = gate(storage)
    g.halt("testing")

    decision = check(g)
    assert decision.reason is BlockReason.HALTED
    assert "!resume" in decision.detail
    assert "testing" in decision.detail


def test_resuming_unblocks(storage):
    g = gate(storage)
    g.halt("testing")
    g.resume()
    assert check(g).allowed is True


# --- the calendar ---------------------------------------------------------


def test_a_closed_market_blocks_entries(storage):
    decision = check(gate(storage), moment=datetime(2026, 10, 6, 8, 0, tzinfo=EASTERN))
    assert decision.reason is BlockReason.MARKET_CLOSED


def test_past_the_cutoff_blocks_entries(storage):
    decision = check(gate(storage), moment=datetime(2026, 10, 6, 15, 45, tzinfo=EASTERN))
    assert decision.reason is BlockReason.PAST_ENTRY_CUTOFF
    assert "15:30" in decision.detail


def test_a_non_trading_day_blocks_entries(storage):
    g = gate(storage)
    decision = g.check_entry(
        SPX_OCC, ENTRY,
        moment=datetime(2026, 11, 26, 11, 0, tzinfo=EASTERN),
        trading_day=date(2026, 11, 26),
    )
    assert decision.reason is BlockReason.NOT_A_TRADING_DAY


# --- caps -----------------------------------------------------------------


def test_the_daily_trade_cap_blocks_further_entries(storage):
    g = gate(storage, risk={"max_trades_per_day": 2})
    g.record_entry(TRADING_DAY)
    assert check(g).allowed is True
    g.record_entry(TRADING_DAY)

    decision = check(g)
    assert decision.reason is BlockReason.MAX_TRADES_PER_DAY
    assert "2 of 2" in decision.detail


def test_the_open_position_cap_blocks_further_entries(storage):
    g = gate(storage, risk={"max_open_positions": 1})
    _seed_position(storage)

    decision = check(g)
    assert decision.reason is BlockReason.MAX_OPEN_POSITIONS


def test_holding_the_same_contract_blocks_adding_to_it(storage):
    g = gate(storage, risk={"max_open_positions": 5})
    _seed_position(storage, ticker="SPX", strike=7815.0)

    decision = g.check_entry(
        "SPXW  261006C07815000", ENTRY, moment=MID_SESSION, trading_day=TRADING_DAY
    )
    assert decision.reason is BlockReason.ALREADY_IN_POSITION
    assert "does not add to a position" in decision.detail


# --- the daily loss limit -------------------------------------------------


def test_crossing_the_loss_limit_blocks_and_auto_halts(storage):
    """Auto-halting the moment it is breached, rather than only on the next
    entry attempt, is what makes the limit visible when it happens."""
    g = gate(storage, risk={"max_daily_loss_usd": 500})
    g.record_realized(-600.0, TRADING_DAY)

    assert g.halted is True
    assert "daily loss limit" in storage.halt_reason()
    assert check(g).reason is BlockReason.HALTED


def test_staying_inside_the_loss_limit_does_not_halt(storage):
    g = gate(storage, risk={"max_daily_loss_usd": 500})
    g.record_realized(-200.0, TRADING_DAY)

    assert g.halted is False
    assert check(g).allowed is True


def test_profit_does_not_trip_the_limit(storage):
    g = gate(storage, risk={"max_daily_loss_usd": 500})
    g.record_realized(900.0, TRADING_DAY)
    assert check(g).allowed is True


def test_losses_accumulate_across_trades(storage):
    g = gate(storage, risk={"max_daily_loss_usd": 500})
    g.record_realized(-300.0, TRADING_DAY)
    assert g.halted is False
    g.record_realized(-250.0, TRADING_DAY)
    assert g.halted is True


def test_a_loss_limit_reached_exactly_counts_as_reached(storage):
    g = gate(storage, risk={"max_daily_loss_usd": 500})
    g.record_realized(-500.0, TRADING_DAY)
    assert g.halted is True


def test_a_manual_halt_is_not_overwritten_by_a_later_loss(storage):
    g = gate(storage, risk={"max_daily_loss_usd": 500})
    g.halt("manual")
    g.record_realized(-600.0, TRADING_DAY)
    assert storage.halt_reason() == "manual"


# --- deduplication --------------------------------------------------------


def test_a_recent_identical_alert_is_blocked_as_a_duplicate(storage):
    from options_scanner.models import EntryAlert, TrimTarget

    g = gate(storage)
    storage.record_alert(
        message_id=910,
        channel_id=1001,
        author_id=3003,
        raw_text="raw",
        status=STATUS_ACCEPTED,
        alert=EntryAlert(
            message_id=910,
            option=OptionKey("SPX", date(2026, 10, 6), 7815.0, "P"),
            entry_price=ENTRY,
            advisor_contracts=25,
            advisor_cost=1188.0,
            trim_targets=(TrimTarget(0.25, 0.594),),
            raw_title="BUY — SPX 7815P · 0DTE",
            is_zero_dte=True,
        ),
    )

    decision = check(g)
    assert decision.reason is BlockReason.DUPLICATE_ALERT
    assert "910" in decision.detail


# --- reporting ------------------------------------------------------------


def test_the_day_summary_reports_every_counter(storage):
    g = gate(storage)
    g.record_entry(TRADING_DAY)
    g.record_realized(125.0, TRADING_DAY)
    storage.bump_day(TRADING_DAY, alerts_received=4, alerts_rejected=1, alerts_skipped=2)

    summary = g.day_summary(TRADING_DAY)
    assert summary == {
        "entries": 1,
        "realized_pnl": 125.0,
        "alerts_received": 4,
        "alerts_rejected": 1,
        "alerts_skipped": 2,
    }
