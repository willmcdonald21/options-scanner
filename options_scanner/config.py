"""Configuration: tunables from config.yaml, secrets from .env.

Nothing sensitive lives in the YAML and nothing tunable lives in the
environment, so config.yaml can be committed and diffed while tokens stay
out of the repo. `Settings.redacted()` is what gets logged; the real secrets
never reach a log line or a Discord message.

Validation is deliberately strict and happens at import time, because a typo
in a stop percentage or a cutoff time is a money bug and 15:59 is a bad time
to discover it.
"""

from __future__ import annotations

import os
from datetime import time
from enum import Enum
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv
from pydantic import BaseModel, Field, model_validator

from options_scanner.market_hours import parse_cutoff
from options_scanner.rules import RulesConfig

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_PATH = PROJECT_ROOT / "config.yaml"


class Mode(str, Enum):
    """DRY_RUN decides and reports but sends nothing to a broker. PAPER trades
    a simulated or paper account. LIVE risks real money and needs both an
    explicit config flag and an interactive confirmation at startup."""

    DRY_RUN = "dry_run"
    PAPER = "paper"
    LIVE = "live"

    @property
    def places_orders(self) -> bool:
        return self is not Mode.DRY_RUN


class DiscordSettings(BaseModel):
    """IDs and the token all come from the environment -- a channel id is not
    secret, but keeping the whole Discord block in one place means config.yaml
    never has to be scrubbed before sharing."""

    bot_token: str = Field(min_length=1)
    alerts_channel_id: int = Field(gt=0)
    updates_channel_id: int = Field(gt=0)
    owner_user_id: int = Field(gt=0)

    @model_validator(mode="after")
    def _channels_must_differ(self) -> "DiscordSettings":
        if self.alerts_channel_id == self.updates_channel_id:
            raise ValueError(
                "alerts and updates must be different channels -- the bot never posts text "
                "into the alerts channel, so pointing both at one channel would silence it"
            )
        return self


class EntrySettings(BaseModel):
    # Limit orders only, never market. Start at the alert's entry price and
    # walk up toward the cap; cancel rather than chase past it.
    max_slippage_pct: float = Field(default=10.0, gt=0, le=100)
    fill_timeout_seconds: float = Field(default=30.0, gt=0)
    limit_walk_steps: int = Field(default=3, ge=1, le=20)

    # If the ask is already past the cap when the alert lands, the move has
    # gone without us. Chasing it is how a good system turns into a bad one.
    skip_if_ask_above_cap: bool = True

    # Re-pasting an alert creates a *new* Discord message, so the message-id
    # check cannot catch it. Treat the same contract at the same entry inside
    # this window as the same alert. Long enough to cover a fat-fingered
    # double paste and a restart, short enough not to block a genuine
    # re-entry later in the session.
    duplicate_window_seconds: float = Field(default=900.0, gt=0)


class TrimSettings(BaseModel):
    # (level percent, percent of the *remaining* position to sell).
    # +100% carries 0 by design: the runner rides the trail from there.
    schedule: list[tuple[int, float]] = Field(
        default_factory=lambda: [(25, 25.0), (50, 25.0), (75, 25.0), (100, 0.0)]
    )
    min_runner_contracts: int = Field(default=1, ge=0)


class TrailSettings(BaseModel):
    arm_at_pct: int = Field(default=75, gt=0)
    # Percent of the gain handed back from the peak. 60 keeps 40% of the best
    # gain: stop = entry + 0.40 * (peak - entry).
    giveback_pct: float = Field(default=60.0, ge=0, lt=100)


class StopSettings(BaseModel):
    # 0.0 = pure bid (what we could actually sell into), 1.0 = pure mid.
    source_blend: float = Field(default=0.0, ge=0, le=1)
    confirm_polls: int = Field(default=2, ge=1, le=10)
    poll_seconds: float = Field(default=1.5, gt=0, le=10)

    # With synthetic stops, no quotes means no stop. This is the threshold for
    # shouting about it rather than carrying on blind.
    stale_quote_seconds: float = Field(default=15.0, gt=0)

    # How far through the bid an exit prices its limit, as a percent. A stop
    # that has triggered needs to actually fill, so the limit is placed below
    # the bid rather than on it -- but it stays a limit, because a market order
    # on a wide 0DTE book is an open-ended cost.
    exit_through_pct: float = Field(default=2.0, ge=0, le=50)

    # Attempts at the same exit before escalating to the owner. An exit that
    # will not fill is the one failure that cannot be left quiet.
    exit_retries: int = Field(default=3, ge=1, le=10)


class RiskSettings(BaseModel):
    max_usd_per_trade: float = Field(default=1000.0, gt=0)
    max_contracts_per_trade: int = Field(default=50, ge=1)
    max_open_positions: int = Field(default=3, ge=1)
    max_daily_loss_usd: float = Field(default=1000.0, gt=0)
    max_trades_per_day: int = Field(default=10, ge=1)


class MarketSettings(BaseModel):
    # No new entries after this (shifted earlier on half days).
    entry_cutoff: str = "15:30"

    # Off by the user's decision. A 0DTE runner whose trail never trips will
    # expire worthless, so the near-close warning exists to make that visible
    # while the choice stands.
    force_exit_enabled: bool = False
    force_exit_time: str = "15:50"
    near_close_warning_minutes: int = Field(default=15, ge=0)

    @model_validator(mode="after")
    def _times_parse(self) -> "MarketSettings":
        self.entry_cutoff_time  # noqa: B018 - force a parse so a typo fails now
        self.force_exit_time_parsed  # noqa: B018
        return self

    @property
    def entry_cutoff_time(self) -> time:
        return parse_cutoff(self.entry_cutoff)

    @property
    def force_exit_time_parsed(self) -> time:
        return parse_cutoff(self.force_exit_time)


class BrokerKind(str, Enum):
    """Which adapter handles orders.

    `simulated` fills locally against real IBKR quotes and sends nothing to the
    broker. `ibkr` sends real orders to whatever account the port points at --
    which is the paper account on 4002, but is still a real order path. The
    default is the simulator because the difference between the two is the
    difference between a dry rehearsal and a working order.
    """

    SIMULATED = "simulated"
    IBKR = "ibkr"


class BrokerSettings(BaseModel):
    kind: BrokerKind = BrokerKind.SIMULATED
    host: str = "127.0.0.1"
    port: int = Field(default=4002, gt=0)

    # Must differ from every other client on this Gateway. 11 belongs to
    # warrior_bot, which shares this paper account.
    client_id: int = Field(default=12, ge=0)
    market_data_type: int = Field(default=1, ge=1, le=4)

    # IBKR's documented live ports. Guarded against regardless of mode so a
    # paper run can never reach a live account by a one-character typo.
    LIVE_PORTS: tuple[int, ...] = (4001, 7496)


class StorageSettings(BaseModel):
    db_path: str = "data/options_scanner.sqlite3"


class LoggingSettings(BaseModel):
    level: str = "INFO"
    file: str = "logs/options_scanner.log"


class Settings(BaseModel):
    mode: Mode = Mode.PAPER

    # Live trading needs this true *and* an interactive confirmation at
    # startup. Two independent gates, because one is a typo away from being
    # flipped by accident.
    live_confirmed: bool = False

    discord: DiscordSettings
    entry: EntrySettings = Field(default_factory=EntrySettings)
    trim: TrimSettings = Field(default_factory=TrimSettings)
    trail: TrailSettings = Field(default_factory=TrailSettings)
    stops: StopSettings = Field(default_factory=StopSettings)
    risk: RiskSettings = Field(default_factory=RiskSettings)
    market: MarketSettings = Field(default_factory=MarketSettings)
    broker: BrokerSettings = Field(default_factory=BrokerSettings)
    storage: StorageSettings = Field(default_factory=StorageSettings)
    logging: LoggingSettings = Field(default_factory=LoggingSettings)

    @model_validator(mode="after")
    def _guard_live(self) -> "Settings":
        if self.mode is Mode.LIVE and not self.live_confirmed:
            raise ValueError(
                "mode is 'live' but live_confirmed is false. Live trading requires both, "
                "plus a typed confirmation at startup."
            )
        if self.mode is not Mode.LIVE and self.broker.port in self.broker.LIVE_PORTS:
            raise ValueError(
                f"mode is {self.mode.value!r} but broker.port {self.broker.port} is a known live "
                "IBKR port. Use 4002 for Gateway paper."
            )
        if self.mode is Mode.LIVE and self.broker.kind is not BrokerKind.IBKR:
            raise ValueError(
                "mode is 'live' but broker.kind is 'simulated'. Simulated fills with a live mode "
                "would report trades that never happened."
            )
        if self.mode is Mode.DRY_RUN and self.broker.kind is BrokerKind.IBKR:
            raise ValueError(
                "mode is 'dry_run' but broker.kind is 'ibkr'. Dry run must not be able to reach "
                "a real order path; use broker.kind 'simulated'."
            )
        return self

    def rules(self) -> RulesConfig:
        """The config the rules engine actually runs on. Percentages are
        fractions in there and whole numbers here, converted in one place."""
        return RulesConfig(
            trim_schedule=tuple((level, pct / 100.0) for level, pct in self.trim.schedule),
            trail_arm_pct=self.trail.arm_at_pct,
            trail_giveback=self.trail.giveback_pct / 100.0,
            breakeven_after_level_pct=min(level for level, _ in self.trim.schedule),
            confirm_breaches=self.stops.confirm_polls,
            min_runner_contracts=self.trim.min_runner_contracts,
        )

    def resolve_path(self, relative: str) -> Path:
        return PROJECT_ROOT / relative

    def redacted(self) -> dict[str, Any]:
        """Safe to log. The token is replaced, not truncated -- a prefix is
        still a credential leak."""
        data = self.model_dump(mode="json")
        data["discord"]["bot_token"] = "***redacted***"
        return data

    def summary_line(self) -> str:
        levels = "/".join(f"+{level}%" for level, pct in self.trim.schedule if pct > 0)
        return (
            f"mode={self.mode.value} broker={self.broker.kind.value} trims={levels} "
            f"trail=arm@+{self.trail.arm_at_pct}%/giveback{self.trail.giveback_pct:.0f}% "
            f"size=${self.risk.max_usd_per_trade:,.0f}/trade (max {self.risk.max_contracts_per_trade}) "
            f"cutoff={self.market.entry_cutoff} ET"
        )


def _discord_from_env() -> dict[str, Any]:
    missing = [
        name
        for name in ("DISCORD_BOT_TOKEN", "ALERTS_CHANNEL_ID", "UPDATES_CHANNEL_ID", "OWNER_USER_ID")
        if not os.environ.get(name)
    ]
    if missing:
        raise ValueError(
            f"missing required environment variable(s): {', '.join(missing)}. "
            "Copy .env.example to .env and fill them in (see the README)."
        )
    return {
        "bot_token": os.environ["DISCORD_BOT_TOKEN"],
        "alerts_channel_id": int(os.environ["ALERTS_CHANNEL_ID"]),
        "updates_channel_id": int(os.environ["UPDATES_CHANNEL_ID"]),
        "owner_user_id": int(os.environ["OWNER_USER_ID"]),
    }


def load_settings(
    config_path: str | Path | None = None,
    *,
    load_env: bool = True,
    overrides: dict[str, Any] | None = None,
) -> Settings:
    """Read config.yaml, pull secrets from the environment, validate.

    `overrides` is for tests and for a one-off `--mode dry_run` on the command
    line; it is shallow-merged over the YAML.
    """
    if load_env:
        load_dotenv(PROJECT_ROOT / ".env")

    path = Path(config_path) if config_path else DEFAULT_CONFIG_PATH
    raw: dict[str, Any] = {}
    if path.exists():
        loaded = yaml.safe_load(path.read_text()) or {}
        if not isinstance(loaded, dict):
            raise ValueError(f"{path} must contain a YAML mapping, got {type(loaded).__name__}")
        raw = loaded
    elif config_path is not None:
        raise FileNotFoundError(f"no config file at {path}")

    raw.pop("discord", None)  # secrets come from the environment, never the YAML
    raw["discord"] = _discord_from_env()

    for key, value in (overrides or {}).items():
        raw[key] = value

    return Settings.model_validate(raw)
