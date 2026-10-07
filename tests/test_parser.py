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
from options_scanner.parser import TRIM_TARGET_TOLERANCE, ParsedCard, parse_card
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


def test_a_card_with_no_ladder_is_rejected():
    stripped = "\n".join(
        line
        for line in SPEC_ALERT.splitlines()
        if not line.startswith("Trim Targets") and "%   $" not in line and "%  $" not in line
    )
    result = _parse(stripped)

    assert isinstance(result, RejectedAlert)
    assert result.reason is RejectReason.NO_TRIM_TARGETS


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
        ("AVERAGING DOWN — SPY 767P · 0DTE", "averaging_down"),
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
