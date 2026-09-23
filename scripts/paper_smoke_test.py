"""One-shot smoke test: places a single small paper order through the real
execution pipeline (bot/contracts.py -> bot/execution.py), independent of
Discord entirely, to validate the IBKR connection/contract-qualification/
order-placement/PositionStore chain before trusting it with live alerts.

Refuses to run unless .env's MODE is "paper".

Usage:
    python scripts/paper_smoke_test.py SPY 2026-09-25 650 C 1.00
    python scripts/paper_smoke_test.py SPX 2026-09-23 6700 P 0.75 --max-usd 300
"""

from __future__ import annotations

import argparse
import logging
from datetime import date

from ib_async import IB

from bot.execution import handle_event
from bot.logging_config import setup_logging
from bot.models import BuyEvent, OptionKey
from bot.notifier import Notifier
from bot.position_store import PositionStore
from config.settings import RiskConfig, load_config

logger = logging.getLogger("options_scanner.scripts.paper_smoke_test")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("ticker")
    parser.add_argument("expiry", help="YYYY-MM-DD")
    parser.add_argument("strike", type=float)
    parser.add_argument("right", choices=["C", "P"])
    parser.add_argument("entry_price", type=float, help="Rough current price -- drives contract sizing only")
    parser.add_argument("--max-usd", type=float, default=200.0, help="Small cap for this one-off test (default $200)")
    args = parser.parse_args()

    setup_logging()
    config = load_config()
    if config.trading.mode != "paper":
        raise SystemExit("Refusing to run the smoke test outside paper mode (MODE must be 'paper' in .env)")

    ib = IB()
    ib.connect(config.trading.host, config.trading.port, clientId=config.trading.client_id)

    store = PositionStore(config.resolve_path("data/smoke_test.sqlite3"))
    notifier = Notifier(config.discord.webhook_alerts)
    risk = RiskConfig(max_usd_per_trade=args.max_usd)

    option = OptionKey(args.ticker.upper(), date.fromisoformat(args.expiry), args.strike, args.right)
    event = BuyEvent(
        message_id=1,
        option=option,
        entry_price=args.entry_price,
        contracts=1,  # channel-side placeholder -- unused for sizing, see bot/execution.py
        cost=args.entry_price * 100,
    )

    logger.info("Placing smoke-test BUY for %s (cap $%.2f)", option, args.max_usd)
    handle_event(event, ib, store, notifier, risk)

    position = store.get_open(option)
    if position is None:
        logger.error("No position was opened -- check the log above for the alert/rejection reason")
    else:
        logger.info(
            "Opened %s contract(s) of %s @ %.4f (IBKR order id %s)",
            position.user_remaining_qty,
            option,
            position.entry_price,
            position.ibkr_order_id_entry,
        )

    store.close()
    ib.disconnect()


if __name__ == "__main__":
    main()
