"""The positions board: what the card says, and how one message is kept current.

No network and no Discord: the webhook is a fake that records posts and edits.
"""

from dataclasses import replace
from datetime import datetime

from conftest import TRADING_DAY, make_settings

from options_scanner.board import BoardRow, BoardSnapshot, offline_board, render_board, snapshot
from options_scanner.broker.base import Quote
from options_scanner.broker.paper import PaperBroker
from options_scanner.market_hours import EASTERN
from options_scanner.models import OptionKey, PositionState
from options_scanner.notifier import COLOR_GREEN, COLOR_GREY, COLOR_RED, COLOR_YELLOW
from options_scanner.position_manager import PositionManager
from options_scanner.risk import RiskGate

SPY = OptionKey("SPY", TRADING_DAY, 772.0, "C")
NOW = datetime(2026, 10, 6, 11, 0, 5, tzinfo=EASTERN)


def row(**overrides) -> BoardRow:
    base = dict(
        option=SPY, entry=1.00, remaining=3, original=6, bid=1.42, peak=1.55,
        stop=1.00, realized=50.0, next_level=(50, 1.50), quote_age=1.0,
    )
    base.update(overrides)
    return BoardRow(**base)


def snap(*rows, **overrides) -> BoardSnapshot:
    base = dict(
        mode="paper", rows=tuple(rows), realized_today=80.0, broker_connected=True,
        outage_seconds=None, halted=False, halt_reason="", session="market open",
        stale_after=15.0, first_stop_pct=25,
    )
    base.update(overrides)
    return BoardSnapshot(**base)


def body(note) -> str:
    return note.to_text()


# --- the numbers ------------------------------------------------------------


def test_open_pnl_is_valued_on_the_bid_for_what_is_still_held():
    r = row(entry=1.00, bid=1.42, remaining=3)

    assert round(r.unrealized, 2) == 126.00
    assert round(r.gain_pct, 1) == 42.0


def test_the_day_total_is_realized_plus_whats_open():
    s = snap(row(bid=1.42, remaining=3), row(option=replace(SPY, strike=773.0), bid=0.90, remaining=2))

    assert round(s.unrealized, 2) == 126.00 - 20.00
    assert round(s.day_total, 2) == 80.0 + 106.00


def test_a_position_with_no_quote_yet_counts_nothing_open():
    s = snap(row(bid=None))

    assert s.unrealized == 0
    assert "No quote yet" in body(render_board(s, updated_at=None))


# --- what the card says -------------------------------------------------------


def test_one_position_shows_everything_that_matters():
    text = body(render_board(snap(row()), updated_at=None))

    assert "Open positions" in text
    assert "SPY 772C" in text
    assert "+42.0%" in text and "bid $1.42" in text and "entry $1.00" in text
    assert "+$126.00" in text and "3 of 6" in text and "realized +$50.00" in text
    assert "Stop $1.00 (break-even)" in text and "peak $1.55" in text
    assert "Next +50% at $1.50" in text
    assert "Day P&L +$206.00" in text


def test_a_position_below_plus_25_says_it_has_no_stop():
    text = body(render_board(snap(row(stop=None)), updated_at=None))

    assert "none until +25%" in text


def test_the_first_stop_level_comes_from_the_config():
    text = body(render_board(snap(row(stop=None), first_stop_pct=50), updated_at=None))

    assert "none until +50%" in text


def test_a_stepped_stop_says_what_it_locks_in():
    text = body(render_board(snap(row(stop=1.25)), updated_at=None))

    assert "Stop $1.25 (locks +25.0%)" in text


def test_a_stale_quote_is_flagged_on_its_row():
    fresh = body(render_board(snap(row(quote_age=3.0)), updated_at=None))
    stale = body(render_board(snap(row(quote_age=42.0)), updated_at=None))

    assert "last quote" not in fresh
    assert "last quote 42s ago" in stale


def test_nothing_open_is_said_plainly():
    note = render_board(snap(), updated_at=None)

    assert note.title == "No open positions"
    assert note.fields == []
    assert "Day P&L +$80.00" in note.description


def test_the_health_line_names_the_mode_broker_and_entries():
    text = render_board(snap(row()), updated_at=None).description

    assert "`paper` · broker connected · taking entries" in text


def test_a_broker_outage_says_the_stops_are_blind():
    note = render_board(snap(row(), broker_connected=False, outage_seconds=180), updated_at=None)

    assert "broker down for 3 min" in note.description
    assert "stops cannot see prices" in note.description


def test_a_halt_is_shown_with_its_reason():
    note = render_board(snap(halted=True, halt_reason="flattened by command"), updated_at=None)

    assert "entries halted** (flattened by command)" in note.description


def test_dry_run_is_labelled_on_the_card():
    note = render_board(snap(mode="dry_run", broker_connected=None), updated_at=None)

    assert note.title == "[DRY RUN] No open positions"
    assert "no broker (dry run)" in note.description


def test_the_timestamp_ticks_in_discord_and_is_left_out_on_request():
    stamped = render_board(snap(row()), updated_at=NOW).description
    plain = render_board(snap(row()), updated_at=None).description

    stamp = int(NOW.timestamp())
    assert f"<t:{stamp}:R>" in stamped
    assert "Updated" not in plain


# --- colour -----------------------------------------------------------------


def test_colour_follows_the_day_and_the_health():
    assert render_board(snap(row()), updated_at=None).color == COLOR_GREEN
    assert render_board(snap(row(bid=0.50), realized_today=0.0), updated_at=None).color == COLOR_RED
    assert render_board(snap(), updated_at=None).color == COLOR_GREY
    assert render_board(snap(row(), broker_connected=False), updated_at=None).color == COLOR_YELLOW
    assert render_board(snap(row(), halted=True, halt_reason="x"), updated_at=None).color == COLOR_YELLOW


def test_the_board_never_pings():
    assert not render_board(snap(row(), broker_connected=False), updated_at=None).mentions_owner


def test_the_offline_card_warns_about_open_positions():
    note = offline_board(stopped_at=NOW, open_positions=2)

    assert note.title == "Bot offline"
    assert note.color == COLOR_GREY
    assert "not updating" in note.description
    assert "2 positions still open with no stop" in note.description
    assert "still open" not in offline_board(stopped_at=NOW, open_positions=0).description


# --- reading the live state ---------------------------------------------------


class _Feed:
    async def quote(self, spec):
        return Quote(bid=0.55, ask=0.56, last=0.55, asof=NOW)

    async def exists(self, spec):
        return True


def _manager(storage):
    settings = make_settings(mode="paper")
    broker = PaperBroker(_Feed())
    broker._connected = True
    risk = RiskGate(settings, storage)
    return settings, risk, broker, PositionManager(settings, storage, risk, broker)


def test_the_snapshot_reads_the_managers_last_bid(storage):
    settings, risk, broker, manager = _manager(storage)
    state = PositionState(option=SPY, entry_fill=0.48, original_qty=6, remaining_qty=6, peak_bid=0.48)
    managed = manager.track(1, state)
    managed.last_bid = 0.55
    managed.last_good_quote = datetime(2026, 10, 6, 11, 0, 0, tzinfo=EASTERN)

    s = snapshot(settings=settings, storage=storage, risk=risk, manager=manager, broker=broker, now=NOW)

    (r,) = s.rows
    assert r.bid == 0.55 and r.entry == 0.48 and r.remaining == 6
    assert r.quote_age == 5.0
    assert r.next_level == (25, 0.6)
    assert s.first_stop_pct == 25  # make_settings has no steps: the first trim
    assert s.broker_connected is True and s.mode == "paper"


def test_without_a_manager_the_snapshot_reads_storage(storage):
    settings = make_settings()
    risk = RiskGate(settings, storage)

    s = snapshot(settings=settings, storage=storage, risk=risk, now=NOW)

    assert s.rows == () and s.broker_connected is None
