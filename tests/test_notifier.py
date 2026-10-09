"""Notification building: colours, pings, and the jump link.

The notification surface is plain dataclasses so it can be asserted without a
Discord connection; only `to_embed` touches discord.py.
"""

import pytest

from datetime import date

from options_scanner.models import OptionKey
from options_scanner.notifier import (
    COLOR_GREEN,
    COLOR_RED,
    COLOR_YELLOW,
    REACTION_PLACED,
    REACTION_RECEIVED,
    REACTION_REJECTED,
    REACTION_SKIPPED,
    Level,
    Notification,
    alert_parsed,
    alert_rejected,
    broker_disconnected,
    daily_loss_limit,
    end_of_day,
    entry_filled,
    money,
    near_close_warning,
    phantom_exit,
    price,
    runner_level,
    signed_pct,
    stale_quotes,
    stop_moved,
    stopped_out,
    trail_armed,
    trim_executed,
)

JUMP = "https://discord.com/channels/1/2/3"
OPT = OptionKey("SPX", date(2026, 10, 6), 7815.0, "P")
TODAY = date(2026, 10, 6)


# --- severity, colour and pings ------------------------------------------


@pytest.mark.parametrize(
    "level,color",
    [
        (Level.SUCCESS, COLOR_GREEN),
        (Level.WARNING, COLOR_YELLOW),
        (Level.ERROR, COLOR_RED),
        (Level.CRITICAL, COLOR_RED),
    ],
)
def test_levels_map_to_the_documented_colours(level, color):
    assert level.color == color


def test_only_errors_and_criticals_ping_the_owner():
    """Pinging on routine events trains the ping to be ignored, which is worse
    than not pinging at all."""
    assert Level.INFO.mentions_owner is False
    assert Level.SUCCESS.mentions_owner is False
    assert Level.WARNING.mentions_owner is False
    assert Level.ERROR.mentions_owner is True
    assert Level.CRITICAL.mentions_owner is True


@pytest.mark.parametrize(
    "builder",
    [
        lambda: broker_disconnected(detail="socket closed"),
        lambda: stale_quotes(occ_symbol="SPXW  261006P07815000", seconds=30),
        lambda: phantom_exit(occ_symbol="SPXW  261006P07815000", expected=8, actual=0),
        lambda: daily_loss_limit(realized=-1200.0, limit=1000.0),
        lambda: near_close_warning(positions=["SPXW  261006P07815000"], minutes_left=10),
    ],
)
def test_the_things_that_need_a_human_now_all_ping(builder):
    assert builder().mentions_owner is True


def test_routine_good_news_does_not_ping():
    note = trim_executed(
        option=OPT, level_pct=25, qty=6, fill_price=0.60, entry_price=0.48, remaining=15,
        original_qty=21, realized=72.0, jump_url=JUMP, today=TODAY,
    )
    assert note.level is Level.SUCCESS
    assert note.mentions_owner is False


def test_a_losing_stop_out_is_red_and_a_winning_one_is_green():
    losing = stopped_out(
        option=OPT, qty=8, stop_price=0.48, fill_price=0.47, entry_price=0.48,
        realized=-50.0, pnl_pct=-5.0, jump_url=JUMP, today=TODAY,
    )
    winning = stopped_out(
        option=OPT, qty=8, stop_price=1.09, fill_price=1.08, entry_price=0.48,
        realized=500.0, pnl_pct=120.0, jump_url=JUMP, today=TODAY,
    )
    assert losing.level is Level.ERROR
    assert winning.level is Level.SUCCESS


# --- the jump link -------------------------------------------------------


def test_a_notification_about_an_alert_carries_its_jump_link():
    note = alert_rejected(reason="bad", detail="why", raw_title="BUY — X", jump_url=JUMP)
    assert note.jump_url == JUMP


def test_the_jump_link_becomes_an_embed_field():
    embed = alert_rejected(reason="r", detail="d", raw_title="t", jump_url=JUMP).to_embed()
    assert any("jump to alert" in (f.value or "") for f in embed.fields)


def test_no_jump_field_when_there_is_no_alert_to_point_at():
    embed = broker_disconnected(detail="gone").to_embed()
    assert not any("jump to alert" in (f.value or "") for f in embed.fields)


# --- embeds --------------------------------------------------------------


def test_dry_run_embeds_are_labelled():
    note = Notification(title="Would place entry order")
    assert note.to_embed(dry_run=True).title == "[DRY RUN] Would place entry order"
    assert note.to_embed(dry_run=False).title == "Would place entry order"


def test_an_empty_field_value_does_not_break_the_embed():
    """Discord rejects a field with an empty value, so it is padded."""
    note = Notification(title="t")
    note.add("Empty", "")
    embed = note.to_embed()
    assert embed.fields[0].value == "​"


def test_to_text_is_loggable_and_carries_the_fields():
    note = Notification(title="Stop moved up", level=Level.SUCCESS, description="why")
    note.add("Stop", "$0.48 → $0.62")
    text = note.to_text()

    assert "[SUCCESS] Stop moved up" in text
    assert "why" in text
    assert "Stop: $0.48 → $0.62" in text


# --- content -------------------------------------------------------------


def test_the_parsed_alert_distinguishes_our_size_from_the_advisors():
    note = alert_parsed(
        option=OPT,
        occ_symbol="SPXW  261006P07815000",
        entry_price=0.475,
        advisor_contracts=25,
        our_contracts=21,
        cost=997.5,
        jump_url=JUMP,
        levels=[(25, 0.59375), (50, 0.7125), (75, 0.83125)],
        today=TODAY,
    )
    values = {name: value for name, value, _ in note.fields}

    assert values["Advisor size"] == "25 contracts"
    assert values["Our size"].startswith("21 contracts")
    assert values["Trim Targets"].count("\n") == 2


def test_stop_moved_explains_why_in_words():
    breakeven = stop_moved(option=OPT, old=None, new=0.48, reason="breakeven", jump_url=JUMP, today=TODAY)
    trail = stop_moved(option=OPT, old=0.48, new=0.62, reason="trail", jump_url=JUMP, today=TODAY)

    assert "break-even" in breakeven.description
    assert "ratcheted" in trail.description
    values = {name: value for name, value, _ in breakeven.fields}
    assert values["Stop"] == "none → $0.48"


def test_the_near_close_warning_explains_the_consequence():
    """Forced exit is off by configuration, so this warning is the only thing
    between a runner and a worthless expiry."""
    note = near_close_warning(positions=["X"], minutes_left=12)

    assert note.level is Level.CRITICAL
    assert "expire worthless" in note.description
    assert "!flatten" in note.description


def test_the_phantom_exit_warning_names_the_likely_cause():
    note = phantom_exit(occ_symbol="X", expected=8, actual=0)
    assert "outside this bot" in note.description
    assert "account-wide" in note.description


def test_end_of_day_is_green_on_profit_and_red_on_loss():
    day = {"entries": 2, "realized_pnl": 300.0, "alerts_received": 5, "alerts_rejected": 1,
           "alerts_skipped": 1}
    assert end_of_day(day=day, open_positions=[]).level is Level.SUCCESS
    assert end_of_day(day={**day, "realized_pnl": -10.0}, open_positions=[]).level is Level.ERROR


# --- formatting ----------------------------------------------------------


@pytest.mark.parametrize(
    "value,expected",
    [
        (0.475, "$0.475"),
        (0.59375, "$0.594"),
        (0.7125, "$0.713"),  # not $0.712 -- must match the advisor's own card
        (0.5, "$0.50"),   # never fewer than two decimals
        (0.8, "$0.80"),
        (0.95, "$0.95"),
        (1.905, "$1.91"),
        (12.0, "$12.00"),
    ],
)
def test_sub_dollar_prices_keep_their_third_decimal(value, expected):
    """0DTE options trade in tenths of a cent; rounding them to two decimals in
    a notification would hide the difference between two rungs."""
    assert price(value) == expected


def test_money_and_percent_formatting():
    assert money(1234.5) == "$1,234.50"
    assert money(-50.0) == "-$50.00"  # sign before the symbol
    assert signed_pct(12.34) == "+12.3%"
    assert signed_pct(-5.0) == "-5.0%"


# --- reactions -----------------------------------------------------------


def test_the_four_reactions_are_distinct():
    reactions = {REACTION_RECEIVED, REACTION_PLACED, REACTION_REJECTED, REACTION_SKIPPED}
    assert len(reactions) == 4


# --- the runner's climb and the trail -------------------------------------


def test_the_runner_level_card_names_the_level_and_sells_nothing():
    note = runner_level(
        option=OPT, level_pct=200, trigger_price=1.43, entry_price=0.475,
        remaining=4, stop_price=0.79, jump_url=JUMP, today=TODAY,
    )

    assert note.title == "RUNNER +200% — SPX 7815P · 0DTE"
    assert "Nothing sold" in note.description
    assert "4 still running" in note.description
    assert note.level is Level.SUCCESS
    assert not note.mentions_owner  # routine good news; a ping here trains the ping away


def test_the_runner_level_card_reports_what_the_stop_locks_in():
    note = runner_level(
        option=OPT, level_pct=500, trigger_price=2.85, entry_price=0.475,
        remaining=4, stop_price=2.00, today=TODAY,
    )

    stop_field = dict((name, value) for name, value, _ in note.fields)["Stop"]
    assert "$2.00" in stop_field
    assert "+321.1%" in stop_field  # 2.00 / 0.475 - 1


def test_the_runner_level_card_says_so_when_there_is_no_stop_yet():
    """Reachable for a 1-contract position below the arming level: it cannot
    trim, so it has earned no stop. A blank field would read as a bug."""
    note = runner_level(
        option=OPT, level_pct=75, trigger_price=0.84, entry_price=0.475,
        remaining=1, stop_price=None, today=TODAY,
    )

    assert dict((name, value) for name, value, _ in note.fields)["Stop"] == "none yet"


def test_the_trail_card_describes_the_distance_below_the_peak():
    note = trail_armed(option=OPT, at_price=0.84, multiplier=0.40, today=TODAY)

    assert "60% below the peak" in note.description
    assert dict((name, value) for name, value, _ in note.fields)["Trails at"] == "40% of peak"


def test_the_trail_card_admits_when_the_trail_is_not_yet_protecting_anything():
    """peak * 0.40 is under the entry fill until the peak reaches +150%.
    Announcing a trailing stop without saying that would overstate the
    protection."""
    note = trail_armed(option=OPT, at_price=0.84, multiplier=0.40, today=TODAY)

    assert "+150%" in note.description
    assert "break-even" in note.description


def test_a_tighter_trail_reports_a_shorter_inert_window():
    note = trail_armed(option=OPT, at_price=0.84, multiplier=0.80, today=TODAY)

    assert "20% below the peak" in note.description
    assert "+25%" in note.description  # 1/0.80 - 1


def test_the_entry_card_reports_where_the_size_came_from():
    note = entry_filled(
        option=OPT, qty=6, fill_price=0.475, cost=285.0, jump_url=JUMP,
        sizing_note="Our size: 3% unit · lotto x0.5 · $485 of $32,310 equity",
        today=TODAY,
    )

    assert "3% unit" in note.description
    assert "lotto" in note.description
    assert "Entered SPX" in note.description  # the advisor's line is still there


def test_the_entry_card_is_unchanged_without_a_sizing_note():
    note = entry_filled(option=OPT, qty=6, fill_price=0.475, cost=285.0, jump_url=JUMP, today=TODAY)

    assert note.description == "Entered SPX Oct06 '26 7815 Put"
