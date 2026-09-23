from __future__ import annotations

import os
from pathlib import Path

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


class TradingConfig(BaseModel):
    mode: str = "paper"
    host: str = "127.0.0.1"
    port: int
    client_id: int = 11

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
            },
            "trading": {
                "mode": os.environ.get("MODE", "paper"),
                "host": os.environ.get("IBKR_HOST", "127.0.0.1"),
                "port": int(os.environ["IBKR_PORT"]),
                "client_id": int(os.environ.get("IBKR_CLIENT_ID", "11")),
            },
            "risk": {
                "max_usd_per_trade": float(os.environ.get("MAX_USD_PER_TRADE", "1000")),
            },
        }
    )
