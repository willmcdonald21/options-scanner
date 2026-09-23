from __future__ import annotations

import logging

import discord
from ib_async import IB, util

from bot.execution import handle_event
from bot.logging_config import setup_logging
from bot.notifier import Notifier
from bot.parser import parse_embed
from bot.position_store import PositionStore
from bot.text_import import parse_chat_export
from config.settings import AppConfig, load_config

logger = logging.getLogger("options_scanner.main")


class SwiftCopyTrader(discord.Client):
    """Listens on a relay channel the user owns (see bot/text_import.py's
    docstring for why -- the source channel isn't ours to get bot API
    access to) and mirrors each alert pasted there into IBKR."""

    def __init__(self, *, config: AppConfig, ib: IB, store: PositionStore, notifier: Notifier):
        intents = discord.Intents.default()
        intents.message_content = True
        super().__init__(intents=intents)
        self.config = config
        self.ib = ib
        self.store = store
        self.notifier = notifier

    async def on_ready(self) -> None:
        if not self.ib.isConnected():
            await self.ib.connectAsync(
                self.config.trading.host,
                self.config.trading.port,
                clientId=self.config.trading.client_id,
            )
        logger.info(
            "Ready: Discord as %s, IBKR connected (mode=%s, port=%s)",
            self.user,
            self.config.trading.mode,
            self.config.trading.port,
        )

    async def on_message(self, message: discord.Message) -> None:
        if message.channel.id != self.config.discord.channel_id:
            return
        if self.user is not None and message.author.id == self.user.id:
            return

        embeds = parse_chat_export(message.content, reference_date=message.created_at)
        for index, parsed in enumerate(embeds):
            # Override text_import's fabricated sequential id with a real,
            # stable one derived from the Discord snowflake -- add (not
            # multiply) the sub-index so a multi-card paste can't overflow
            # SQLite's 64-bit INTEGER the way multiplying a full snowflake
            # would (a same-millisecond collision with another real message
            # is theoretical, not practical, at this manual-paste cadence).
            parsed.message_id = message.id + index
            event = parse_embed(parsed)
            handle_event(event, self.ib, self.store, self.notifier, self.config.risk)


def main() -> None:
    setup_logging()
    config = load_config()
    util.patchAsyncio()  # let ib_async's sync-looking calls reenter discord.py's running loop

    db_path = config.resolve_path("data/positions.sqlite3")
    db_path.parent.mkdir(parents=True, exist_ok=True)
    store = PositionStore(db_path)
    notifier = Notifier(config.discord.webhook_alerts)
    ib = IB()

    client = SwiftCopyTrader(config=config, ib=ib, store=store, notifier=notifier)
    try:
        client.run(config.discord.bot_token)
    finally:
        store.close()
        if ib.isConnected():
            ib.disconnect()


if __name__ == "__main__":
    main()
