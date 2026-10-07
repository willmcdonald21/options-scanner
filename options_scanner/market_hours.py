"""Market session awareness, in America/New_York.

Every time decision in the bot goes through here so that "is the market
open", "what is today's date for the 0DTE check" and "how long until the
close" all agree with each other and with the exchange calendar, including
holidays and early closes.

Half days are the reason this uses a real calendar rather than a hardcoded
09:30-16:00 window: on 2026-11-27 the close is 13:00, and an entry cutoff
that did not know it would happily take a position minutes before the bell.

Note on index options: SPX/SPXW trade until 16:15 ET, fifteen minutes past
the equity close this calendar reports. The bot deliberately uses the equity
close as its reference anyway -- the conservative end of the two -- so no
cutoff or near-close warning is ever late.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from functools import lru_cache
from zoneinfo import ZoneInfo

import exchange_calendars as xcals
import pandas as pd

EASTERN = ZoneInfo("America/New_York")
_CALENDAR_NAME = "XNYS"

# The equity session. Used only as a fallback if the calendar cannot answer,
# which should not happen for any date it has data for.
REGULAR_OPEN = time(9, 30)
REGULAR_CLOSE = time(16, 0)


@lru_cache(maxsize=1)
def _calendar():
    return xcals.get_calendar(_CALENDAR_NAME)


def now_et() -> datetime:
    """Current time in Eastern. The single source of "now" for the bot, so a
    test can monkeypatch one function instead of chasing datetime.now()."""
    return datetime.now(EASTERN)


def today_et() -> date:
    """Today's date in Eastern -- what the parser's 0DTE check compares
    against. Using the local date instead would misjudge any alert pasted
    after 21:00 Pacific or before 09:00 in Europe."""
    return now_et().date()


@dataclass(frozen=True)
class Session:
    """One trading session, with its real open and close in Eastern."""

    day: date
    open_at: datetime
    close_at: datetime

    @property
    def is_early_close(self) -> bool:
        return self.close_at.timetz() < REGULAR_CLOSE.replace(tzinfo=self.close_at.tzinfo)

    def contains(self, moment: datetime) -> bool:
        return self.open_at <= moment < self.close_at


def session_for(day: date) -> Session | None:
    """The session on `day`, or None if the market is closed that day."""
    calendar = _calendar()
    stamp = pd.Timestamp(day)
    try:
        if not calendar.is_session(stamp):
            return None
        open_at = calendar.session_open(stamp).tz_convert(EASTERN).to_pydatetime()
        close_at = calendar.session_close(stamp).tz_convert(EASTERN).to_pydatetime()
    except Exception:
        # Outside the calendar's range. Fall back to the regular window on
        # weekdays rather than refusing to answer, and let the caller's own
        # guards do the rest.
        if day.weekday() >= 5:
            return None
        open_at = datetime.combine(day, REGULAR_OPEN, tzinfo=EASTERN)
        close_at = datetime.combine(day, REGULAR_CLOSE, tzinfo=EASTERN)
    return Session(day=day, open_at=open_at, close_at=close_at)


def is_trading_day(day: date | None = None) -> bool:
    return session_for(day or today_et()) is not None


def is_market_open(moment: datetime | None = None) -> bool:
    """True only during regular hours. Pre- and post-market are not tradeable
    for this strategy: 0DTE option liquidity outside the session is bad
    enough that a marketable limit is not reliably fillable, which would
    leave a synthetic stop unable to do its job."""
    moment = moment or now_et()
    session = session_for(moment.astimezone(EASTERN).date())
    return session is not None and session.contains(moment.astimezone(EASTERN))


def minutes_to_close(moment: datetime | None = None) -> float | None:
    """Minutes until today's close, or None when the market is not open."""
    moment = (moment or now_et()).astimezone(EASTERN)
    session = session_for(moment.date())
    if session is None or not session.contains(moment):
        return None
    return (session.close_at - moment).total_seconds() / 60.0


def session_close(day: date | None = None) -> datetime | None:
    session = session_for(day or today_et())
    return session.close_at if session else None


def parse_cutoff(value: str) -> time:
    """Parse an "HH:MM" config value into a time. Raises on anything else, so
    a typo in config.yaml fails at startup rather than at 15:59."""
    try:
        hour, minute = (int(part) for part in value.split(":"))
        return time(hour, minute)
    except Exception as exc:
        raise ValueError(f"expected an 'HH:MM' time, got {value!r}") from exc


def is_past_cutoff(cutoff: time, moment: datetime | None = None) -> bool:
    """True once `cutoff` has passed in Eastern, shifted earlier by the same
    amount on a half day so a 15:30 cutoff on a 13:00 close becomes 12:30
    rather than being unreachable."""
    moment = (moment or now_et()).astimezone(EASTERN)
    session = session_for(moment.date())
    if session is None:
        return True

    cutoff_at = datetime.combine(moment.date(), cutoff, tzinfo=EASTERN)
    regular_close = datetime.combine(moment.date(), REGULAR_CLOSE, tzinfo=EASTERN)
    if session.close_at < regular_close:
        cutoff_at -= regular_close - session.close_at

    return moment >= cutoff_at


def next_session(after: date | None = None) -> date | None:
    """The next trading day strictly after `after`. Used by the end-of-day
    summary to say when the bot will next be live."""
    day = (after or today_et()) + timedelta(days=1)
    for _ in range(10):
        if is_trading_day(day):
            return day
        day += timedelta(days=1)
    return None


def describe(moment: datetime | None = None) -> str:
    """One-line session status for a startup message or `!status`."""
    moment = (moment or now_et()).astimezone(EASTERN)
    session = session_for(moment.date())
    if session is None:
        nxt = next_session(moment.date())
        return f"market closed ({moment:%a %d %b}); next session {nxt:%a %d %b}" if nxt else "market closed"
    if session.contains(moment):
        left = minutes_to_close(moment) or 0
        early = " (early close)" if session.is_early_close else ""
        return f"market open, {left:.0f} min to {session.close_at:%H:%M} ET close{early}"
    when = "pre-market" if moment < session.open_at else "after hours"
    return f"{when}; session {session.open_at:%H:%M}-{session.close_at:%H:%M} ET"
