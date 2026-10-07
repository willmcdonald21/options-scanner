"""Card splitting: turning one pasted Discord message into alert cards.

This layer caused real silent drops before, so the boundary cases get direct
tests rather than being covered only through the parser.
"""

from datetime import datetime
from pathlib import Path

from options_scanner.text_import import parse_cards

FIXTURE = Path(__file__).parent / "fixtures" / "swift_chat_dump.txt"
REF = datetime(2026, 10, 6)

ENTRY_WITH_BANNER = """SWIFT TRADES · LIVE DESK
 BUY — SPX 7815P · 0DTE
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

Real trade · data via IBKR · Not financial advice•10/6/26, 1:25 PM"""

EXIT_WITH_OTHER_BANNER = """Analyst - SWIFT
 SOLD ALL +130% — GOOGL 350C · Oct 2
Fully out of GOOGL Oct02 '26 350 Call.
Open Live Dashboard →
Entry → avg exit
$1.596 → $2.687
Realized
+68.34%

Not financial advice"""


def test_a_card_with_the_live_desk_banner_is_found():
    cards = parse_cards(ENTRY_WITH_BANNER, reference_date=REF)

    assert len(cards) == 1
    assert "BUY" in cards[0].title
    assert cards[0].fields["Entry"] == "$0.475"


def test_an_exit_card_with_a_different_banner_is_found():
    """Exit cards copied out of the client are headed 'Analyst - SWIFT' and
    carry no LIVE DESK banner -- splitting on that banner alone dropped every
    one of them without a trace."""
    cards = parse_cards(EXIT_WITH_OTHER_BANNER, reference_date=REF)

    assert len(cards) == 1
    assert "SOLD ALL" in cards[0].title


def test_a_card_pasted_with_no_banner_at_all_is_still_found():
    bare = "\n".join(ENTRY_WITH_BANNER.splitlines()[1:])
    cards = parse_cards(bare, reference_date=REF)

    assert len(cards) == 1
    assert "BUY" in cards[0].title


def test_several_cards_in_one_paste_are_split_apart():
    cards = parse_cards(ENTRY_WITH_BANNER + "\n" + EXIT_WITH_OTHER_BANNER, reference_date=REF)

    assert len(cards) == 2
    assert "BUY" in cards[0].title
    assert "SOLD ALL" in cards[1].title


def test_each_card_in_a_paste_gets_its_own_index():
    cards = parse_cards(ENTRY_WITH_BANNER + "\n" + EXIT_WITH_OTHER_BANNER, reference_date=REF)

    assert [c.message_id for c in cards] == [0, 1]


def test_ordinary_chatter_yields_nothing():
    assert parse_cards("anyone else get filled on that?", reference_date=REF) == []
    assert parse_cards("", reference_date=REF) == []
    assert parse_cards("   \n\n  ", reference_date=REF) == []


def test_a_multiline_trim_targets_block_is_captured_whole():
    cards = parse_cards(ENTRY_WITH_BANNER, reference_date=REF)

    raw = cards[0].fields["Trim Targets"]
    assert raw.splitlines() == ["25%   $0.594", "50%   $0.713", "75%   $0.831", "100%  $0.950"]


def test_a_field_following_the_ladder_does_not_get_swallowed_into_it():
    """'News backdrop' sits directly after the last rung."""
    with_backdrop = ENTRY_WITH_BANNER.replace(
        "100%  $0.950\n",
        "100%  $0.950\nNews backdrop\n Broad Market leaning up — 2 up / 1 down in the last 6h\n",
    )
    cards = parse_cards(with_backdrop, reference_date=REF)

    assert len(cards[0].fields["Trim Targets"].splitlines()) == 4
    assert "Broad Market" not in cards[0].fields["Trim Targets"]


def test_the_contract_line_lands_in_the_description():
    cards = parse_cards(ENTRY_WITH_BANNER, reference_date=REF)

    assert "Entered SPX Oct06 '26 7815 Put" in cards[0].description


def test_the_explicit_timestamp_in_the_footer_is_read():
    cards = parse_cards(ENTRY_WITH_BANNER, reference_date=REF)

    assert cards[0].timestamp == datetime(2026, 10, 6, 13, 25)


def test_a_card_with_no_timestamp_falls_back_to_the_reference_date():
    cards = parse_cards(EXIT_WITH_OTHER_BANNER, reference_date=REF)

    assert cards[0].timestamp == REF


def test_the_historical_corpus_still_splits_into_80_cards():
    """Regression guard: the splitting strategies must not change how real
    traffic is segmented."""
    cards = parse_cards(FIXTURE.read_text(), reference_date=datetime(2026, 9, 23))

    assert len(cards) == 80
    assert all(c.title for c in cards)
