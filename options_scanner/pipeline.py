"""What happens when a message appears in the alerts channel.

All of the decision-making lives here, behind a plain async method that takes
primitives and returns a reaction plus a list of notifications. `discord_bot`
is then a thin adapter that knows about Discord and nothing about trading,
which is what makes the whole alert path testable without a connection.

Order of business for one paste, and the reasoning behind it:

1. **Mark it seen first.** The alerts channel gets no text from the bot, only
   a reaction, so the reaction is the only evidence a paste was picked up.
2. **Deduplicate on the message id**, from the database rather than memory, so
   a restart cannot re-trade yesterday's alerts.
3. **Split into cards**, and report a paste that yields none -- a dropped
   paste and an unreadable paste look identical otherwise.
4. **Parse and validate** each card. Anything invalid is reported with its
   specific reason and never traded.
5. **Risk gate**, which can only ever refuse to open.
6. **Size from our own config**, never the advisor's contract count.
7. **Confirm the contract exists** before ordering, so a mis-parsed strike
   fails loudly instead of becoming a silently different contract.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, datetime

from options_scanner.broker.base import Broker, BrokerError, Quote
from options_scanner.config import Mode, Settings
from options_scanner.contracts import build_spec
from options_scanner.execution import execute_entry
from options_scanner.market_hours import now_et, today_et
from options_scanner.models import EntryAlert, InfoAlert, PositionState, RejectedAlert
from options_scanner.notifier import (
    REACTION_PLACED,
    REACTION_RECEIVED,
    REACTION_REJECTED,
    REACTION_SKIPPED,
    Notification,
    alert_duplicate,
    alert_informational,
    alert_parsed,
    alert_rejected,
    alert_skipped,
    entry_filled,
    entry_unfilled,
    error as error_note,
    partial_fill,
    unreadable_paste,
    would_place_entry,
)
from options_scanner.parser import parse_card
from options_scanner.risk import RiskGate
from options_scanner.rules import level_price
from options_scanner.sizing import compute_contracts, position_cost, sizing_note, unit_cap
from options_scanner.storage import (
    STATUS_ACCEPTED,
    STATUS_DUPLICATE,
    STATUS_INFO,
    STATUS_REJECTED,
    STATUS_SKIPPED,
    Storage,
)
from options_scanner.text_import import parse_cards

logger = logging.getLogger("options_scanner.pipeline")

# Which reaction wins when one paste contains several cards. An accepted entry
# is the most important thing to surface; a plain acknowledgement the least.
_REACTION_PRECEDENCE = [REACTION_PLACED, REACTION_REJECTED, REACTION_SKIPPED, REACTION_RECEIVED]


@dataclass
class Handled:
    """The outcome of one pasted message."""

    reactions: list[str] = field(default_factory=list)
    notifications: list[Notification] = field(default_factory=list)
    accepted: list[EntryAlert] = field(default_factory=list)

    def add(self, reaction: str, *notes: Notification) -> None:
        if reaction not in self.reactions:
            self.reactions.append(reaction)
        self.notifications.extend(notes)

    @property
    def final_reaction(self) -> str:
        for candidate in _REACTION_PRECEDENCE:
            if candidate in self.reactions:
                return candidate
        return REACTION_RECEIVED


class AlertPipeline:
    def __init__(
        self,
        settings: Settings,
        storage: Storage,
        risk: RiskGate,
        broker: Broker | None = None,
        manager=None,
    ):
        self.settings = settings
        self.storage = storage
        self.risk = risk
        self.broker = broker
        # The PositionManager, once an entry fills. Optional so the dry-run
        # path and the parser tests need no loop at all.
        self.manager = manager

    @property
    def dry_run(self) -> bool:
        return self.settings.mode is Mode.DRY_RUN

    async def handle_paste(
        self,
        *,
        message_id: int,
        channel_id: int,
        author_id: int,
        text: str,
        jump_url: str | None = None,
        moment: datetime | None = None,
        trading_day: date | None = None,
    ) -> Handled:
        """Process one message from the alerts channel. Never raises."""
        moment = moment or now_et()
        trading_day = trading_day or today_et()
        result = Handled()

        if self.storage.alert_seen(message_id):
            logger.info("message %s already processed; ignoring", message_id)
            result.add(
                REACTION_RECEIVED,
                alert_duplicate(
                    detail=f"Message {message_id} was already processed, so nothing was re-traded.",
                    jump_url=jump_url,
                ),
            )
            return result

        cards = parse_cards(text, reference_date=moment)
        self.storage.bump_day(trading_day, alerts_received=1)

        if not cards:
            self.storage.record_alert(
                message_id=message_id,
                channel_id=channel_id,
                author_id=author_id,
                raw_text=text,
                jump_url=jump_url,
                status=STATUS_REJECTED,
                reason="no alert card found in the paste",
            )
            self.storage.bump_day(trading_day, alerts_rejected=1)
            result.add(REACTION_REJECTED, unreadable_paste(preview=text, jump_url=jump_url))
            return result

        for index, card in enumerate(cards):
            # One Discord message can hold several cards, and every alert row
            # needs its own id. Add rather than multiply: multiplying a
            # snowflake overflows SQLite's signed 64-bit INTEGER, while adding
            # a small index cannot, and a collision with a real snowflake
            # would need two messages in the same millisecond.
            card_id = message_id + index
            card.message_id = card_id
            try:
                await self._handle_card(
                    card_id=card_id,
                    channel_id=channel_id,
                    author_id=author_id,
                    raw_text=text,
                    jump_url=jump_url,
                    card=card,
                    moment=moment,
                    trading_day=trading_day,
                    result=result,
                )
            except Exception as exc:  # one bad card must not abort the rest
                logger.exception("failed handling card %s of message %s", index, message_id)
                self.storage.record_alert(
                    message_id=card_id,
                    channel_id=channel_id,
                    author_id=author_id,
                    raw_text=text,
                    jump_url=jump_url,
                    status=STATUS_REJECTED,
                    reason=f"internal error: {exc}",
                )
                result.add(
                    REACTION_REJECTED,
                    error_note(
                        title="Error handling alert",
                        detail=f"{type(exc).__name__}: {exc}\nNothing was traded for this card.",
                        jump_url=jump_url,
                    ),
                )

        return result

    async def _handle_card(
        self,
        *,
        card_id: int,
        channel_id: int,
        author_id: int,
        raw_text: str,
        jump_url: str | None,
        card,
        moment: datetime,
        trading_day: date,
        result: Handled,
    ) -> None:
        parsed = parse_card(card, today=trading_day)

        def record(status: str, reason: str | None = None, alert: EntryAlert | None = None) -> None:
            self.storage.record_alert(
                message_id=card_id,
                channel_id=channel_id,
                author_id=author_id,
                raw_text=raw_text,
                jump_url=jump_url,
                status=status,
                reason=reason,
                alert=alert,
            )

        if isinstance(parsed, RejectedAlert):
            record(STATUS_REJECTED, f"{parsed.reason.value}: {parsed.detail}")
            self.storage.bump_day(trading_day, alerts_rejected=1)
            result.add(
                REACTION_REJECTED,
                alert_rejected(
                    reason=parsed.reason.value,
                    detail=parsed.detail,
                    raw_title=parsed.raw_title,
                    jump_url=jump_url,
                ),
            )
            return

        if isinstance(parsed, InfoAlert):
            record(STATUS_INFO, f"{parsed.kind}: {parsed.detail}")
            result.add(
                REACTION_RECEIVED,
                alert_informational(
                    kind=parsed.kind,
                    raw_title=parsed.raw_title,
                    detail=parsed.detail or "Recognized but not actionable.",
                    jump_url=jump_url,
                ),
            )
            return

        await self._handle_entry(
            alert=parsed,
            record=record,
            jump_url=jump_url,
            moment=moment,
            trading_day=trading_day,
            result=result,
        )

    async def _handle_entry(
        self,
        *,
        alert: EntryAlert,
        record,
        jump_url: str | None,
        moment: datetime,
        trading_day: date,
        result: Handled,
    ) -> None:
        spec = build_spec(alert.option)
        settings = self.settings

        decision = self.risk.check_entry(
            spec.occ_symbol, alert.entry_price, moment=moment, trading_day=trading_day
        )
        if not decision.allowed:
            status = STATUS_DUPLICATE if decision.reason.value == "duplicate_alert" else STATUS_SKIPPED
            record(status, f"{decision.reason.value}: {decision.detail}")
            self.storage.bump_day(trading_day, alerts_skipped=1)
            result.add(
                REACTION_SKIPPED,
                alert_skipped(
                    reason=decision.reason.value,
                    detail=decision.detail,
                    jump_url=jump_url,
                    option=alert.option,
                    today=trading_day,
                ),
            )
            return

        # The contract must exist before anything is ordered. A mis-parsed
        # strike or a wrong trading class has to fail here, not become a
        # silently different contract at the broker.
        if self.broker is not None:
            try:
                if not await self.broker.get_option_chain(spec):
                    record(STATUS_REJECTED, f"contract not found at broker: {spec.occ_symbol}")
                    self.storage.bump_day(trading_day, alerts_rejected=1)
                    result.add(
                        REACTION_REJECTED,
                        alert_rejected(
                            reason="contract_not_found",
                            detail=(
                                f"`{spec.occ_symbol}` was not found in the option chain "
                                f"({spec.trading_class} on {spec.exchange}). Nothing was ordered."
                            ),
                            raw_title=alert.raw_title,
                            jump_url=jump_url,
                        ),
                    )
                    return
            except BrokerError as exc:
                record(STATUS_SKIPPED, f"chain lookup failed: {exc}")
                self.storage.bump_day(trading_day, alerts_skipped=1)
                result.add(
                    REACTION_SKIPPED,
                    error_note(
                        title="Could not verify the contract",
                        detail=f"{exc}\nNothing was ordered.",
                        jump_url=jump_url,
                    ),
                )
                return

        quote = await self._quote(spec)
        reference_price = alert.entry_price
        cap_price = reference_price * (1 + settings.entry.max_slippage_pct / 100.0)

        if quote is not None and quote.ask and settings.entry.skip_if_ask_above_cap:
            if quote.ask > cap_price:
                detail = (
                    f"ask is {quote.ask:.3f} but the slippage cap is {cap_price:.3f} "
                    f"({settings.entry.max_slippage_pct:.0f}% over the {reference_price:.3f} alert "
                    "entry). The move went without us; not chasing."
                )
                record(STATUS_SKIPPED, f"ask_above_cap: {detail}")
                self.storage.bump_day(trading_day, alerts_skipped=1)
                result.add(
                    REACTION_SKIPPED,
                    alert_skipped(
                        reason="ask_above_cap", detail=detail, jump_url=jump_url,
                        option=alert.option, today=trading_day,
                    ),
                )
                return

        # Our own size, from our own account. The advisor's contract count
        # reflects their account and is recorded for the audit trail only.
        budget = await self._unit_cap(alert.tier)
        if budget is None:
            detail = (
                "could not read account equity, and the unit is a percent of it. "
                "Sizing off a guess is how one bad read becomes a position nobody "
                "intended; nothing was ordered."
            )
            record(STATUS_SKIPPED, f"equity_unavailable: {detail}")
            self.storage.bump_day(trading_day, alerts_skipped=1)
            result.add(
                REACTION_SKIPPED,
                alert_skipped(
                    reason="equity_unavailable", detail=detail, jump_url=jump_url,
                    option=alert.option, today=trading_day,
                ),
            )
            return
        cap_usd, equity, equity_is_assumed = budget

        contracts = compute_contracts(
            reference_price,
            cap_usd,
            max_contracts=settings.risk.max_contracts_per_trade,
        )
        cost = position_cost(contracts, reference_price)
        size_note = sizing_note(
            equity=equity,
            unit_pct=settings.risk.unit_pct_of_account,
            tier=alert.tier,
            cap_usd=cap_usd,
            equity_is_assumed=equity_is_assumed,
        )

        ladder = [
            (level, level_price(reference_price, level))
            for level, pct in settings.trim.schedule
            if pct > 0
        ]

        parsed_note = alert_parsed(
            option=alert.option,
            occ_symbol=spec.occ_symbol,
            entry_price=reference_price,
            advisor_contracts=alert.advisor_contracts,
            our_contracts=contracts,
            cost=cost,
            jump_url=jump_url,
            levels=ladder,
            sizing_note=size_note,
            today=trading_day,
        )

        if self.dry_run:
            record(STATUS_ACCEPTED, "dry run: would have ordered", alert=alert)
            self.risk.record_entry(trading_day)
            self.storage.record_order(
                occ_symbol=spec.occ_symbol,
                intent="ENTRY",
                side="BUY",
                qty=contracts,
                status="DRY_RUN",
                alert_message_id=alert.message_id,
                limit_price=reference_price,
                detail="dry run; nothing sent to the broker",
            )
            result.accepted.append(alert)
            result.add(
                REACTION_PLACED,
                parsed_note,
                would_place_entry(
                    option=alert.option,
                    qty=contracts,
                    limit_price=reference_price,
                    cap_price=cap_price,
                    timeout_seconds=settings.entry.fill_timeout_seconds,
                    jump_url=jump_url,
                    today=trading_day,
                ),
            )
            return

        if self.broker is None:
            record(STATUS_SKIPPED, "no broker configured")
            result.add(
                REACTION_SKIPPED,
                error_note(
                    title="No broker configured",
                    detail=f"`{spec.occ_symbol}` passed every check but there is no broker to order through.",
                    jump_url=jump_url,
                ),
            )
            return

        result.add(REACTION_RECEIVED, parsed_note)

        # Claim the alert before the slow part, and before the order row that
        # references it. The walk can take tens of seconds, and the fingerprint
        # has to be on record for that whole window or a second paste during it
        # would be ordered as well.
        record(STATUS_ACCEPTED, "entry order working", alert=alert)

        order_id = self.storage.record_order(
            occ_symbol=spec.occ_symbol,
            intent="ENTRY",
            side="BUY",
            qty=contracts,
            status="PENDING",
            alert_message_id=alert.message_id,
            limit_price=reference_price,
        )

        outcome = await execute_entry(
            self.broker,
            spec,
            contracts,
            start_price=reference_price,
            cap_price=cap_price,
            steps=settings.entry.limit_walk_steps,
            timeout_seconds=settings.entry.fill_timeout_seconds,
        )
        self.storage.update_order(
            order_id,
            status=outcome.status,
            filled_qty=outcome.filled_qty,
            avg_fill_price=outcome.avg_price,
            detail=outcome.detail,
        )

        if not outcome.any_fill:
            record(STATUS_SKIPPED, f"entry not filled: {outcome.detail}")
            result.add(
                REACTION_SKIPPED,
                entry_unfilled(
                    occ_symbol=spec.occ_symbol,
                    qty=contracts,
                    cap_price=cap_price,
                    timeout_seconds=settings.entry.fill_timeout_seconds,
                    jump_url=jump_url,
                    today=trading_day,
                ),
            )
            return

        # Everything from here measures against the fill we actually got, never
        # the advisor's quoted entry or our own requested quantity.
        fill_price = outcome.avg_price or reference_price
        filled = outcome.filled_qty

        if not outcome.is_filled:
            result.add(
                REACTION_PLACED,
                partial_fill(
                    option=alert.option,
                    filled=filled,
                    requested=contracts,
                    fill_price=fill_price,
                    jump_url=jump_url,
                    today=trading_day,
                ),
            )

        state = PositionState(
            option=alert.option,
            entry_fill=fill_price,
            original_qty=filled,
            remaining_qty=filled,
            peak_bid=fill_price,
        )
        position_id = self.storage.create_position(alert.message_id, state)
        self.storage.update_order(order_id, detail=f"position {position_id}")

        record(STATUS_ACCEPTED, f"filled {filled} @ {fill_price:.3f}", alert=alert)
        self.risk.record_entry(trading_day)
        result.accepted.append(alert)

        if self.manager is not None:
            self.manager.track(position_id, state, jump_url=jump_url)

        result.add(
            REACTION_PLACED,
            entry_filled(
                option=alert.option,
                qty=filled,
                fill_price=fill_price,
                cost=filled * fill_price * 100,
                jump_url=jump_url,
                # Our ladder, recomputed off the fill we actually got -- not
                # the one the card advertised off the advisor's entry.
                levels=[
                    (level, level_price(fill_price, level))
                    for level, pct in settings.trim.schedule
                    if pct > 0
                ],
                sizing_note=size_note,
                today=trading_day,
            ),
        )

    async def _unit_cap(self, tier: str) -> tuple[float, float, bool] | None:
        """The dollar budget for one trade: (cap, equity, equity_is_assumed).

        None means the equity read failed and the caller must skip the alert.
        That is the whole reason this returns a tuple rather than a float: with
        the unit expressed as a percent of equity, a failed read has no safe
        default. Falling back to a configured number would size a live trade
        off a stale guess, and silently sizing to the ceiling would do the same
        thing with extra steps.

        dry_run is the one case where an assumed equity is correct -- it has no
        broker at all -- and the flag it returns is what makes the entry card
        say so.
        """
        risk = self.settings.risk

        if self.broker is None:
            return (
                unit_cap(
                    risk.fallback_equity,
                    risk.unit_pct_of_account,
                    tier,
                    lotto_multiplier=risk.lotto_multiplier,
                    super_lotto_multiplier=risk.super_lotto_multiplier,
                    ceiling_usd=risk.max_usd_per_trade,
                ),
                risk.fallback_equity,
                True,
            )

        try:
            snapshot = await self.broker.get_account()
        except BrokerError as exc:
            logger.warning("account equity lookup failed: %s", exc)
            return None

        equity = getattr(snapshot, "net_liquidation", 0.0) or 0.0
        if equity <= 0:
            # A zero net liquidation is a failed read, not an empty account:
            # IBKR reports it that way before the account summary arrives.
            logger.warning("account equity read as %r; refusing to size off it", equity)
            return None

        return (
            unit_cap(
                equity,
                risk.unit_pct_of_account,
                tier,
                lotto_multiplier=risk.lotto_multiplier,
                super_lotto_multiplier=risk.super_lotto_multiplier,
                ceiling_usd=risk.max_usd_per_trade,
            ),
            equity,
            False,
        )

    async def _quote(self, spec) -> Quote | None:
        """Best-effort quote. A failure here is not fatal in dry run, where the
        alert's own entry price is the reference anyway."""
        if self.broker is None:
            return None
        try:
            return await self.broker.get_quote(spec)
        except BrokerError as exc:
            logger.warning("quote lookup failed for %s: %s", spec.occ_symbol, exc)
            return None
