"""Replays the real historical chat dump (tests/fixtures/swift_chat_dump.txt,
the same 80 messages tests/test_parser.py classifies) through the full
parser -> execution pipeline against a REAL connected IBKR paper session --
the closest thing to a dress rehearsal available without live relay-channel
traffic. Places ~20 real (paper) BUY orders plus their trims/exits. Uses a
throwaway SQLite file, wiped on every run, so it never touches the real
positions.sqlite3.

Refuses to run unless .env's MODE is "paper".

Usage:
    python scripts/replay_fixture_paper.py
"""

from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path

from ib_async import IB

from bot.execution import handle_event
from bot.logging_config import setup_logging
from bot.notifier import Notifier
from bot.parser import parse_embed
from bot.position_store import PositionStore
from bot.text_import import parse_chat_export
from config.settings import load_config

logger = logging.getLogger("options_scanner.scripts.replay_fixture_paper")

FIXTURE = Path(__file__).resolve().parent.parent / "tests" / "fixtures" / "swift_chat_dump.txt"


def main() -> None:
    setup_logging()
    config = load_config()
    if config.trading.mode != "paper":
        raise SystemExit("Refusing to replay against anything but paper mode (MODE must be 'paper' in .env)")

    ib = IB()
    ib.connect(config.trading.host, config.trading.port, clientId=config.trading.client_id)

    db_path = config.resolve_path("data/replay_test.sqlite3")
    db_path.parent.mkdir(parents=True, exist_ok=True)
    db_path.unlink(missing_ok=True)  # fresh run every time
    store = PositionStore(db_path)
    notifier = Notifier(config.discord.webhook_alerts)

    parsed_embeds = parse_chat_export(FIXTURE.read_text(), reference_date=datetime(2026, 9, 23))

    counts: dict[str, int] = {}
    for parsed in parsed_embeds:
        event = parse_embed(parsed)
        counts[type(event).__name__] = counts.get(type(event).__name__, 0) + 1
        handle_event(event, ib, store, notifier, config.risk)

    logger.info("Replayed %d messages: %s", len(parsed_embeds), counts)

    open_positions = store.list_open()
    logger.info("Open positions after replay: %d", len(open_positions))
    for position in open_positions:
        logger.info("  %s x%s @ %.4f", position.option, position.user_remaining_qty, position.entry_price)

    store.close()
    ib.disconnect()


if __name__ == "__main__":
    main()
