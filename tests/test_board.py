"""The positions board: what the card says, and how one message is kept current.

No network and no Discord: the webhook is a fake that records posts and edits.
"""

import asyncio
from dataclasses import replace
from datetime import datetime

from conftest import TRADING_DAY, make_settings

from options_scanner.board import (
    MAX_ROWS,
    STATE_KEY,
    BoardRow,
    BoardSnapshot,
    PositionsBoard,
    offline_board,
    render_board,
    snapshot,
)
from options_scanner.broker.base import Quote
from options_scanner.broker.paper import PaperBroker
from options_scanner.market_hours import EASTERN
from options_scanner.models import OptionKey, PositionState
from options_scanner.notifier import COLOR_GREEN, COLOR_GREY, COLOR_RED, COLOR_YELLOW
from options_scanner.position_manager import PositionManager
from options_scanner.risk import RiskGate
from options_scanner.webhook import EditResult

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


def test_an_initial_stop_says_it_caps_the_loss():
    text = body(render_board(snap(row(stop=0.65)), updated_at=None))

    assert "Stop $0.65 (initial, max loss -35.0%)" in text


def test_with_an_initial_stop_step_a_brand_new_position_says_none_yet():
    text = body(render_board(snap(row(stop=None), first_stop_pct=0), updated_at=None))

    assert "Stop **none yet**" in text


def test_a_stepped_stop_says_what_it_locks_in():
    text = body(render_board(snap(row(stop=1.25)), updated_at=None))

    assert "Stop $1.25 (locks +25.0%)" in text


def test_locked_runners_show_their_own_stop():
    text = body(render_board(snap(row(stop=1.50, lock_qty=2, lock_stop=2.25)), updated_at=None))

    assert "Stop $1.50 (locks +50.0%)" in text
    assert "2 locked runners · stop $2.25 (locks +125.0%)" in text


def test_no_lock_line_without_a_lock():
    assert "locked" not in body(render_board(snap(row()), updated_at=None))


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


def test_a_long_list_is_cut_short_within_discords_limits():
    rows = [row(option=replace(SPY, strike=700.0 + i)) for i in range(MAX_ROWS + 3)]

    note = render_board(snap(*rows), updated_at=None)

    assert len(note.fields) == MAX_ROWS + 1
    assert note.fields[-1][0] == "…and 3 more"


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


# --- keeping one message current ---------------------------------------------

HOOK = "https://discord.com/api/webhooks/4242/board-token"


class FakeHook:
    """Records posts and edits. `edits` scripts what each edit answers."""

    def __init__(self, *, edits=None, post_id="900"):
        self.url = HOOK
        self.posts: list[list[dict]] = []
        self.edited: list[tuple[str, list[dict]]] = []
        self._edits = list(edits or [])
        self._post_id = post_id

    async def post_message(self, embeds):
        self.posts.append(embeds)
        return self._post_id

    async def edit_message(self, message_id, embeds):
        self.edited.append((message_id, embeds))
        return self._edits.pop(0) if self._edits else EditResult.OK


class Clock:
    def __init__(self):
        self.now = 1_000.0

    def __call__(self):
        return self.now


def _board(storage, *, hook=None, market_open=True, positions=0):
    settings, risk, broker, manager = _manager(storage)
    for i in range(positions):
        state = PositionState(
            option=replace(SPY, strike=772.0 + i), entry_fill=1.0, original_qty=6,
            remaining_qty=6, peak_bid=1.0,
        )
        manager.track(i + 1, state).last_bid = 1.10
    board = PositionsBoard(
        hook or FakeHook(), settings=settings, storage=storage, risk=risk,
        manager=manager, broker=broker,
    )
    clock = Clock()
    board._monotonic = clock
    board._now = lambda: NOW
    board._is_market_open = lambda: market_open
    return board, clock, manager


async def test_the_first_refresh_posts_and_remembers_the_message(storage):
    board, _, _ = _board(storage)

    assert await board.refresh() is True

    assert len(board.webhook.posts) == 1
    assert storage.get_state(STATE_KEY) == "4242:900"


async def test_later_refreshes_edit_that_same_message(storage):
    board, clock, _ = _board(storage, positions=1)
    await board.refresh()

    clock.now += 2
    await board.refresh()

    assert len(board.webhook.posts) == 1
    assert [mid for mid, _ in board.webhook.edited] == ["900"]


async def test_a_restart_edits_the_message_it_posted_before(storage):
    storage.set_state(STATE_KEY, "4242:555")
    board, _, _ = _board(storage)

    await board.refresh()

    assert board.webhook.posts == []
    assert [mid for mid, _ in board.webhook.edited] == ["555"]


async def test_a_new_webhook_starts_a_new_board(storage):
    """A different webhook cannot edit the old one's message."""
    storage.set_state(STATE_KEY, "1111:555")
    board, _, _ = _board(storage)

    await board.refresh()

    assert board.webhook.edited == []
    assert len(board.webhook.posts) == 1
    assert storage.get_state(STATE_KEY) == "4242:900"


async def test_a_deleted_board_is_posted_again(storage):
    storage.set_state(STATE_KEY, "4242:555")
    board, _, _ = _board(storage, hook=FakeHook(edits=[EditResult.MISSING], post_id="901"))

    assert await board.refresh() is True

    assert len(board.webhook.posts) == 1
    assert storage.get_state(STATE_KEY) == "4242:901"


async def test_a_failed_edit_keeps_the_message_and_tries_again(storage):
    storage.set_state(STATE_KEY, "4242:555")
    board, clock, _ = _board(storage, hook=FakeHook(edits=[EditResult.FAILED]))

    assert await board.refresh() is False
    clock.now += 2
    assert await board.refresh() is True

    assert board.webhook.posts == []
    assert [mid for mid, _ in board.webhook.edited] == ["555", "555"]


async def test_while_positions_are_open_in_session_every_refresh_edits(storage):
    """So the Updated stamp stays seconds old for as long as the bot is alive."""
    board, clock, _ = _board(storage, positions=1, market_open=True)
    await board.refresh()

    for _ in range(5):
        clock.now += 2
        assert await board.refresh() is True

    assert len(board.webhook.edited) == 5


async def test_an_idle_board_is_only_edited_when_something_changes(storage):
    board, clock, _ = _board(storage, positions=0)
    await board.refresh()

    clock.now += 2
    assert await board.refresh() is False

    storage.set_halted(True, "halted by command")
    clock.now += 2
    assert await board.refresh() is True


async def test_an_idle_board_still_beats_once_a_minute(storage):
    board, clock, _ = _board(storage, positions=0)
    await board.refresh()

    clock.now += 59
    assert await board.refresh() is False
    clock.now += 1
    assert await board.refresh() is True


async def test_after_hours_an_open_position_is_idle_too(storage):
    board, clock, _ = _board(storage, positions=1, market_open=False)
    await board.refresh()

    clock.now += 2
    assert await board.refresh() is False


async def test_a_failed_post_is_tried_again_next_time(storage):
    class NoPost(FakeHook):
        async def post_message(self, embeds):
            self.posts.append(embeds)
            return None

    board, clock, _ = _board(storage, hook=NoPost())

    assert await board.refresh() is False
    clock.now += 2
    assert await board.refresh() is False
    assert len(board.webhook.posts) == 2
    assert storage.get_state(STATE_KEY) is None


async def test_the_card_sent_is_the_live_board(storage):
    board, _, _ = _board(storage, positions=1)

    await board.refresh()

    (embed,) = board.webhook.posts[0]
    assert embed["title"] == "Open positions"
    assert f"<t:{int(NOW.timestamp())}:R>" in embed["description"]
    assert "+10.0%" in embed["fields"][0]["value"]


async def test_shutdown_marks_the_board_offline(storage):
    board, _, _ = _board(storage, positions=2)
    await board.refresh()

    await board.shutdown()

    message_id, (embed,) = board.webhook.edited[-1]
    assert message_id == "900"
    assert embed["title"] == "Bot offline"
    assert "2 positions still open" in embed["description"]


async def test_shutdown_with_no_board_posts_nothing(storage):
    board, _, _ = _board(storage)

    await board.shutdown()

    assert board.webhook.posts == [] and board.webhook.edited == []


async def test_a_failing_webhook_never_escapes_the_loop(storage):
    class Broken(FakeHook):
        def __init__(self):
            super().__init__()
            self.attempts = 0

        async def post_message(self, embeds):
            self.attempts += 1
            raise RuntimeError("discord is on fire")

    board, _, _ = _board(storage, hook=Broken())
    board.settings.board.refresh_seconds = 0.0  # not re-validated after construction

    async def stop_soon():
        await asyncio.sleep(0.05)
        board._running = False

    await asyncio.gather(board.run(), stop_soon())

    assert board.webhook.attempts > 1  # it kept going after the first failure
