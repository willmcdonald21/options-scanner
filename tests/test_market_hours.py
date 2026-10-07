"""Session awareness. Half days are the point: a cutoff that does not know
about a 13:00 close would take a position minutes before the bell.
"""

from datetime import date, datetime, time

import pytest

from options_scanner.market_hours import (
    EASTERN,
    describe,
    is_market_open,
    is_past_cutoff,
    is_trading_day,
    minutes_to_close,
    next_session,
    parse_cutoff,
    session_for,
    today_et,
)

REGULAR = date(2026, 10, 6)       # a Tuesday
HALF_DAY = date(2026, 11, 27)     # day after Thanksgiving, 13:00 close
HOLIDAY = date(2026, 11, 26)      # Thanksgiving
WEEKEND = date(2026, 10, 10)      # Saturday


def at(day: date, hour: int, minute: int = 0) -> datetime:
    return datetime(day.year, day.month, day.day, hour, minute, tzinfo=EASTERN)


# --- trading days ---------------------------------------------------------


@pytest.mark.parametrize(
    "day,expected",
    [(REGULAR, True), (HALF_DAY, True), (HOLIDAY, False), (WEEKEND, False)],
)
def test_trading_days(day, expected):
    assert is_trading_day(day) is expected


def test_a_holiday_has_no_session():
    assert session_for(HOLIDAY) is None


def test_next_session_skips_the_weekend():
    assert next_session(date(2026, 10, 9)) == date(2026, 10, 12)


def test_next_session_skips_a_holiday():
    assert next_session(date(2026, 11, 25)) == HALF_DAY


# --- open and closed ------------------------------------------------------


@pytest.mark.parametrize(
    "hour,minute,expected",
    [(9, 29, False), (9, 30, True), (12, 0, True), (15, 59, True), (16, 0, False), (17, 0, False)],
)
def test_regular_session_boundaries(hour, minute, expected):
    assert is_market_open(at(REGULAR, hour, minute)) is expected


def test_a_half_day_closes_early():
    assert is_market_open(at(HALF_DAY, 12, 59)) is True
    assert is_market_open(at(HALF_DAY, 13, 0)) is False
    # The same clock time is fine on a regular day.
    assert is_market_open(at(REGULAR, 13, 0)) is True


def test_nothing_is_open_on_a_holiday():
    assert is_market_open(at(HOLIDAY, 12, 0)) is False


def test_minutes_to_close_counts_down():
    assert minutes_to_close(at(REGULAR, 15, 30)) == pytest.approx(30.0)
    assert minutes_to_close(at(HALF_DAY, 12, 30)) == pytest.approx(30.0)
    assert minutes_to_close(at(REGULAR, 17, 0)) is None


# --- the entry cutoff -----------------------------------------------------


def test_parse_cutoff_accepts_hh_mm():
    assert parse_cutoff("15:30") == time(15, 30)
    assert parse_cutoff("9:05") == time(9, 5)


@pytest.mark.parametrize("bad", ["", "half past three", "15", "15:xx", "25:00:00"])
def test_a_bad_cutoff_fails_loudly_at_startup(bad):
    with pytest.raises(ValueError):
        parse_cutoff(bad)


def test_the_cutoff_applies_as_written_on_a_regular_day():
    cutoff = parse_cutoff("15:30")
    assert is_past_cutoff(cutoff, at(REGULAR, 15, 29)) is False
    assert is_past_cutoff(cutoff, at(REGULAR, 15, 30)) is True


def test_the_cutoff_shifts_earlier_on_a_half_day():
    """A 15:30 cutoff against a 13:00 close becomes 12:30, not unreachable."""
    cutoff = parse_cutoff("15:30")
    assert is_past_cutoff(cutoff, at(HALF_DAY, 12, 29)) is False
    assert is_past_cutoff(cutoff, at(HALF_DAY, 12, 30)) is True


def test_everything_is_past_the_cutoff_on_a_non_trading_day():
    assert is_past_cutoff(parse_cutoff("15:30"), at(HOLIDAY, 10, 0)) is True


# --- description ----------------------------------------------------------


def test_describe_names_the_state():
    assert "market open" in describe(at(REGULAR, 11, 0))
    assert "pre-market" in describe(at(REGULAR, 8, 0))
    assert "after hours" in describe(at(REGULAR, 17, 0))
    assert "market closed" in describe(at(HOLIDAY, 11, 0))


def test_describe_flags_an_early_close():
    assert "early close" in describe(at(HALF_DAY, 12, 0))
    assert "13:00" in describe(at(HALF_DAY, 12, 0))


def test_today_et_is_a_date():
    assert isinstance(today_et(), date)
