"""Entry point: wire everything together and run.

Usage:
    python -m options_scanner.main
    python -m options_scanner.main --mode dry_run
    python -m options_scanner.main --config other.yaml --check
"""

from __future__ import annotations

import argparse
import logging
import sys

from options_scanner.broker.base import Broker
from options_scanner.broker.ibkr import IBKRBroker
from options_scanner.broker.ibkr_quotes import IBKRQuotes
from options_scanner.broker.paper import PaperBroker
from options_scanner.config import BrokerKind, Mode, Settings, load_settings
from options_scanner.discord_bot import SwiftAlertBot, confirm_live_interactively
from options_scanner.logging_config import setup_logging
from options_scanner.market_hours import describe as describe_session
from options_scanner.pipeline import AlertPipeline
from options_scanner.position_manager import PositionManager
from options_scanner.risk import RiskGate
from options_scanner.storage import Storage

logger = logging.getLogger("options_scanner.main")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default=None, help="path to config.yaml")
    parser.add_argument(
        "--mode",
        choices=[m.value for m in Mode],
        default=None,
        help="override the configured mode for this run",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="validate config and storage, print the resolved settings, and exit without connecting",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    overrides = {"mode": args.mode} if args.mode else None
    settings = load_settings(args.config, overrides=overrides)

    setup_logging(level=settings.logging.level, log_file=settings.logging.file)
    logger.info("settings: %s", settings.summary_line())
    logger.info("session: %s", describe_session())

    storage = Storage(settings.resolve_path(settings.storage.db_path))
    risk = RiskGate(settings, storage)

    broker = build_broker(settings)
    manager = (
        PositionManager(settings, storage, risk, broker) if broker is not None else None
    )
    pipeline = AlertPipeline(settings, storage, risk, broker, manager)

    if args.check:
        print(f"mode      : {settings.mode.value}")
        print(f"broker    : {settings.broker.kind.value} -> "
              f"{settings.broker.host}:{settings.broker.port} client {settings.broker.client_id}")
        print(f"session   : {describe_session()}")
        print(f"summary   : {settings.summary_line()}")
        print(f"database  : {settings.resolve_path(settings.storage.db_path)}")
        print(f"halted    : {storage.is_halted()}")
        print(f"open      : {storage.open_position_count()} position(s)")
        print(f"alerts ch : {settings.discord.alerts_channel_id}")
        print(f"updates ch: {settings.discord.updates_channel_id}")
        storage.close()
        return 0

    confirm_live_interactively(settings)

    client = SwiftAlertBot(
        settings=settings, storage=storage, risk=risk, pipeline=pipeline, manager=manager
    )
    try:
        client.run(settings.discord.bot_token, log_handler=None)
    except KeyboardInterrupt:
        logger.info("interrupted; shutting down")
    finally:
        storage.close()
    return 0


def build_broker(settings: Settings) -> Broker | None:
    """The adapter for this mode and broker kind.

    DRY_RUN gets no adapter at all. A broker it could accidentally order
    through would be a liability rather than a convenience, and the config
    refuses the combination anyway.

    Otherwise the quote feed is always the real IBKR one -- a paper run against
    synthetic prices tells you almost nothing about how a 0DTE ladder behaves --
    and `broker.kind` decides whether fills are simulated locally or sent to
    IBKR for real.
    """
    if settings.mode is Mode.DRY_RUN:
        return None

    def report(message: str) -> None:
        logger.error(message)

    if settings.broker.kind is BrokerKind.SIMULATED:
        quotes = IBKRQuotes(
            settings.broker.host,
            settings.broker.port,
            settings.broker.client_id,
            market_data_type=settings.broker.market_data_type,
            on_no_market_data=report,
        )
        logger.info("broker: simulated fills over real IBKR quotes")
        return PaperBroker(quotes)

    logger.warning(
        "broker: REAL IBKR orders to %s:%s as client %s",
        settings.broker.host,
        settings.broker.port,
        settings.broker.client_id,
    )
    return IBKRBroker(
        settings.broker.host,
        settings.broker.port,
        settings.broker.client_id,
        market_data_type=settings.broker.market_data_type,
        on_no_market_data=report,
    )


if __name__ == "__main__":
    sys.exit(main())
