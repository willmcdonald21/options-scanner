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
    python scripts/replay_fixture_paper.py --keep-orders
"""

from __future__ import annotations

import argparse
import logging
from datetime import datetime
from pathlib import Path

from ib_async import IB

from bot.execution import handle_event
from bot.fill_watcher import FillWatcher
from bot.logging_config import setup_logging
from bot.notifier import Notifier
from bot.orders import cancel_all_legs_for_position
from bot.parser import parse_embed
from bot.position_store import PositionStore
from bot.text_import import parse_chat_export
from config.settings import load_config

logger = logging.getLogger("options_scanner.scripts.replay_fixture_paper")

FIXTURE = Path(__file__).resolve().parent.parent / "tests" / "fixtures" / "swift_chat_dump.txt"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "--keep-orders",
        dest="cancel_at_end",
        action="store_false",
        help="leave the replay's resting GTC exit orders at IBKR for inspection (default: cancel them)",
    )
    args = parser.parse_args()

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

    # The brackets placed below are GTC and real (on paper). Running the
    # watcher means a target that fills during the replay ratchets the
    # stop exactly as it would live.
    watcher = FillWatcher(
        ib=ib,
        store=store,
        risk=config.risk,
        notifier=notifier,
        market_data_type=config.trading.market_data_type,
    )
    watcher.start()

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
        logger.info(
            "  %s x%s @ %.4f, stop %s",
            position.option,
            position.user_remaining_qty,
            position.entry_price,
            f"{position.current_stop_price:.2f}" if position.current_stop_price else "unset",
        )
        for leg in store.list_legs(position.id, only_live=True):
            logger.info(
                "      t%s %sx %s stop %.2f (oca %s)",
                leg.tier_index,
                leg.qty,
                f"limit {leg.tp_price:.2f}" if leg.tp_price is not None else "runner, no limit",
                leg.current_stop_price,
                leg.oca_group,
            )

    if args.cancel_at_end:
        # These are GTC: left alone they would still be resting at IBKR
        # tomorrow, against contracts this replay only pretended to hold.
        total = sum(cancel_all_legs_for_position(ib, store, p.id) for p in open_positions if p.id)
        logger.info("Cancelled %d resting exit order(s) left over from the replay", total)
    else:
        logger.warning("Leaving resting GTC exit orders in place -- cancel them in TWS when done")

    watcher.stop()
    store.close()
    ib.disconnect()


if __name__ == "__main__":
    main()
