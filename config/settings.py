from __future__ import annotations

import os
from pathlib import Path
from typing import Literal

from dotenv import load_dotenv
from pydantic import BaseModel, Field, model_validator

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# IBKR's documented paper ports (Gateway 4002 / TWS 7497) vs live ports
# (Gateway 4001 / TWS 7496). Not assumed as *the* port -- this machine's
# ~/Jts/jts.ini currently runs Gateway paper on 4000, a non-default value --
# but used to guard against ever firing live orders while MODE=paper.
_LIVE_PORTS = {4001, 7496}


class DiscordConfig(BaseModel):
    bot_token: str
    channel_id: int
    webhook_alerts: str = ""
    webhook_trade_activity: str = ""


class TradingConfig(BaseModel):
    mode: str = "paper"
    host: str = "127.0.0.1"
    port: int
    client_id: int = 11

    # IBKR market data type: 1 live, 2 frozen, 3 delayed, 4 delayed-frozen.
    # Runners need ticks to know a target was reached, so 1 is the default;
    # bot/fill_watcher.py downgrades to 3 and alerts if the account has no
    # OPRA subscription rather than leaving runners silently un-ratcheting.
    market_data_type: int = Field(default=1, ge=1, le=4)

    @model_validator(mode="after")
    def _guard_mode(self) -> "TradingConfig":
        if self.mode not in ("paper", "live"):
            raise ValueError(f"MODE must be 'paper' or 'live', got {self.mode!r}")
        if self.mode == "paper" and self.port in _LIVE_PORTS:
            raise ValueError(
                f"MODE is 'paper' but IBKR_PORT {self.port} is a known live-account port. "
                "Confirm the paper port in Gateway/TWS's API settings dialog."
            )
        return self


class RiskConfig(BaseModel):
    max_usd_per_trade: float = Field(gt=0)

    # Initial protective stop, as a fraction below the entry fill, placed
    # the moment the buy fills. From there it only ever ratchets up: each
    # trim target reached moves it to the previous rung (see
    # bot/exit_plan.py's ladder_stop).
    stop_loss_pct: float = Field(default=0.30, gt=0, lt=1)

    # STP triggers a market order -- certain to exit, at whatever the book
    # offers. STP_LMT won't fill below its limit, which on a fast-moving
    # option can mean not exiting at all. STP is the safer default for a
    # protective stop; switch deliberately.
    stop_order_type: Literal["STP", "STP_LMT"] = "STP"

    # How far a STP_LMT's limit sits below its trigger, as a fraction of
    # the trigger price. Ignored when stop_order_type is STP.
    stop_limit_offset_pct: float = Field(default=0.10, ge=0, lt=1)

    # A runner has no resting limit order, so nothing caps how far it can
    # climb. True keeps extending the ladder past the channel's last
    # published target at the ladder's own spacing (125%, 150%, ...), so
    # the stop never stops ratcheting. False freezes it at the last
    # published tier.
    runner_ladder_extends: bool = True


class AppConfig(BaseModel):
    discord: DiscordConfig
    trading: TradingConfig
    risk: RiskConfig

    def resolve_path(self, relative: str) -> Path:
        return PROJECT_ROOT / relative


def load_config() -> AppConfig:
    load_dotenv(PROJECT_ROOT / ".env")
    return AppConfig.model_validate(
        {
            "discord": {
                "bot_token": os.environ["DISCORD_BOT_TOKEN"],
                "channel_id": int(os.environ["DISCORD_CHANNEL_ID"]),
                "webhook_alerts": os.environ.get("DISCORD_WEBHOOK_ALERTS", ""),
                "webhook_trade_activity": os.environ.get("DISCORD_WEBHOOK_TRADE_ACTIVITY", ""),
            },
            "trading": {
                "mode": os.environ.get("MODE", "paper"),
                "host": os.environ.get("IBKR_HOST", "127.0.0.1"),
                "port": int(os.environ["IBKR_PORT"]),
                "client_id": int(os.environ.get("IBKR_CLIENT_ID", "11")),
                "market_data_type": int(os.environ.get("IBKR_MARKET_DATA_TYPE", "1")),
            },
            "risk": {
                "max_usd_per_trade": float(os.environ.get("MAX_USD_PER_TRADE", "1000")),
                "stop_loss_pct": float(os.environ.get("STOP_LOSS_PCT", "30")) / 100.0,
                "stop_order_type": os.environ.get("STOP_ORDER_TYPE", "STP"),
                "stop_limit_offset_pct": float(os.environ.get("STOP_LIMIT_OFFSET_PCT", "10")) / 100.0,
                "runner_ladder_extends": os.environ.get("RUNNER_LADDER_EXTENDS", "true").lower()
                in ("1", "true", "yes"),
            },
        }
    )
