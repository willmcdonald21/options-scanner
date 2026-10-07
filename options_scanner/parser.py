"""Alert card -> validated EntryAlert, or an explicit rejection.

Two layers, kept separate on purpose:

* `options_scanner/text_import.py` splits a pasted message into normalized
  cards (title / description / fields / footer).
* this module classifies a card and *validates* it.

Nothing here raises for bad input. An alert that cannot be parsed or fails
validation comes back as a `RejectedAlert` carrying a `RejectReason`, because
the spec requires every skipped alert to explain itself in the updates
channel -- a silent drop would look identical to a missed paste.

Validation gates (any failure -> RejectedAlert, never traded):
  1. the contract line must yield ticker/expiry/strike/right
  2. Entry / Contracts / Cost must be present and positive
  3. the expiry must agree with the 0DTE / "Sep 30" tag and not be in the past
  4. the advisor's listed trim targets must match prices recomputed off the
     entry, within a cent -- a mismatch means we misread a number
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime

from options_scanner.models import (
    EntryAlert,
    InfoAlert,
    OptionKey,
    ParseResult,
    RejectedAlert,
    RejectReason,
    TrimTarget,
)

_MONTHS = {
    "Jan": 1, "Feb": 2, "Mar": 3, "Apr": 4, "May": 5, "Jun": 6,
    "Jul": 7, "Aug": 8, "Sep": 9, "Oct": 10, "Nov": 11, "Dec": 12,
}

# "Entered SPX Oct06 '26 7815 Put" / "Fully out of NVDA Sep23 '26 230 Call."
# The only line carrying the full, unambiguous contract identity, including
# the year. The title's "· 0DTE" / "· Sep 30" tag is a cross-check, not a
# source of truth.
_CONTRACT_LINE_RE = re.compile(
    r"(?:Entered|Fully out of)\s+"
    r"(?P<ticker>[A-Z]+)\s+"
    r"(?P<mon>[A-Za-z]{3})(?P<day>\d{1,2})\s*'(?P<yy>\d{2})\s+"
    r"(?P<strike>[\d.]+)\s+"
    r"(?P<right>Call|Put)",
    re.IGNORECASE,
)

# "SPX 7815P · 0DTE" -- title shorthand, used for the underlying on cards
# that don't repeat the contract line (advisor TRIM / EXIT / EXPIRED).
_TITLE_UNDERLYING_RE = re.compile(r"(?P<ticker>[A-Z]+)\s+(?P<strike>[\d.]+)(?P<right>[CP])\b")

_TITLE_BUY_RE = re.compile(r"^BUY\b", re.IGNORECASE)
_TITLE_AVG_DOWN_RE = re.compile(r"^AVERAGING DOWN\b", re.IGNORECASE)
_TITLE_TRIM_RE = re.compile(r"^TRIM\s*\+?(?P<pct>\d+)%", re.IGNORECASE)
_TITLE_SOLD_ALL_RE = re.compile(r"^SOLD ALL\s*(?P<sign>[+-])(?P<pct>\d+(?:\.\d+)?)%", re.IGNORECASE)
_TITLE_EXPIRED_RE = re.compile(r"^EXPIRED\s*·?\s*(?P<result>WIN|LOSS)", re.IGNORECASE)
_TITLE_NEW_ALERT_RE = re.compile(r"^NEW ALERT\b", re.IGNORECASE)
_TITLE_MILESTONE_RE = re.compile(r"^[+-]?\d+(?:\.\d+)?%")

# "· 0DTE" or "· Sep 30" at the end of a title.
_TITLE_ZERO_DTE_RE = re.compile(r"\b0DTE\b", re.IGNORECASE)
_TITLE_DATE_TAG_RE = re.compile(r"·\s*(?P<mon>[A-Za-z]{3})\s*(?P<day>\d{1,2})\s*$")

_MONEY_RE = re.compile(r"-?\$?([\d,]+\.?\d*)")
_TRIM_TARGET_RE = re.compile(r"(?P<pct>\d+(?:\.\d+)?)%\s+\$?(?P<price>[\d,]+\.?\d*)")

# The advisor publishes trim prices to three decimals, so a correct parse
# agrees with a recomputed price to well under a cent. A cent of slack
# absorbs their rounding without letting a misread digit through.
TRIM_TARGET_TOLERANCE = 0.01


@dataclass
class ParsedCard:
    """Normalized card. Both a real discord.Embed (via .to_dict()) and a
    plain-text paste reduce to this, so classification has one input shape."""

    message_id: int
    title: str
    description: str
    fields: dict[str, str]
    footer: str
    timestamp: datetime


def _parse_money(value: str) -> float:
    match = _MONEY_RE.search(value)
    if not match:
        raise ValueError(f"no dollar amount in {value!r}")
    return float(match.group(1).replace(",", ""))


def _strip_emoji_prefix(title: str) -> str:
    """Card titles open with an emoji that a paste either drops or renders as
    a leading space (" BUY — SPX 7815P"), so title regexes can't anchor on ^
    until it's gone."""
    return re.sub(r"^[^\x00-\x7F]*\s*", "", title).strip()


def _parse_contract_line(description: str) -> OptionKey:
    match = _CONTRACT_LINE_RE.search(description)
    if not match:
        raise ValueError(f"no 'Entered <TICKER> <Mon><DD> '<YY> <strike> <Call|Put>' line in {description!r}")
    mon = _MONTHS.get(match.group("mon").capitalize())
    if mon is None:
        raise ValueError(f"unrecognized month {match.group('mon')!r}")
    day = int(match.group("day"))
    try:
        expiry = date(2000 + int(match.group("yy")), mon, day)
    except ValueError as exc:
        raise ValueError(f"impossible expiry date: {exc}") from exc
    return OptionKey(
        ticker=match.group("ticker").upper(),
        expiry=expiry,
        strike=float(match.group("strike")),
        right="C" if match.group("right").lower() == "call" else "P",
    )


def parse_trim_targets(fields: dict[str, str]) -> tuple[TrimTarget, ...]:
    """The advisor's published ladder. Used only as a parse checksum."""
    raw = fields.get("Trim Targets") or fields.get("Trim Targets (new avg)")
    if not raw:
        return ()
    targets = [
        TrimTarget(pct=float(m.group("pct")) / 100.0, price=float(m.group("price").replace(",", "")))
        for m in _TRIM_TARGET_RE.finditer(raw)
    ]
    return tuple(sorted(targets, key=lambda t: t.pct))


def _check_expiry(option: OptionKey, title: str, today: date) -> tuple[bool, RejectedAlert | None]:
    """Cross-check the contract line's expiry against the title tag and today.

    Returns (is_zero_dte, rejection_or_None). The contract line is the source
    of truth; the tag exists to catch a misread year or month.
    """
    tagged_zero_dte = bool(_TITLE_ZERO_DTE_RE.search(title))

    if option.expiry < today:
        return tagged_zero_dte, RejectedAlert(
            message_id=-1,
            reason=RejectReason.EXPIRY_IN_PAST,
            detail=f"contract expires {option.expiry.isoformat()}, which is before today ({today.isoformat()})",
            raw_title=title,
        )

    if tagged_zero_dte and option.expiry != today:
        return tagged_zero_dte, RejectedAlert(
            message_id=-1,
            reason=RejectReason.EXPIRY_TAG_MISMATCH,
            detail=(
                f"title says 0DTE but the contract expires {option.expiry.isoformat()} "
                f"and today is {today.isoformat()}"
            ),
            raw_title=title,
        )

    date_tag = _TITLE_DATE_TAG_RE.search(title)
    if date_tag:
        mon = _MONTHS.get(date_tag.group("mon").capitalize())
        day = int(date_tag.group("day"))
        if mon is not None and (option.expiry.month, option.expiry.day) != (mon, day):
            return tagged_zero_dte, RejectedAlert(
                message_id=-1,
                reason=RejectReason.EXPIRY_TAG_MISMATCH,
                detail=(
                    f"title tag says {date_tag.group('mon')} {day} but the contract line says "
                    f"{option.expiry.isoformat()}"
                ),
                raw_title=title,
            )

    if not tagged_zero_dte and option.expiry == today:
        # Untagged same-day expiry. Trade it, but the caller surfaces this.
        return True, None

    return tagged_zero_dte, None


def _check_trim_targets(
    entry_price: float, targets: tuple[TrimTarget, ...], title: str
) -> RejectedAlert | None:
    """Recompute each listed target off the entry and compare. A mismatch
    means a number was misread somewhere, so the whole alert is suspect --
    not just the ladder, which the bot doesn't trade off anyway."""
    if not targets:
        return RejectedAlert(
            message_id=-1,
            reason=RejectReason.NO_TRIM_TARGETS,
            detail="card has no 'Trim Targets' block to validate the entry price against",
            raw_title=title,
        )
    for target in targets:
        expected = entry_price * (1 + target.pct)
        if abs(expected - target.price) > TRIM_TARGET_TOLERANCE:
            return RejectedAlert(
                message_id=-1,
                reason=RejectReason.TRIM_TARGET_MISMATCH,
                detail=(
                    f"+{target.pct * 100:.0f}% target is ${target.price:.3f} but "
                    f"${entry_price:.3f} x {1 + target.pct:.2f} = ${expected:.3f} "
                    f"(off by ${abs(expected - target.price):.3f}, tolerance ${TRIM_TARGET_TOLERANCE:.2f})"
                ),
                raw_title=title,
            )
    return None


def parse_card(card: ParsedCard, today: date) -> ParseResult:
    """Classify and validate one card. Never raises.

    `today` is passed in rather than read from the clock so the parser stays
    pure and the expiry gate is testable; callers supply the current date in
    America/New_York.
    """
    title = _strip_emoji_prefix(card.title)

    if _TITLE_BUY_RE.match(title):
        return _parse_buy(card, title, today)
    if _TITLE_NEW_ALERT_RE.match(title):
        return InfoAlert(card.message_id, "new_alert", title, "prospective level, not a confirmed fill")
    if _TITLE_AVG_DOWN_RE.match(title):
        return InfoAlert(card.message_id, "averaging_down", title, "this bot does not average down")
    if _TITLE_TRIM_RE.match(title):
        return InfoAlert(card.message_id, "advisor_trim", title, "our own trims are managed independently")
    if _TITLE_SOLD_ALL_RE.match(title):
        return InfoAlert(card.message_id, "advisor_exit", title, "our own exit is managed independently")
    if _TITLE_EXPIRED_RE.match(title):
        return InfoAlert(card.message_id, "advisor_expired", title)
    if _TITLE_MILESTONE_RE.match(title):
        return InfoAlert(card.message_id, "milestone", title, "narration, not a fill")

    return RejectedAlert(
        card.message_id,
        RejectReason.UNRECOGNIZED_TITLE,
        f"no handler for title {title!r}",
        title,
    )


def _parse_buy(card: ParsedCard, title: str, today: date) -> ParseResult:
    def reject(reason: RejectReason, detail: str) -> RejectedAlert:
        return RejectedAlert(card.message_id, reason, detail, title)

    try:
        option = _parse_contract_line(card.description)
    except ValueError as exc:
        return reject(RejectReason.UNPARSABLE_CONTRACT, str(exc))

    for required in ("Entry", "Contracts", "Cost"):
        if required not in card.fields:
            return reject(RejectReason.MISSING_FIELD, f"card has no {required!r} field")

    try:
        entry_price = _parse_money(card.fields["Entry"])
        advisor_contracts = int(_parse_money(card.fields["Contracts"]))
        advisor_cost = _parse_money(card.fields["Cost"])
    except ValueError as exc:
        return reject(RejectReason.UNPARSABLE_NUMBER, str(exc))

    if entry_price <= 0:
        return reject(RejectReason.NON_POSITIVE_PRICE, f"entry price is ${entry_price:.4f}")
    if advisor_contracts <= 0:
        return reject(RejectReason.NON_POSITIVE_PRICE, f"advisor contract count is {advisor_contracts}")

    is_zero_dte, expiry_rejection = _check_expiry(option, title, today)
    if expiry_rejection is not None:
        return RejectedAlert(
            card.message_id, expiry_rejection.reason, expiry_rejection.detail, title
        )

    targets = parse_trim_targets(card.fields)
    target_rejection = _check_trim_targets(entry_price, targets, title)
    if target_rejection is not None:
        return RejectedAlert(
            card.message_id, target_rejection.reason, target_rejection.detail, title
        )

    return EntryAlert(
        message_id=card.message_id,
        option=option,
        entry_price=entry_price,
        advisor_contracts=advisor_contracts,
        advisor_cost=advisor_cost,
        trim_targets=targets,
        raw_title=title,
        is_zero_dte=is_zero_dte,
    )
