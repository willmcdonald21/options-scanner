"""The alert path end to end, in DRY_RUN, with no Discord and no real broker.

This is the phase 3 gate: paste an alert, get the right reaction, and get an
embed in the updates channel saying exactly what would have happened.
"""

from __future__ import annotations

from datetime import date, datetime

from conftest import SPEC_ALERT, SPX_OCC, TRADING_DAY, FakeBroker, find, make_settings, paste

from options_scanner.market_hours import EASTERN
from options_scanner.notifier import (
    REACTION_PLACED,
    REACTION_RECEIVED,
    REACTION_REJECTED,
    REACTION_SKIPPED,
    Level,
)
from options_scanner.pipeline import AlertPipeline
from options_scanner.risk import RiskGate
from options_scanner.storage import (
    STATUS_ACCEPTED,
    STATUS_DUPLICATE,
    STATUS_INFO,
    STATUS_REJECTED,
    STATUS_SKIPPED,
)


# --- the happy path -------------------------------------------------------


async def test_a_valid_alert_is_accepted_and_reported(pipeline, storage):
    result = await paste(pipeline)

    assert result.final_reaction == REACTION_PLACED
    assert len(result.accepted) == 1
    assert find(result, "PARSED")
    assert find(result, "WOULD BUY")


async def test_the_accepted_alert_is_sized_from_our_config_not_the_advisor(pipeline):
    result = await paste(pipeline)

    note = find(result, "PARSED")
    values = {name: value for name, value, _ in note.fields}
    assert values["Advisor size"] == "25 contracts"
    # 3% of the fake broker's $100,000 is $3,000, capped by max_usd_per_trade
    # at $1,000, which buys 21 at $47.50 -- nothing to do with the advisor's 25.
    assert values["Our size"].startswith("21 contracts")


async def test_the_dry_run_order_is_a_limit_with_a_walk_cap(pipeline):
    result = await paste(pipeline)

    note = find(result, "WOULD BUY")
    values = {name: value for name, value, _ in note.fields}
    assert values["Order"] == "BUY 21 @ limit $0.475"
    assert values["Walk to"] == "$0.523 max"  # +10% of 0.475
    assert values["Give up after"] == "30s"
    assert "nothing was sent" in note.description.lower()


async def test_the_reported_ladder_uses_our_rungs_not_the_cards(pipeline):
    result = await paste(pipeline)

    note = find(result, "PARSED")
    ladder = {name: value for name, value, _ in note.fields}["Trim Targets"]
    # Two selling rungs, in the advisor's own "25%   $0.594" shape. The levels
    # above +50% sell nothing, so listing them here would advertise trims the
    # bot will never place.
    assert ladder.count("\n") == 1
    assert "25%" in ladder and "50%" in ladder
    assert "75%" not in ladder
    assert "100%" not in ladder


async def test_an_accepted_alert_is_persisted_with_its_contract(pipeline, storage):
    result = await paste(pipeline, message_id=777)

    row = storage.get_alert(777)
    assert row["status"] == STATUS_ACCEPTED
    assert row["occ_symbol"] == SPX_OCC
    assert row["entry_price"] == 0.475
    assert row["advisor_contracts"] == 25
    assert row["jump_url"].endswith("777")


async def test_the_dry_run_records_an_order_row_marked_as_such(pipeline, storage):
    await paste(pipeline)

    orders = storage._conn.execute("SELECT * FROM orders").fetchall()
    assert len(orders) == 1
    assert orders[0]["status"] == "DRY_RUN"
    assert orders[0]["side"] == "BUY"
    assert orders[0]["qty"] == 21
    assert orders[0]["order_type"] == "LMT"  # never a market order


async def test_every_notification_links_back_to_the_alert(pipeline):
    result = await paste(pipeline, message_id=4242)

    assert result.notifications
    for note in result.notifications:
        assert note.jump_url and note.jump_url.endswith("4242")


# --- deduplication --------------------------------------------------------


async def test_the_same_message_is_never_processed_twice(pipeline, storage):
    first = await paste(pipeline, message_id=900)
    second = await paste(pipeline, message_id=900)

    assert len(first.accepted) == 1
    assert second.accepted == []
    assert find(second, "DUPLICATE")
    assert len(storage._conn.execute("SELECT * FROM orders").fetchall()) == 1


async def test_dedup_survives_a_restart(settings, storage, tmp_path):
    """The dedup record is a row, not memory, so a fresh pipeline over the
    same database still refuses the alert."""
    risk = RiskGate(settings, storage)
    first = AlertPipeline(settings, storage, risk, FakeBroker())
    await paste(first, message_id=901)

    rebuilt = AlertPipeline(settings, storage, RiskGate(settings, storage), FakeBroker())
    again = await paste(rebuilt, message_id=901)

    assert again.accepted == []
    assert find(again, "DUPLICATE")


async def test_a_repasted_alert_under_a_new_message_id_is_caught(pipeline, storage):
    """Re-pasting creates a new message, so the id check cannot catch it; the
    contract-and-entry fingerprint does."""
    await paste(pipeline, message_id=910)
    again = await paste(pipeline, message_id=911)

    assert again.accepted == []
    assert again.final_reaction == REACTION_SKIPPED
    note = find(again, "SKIPPED")
    assert "already accepted as message 910" in note.description
    assert storage.get_alert(911)["status"] == STATUS_DUPLICATE


async def test_a_second_alert_at_a_different_price_is_not_a_duplicate(pipeline):
    """The fingerprint is contract *and* entry, so a genuine re-entry at a new
    price still gets through. In dry run no position is held, so the
    already-in-position check does not apply either."""
    await paste(pipeline, message_id=920)
    other = SPEC_ALERT.replace("$0.475", "$0.600").replace(
        "25%   $0.594\n50%   $0.713\n75%   $0.831\n100%  $0.950",
        "25%   $0.750\n50%   $0.900\n75%   $1.050\n100%  $1.200",
    )
    second = await paste(pipeline, other, message_id=921)

    assert second.final_reaction == REACTION_PLACED
    assert len(second.accepted) == 1


# --- rejections -----------------------------------------------------------


async def test_an_unreadable_paste_is_reported_not_ignored(pipeline, storage):
    result = await paste(pipeline, "anyone else get filled on that?", message_id=930)

    assert result.final_reaction == REACTION_REJECTED
    note = find(result, "UNREADABLE")
    assert note.level is Level.WARNING
    assert storage.get_alert(930)["status"] == STATUS_REJECTED


async def test_a_failed_validation_names_its_reason(pipeline, storage):
    result = await paste(pipeline, SPEC_ALERT.replace("$0.475", "$0.485"), message_id=940)

    assert result.final_reaction == REACTION_REJECTED
    note = find(result, "REJECTED")
    assert "trim_target_mismatch" in note.to_text()
    assert "0.606" in note.description
    assert storage.get_alert(940)["status"] == STATUS_REJECTED


async def test_a_wrong_day_0dte_alert_is_rejected(pipeline):
    result = await paste(pipeline, trading_day=date(2026, 10, 5))

    assert result.final_reaction == REACTION_REJECTED
    assert "expiry_tag_mismatch" in find(result, "REJECTED").to_text()


async def test_a_rejected_alert_never_reaches_the_broker(pipeline, broker):
    await paste(pipeline, SPEC_ALERT.replace("$0.475", "$0.485"))

    assert broker.chain_lookups == []
    assert broker.orders == []


# --- informational cards --------------------------------------------------


async def test_an_advisor_trim_card_is_informational_only(pipeline, storage):
    card = """TRIM +25% — QQQ 740C · Oct 2
Sold 1 of 10 @ $6.413 · 9 still running.
Open Live Dashboard →
Entry
$5.13

Not financial advice"""
    result = await paste(pipeline, card, message_id=950)

    assert result.final_reaction == REACTION_RECEIVED
    assert find(result, "NOTED")
    assert storage.get_alert(950)["status"] == STATUS_INFO
    assert result.accepted == []


# --- the risk gate --------------------------------------------------------


async def test_a_halted_bot_skips_entries_but_still_reports(pipeline, risk, storage):
    risk.halt("testing")

    result = await paste(pipeline, message_id=960)

    assert result.final_reaction == REACTION_SKIPPED
    note = find(result, "SKIPPED")
    assert "halted" in note.description
    assert "!resume" in note.description
    assert storage.get_alert(960)["status"] == STATUS_SKIPPED


async def test_an_alert_outside_market_hours_is_skipped(pipeline):
    result = await paste(pipeline, moment=datetime(2026, 10, 6, 8, 0, tzinfo=EASTERN))

    assert result.final_reaction == REACTION_SKIPPED
    assert "market is closed" in find(result, "SKIPPED").description


async def test_an_alert_past_the_entry_cutoff_is_skipped(pipeline):
    result = await paste(pipeline, moment=datetime(2026, 10, 6, 15, 45, tzinfo=EASTERN))

    assert result.final_reaction == REACTION_SKIPPED
    assert "cutoff" in find(result, "SKIPPED").description


async def test_the_daily_trade_cap_blocks_further_entries(settings, storage):
    tight = make_settings(risk={"max_trades_per_day": 1})
    risk = RiskGate(tight, storage)
    pipe = AlertPipeline(tight, storage, risk, FakeBroker())

    first = await paste(pipe, message_id=970)
    other = SPEC_ALERT.replace("7815", "7820")
    second = await paste(pipe, other, message_id=980)

    assert first.final_reaction == REACTION_PLACED
    assert second.final_reaction == REACTION_SKIPPED
    assert "1 of 1 allowed trades" in find(second, "SKIPPED").description


async def test_the_daily_loss_limit_blocks_entries(settings, storage):
    tight = make_settings(risk={"max_daily_loss_usd": 500})
    risk = RiskGate(tight, storage)
    risk.record_realized(-600.0, TRADING_DAY)
    pipe = AlertPipeline(tight, storage, risk, FakeBroker())

    result = await paste(pipe)

    assert result.final_reaction == REACTION_SKIPPED
    # Breaching the limit auto-halts, so that is the reason reported first.
    assert "halted" in find(result, "SKIPPED").description
    assert risk.halted


async def test_the_open_position_cap_blocks_further_entries(settings, storage):
    from options_scanner.models import OptionKey, PositionState

    tight = make_settings(risk={"max_open_positions": 1})
    storage.record_alert(
        message_id=1, channel_id=1001, author_id=3003, raw_text="seed", status=STATUS_ACCEPTED
    )
    storage.create_position(
        1,
        PositionState(
            option=OptionKey("QQQ", date(2026, 10, 6), 740.0, "C"),
            entry_fill=1.0,
            original_qty=5,
            remaining_qty=5,
            peak_bid=1.0,
        ),
    )
    pipe = AlertPipeline(tight, storage, RiskGate(tight, storage), FakeBroker())

    result = await paste(pipe)

    assert result.final_reaction == REACTION_SKIPPED
    assert "position slots already in use" in find(result, "SKIPPED").description


# --- broker interaction ---------------------------------------------------


async def test_the_contract_is_verified_against_the_chain_before_ordering(pipeline, broker):
    await paste(pipeline)

    assert broker.chain_lookups == [SPX_OCC]


async def test_a_contract_missing_from_the_chain_is_rejected(settings, storage):
    missing = FakeBroker(chain_has_contract=False)
    pipe = AlertPipeline(settings, storage, RiskGate(settings, storage), missing)

    result = await paste(pipe)

    assert result.final_reaction == REACTION_REJECTED
    note = find(result, "REJECTED")
    assert "not found in the option chain" in note.description
    assert "SPXW" in note.description  # names the trading class it looked under


async def test_a_chain_lookup_failure_skips_rather_than_guesses(settings, storage):
    flaky = FakeBroker(chain_raises=True)
    pipe = AlertPipeline(settings, storage, RiskGate(settings, storage), flaky)

    result = await paste(pipe)

    assert result.final_reaction == REACTION_SKIPPED
    assert "Nothing was ordered" in find(result, "verify").description


async def test_an_ask_already_above_the_slippage_cap_is_skipped(settings, storage):
    # Cap is 0.475 * 1.10 = 0.5225; an ask of 0.60 is well past it.
    expensive = FakeBroker(bid=0.58, ask=0.60)
    pipe = AlertPipeline(settings, storage, RiskGate(settings, storage), expensive)

    result = await paste(pipe)

    assert result.final_reaction == REACTION_SKIPPED
    note = find(result, "SKIPPED")
    assert "not chasing" in note.description.lower()
    assert "ask_above_cap" in note.to_text()


async def test_an_ask_inside_the_cap_is_accepted(settings, storage):
    ok = FakeBroker(bid=0.50, ask=0.51)
    pipe = AlertPipeline(settings, storage, RiskGate(settings, storage), ok)

    result = await paste(pipe)

    assert result.final_reaction == REACTION_PLACED


async def test_a_quote_failure_does_not_block_a_dry_run(settings, storage):
    """In dry run the alert's own entry is the reference, so a missing quote is
    not a reason to refuse."""
    no_data = FakeBroker(quote_raises=True)
    pipe = AlertPipeline(settings, storage, RiskGate(settings, storage), no_data)

    result = await paste(pipe)

    assert result.final_reaction == REACTION_PLACED


async def test_the_pipeline_works_with_no_broker_at_all(settings, storage):
    pipe = AlertPipeline(settings, storage, RiskGate(settings, storage), None)

    result = await paste(pipe)

    assert result.final_reaction == REACTION_PLACED
    assert len(result.accepted) == 1


# --- multi-card pastes ----------------------------------------------------


async def test_several_cards_in_one_paste_are_each_handled(pipeline, storage):
    info_card = """TRIM +25% — QQQ 740C · Oct 2
Sold 1 of 10 @ $6.413 · 9 still running.
Open Live Dashboard →
Entry
$5.13

Not financial advice"""
    result = await paste(pipeline, SPEC_ALERT + "\n" + info_card, message_id=1100)

    assert len(result.accepted) == 1
    assert find(result, "WOULD BUY")
    assert find(result, "NOTED")
    # Each card gets its own row, at message_id + index.
    assert storage.get_alert(1100)["status"] == STATUS_ACCEPTED
    assert storage.get_alert(1101)["status"] == STATUS_INFO


async def test_the_strongest_reaction_wins_for_a_mixed_paste(pipeline):
    bad = SPEC_ALERT.replace("$0.475", "$0.485")
    result = await paste(pipeline, SPEC_ALERT + "\n" + bad, message_id=1200)

    # One accepted, one rejected -- the accepted entry is what matters most.
    assert result.final_reaction == REACTION_PLACED
    assert find(result, "REJECTED")


async def test_one_broken_card_does_not_stop_the_others(pipeline, monkeypatch):
    import options_scanner.pipeline as pipeline_module

    real = pipeline_module.parse_card
    calls = {"n": 0}

    def flaky(card, today):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("boom")
        return real(card, today)

    monkeypatch.setattr(pipeline_module, "parse_card", flaky)
    result = await paste(pipeline, SPEC_ALERT + "\n" + SPEC_ALERT, message_id=1300)

    assert find(result, "Error handling alert")
    assert len(result.accepted) == 1  # the second card still went through


# --- counters -------------------------------------------------------------


async def test_the_day_counters_track_what_happened(pipeline, risk, storage):
    await paste(pipeline, message_id=1400)
    await paste(pipeline, "just chatting", message_id=1500)
    risk.halt("no more")
    await paste(pipeline, SPEC_ALERT.replace("7815", "7830"), message_id=1600)

    day = risk.day_summary(TRADING_DAY)
    assert day["alerts_received"] == 3
    assert day["alerts_rejected"] == 1
    assert day["alerts_skipped"] == 1
    assert day["entries"] == 1


# --- mode gating ----------------------------------------------------------


async def test_paper_mode_with_no_broker_refuses_rather_than_pretending(settings, storage):
    """Reporting an accepted alert with nothing behind it would claim a
    position the bot does not hold."""
    paper = make_settings(mode="paper")
    pipe = AlertPipeline(paper, storage, RiskGate(paper, storage), None)

    result = await paste(pipe)

    assert result.final_reaction == REACTION_SKIPPED
    assert find(result, "No broker configured")
    assert result.accepted == []


async def test_dry_run_embeds_are_labelled_as_such(pipeline):
    result = await paste(pipeline)

    note = find(result, "WOULD BUY")
    assert pipeline.dry_run is True
    # to_embed needs discord installed; the label logic is what matters here.
    assert note.to_embed(dry_run=True).title.startswith("[DRY RUN]")
    assert not note.to_embed(dry_run=False).title.startswith("[DRY RUN]")


# --- sizing off account equity -------------------------------------------


async def test_the_unit_is_a_percent_of_live_equity(storage, risk):
    """3% of $20,000 is $600, which buys 12 contracts at $47.50 -- well under
    the $1,000 ceiling, so the percentage is what is doing the sizing."""
    broker = FakeBroker()
    broker.get_account = _equity(20_000.0)
    settings = make_settings()
    pipeline = AlertPipeline(settings, storage, RiskGate(settings, storage), broker)

    result = await paste(pipeline)

    values = {name: value for name, value, _ in find(result, "PARSED").fields}
    assert values["Our size"].startswith("12 contracts")


async def test_the_ceiling_still_binds_on_a_large_account(storage):
    broker = FakeBroker()
    broker.get_account = _equity(5_000_000.0)
    settings = make_settings()
    pipeline = AlertPipeline(settings, storage, RiskGate(settings, storage), broker)

    result = await paste(pipeline)

    # 3% of $5m is $150,000. The $1,000 ceiling is the only thing between that
    # and a position nobody intended.
    values = {name: value for name, value, _ in find(result, "PARSED").fields}
    assert values["Our size"].startswith("21 contracts")


async def test_a_lotto_is_sized_at_half_a_unit(storage):
    broker = FakeBroker()
    broker.get_account = _equity(20_000.0)
    settings = make_settings()
    pipeline = AlertPipeline(settings, storage, RiskGate(settings, storage), broker)

    result = await paste(pipeline, text=_tagged(SPEC_ALERT, " Lotto Trade — RISKY"))

    # $600 halved is $300, which buys 6 at $47.50.
    values = {name: value for name, value, _ in find(result, "PARSED").fields}
    assert values["Our size"].startswith("6 contracts")


async def test_a_super_lotto_is_sized_at_a_quarter_unit(storage):
    broker = FakeBroker()
    broker.get_account = _equity(20_000.0)
    settings = make_settings()
    pipeline = AlertPipeline(settings, storage, RiskGate(settings, storage), broker)

    result = await paste(
        pipeline, text=_tagged(SPEC_ALERT, " Super Lotto Trade — Super RISKY")
    )

    # $600 quartered is $150, which buys 3 at $47.50.
    values = {name: value for name, value, _ in find(result, "PARSED").fields}
    assert values["Our size"].startswith("3 contracts")


async def test_an_unreadable_equity_skips_the_alert_rather_than_guessing(storage):
    """With the unit expressed as a percent of equity, a failed read has no
    safe default: falling back to a number would size a live trade off a
    stale guess."""
    from options_scanner.broker.base import BrokerError

    async def boom():
        raise BrokerError("account summary never arrived")

    broker = FakeBroker()
    broker.get_account = boom
    settings = make_settings()
    pipeline = AlertPipeline(settings, storage, RiskGate(settings, storage), broker)

    result = await paste(pipeline)

    assert result.final_reaction == REACTION_SKIPPED
    assert result.accepted == []
    assert broker.orders == []
    assert "equity" in find(result, "skipped").description.lower()


async def test_a_zero_equity_is_treated_as_a_failed_read_not_an_empty_account(storage):
    """IBKR reports net liquidation as 0 before the account summary arrives.
    Sized literally that is a zero budget; treated as a failure it is a skip."""
    broker = FakeBroker()
    broker.get_account = _equity(0.0)
    settings = make_settings()
    pipeline = AlertPipeline(settings, storage, RiskGate(settings, storage), broker)

    result = await paste(pipeline)

    assert result.final_reaction == REACTION_SKIPPED
    assert broker.orders == []


async def test_dry_run_sizes_off_the_fallback_equity_and_says_so(storage):
    """No broker at all, so an assumed balance is the only option -- and the
    card has to admit it."""
    settings = make_settings(risk={"fallback_equity": 20_000.0})
    pipeline = AlertPipeline(settings, storage, RiskGate(settings, storage), broker=None)

    result = await paste(pipeline)

    assert result.final_reaction == REACTION_PLACED
    values = {name: value for name, value, _ in find(result, "PARSED").fields}
    assert values["Our size"].startswith("12 contracts")


def _equity(net_liquidation: float):
    from options_scanner.broker.base import AccountSnapshot

    async def read():
        return AccountSnapshot(net_liquidation=net_liquidation, buying_power=net_liquidation / 2)

    return read


def _tagged(alert: str, tag: str) -> str:
    """Insert a tier tag as a description line, where the advisor puts it."""
    lines = alert.split("\n")
    lines.insert(2, tag)
    return "\n".join(lines)
