"""The Discord bot's wiring of the P&L channel. No Discord connection: the bot
is constructed but never started, and webhooks are replaced by fakes."""

from conftest import make_settings

from options_scanner.broker.paper import PaperBroker
from options_scanner.discord_bot import SwiftAlertBot
from options_scanner.notifier import Notification
from options_scanner.pipeline import AlertPipeline
from options_scanner.position_manager import PositionManager
from options_scanner.risk import RiskGate
from options_scanner.webhook import WebhookError

PNL_HOOK = "https://discord.com/api/webhooks/111/pnl-token"
UPDATES_HOOK = "https://discord.com/api/webhooks/222/updates-token"


class _Quotes:
    async def quote(self, spec):
        return None


class FakeWebhook:
    def __init__(self, *, valid=True):
        self.valid = valid
        self.posted: list[Notification] = []
        self.closed = False

    async def validate(self):
        if not self.valid:
            raise WebhookError("webhook does not exist (404)")
        return {"channel_id": "999"}

    async def post(self, note):
        self.posted.append(note)
        return True

    async def close(self):
        self.closed = True


def _bot(storage, *, pnl=PNL_HOOK, updates=UPDATES_HOOK):
    settings = make_settings(
        mode="paper",
        discord={
            "bot_token": "t", "alerts_channel_id": 1001, "updates_channel_id": 2002,
            "owner_user_id": 3003, "pnl_webhook_url": pnl, "updates_webhook_url": updates,
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
