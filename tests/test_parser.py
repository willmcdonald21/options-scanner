"""Parser and validation tests -- no broker, no Discord, no network.

Three groups: the spec's canonical sample, the validation gates that keep a
misread alert from being traded, and malformed variants.
"""

from datetime import date, datetime
from pathlib import Path

import pytest

from options_scanner.models import (
    EntryAlert,
    InfoAlert,
    OptionKey,
    RejectedAlert,
    RejectReason,
)
from options_scanner.parser import (
    TRIM_TARGET_TOLERANCE,
    ParsedCard,
    detect_tier,
    parse_card,
    parse_simple_alert,
)
from options_scanner.text_import import parse_cards

FIXTURE = Path(__file__).parent / "fixtures" / "swift_chat_dump.txt"

TODAY = date(2026, 10, 6)

# The canonical sample from the spec, verbatim.
SPEC_ALERT = """BUY — SPX 7815P · 0DTE
Entered SPX Oct06 '26 7815 Put
Open Live Dashboard →
Entry
$0.475
Contracts
25
Cost
$1,188
Trim Targets
25%   $0.594
50%   $0.713
75%   $0.831
100%  $0.950
News backdrop
 Broad Market leaning up — 2 up / 1 down in the last 6h

Not financial advice"""


def _parse(text: str, today: date = TODAY):
    cards = parse_cards(text, reference_date=datetime(today.year, today.month, today.day))
    assert len(cards) == 1, f"expected exactly one card, got {len(cards)}"
    return parse_card(cards[0], today=today)


# --- the canonical sample -------------------------------------------------


def test_spec_sample_parses_into_a_tradeable_alert():
    alert = _parse(SPEC_ALERT)

    assert isinstance(alert, EntryAlert)
    assert alert.option == OptionKey("SPX", date(2026, 10, 6), 7815.0, "P")
    assert alert.entry_price == 0.475
    assert alert.advisor_contracts == 25
    assert alert.advisor_cost == 1188.0
    assert alert.is_zero_dte is True


def test_spec_sample_ladder_is_captured_in_order():
    alert = _parse(SPEC_ALERT)

    assert [t.pct for t in alert.trim_targets] == [0.25, 0.50, 0.75, 1.00]
    assert [t.price for t in alert.trim_targets] == [0.594, 0.713, 0.831, 0.950]


def test_spec_sample_builds_the_spxw_occ_symbol():
    """Index options trade under a different root than their ticker; SPX
    weeklies are SPXW, a genuinely different contract."""
    alert = _parse(SPEC_ALERT)

    assert alert.option.occ_symbol == "SPXW  261006P07815000"


def test_noise_lines_are_ignored():
    """'Open Live Dashboard', 'News backdrop' and the disclaimer must not be
    mistaken for fields -- News backdrop sits directly after the ladder."""
    alert = _parse(SPEC_ALERT)

    assert len(alert.trim_targets) == 4  # the backdrop line didn't leak in
    assert alert.advisor_cost == 1188.0


# --- expiry cross-check ---------------------------------------------------


def test_0dte_tag_on_a_future_expiry_is_rejected():
    result = _parse(SPEC_ALERT, today=date(2026, 10, 5))

    assert isinstance(result, RejectedAlert)
    assert result.reason is RejectReason.EXPIRY_TAG_MISMATCH
    assert "0DTE" in result.detail


def test_an_expiry_already_in_the_past_is_rejected():
    result = _parse(SPEC_ALERT, today=date(2026, 10, 7))

    assert isinstance(result, RejectedAlert)
    assert result.reason is RejectReason.EXPIRY_IN_PAST


def test_a_dated_tag_disagreeing_with_the_contract_line_is_rejected():
    card = SPEC_ALERT.replace("· 0DTE", "· Oct 9")
    result = _parse(card)

    assert isinstance(result, RejectedAlert)
    assert result.reason is RejectReason.EXPIRY_TAG_MISMATCH


def test_a_dated_tag_agreeing_with_the_contract_line_is_accepted():
    card = SPEC_ALERT.replace("Oct06 '26 7815 Put", "Oct09 '26 7815 Put").replace("· 0DTE", "· Oct 9")
    result = _parse(card)

    assert isinstance(result, EntryAlert)
    assert result.option.expiry == date(2026, 10, 9)
    assert result.is_zero_dte is False


def test_same_day_expiry_without_a_0dte_tag_is_still_treated_as_0dte():
    card = SPEC_ALERT.replace(" · 0DTE", "")
    result = _parse(card)

    assert isinstance(result, EntryAlert)
    assert result.is_zero_dte is True


# --- trim-target checksum -------------------------------------------------


def test_a_misread_entry_price_is_caught_by_the_ladder_checksum():
    """The ladder is not traded off; it exists to prove the entry parsed."""
    result = _parse(SPEC_ALERT.replace("$0.475", "$0.485"))

    assert isinstance(result, RejectedAlert)
    assert result.reason is RejectReason.TRIM_TARGET_MISMATCH
    assert "0.606" in result.detail  # shows the recomputed value


def test_rounding_inside_a_cent_is_tolerated():
    """0.475 x 1.25 = 0.59375, published as 0.594 -- a correct parse is
    always within a cent, never exact."""
    alert = _parse(SPEC_ALERT)

    for target in alert.trim_targets:
        assert abs(alert.entry_price * (1 + target.pct) - target.price) <= TRIM_TARGET_TOLERANCE


def _without_ladder(card: str, keep_heading: bool = False) -> str:
    return "\n".join(
        line
        for line in card.splitlines()
        if (keep_heading or not line.startswith("Trim Targets"))
        and "%   $" not in line and "%  $" not in line
    )


def test_a_card_with_no_ladder_is_accepted_and_the_bot_assigns_its_own():
    """The targets were only ever a checksum; the bot builds its own ladder
    from the fill. A missing block must not block the trade."""
    result = _parse(_without_ladder(SPEC_ALERT))

    assert isinstance(result, EntryAlert)
    assert result.trim_targets == ()
    assert result.entry_price == 0.475


def test_an_empty_trim_targets_heading_is_accepted():
    """2026-10-19 13:15 ET, QQQ 721C: the card ended at a bare "Trim Targets"
    heading with nothing under it, and the whole alert was rejected."""
    result = _parse(_without_ladder(SPEC_ALERT, keep_heading=True))

    assert isinstance(result, EntryAlert)
    assert result.trim_targets == ()


def test_a_ladder_that_disagrees_with_the_entry_is_still_rejected():
    """Missing targets are fine; WRONG ones mean a number was misread."""
    tampered = SPEC_ALERT.replace("25%   $0.594", "25%   $0.700")

    result = _parse(tampered)

    assert isinstance(result, RejectedAlert)
    assert result.reason is RejectReason.TRIM_TARGET_MISMATCH


# The real card from 2026-10-19, verbatim.
QQQ_721C_NO_TARGETS = """BUY — QQQ 721C · 0DTE
 Super Lotto Trade — RISKY
Size Very Less
Entered QQQ Oct19 '26 721 Call
Open Live Dashboard →
Entry
$0.585
Contracts
15
Cost
$877
Trim Targets"""


def test_the_real_card_that_was_rejected_now_parses():
    result = _parse(QQQ_721C_NO_TARGETS, today=date(2026, 10, 19))

    assert isinstance(result, EntryAlert)
    assert result.option == OptionKey("QQQ", date(2026, 10, 19), 721.0, "C")
    assert result.entry_price == 0.585
    assert result.tier == "super_lotto"
    assert result.trim_targets == ()


def test_one_bad_rung_rejects_the_whole_alert():
    result = _parse(SPEC_ALERT.replace("75%   $0.831", "75%   $0.900"))

    assert isinstance(result, RejectedAlert)
    assert result.reason is RejectReason.TRIM_TARGET_MISMATCH


# --- malformed variants ---------------------------------------------------


@pytest.mark.parametrize("missing", ["Entry", "Contracts", "Cost"])
def test_a_missing_required_field_is_rejected_by_name(missing):
    lines = SPEC_ALERT.splitlines()
    idx = lines.index(missing)
    del lines[idx : idx + 2]  # the label and its value
    result = _parse("\n".join(lines))

    assert isinstance(result, RejectedAlert)
    assert result.reason is RejectReason.MISSING_FIELD
    assert missing in result.detail


def test_a_missing_contract_line_is_rejected():
    result = _parse(SPEC_ALERT.replace("Entered SPX Oct06 '26 7815 Put\n", ""))

    assert isinstance(result, RejectedAlert)
    assert result.reason is RejectReason.UNPARSABLE_CONTRACT


def test_an_impossible_date_is_rejected_not_crashed():
    result = _parse(SPEC_ALERT.replace("Oct06 '26", "Feb30 '26"))

    assert isinstance(result, RejectedAlert)
    assert result.reason is RejectReason.UNPARSABLE_CONTRACT


def test_a_nonsense_month_is_rejected():
    result = _parse(SPEC_ALERT.replace("Oct06 '26", "Xyz06 '26"))

    assert isinstance(result, RejectedAlert)
    assert result.reason is RejectReason.UNPARSABLE_CONTRACT


def test_commas_in_prices_survive():
    """Index contracts run into four-figure costs."""
    card = SPEC_ALERT.replace("$1,188", "$11,880").replace("Contracts\n25", "Contracts\n250")
    alert = _parse(card)

    assert isinstance(alert, EntryAlert)
    assert alert.advisor_cost == 11880.0
    assert alert.advisor_contracts == 250


def test_extra_whitespace_and_a_different_emoji_still_parse():
    card = SPEC_ALERT.replace("BUY —", "\U0001f6a8  BUY   —").replace("25%   $0.594", "25%      $0.594")
    alert = _parse(card)

    assert isinstance(alert, EntryAlert)
    assert alert.trim_targets[0].price == 0.594


def test_a_call_parses_as_a_call():
    card = (
        SPEC_ALERT.replace("7815P", "7815C")
        .replace("7815 Put", "7815 Call")
    )
    alert = _parse(card)

    assert isinstance(alert, EntryAlert)
    assert alert.option.right == "C"


def test_a_zero_entry_price_is_rejected():
    card = SPEC_ALERT.replace("$0.475", "$0.000")
    result = _parse(card)

    assert isinstance(result, RejectedAlert)
    # A $0 entry makes every recomputed rung $0 too, so the checksum fires
    # first -- either rejection is correct, neither trades.
    assert result.reason in (RejectReason.NON_POSITIVE_PRICE, RejectReason.TRIM_TARGET_MISMATCH)


def test_an_unrecognized_title_is_rejected_with_the_title_quoted():
    card = ParsedCard(
        message_id=7,
        title="MARGIN CALL — SPX",
        description="",
        fields={},
        footer="",
        timestamp=datetime(2026, 10, 6),
    )
    result = parse_card(card, today=TODAY)

    assert isinstance(result, RejectedAlert)
    assert result.reason is RejectReason.UNRECOGNIZED_TITLE
    assert "MARGIN CALL" in result.detail


def test_ordinary_chatter_produces_no_cards_at_all():
    """main.py must report an empty result rather than ignore it, so this
    asserts the boundary rather than a parse."""
    assert parse_cards("anyone else get filled on that?") == []
    assert parse_cards("") == []


# --- non-entry card types are informational, never traded -----------------


@pytest.mark.parametrize(
    "title,expected_kind",
    [
        ("TRIM +25% — QQQ 740C · Oct 2", "advisor_trim"),
        ("SOLD ALL +130% — GOOGL 350C · Oct 2", "advisor_exit"),
        ("EXPIRED · WIN — SPY 772P · 0DTE", "advisor_expired"),
        ("NEW ALERT — SPY 770C · 0DTE", "new_alert"),
        ("+25% — TSLA 380C · 0DTE", "milestone"),
    ],
)
def test_non_entry_titles_classify_as_info(title, expected_kind):
    card = ParsedCard(
        message_id=1, title=title, description="", fields={}, footer="", timestamp=datetime(2026, 10, 6)
    )
    result = parse_card(card, today=TODAY)

    assert isinstance(result, InfoAlert)
    assert result.kind == expected_kind


# --- the 80-message regression corpus ------------------------------------


def _fixture_results():
    text = FIXTURE.read_text()
    cards = parse_cards(text, reference_date=datetime(2026, 9, 23))
    # The corpus spans 2026-09-21..23; validate each card against its own
    # expiry date so the expiry gate doesn't reject the whole historical set.
    out = []
    for card in cards:
        result = parse_card(card, today=date(2026, 9, 21))
        out.append(result)
    return cards, out


def test_every_card_in_the_corpus_is_classified():
    cards, results = _fixture_results()

    assert len(cards) == 80
    unrecognized = [
        r for r in results
        if isinstance(r, RejectedAlert) and r.reason is RejectReason.UNRECOGNIZED_TITLE
    ]
    assert unrecognized == [], [r.detail for r in unrecognized]


def test_the_corpus_entry_alerts_all_pass_the_ladder_checksum():
    """20 real BUY cards. Any checksum failure here means the parser is
    misreading a price on real traffic."""
    _, results = _fixture_results()
    entries = [r for r in results if isinstance(r, EntryAlert)]
    checksum_failures = [
        r for r in results
        if isinstance(r, RejectedAlert) and r.reason is RejectReason.TRIM_TARGET_MISMATCH
    ]

    assert checksum_failures == [], [r.detail for r in checksum_failures]
    assert len(entries) >= 1
    for alert in entries:
        assert alert.entry_price > 0
        assert len(alert.trim_targets) == 4


# --- the sizing tier -----------------------------------------------------

# The real AMD card from the corpus, verbatim. The tag is a description line
# directly under "Entered ...".
LOTTO_ALERT = """BUY — AMD 620C · 0DTE
Entered AMD Sep21 '26 620 Call
 Lotto Trade — RISKY
Open Live Dashboard →
Entry
$1.40
Contracts
25
Cost
$3,500
Trim Targets
25%   $1.750
50%   $2.100
75%   $2.450
100%  $2.800

Not financial advice"""

# The real SPX card from the corpus.
SUPER_LOTTO_ALERT = """BUY — SPX 7765P · 0DTE
Entered SPX Sep21 '26 7765 Put
 Super Lotto Trade — Super RISKY
Open Live Dashboard →
Entry
$0.725
Contracts
15
Cost
$1,088
Trim Targets
25%   $0.906
50%   $1.088
75%   $1.269
100%  $1.450

Not financial advice"""

SEPT_21 = date(2026, 9, 21)


def test_an_untagged_card_is_a_normal_unit():
    assert _parse(SPEC_ALERT).tier == "normal"


def test_the_real_lotto_card_is_detected():
    alert = _parse(LOTTO_ALERT, today=SEPT_21)

    assert isinstance(alert, EntryAlert)
    assert alert.tier == "lotto"


def test_the_real_super_lotto_card_is_detected():
    """'super lotto' contains 'lotto', so the order of the two checks is the
    whole test: read as a plain lotto this would be sized at half a unit
    instead of a quarter -- twice the intended risk on the riskiest trade
    there is."""
    alert = _parse(SUPER_LOTTO_ALERT, today=SEPT_21)

    assert isinstance(alert, EntryAlert)
    assert alert.tier == "super_lotto"


def test_the_tag_is_found_in_a_notes_field_too():
    """Some cards carry it under "Notes:" rather than as a description line."""
    card = ParsedCard(
        message_id=1,
        title="BUY — AMD 620C · 0DTE",
        description="Entered AMD Sep21 '26 620 Call",
        fields={"Notes": " Lotto Trade — RISKY"},
        footer="",
        timestamp=datetime(2026, 9, 21),
    )

    assert detect_tier(card) == "lotto"


@pytest.mark.parametrize(
    "text,expected",
    [
        (" Lotto Trade — RISKY", "lotto"),
        (" Super Lotto Trade — Super RISKY", "super_lotto"),
        ("super lotto", "super_lotto"),
        ("SUPER  LOTTO", "super_lotto"),  # whitespace and case are not signal
        ("lottery ticket", "normal"),     # not a word match
        ("", "normal"),
    ],
)
def test_tier_detection_cases(text, expected):
    card = ParsedCard(
        message_id=1, title="BUY — AMD 620C", description=text, fields={},
        footer="", timestamp=datetime(2026, 9, 21),
    )

    assert detect_tier(card) == expected


def test_the_corpus_tiers_are_what_the_advisor_wrote():
    """Of the seven BUY cards in the 80-message corpus: two lottos, one super
    lotto, four normal."""
    _, results = _fixture_results()
    entries = [r for r in results if isinstance(r, EntryAlert) and not r.average_down]
    tiers = sorted(a.tier for a in entries)

    assert tiers.count("lotto") == 2
    assert tiers.count("super_lotto") == 1
    assert tiers.count("normal") == 4


def test_a_tier_said_only_in_chat_is_not_detectable():
    """The advisor also posts "Super lotto size less" as a bare chat message,
    outside any card. The bot reads cards, so that trade sizes as normal --
    a known gap, asserted here so it cannot be mistaken for a bug later.

    It is also the direction that sizes UP, which is why the README says to
    watch the channel on lotto days.
    """
    assert parse_cards("Super lotto size less") == []



# --- the one-line format: `QQQ 718P 1.99` --------------------------------

OCT_15 = date(2026, 10, 15)


def _simple(text, today=OCT_15):
    return parse_simple_alert(text, message_id=42, today=today)


def test_a_one_line_alert_means_todays_contract():
    """Checked against the live chain on 2026-10-15: QQQ 718P at 1.99 matched
    only the same-day put (tomorrow's was 3.80)."""
    alert = _simple("QQQ 718P 1.99")

    assert isinstance(alert, EntryAlert)
    assert alert.option == OptionKey("QQQ", OCT_15, 718.0, "P")
    assert alert.entry_price == 1.99
    assert alert.is_zero_dte
    assert alert.source == "simple"
    assert alert.tier == "normal"
    assert alert.trim_targets == ()


@pytest.mark.parametrize(
    "text",
    ["qqq 718p 1.99", "$QQQ 718P $1.99", "QQQ 718P @ 1.99", "  QQQ   718P   1.99  ", "QQQ 718 P 1.99"],
)
def test_harmless_variations_read_the_same(text):
    assert _simple(text).option == OptionKey("QQQ", OCT_15, 718.0, "P")


def test_a_half_strike_and_a_bare_decimal_price():
    alert = _simple("SPY 367.5C .85")

    assert alert.option.strike == 367.5
    assert alert.option.right == "C"
    assert alert.entry_price == 0.85


def test_spx_resolves_to_the_weekly_trading_class():
    alert = _simple("SPX 5800C 2.40")

    assert alert.option.occ_symbol.startswith("SPXW")


@pytest.mark.parametrize(
    "text,expected",
    [
        ("QQQ 10/16 718P 1.99", date(2026, 10, 16)),
        ("QQQ 1/15/27 718P 1.99", date(2027, 1, 15)),
        ("QQQ 1/15/2027 718P 1.99", date(2027, 1, 15)),
    ],
)
def test_a_written_date_overrides_today(text, expected):
    alert = _simple(text)

    assert alert.option.expiry == expected
    assert not alert.is_zero_dte


def test_a_past_date_is_rejected_not_rolled_to_next_year():
    """Rolling 10/14 forward to 2027 would buy a contract nobody asked for."""
    result = _simple("QQQ 10/14 718P 1.99")

    assert isinstance(result, RejectedAlert)
    assert result.reason is RejectReason.EXPIRY_IN_PAST


def test_an_impossible_date_is_rejected():
    result = _simple("QQQ 13/40 718P 1.99")

    assert isinstance(result, RejectedAlert)
    assert result.reason is RejectReason.UNPARSABLE_CONTRACT


def test_a_zero_price_is_rejected():
    result = _simple("QQQ 718P 0")

    assert isinstance(result, RejectedAlert)
    assert result.reason is RejectReason.NON_POSITIVE_PRICE


@pytest.mark.parametrize(
    "text",
    [
        "QQQ 718P 1.99 lotto",       # anything extra: not this format
        "QQQ 718P",                  # no price
        "718P 1.99",                 # no ticker
        "QQQ 718X 1.99",             # not a call or a put
        "QQQ 718P\n1.99",            # split across lines
        "BUY — QQQ 730C · 0DTE",     # a card's title
        "anyone filled on QQQ?",
        "",
    ],
)
def test_anything_else_is_not_a_one_line_alert(text):
    """None, not a rejection: the paste then goes to the card parser, and the
    bot never guesses at a partial match."""
    assert _simple(text) is None


def test_a_full_card_is_never_taken_for_a_one_line_alert():
    assert _simple(SPEC_ALERT) is None



# --- AVERAGING DOWN cards are buys ---------------------------------------

# The real SPY card from the corpus, verbatim.
AVG_DOWN_ALERT = """AVERAGING DOWN — SPY 767P · 0DTE
Averaging down on SPY Sep21 '26 767 Put
Added 40 @ $0.175
Open Live Dashboard →

Average $0.255 → $0.215 · 40 → 80 contracts
New Avg
$0.215
Total Contracts
80
Cost
$1,720
Trim Targets (new avg)
25%   $0.269
50%   $0.323
75%   $0.376
100%  $0.430

Not financial advice"""


def test_an_averaging_down_card_is_a_buy_at_the_added_price():
    alert = _parse(AVG_DOWN_ALERT, today=SEPT_21)

    assert isinstance(alert, EntryAlert)
    assert alert.average_down
    assert alert.option == OptionKey("SPY", SEPT_21, 767.0, "P")
    assert alert.entry_price == 0.175       # what is being paid now
    assert alert.advisor_contracts == 40    # what the advisor added
    assert alert.is_zero_dte


def test_the_checksum_runs_against_the_new_average_not_the_add_price():
    """The card's targets are measured from New Avg ($0.215): $0.269 is
    0.215 x 1.25. Checked against the $0.175 add price they would all fail."""
    alert = _parse(AVG_DOWN_ALERT, today=SEPT_21)

    assert isinstance(alert, EntryAlert)
    assert alert.trim_targets[0].price == 0.269


def test_a_misread_new_average_is_caught_by_the_checksum():
    tampered = AVG_DOWN_ALERT.replace("New Avg\n$0.215", "New Avg\n$0.315")

    result = _parse(tampered, today=SEPT_21)

    assert isinstance(result, RejectedAlert)
    assert result.reason is RejectReason.TRIM_TARGET_MISMATCH


@pytest.mark.parametrize(
    "remove,reason",
    [
        ("Added 40 @ $0.175\n", RejectReason.MISSING_FIELD),
        ("New Avg\n$0.215\n", RejectReason.MISSING_FIELD),
    ],
)
def test_an_averaging_down_card_missing_its_numbers_is_rejected(remove, reason):
    result = _parse(AVG_DOWN_ALERT.replace(remove, ""), today=SEPT_21)

    assert isinstance(result, RejectedAlert)
    assert result.reason is reason


def test_every_averaging_down_card_in_the_corpus_parses_on_its_own_day():
    """Seven real cards. Each is checked on its own expiry day, since most are
    0DTE and would otherwise fail the date check."""
    text = FIXTURE.read_text()
    cards = [c for c in parse_cards(text, reference_date=datetime(2026, 9, 21))
             if c.title.startswith("AVERAGING DOWN")]
    assert len(cards) == 7

    for card in cards:
        from options_scanner.parser import _parse_contract_line

        day = _parse_contract_line(card.description).expiry if "0DTE" in card.title else date(2026, 9, 21)
        result = parse_card(card, today=day)
        assert isinstance(result, EntryAlert), (card.title, getattr(result, "detail", ""))
        assert result.average_down


# --- one-line average-downs ----------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "avg QQQ 718P 1.50",
        "AVG QQQ 718P 1.50",
        "avg down QQQ 718P 1.50",
        "average down QQQ 718P 1.50",
        "averaging down QQQ 718P 1.50",
        "QQQ 718P 1.50 avg",
        "QQQ 718P 1.50 avg down",
        "QQQ 718P 1.50 average down",
    ],
)
def test_an_avg_keyword_marks_a_one_line_average_down(text):
    alert = _simple(text)

    assert isinstance(alert, EntryAlert)
    assert alert.average_down
    assert alert.option == OptionKey("QQQ", OCT_15, 718.0, "P")
    assert alert.entry_price == 1.50


def test_a_plain_one_line_alert_is_not_an_average_down():
    assert _simple("QQQ 718P 1.50").average_down is False


def test_an_average_down_can_name_a_date():
    alert = _simple("avg QQQ 10/16 718P 1.50")

    assert alert.average_down
    assert alert.option.expiry == date(2026, 10, 16)


@pytest.mark.parametrize("text", ["avg QQQ 718P", "avg", "QQQ 718P 1.50 avg lotto", "avg avg QQQ 718P 1.50"])
def test_a_malformed_average_down_is_not_guessed_at(text):
    assert _simple(text) is None



def test_an_averaging_down_card_without_a_ladder_needs_no_new_avg():
    stripped = "\n".join(
        line for line in AVG_DOWN_ALERT.splitlines()
        if not line.startswith(("Trim Targets", "New Avg", "$0.215")) and "%   $" not in line
    )

    result = _parse(stripped, today=SEPT_21)

    assert isinstance(result, EntryAlert)
    assert result.average_down
    assert result.entry_price == 0.175
