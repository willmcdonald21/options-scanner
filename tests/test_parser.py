from datetime import date, datetime
from pathlib import Path

from options_scanner.models import (
    BuyEvent,
    ExpiredEvent,
    InfoEvent,
    OptionKey,
    SoldAllEvent,
    TrimEvent,
    UnderlyingKey,
    UnknownEvent,
)
from options_scanner.parser import parse_embed
from options_scanner.text_import import parse_chat_export

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
        "BuyEvent": 20,
        "TrimEvent": 2,
        "SoldAllEvent": 15,
        "ExpiredEvent": 4,
        "InfoEvent": 39,  # milestones + 7 AVERAGING DOWN + 1 NEW ALERT, all disregarded
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


def test_new_alert_is_disregarded_not_traded():
    events = _events()
    new_alerts = [e for e in events if isinstance(e, InfoEvent) and e.kind == "new_alert"]
    assert len(new_alerts) == 1
    assert "NEW ALERT" in new_alerts[0].raw_title
    # and it must not also show up as a BuyEvent for that contract
    assert not any(isinstance(e, BuyEvent) and e.option.ticker == "QQQ" and e.option.expiry == date(2026, 9, 25) for e in events)


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
    assert kinds == {"milestone", "averaging_down", "new_alert"}


# --- the trim ladder on a BUY card ---------------------------------------


def test_buy_events_carry_the_published_trim_ladder():
    buys = [e for e in _events() if isinstance(e, BuyEvent)]

    assert len(buys) == 20
    for buy in buys:
        assert len(buy.trim_targets) == 4, f"{buy.option} lost its ladder"
        assert [t.pct for t in buy.trim_targets] == [0.25, 0.50, 0.75, 1.00]
        # Rungs ascend and all sit above the entry.
        prices = [t.price for t in buy.trim_targets]
        assert prices == sorted(prices)
        assert prices[0] > buy.entry_price


def test_trim_ladder_prices_match_the_card_exactly():
    buy = next(e for e in _events() if isinstance(e, BuyEvent) and e.option.ticker == "SPY")

    assert buy.entry_price == 0.885
    assert [t.price for t in buy.trim_targets] == [1.106, 1.328, 1.549, 1.770]


def test_averaging_down_card_ladder_is_read_from_the_new_avg_field():
    """After an AVERAGING DOWN the field is titled "Trim Targets (new avg)"."""
    from options_scanner.parser import _parse_trim_targets

    targets = _parse_trim_targets({"Trim Targets (new avg)": "25%   $0.269\n50%   $0.323\n75%   $0.376\n100%  $0.430"})

    assert [t.price for t in targets] == [0.269, 0.323, 0.376, 0.430]


def test_a_buy_without_a_ladder_field_parses_with_an_empty_one():
    """exit_plan substitutes a computed ladder; the parser just reports
    that the card didn't carry one."""
    from options_scanner.parser import _parse_trim_targets

    assert _parse_trim_targets({"Entry": "$1.00"}) == ()


# --- cards pasted without the "SWIFT TRADES" banner ----------------------

_ANALYST_TRIM = """Analyst - SWIFT 
 TRIM +25% — QQQ 740C · Oct 2
Sold 1 of 10 @ $6.413 · 9 still running.

 Stop moved to break-even. Trim filled at $6.413 (+25%, trigger was +25%). Remaining 9 now stopped at $5.29 — this position can no longer lose money.
Open Live Dashboard →
Entry
$5.13
Exit
$6.413
Locked In
+$128.25

Not financial advice"""

_ANALYST_SOLD_ALL = """Analyst - SWIFT 
 SOLD ALL +130% — GOOGL 350C · Oct 2
Fully out of GOOGL Oct02 '26 350 Call.
Open Live Dashboard →
Entry → avg exit
$1.596 → $2.687
Realized
+68.34%
Best fill
$3.592
Peak
+130.2% ($3.675)

Not financial advice"""


def test_analyst_headed_trim_card_is_not_dropped():
    """These carry no "SWIFT TRADES · LIVE DESK" banner at all -- splitting
    on that marker alone discarded every exit message silently."""
    embeds = parse_chat_export(_ANALYST_TRIM, reference_date=datetime(2026, 10, 2))
    assert len(embeds) == 1

    event = parse_embed(embeds[0])
    assert isinstance(event, TrimEvent)
    assert event.underlying == UnderlyingKey("QQQ", 740.0, "C")
    assert event.tier_pct == 0.25
    assert (event.sold_this_event, event.channel_remaining_after) == (1, 9)


def test_analyst_headed_sold_all_card_is_not_dropped():
    embeds = parse_chat_export(_ANALYST_SOLD_ALL, reference_date=datetime(2026, 10, 2))
    assert len(embeds) == 1

    event = parse_embed(embeds[0])
    assert isinstance(event, SoldAllEvent)
    assert event.underlying == UnderlyingKey("GOOGL", 350.0, "C")
    assert event.avg_exit_price == 2.687


def test_a_card_pasted_with_no_header_at_all_still_parses():
    bare = "\n".join(_ANALYST_SOLD_ALL.splitlines()[1:])
    embeds = parse_chat_export(bare, reference_date=datetime(2026, 10, 2))

    assert len(embeds) == 1
    assert isinstance(parse_embed(embeds[0]), SoldAllEvent)


def test_unrelated_chatter_yields_no_cards():
    """main.py alerts on an empty result, so ordinary conversation must
    not look like a card."""
    assert parse_chat_export("hey is anyone else seeing this fill?") == []
    assert parse_chat_export("") == []


def test_the_live_buy_card_parses_with_its_ladder():
    card = """SWIFT TRADES · LIVE DESK
 BUY — SPY 764C · Sep 30
Entered SPY Sep30 '26 764 Call
Open Live Dashboard →
Entry
$1.905
Contracts
25
Cost
$4,763
Trim Targets
25%   $2.381
50%   $2.858
75%   $3.334
100%  $3.810
News backdrop
 Broad Market leaning down — 2 up / 4 down in the last 6h

Real trade · data via IBKR · Not financial advice•9/29/26, 1:25 PM"""

    event = parse_embed(parse_chat_export(card, reference_date=datetime(2026, 9, 29))[0])

    assert isinstance(event, BuyEvent)
    assert event.option == OptionKey("SPY", date(2026, 9, 30), 764.0, "C")
    assert (event.entry_price, event.contracts, event.cost) == (1.905, 25, 4763.0)
    # The News backdrop field sits directly after the ladder and must not
    # be swallowed into it.
    assert [t.price for t in event.trim_targets] == [2.381, 2.858, 3.334, 3.810]
