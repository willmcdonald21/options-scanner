from datetime import date, datetime
from pathlib import Path

from bot.models import (
    BuyEvent,
    ExpiredEvent,
    InfoEvent,
    OptionKey,
    SoldAllEvent,
    TrimEvent,
    UnderlyingKey,
    UnknownEvent,
)
from bot.parser import parse_embed
from bot.text_import import parse_chat_export

FIXTURE = Path(__file__).parent / "fixtures" / "swift_chat_dump.txt"


def _events():
    text = FIXTURE.read_text()
    embeds = parse_chat_export(text, reference_date=datetime(2026, 9, 23))
    return [parse_embed(e) for e in embeds]


def test_every_message_in_real_dump_is_classified():
    events = _events()
    assert len(events) == 80
    unknown = [e for e in events if isinstance(e, UnknownEvent)]
    assert unknown == [], f"unrecognized message shapes: {[e.reason for e in unknown]}"


def test_type_breakdown_matches_manual_count():
    events = _events()
    counts: dict[str, int] = {}
    for e in events:
        counts[type(e).__name__] = counts.get(type(e).__name__, 0) + 1
    assert counts == {
        "BuyEvent": 21,  # 20 BUY + 1 NEW ALERT, now sized off the per-trade cap
        "TrimEvent": 2,
        "SoldAllEvent": 15,
        "ExpiredEvent": 4,
        "InfoEvent": 38,  # milestones + the 7 AVERAGING DOWN messages, both disregarded
    }


def test_buy_event_fields():
    events = _events()
    amd = next(e for e in events if isinstance(e, BuyEvent) and e.option.ticker == "AMD")
    assert amd.option == OptionKey("AMD", date(2026, 9, 21), 620.0, "C")
    assert amd.entry_price == 1.40
    assert amd.contracts == 25
    assert amd.cost == 3500.0
    assert amd.is_lotto is True

    googl = next(e for e in events if isinstance(e, BuyEvent) and e.option.ticker == "GOOGL")
    assert googl.option.strike == 367.5
    assert googl.is_lotto is False


def test_new_alert_is_sized_off_the_cap_and_treated_as_a_buy():
    events = _events()
    ev = next(e for e in events if isinstance(e, BuyEvent) and e.option.ticker == "QQQ" and e.option.expiry == date(2026, 9, 25))
    assert ev.entry_price == 3.755
    assert ev.contracts == 2  # floor(1000 / (3.755 * 100))
    assert ev.cost == ev.contracts * ev.entry_price * 100
    assert ev.is_lotto is False


def test_averaging_down_is_disregarded():
    events = _events()
    avg_downs = [e for e in events if isinstance(e, InfoEvent) and e.kind == "averaging_down"]
    assert len(avg_downs) == 7
    assert all("AVERAGING DOWN" in e.raw_title for e in avg_downs)


def test_trim_event_handles_bundled_and_single_tier_forms():
    events = _events()
    trims = [e for e in events if isinstance(e, TrimEvent)]
    assert len(trims) == 2

    bundled = next(t for t in trims if t.underlying.ticker == "QQQ" and t.underlying.strike == 740.0 and t.underlying.right == "C")
    assert bundled.tier_pct == 0.75
    assert bundled.sold_this_event == 12
    assert bundled.channel_total_before == 20
    assert bundled.channel_remaining_after == 8
    assert bundled.avg_exit_price == 1.328

    single = next(t for t in trims if t.underlying.ticker == "NVDA")
    assert single.tier_pct == 0.25
    assert single.sold_this_event == 3
    assert single.channel_total_before == 15
    assert single.channel_remaining_after == 12
    assert single.avg_exit_price == 1.069


def test_sold_all_event_handles_negative_pct():
    events = _events()
    ev = next(e for e in events if isinstance(e, SoldAllEvent) and e.underlying.ticker == "QQQ" and e.underlying.strike == 746.0)
    assert ev.realized_pct == -0.75
    assert ev.avg_exit_price == 0.10


def test_expired_event_win_and_loss():
    events = _events()
    win = next(e for e in events if isinstance(e, ExpiredEvent) and e.underlying.ticker == "SPY" and e.underlying.strike == 767.0)
    loss = next(e for e in events if isinstance(e, ExpiredEvent) and e.underlying.ticker == "SPX")
    assert win.won is True
    assert loss.won is False


def test_bare_milestones_are_informational_only():
    events = _events()
    kinds = {e.kind for e in events if isinstance(e, InfoEvent)}
    assert kinds == {"milestone", "averaging_down"}
