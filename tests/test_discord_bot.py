"""The Discord bot's wiring of the P&L channel. No Discord connection: the bot
is constructed but never started, and webhooks are replaced by fakes."""

import pytest
from conftest import make_settings

from options_scanner.broker.paper import PaperBroker
from options_scanner.discord_bot import SwiftAlertBot, parse_close_command
from options_scanner.notifier import Notification
from options_scanner.pipeline import AlertPipeline
from options_scanner.position_manager import PositionManager
from options_scanner.risk import RiskGate
from options_scanner.webhook import WebhookError

PNL_HOOK = "https://discord.com/api/webhooks/111/pnl-token"
UPDATES_HOOK = "https://discord.com/api/webhooks/222/updates-token"
BOARD_HOOK = "https://discord.com/api/webhooks/333/board-token"


class _Quotes:
    async def quote(self, spec):
        return None


class FakeWebhook:
    def __init__(self, *, valid=True, channel_id="999"):
        self.valid = valid
        self.channel_id = channel_id
        self.posted: list[Notification] = []
        self.closed = False

    async def validate(self):
        if not self.valid:
            raise WebhookError("webhook does not exist (404)")
        return {"channel_id": self.channel_id}

    async def post(self, note):
        self.posted.append(note)
        return True

    async def close(self):
        self.closed = True


def _bot(storage, *, pnl=PNL_HOOK, updates=UPDATES_HOOK, board=""):
    settings = make_settings(
        mode="paper",
        discord={
            "bot_token": "t", "alerts_channel_id": 1001, "updates_channel_id": 2002,
            "owner_user_id": 3003, "pnl_webhook_url": pnl, "updates_webhook_url": updates,
            "positions_webhook_url": board,
        },
    )
    risk = RiskGate(settings, storage)
    manager = PositionManager(settings, storage, risk, PaperBroker(_Quotes()))
    pipeline = AlertPipeline(settings, storage, risk, None, manager)
    bot = SwiftAlertBot(settings=settings, storage=storage, risk=risk, pipeline=pipeline, manager=manager)
    return bot, manager


async def test_with_a_pnl_webhook_the_manager_reports_sales_to_it(storage):
    bot, manager = _bot(storage)

    assert manager._notify_pnl == bot.post_pnl
    await bot._pnl_webhook.close()


async def test_without_one_there_is_no_pnl_channel(storage):
    bot, manager = _bot(storage, pnl="")

    assert bot._pnl_webhook is None
    assert manager._notify_pnl is None


async def test_pnl_cards_go_to_the_pnl_channel_and_nowhere_else(storage):
    bot, _ = _bot(storage)
    await bot._pnl_webhook.close()
    bot._pnl_webhook, bot._webhook = FakeWebhook(), FakeWebhook()

    await bot.post_pnl(Notification(title="TRIM +25% — SPY 772C · 0DTE"))

    assert [n.title for n in bot._pnl_webhook.posted] == ["TRIM +25% — SPY 772C · 0DTE"]
    assert bot._webhook.posted == []


async def test_a_broken_pnl_webhook_turns_pnl_cards_off_but_not_the_bot(storage):
    """A report, not a safety mechanism: it must never stop trading."""
    bot, manager = _bot(storage)
    await bot._pnl_webhook.close()
    broken = FakeWebhook(valid=False)
    bot._pnl_webhook = broken

    await bot._check_pnl_webhook()

    assert bot._pnl_webhook is None
    assert manager._notify_pnl is None
    assert broken.closed
    assert "404" in bot._pnl_startup_problem


async def test_a_working_pnl_webhook_stays_on(storage):
    bot, manager = _bot(storage)
    await bot._pnl_webhook.close()
    bot._pnl_webhook = FakeWebhook()

    await bot._check_pnl_webhook()

    assert bot._pnl_webhook is not None
    assert manager._notify_pnl == bot.post_pnl
    assert bot._pnl_startup_problem is None


# --- the positions board ------------------------------------------------------


async def test_with_a_positions_webhook_the_board_reads_the_live_manager(storage):
    bot, manager = _bot(storage, board=BOARD_HOOK)

    assert bot.board is not None
    assert bot.board.manager is manager
    assert bot.board.broker is bot.pipeline.broker
    await bot._board_webhook.close()


async def test_without_one_there_is_no_board(storage):
    bot, _ = _bot(storage)

    assert bot.board is None and bot._board_webhook is None


async def _check_board(storage, hook):
    bot, _ = _bot(storage, board=BOARD_HOOK)
    await bot._board_webhook.close()
    bot._board_webhook = hook
    await bot._check_board_webhook()
    return bot


async def test_a_working_board_webhook_stays_on(storage):
    bot = await _check_board(storage, FakeWebhook(channel_id="4004"))

    assert bot.board is not None
    assert bot._board_startup_problem is None


async def test_a_broken_board_webhook_turns_the_board_off_but_not_the_bot(storage):
    hook = FakeWebhook(valid=False)
    bot = await _check_board(storage, hook)

    assert bot.board is None and bot._board_webhook is None
    assert hook.closed
    assert "404" in bot._board_startup_problem


async def test_the_board_may_not_post_in_the_alerts_channel(storage):
    """The bot never posts text there; the reaction is the only signal."""
    bot = await _check_board(storage, FakeWebhook(channel_id="1001"))

    assert bot.board is None
    assert "alerts channel" in bot._board_startup_problem


async def test_the_board_may_not_share_the_updates_channel(storage):
    bot = await _check_board(storage, FakeWebhook(channel_id="2002"))

    assert bot.board is None
    assert "updates channel" in bot._board_startup_problem


async def test_closing_the_bot_marks_the_board_offline(storage):
    bot, _ = _bot(storage, pnl="", updates="", board=BOARD_HOOK)
    calls = []

    class Board:
        async def shutdown(self):
            calls.append("shutdown")

    bot.board = Board()
    await bot.close()

    assert calls == ["shutdown"]


async def test_sigterm_closes_the_bot_so_the_board_is_marked_offline(storage):
    """launchd stops the bot with SIGTERM; unhandled, close() never runs."""
    import asyncio
    import signal

    bot, _ = _bot(storage, pnl="", updates="")
    closed = []

    async def close():
        closed.append(True)

    bot.close = close
    loop = asyncio.get_running_loop()
    registered = {}
    original = loop.add_signal_handler
    loop.add_signal_handler = lambda sig, cb: registered.setdefault(sig, cb)
    try:
        await bot.setup_hook()
    finally:
        loop.add_signal_handler = original

    registered[signal.SIGTERM]()
    await asyncio.sleep(0)

    assert closed == [True]


# --- !close -----------------------------------------------------------------

from datetime import date  # noqa: E402

from options_scanner.discord_bot import match_positions  # noqa: E402
from options_scanner.models import OptionKey  # noqa: E402

D = date(2026, 10, 21)
OPEN = {
    "NVDA  261021C00220000": OptionKey("NVDA", D, 220.0, "C"),
    "SPY   261021C00772000": OptionKey("SPY", D, 772.0, "C"),
    "QQQ   261021P00718000": OptionKey("QQQ", D, 718.0, "P"),
    "QQQ   261021C00720000": OptionKey("QQQ", D, 720.0, "C"),
}


def test_a_ticker_picks_its_one_position():
    assert match_positions(["NVDA"], OPEN) == ["NVDA  261021C00220000"]
    assert match_positions(["nvda"], OPEN) == ["NVDA  261021C00220000"]


def test_a_ticker_held_twice_matches_both_so_the_bot_will_not_guess():
    assert sorted(match_positions(["QQQ"], OPEN)) == ["QQQ   261021C00720000", "QQQ   261021P00718000"]


def test_strike_and_right_narrow_it_down():
    assert match_positions(["QQQ", "718P"], OPEN) == ["QQQ   261021P00718000"]
    assert match_positions(["QQQ", "718p"], OPEN) == ["QQQ   261021P00718000"]


def test_the_full_occ_symbol_matches_exactly():
    assert match_positions(["NVDA", "261021C00220000"], OPEN) == ["NVDA  261021C00220000"]


def test_nothing_held_matches_nothing():
    assert match_positions(["TSLA"], OPEN) == []
    assert match_positions(["QQQ", "999C"], OPEN) == []
    assert match_positions([], OPEN) == []


class FakeManager:
    """Records closes; holds the OPEN positions above."""

    def __init__(self):
        from types import SimpleNamespace

        self.managed = {sym: SimpleNamespace(state=SimpleNamespace(option=opt)) for sym, opt in OPEN.items()}
        self.closed = []

    async def close_position(self, symbol, *, action):
        self.closed.append((symbol, action))
        return [Notification(title=f"SOLD ALL — {symbol}")]


def _bot_with_fake(storage):
    bot, _ = _bot(storage, pnl="")
    bot.manager = FakeManager()
    posted = []

    async def post(note):
        posted.append(note)

    bot.post = post
    return bot, posted


async def test_close_closes_exactly_the_named_position(storage):
    bot, posted = _bot_with_fake(storage)

    await bot._cmd_close(["NVDA"])

    assert bot.manager.closed == [("NVDA  261021C00220000", "CLOSED")]
    assert any("SOLD ALL" in n.title for n in posted)


async def test_an_ambiguous_close_closes_nothing_and_says_which(storage):
    bot, posted = _bot_with_fake(storage)

    await bot._cmd_close(["QQQ"])

    assert bot.manager.closed == []
    assert "!close QQQ 718P" in posted[-1].description
    assert "!close QQQ 720C" in posted[-1].description


async def test_an_unknown_close_closes_nothing_and_lists_what_is_open(storage):
    bot, posted = _bot_with_fake(storage)

    await bot._cmd_close(["TSLA"])

    assert bot.manager.closed == []
    assert "NVDA" in dict((n, v) for n, v, _ in posted[-1].fields)["Open"]


async def test_close_does_not_halt_new_entries(storage):
    """Unlike !flatten: closing one position is not a stop-trading signal."""
    bot, _ = _bot_with_fake(storage)

    await bot._cmd_close(["NVDA"])

    assert not bot.risk.halted


async def test_close_with_no_argument_explains_itself(storage):
    bot, posted = _bot_with_fake(storage)

    await bot._cmd_close([])

    assert bot.manager.closed == []
    assert "!close NVDA" in posted[-1].title


# --- close commands typed in the alerts channel -----------------------------


@pytest.mark.parametrize(
    "text,expected",
    [
        ("close !", ["!"]),
        ("CLOSE !", ["!"]),
        ("!close !", ["!"]),
        ("  close !  ", ["!"]),
        ("close NVDA", ["NVDA"]),
        ("close nvda", ["nvda"]),
        ("close $TSLA", ["$TSLA"]),
        ("Close SPY", ["SPY"]),
        ("close QQQ 718P", ["QQQ", "718P"]),
        ("close QQQ 718.5p", ["QQQ", "718.5p"]),
        ("close NVDA 261021C00220000", ["NVDA", "261021C00220000"]),
        ("close all", ["all"]),  # the ticker ALL; the handler points at `close !`
    ],
)
def test_close_commands_are_recognised(text, expected):
    assert parse_close_command(text) == expected


@pytest.mark.parametrize(
    "text",
    [
        "Close or trim & set SL to breakeven.",  # an advisor line
        "close",
        "close NVDA now",
        "close NVDA 220",                      # no call/put
        "close !!",
        "close NVDA\nTrim Targets: 1.20",       # multi-line is a paste
        "SPX 7815P 0.475",
        "closing NVDA",
        "close 123",
        "",
    ],
)
def test_anything_else_is_left_for_the_alert_parser(text):
    assert parse_close_command(text) is None
