"""Config loading, secret hygiene, and the gates that protect real money."""

from pathlib import Path

import pytest
from conftest import env, make_settings

from options_scanner.config import BrokerKind, Mode, Settings, load_settings


def write_config(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "config.yaml"
    path.write_text(body)
    return path


# --- loading --------------------------------------------------------------


def test_secrets_come_from_the_environment(monkeypatch, tmp_path):
    env(monkeypatch)
    settings = load_settings(write_config(tmp_path, "mode: dry_run\n"), load_env=False)

    assert settings.discord.bot_token == "test-token-not-a-real-secret"
    assert settings.discord.alerts_channel_id == 1001
    assert settings.mode is Mode.DRY_RUN


def test_a_discord_block_in_the_yaml_is_ignored(monkeypatch, tmp_path):
    """Secrets must not be loadable from a committed file, even if someone
    helpfully pastes them in."""
    env(monkeypatch)
    path = write_config(tmp_path, "discord:\n  bot_token: leaked-from-yaml\n  owner_user_id: 9\n")
    settings = load_settings(path, load_env=False)

    assert settings.discord.bot_token == "test-token-not-a-real-secret"
    assert settings.discord.owner_user_id == 3003


@pytest.mark.parametrize(
    "missing", ["DISCORD_BOT_TOKEN", "ALERTS_CHANNEL_ID", "UPDATES_CHANNEL_ID", "OWNER_USER_ID"]
)
def test_a_missing_secret_names_itself(monkeypatch, tmp_path, missing):
    env(monkeypatch)
    monkeypatch.delenv(missing)

    with pytest.raises(ValueError, match=missing):
        load_settings(write_config(tmp_path, "mode: dry_run\n"), load_env=False)


def test_a_missing_config_file_is_an_error_when_named(monkeypatch, tmp_path):
    env(monkeypatch)
    with pytest.raises(FileNotFoundError):
        load_settings(tmp_path / "nope.yaml", load_env=False)


def test_a_non_mapping_config_is_rejected(monkeypatch, tmp_path):
    env(monkeypatch)
    with pytest.raises(ValueError, match="YAML mapping"):
        load_settings(write_config(tmp_path, "- just\n- a\n- list\n"), load_env=False)


def test_overrides_win_over_the_file(monkeypatch, tmp_path):
    env(monkeypatch)
    settings = load_settings(
        write_config(tmp_path, "mode: paper\n"), load_env=False, overrides={"mode": "dry_run"}
    )
    assert settings.mode is Mode.DRY_RUN


def test_the_committed_config_is_valid(monkeypatch):
    """The config.yaml in the repo must actually load, or the first run fails."""
    env(monkeypatch)
    settings = load_settings(load_env=False)
    assert settings.mode is Mode.DRY_RUN  # the committed default is the safe one


# --- secret hygiene ------------------------------------------------------


def test_the_token_never_appears_in_the_redacted_dump():
    settings = make_settings()
    dumped = settings.redacted()

    assert dumped["discord"]["bot_token"] == "***redacted***"
    assert "test-token-not-a-real-secret" not in str(dumped)


def test_the_summary_line_carries_no_secret():
    assert "test-token" not in make_settings().summary_line()


# --- the live-trading gates ----------------------------------------------


def test_live_mode_requires_the_confirmation_flag():
    with pytest.raises(ValueError, match="live_confirmed is false"):
        make_settings(mode="live", broker={"port": 4002})


def test_live_mode_with_the_flag_is_allowed():
    settings = make_settings(
        mode="live", live_confirmed=True, broker={"port": 4001, "kind": "ibkr"}
    )
    assert settings.mode is Mode.LIVE
    assert settings.broker.kind is BrokerKind.IBKR


def test_live_mode_refuses_simulated_fills():
    """Simulated fills in live mode would report trades that never happened."""
    with pytest.raises(ValueError, match="would report trades that never happened"):
        make_settings(mode="live", live_confirmed=True, broker={"port": 4001, "kind": "simulated"})


def test_dry_run_refuses_a_real_order_path():
    with pytest.raises(ValueError, match="must not be able to reach"):
        make_settings(mode="dry_run", broker={"kind": "ibkr"})


def test_the_default_broker_kind_is_the_simulator():
    """The difference between the two is a dry rehearsal and a working order."""
    assert make_settings().broker.kind is BrokerKind.SIMULATED


def test_paper_mode_may_use_either_adapter():
    assert make_settings(mode="paper").broker.kind is BrokerKind.SIMULATED
    assert make_settings(mode="paper", broker={"kind": "ibkr"}).broker.kind is BrokerKind.IBKR


def test_an_unknown_broker_kind_is_rejected():
    with pytest.raises(ValueError):
        make_settings(broker={"kind": "robinhood"})


def test_the_summary_line_names_the_broker_kind():
    assert "broker=simulated" in make_settings().summary_line()


@pytest.mark.parametrize("live_port", [4001, 7496])
def test_a_non_live_mode_refuses_a_live_port(live_port):
    """One mistyped digit should not be able to point a paper run at a live
    account."""
    with pytest.raises(ValueError, match="live IBKR port"):
        make_settings(mode="paper", broker={"port": live_port})


def test_dry_run_does_not_place_orders_and_the_others_do():
    assert Mode.DRY_RUN.places_orders is False
    assert Mode.PAPER.places_orders is True
    assert Mode.LIVE.places_orders is True


# --- channel discipline --------------------------------------------------


def test_the_two_channels_must_differ():
    with pytest.raises(ValueError, match="different channels"):
        Settings.model_validate(
            {
                "discord": {
                    "bot_token": "t",
                    "alerts_channel_id": 1,
                    "updates_channel_id": 1,
                    "owner_user_id": 2,
                }
            }
        )


# --- validation of the trading tunables ----------------------------------


def test_a_bad_cutoff_time_fails_at_load_not_at_1559():
    with pytest.raises(ValueError):
        make_settings(market={"entry_cutoff": "half past three"})


def test_a_bad_force_exit_time_also_fails_early():
    with pytest.raises(ValueError):
        make_settings(market={"force_exit_time": "25:99"})


@pytest.mark.parametrize(
    "block,payload",
    [
        ("entry", {"max_slippage_pct": 0}),
        ("entry", {"fill_timeout_seconds": 0}),
        ("entry", {"duplicate_window_seconds": 0}),
        ("trail", {"arm_at_pct": 0}),
        ("trail", {"schedule": [(0, 1.0)]}),
        ("trail", {"schedule": [(200, 0.4)]}),  # must start at a 0% threshold
        ("trail", {"schedule": [(0, 0.6), (200, 0.4)]}),  # must not loosen
        ("trim", {"runner_levels": [25, 300]}),  # 25 is already a trim rung
        ("risk", {"unit_pct_of_account": 0}),
        ("risk", {"lotto_multiplier": 0}),
        ("risk", {"super_lotto_multiplier": 1.5}),
        ("risk", {"fallback_equity": 0}),
        ("stops", {"confirm_polls": 0}),
        ("stops", {"source_blend": 1.5}),
        ("risk", {"max_usd_per_trade": 0}),
        ("risk", {"max_contracts_per_trade": 0}),
        ("risk", {"max_open_positions": 0}),
    ],
)
def test_nonsensical_tunables_are_rejected(block, payload):
    with pytest.raises(ValueError):
        make_settings(**{block: payload})


# --- translation into the rules engine -----------------------------------


def test_whole_percent_config_becomes_fractions_for_the_rules_engine():
    rules = make_settings().rules()

    assert rules.trim_schedule == ((25, 0.50), (50, 0.25))
    assert rules.runner_levels == (75, 100, 150, 200, 500, 1000, 2000)
    assert rules.trail_schedule == ((0, 0.40), (200, 0.55), (500, 0.70))
    assert rules.trail_arm_pct == 75
    assert rules.breakeven_after_level_pct == 25


def test_a_custom_schedule_carries_through_to_the_rules_engine():
    settings = make_settings(
        trim={"schedule": [(50, 50.0)], "runner_levels": [200]},
        trail={"arm_at_pct": 200},
    )
    rules = settings.rules()

    assert rules.trim_schedule == ((50, 0.5),)
    assert rules.runner_levels == (200,)
    assert rules.breakeven_after_level_pct == 50  # the lowest rung, not a hardcoded 25
    assert rules.trail_arm_pct == 200


def test_a_custom_trail_schedule_carries_through_to_the_rules_engine():
    rules = make_settings(trail={"schedule": [(0, 0.5), (1000, 0.8)]}).rules()

    assert rules.trail_schedule == ((0, 0.5), (1000, 0.8))


def test_the_confirmation_window_is_shared_with_the_rules_engine():
    assert make_settings(stops={"confirm_polls": 3}).rules().confirm_breaches == 3
