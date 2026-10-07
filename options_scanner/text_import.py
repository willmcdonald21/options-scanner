from __future__ import annotations

import re
from datetime import datetime, timedelta

from options_scanner.parser import ParsedCard

# Header lines that start a new alert card. BUY cards carry the "SWIFT
# TRADES · LIVE DESK" banner; TRIM / SOLD ALL cards as copied out of the
# client are headed "Analyst - SWIFT" instead and carry no banner at all,
# so splitting on the banner alone silently dropped every exit message.
_BLOCK_MARKERS = ("SWIFT TRADES · LIVE DESK", "Analyst - SWIFT")
_DASHBOARD_LINE = "Open Live Dashboard"
_FOOTER_MARKER = "financial advice"

# Fields whose value is the single following line.
_KNOWN_SIMPLE_FIELDS = {"Entry", "Contracts", "Cost", "New Avg", "Total Contracts", "Entry → avg exit"}

# Fields whose value spans every following line until the next field name
# or a blank line. "Trim Targets" is the exit ladder we rest at the broker;
# "(new avg)" is the same field after an averaging-down.
_KNOWN_MULTILINE_FIELDS = {"Trim Targets", "Trim Targets (new avg)"}

_ALL_FIELD_NAMES = _KNOWN_SIMPLE_FIELDS | _KNOWN_MULTILINE_FIELDS | {"News backdrop", "Realized", "Best fill", "Peak", "Locked In", "Exit"}

_TITLE_KEYWORDS = r"BUY|TRIM|SOLD ALL|EXPIRED|NEW ALERT|AVERAGING DOWN|[+-]?\d+(?:\.\d+)?%"

# Loose check: does this line look like the start of a card at all? Used only
# to decide whether a banner-less paste is an alert or ordinary chatter.
_BARE_TITLE_RE = re.compile(rf"^\W*({_TITLE_KEYWORDS})\b", re.IGNORECASE)

# Strict check, used to *split* a banner-less paste into cards: the line must
# carry both a title keyword and the contract shorthand every real title has
# ("— SPX 7815P"). The loose pattern alone cannot do this job, because a
# "Trim Targets" row like "100%  $3.810" matches the percentage branch and
# would cut a card in half at its own ladder.
_CARD_TITLE_RE = re.compile(
    rf"^\W*(?:{_TITLE_KEYWORDS})\b.*?[A-Z]{{1,6}}\s+\d+(?:\.\d+)?[CP]\b"
)

_EXPLICIT_TS_RE = re.compile(r"(\d{1,2})/(\d{1,2})/(\d{2}),\s*(\d{1,2}):(\d{2})\s*(AM|PM)")
_RELATIVE_TS_RE = re.compile(r"(Today|Yesterday) at (\d{1,2}):(\d{2})\s*(AM|PM)")


def _parse_timestamp(text: str, reference_date: datetime) -> datetime:
    match = _EXPLICIT_TS_RE.search(text)
    if match:
        mo, day, yy, hh, mm, ap = match.groups()
        hour = int(hh) % 12 + (12 if ap == "PM" else 0)
        return datetime(2000 + int(yy), int(mo), int(day), hour, int(mm))
    match = _RELATIVE_TS_RE.search(text)
    if match:
        which, hh, mm, ap = match.groups()
        base = reference_date if which == "Today" else reference_date - timedelta(days=1)
        hour = int(hh) % 12 + (12 if ap == "PM" else 0)
        return base.replace(hour=hour, minute=int(mm), second=0, microsecond=0)
    return reference_date


def parse_cards(text: str, reference_date: datetime | None = None) -> list[ParsedCard]:
    """Split a pasted Discord message into one ParsedCard per alert card.

    Cards are delimited by a header banner, but exit cards copied out of the
    client carry a different banner from entry cards and a single alert
    pasted on its own may carry none at all, so there are three strategies
    in descending order of confidence: a known banner, then a recognizable
    alert title on the first line, then nothing.

    Returns [] when no card is found. Callers must treat that as something to
    report, not something to ignore -- a dropped paste and an unreadable
    paste look identical to the user otherwise.
    """
    reference_date = reference_date or datetime.now()
    lines = text.splitlines()
    blocks: list[list[str]] = []
    current: list[str] | None = None
    for line in lines:
        if any(marker in line for marker in _BLOCK_MARKERS):
            if current is not None:
                blocks.append(current)
            current = []
            continue
        if current is not None:
            current.append(line)
    if current is not None:
        blocks.append(current)

    # No banner anywhere. Fall back to splitting on the title lines
    # themselves, so several banner-less cards in one paste still come apart
    # instead of being merged into one unreadable block.
    if not blocks:
        blocks = _split_on_titles(lines)

    # Still nothing, but the paste opens with something that looks like a
    # card: take it whole rather than discard it. Covers a title shape the
    # strict splitter does not recognize yet.
    if not blocks:
        non_empty = [ln for ln in lines if ln.strip()]
        if non_empty and _BARE_TITLE_RE.match(non_empty[0].strip()):
            blocks = [lines]

    embeds: list[ParsedCard] = []
    for message_id, block in enumerate(blocks):
        embeds.append(_parse_block(message_id, block, reference_date))
    return embeds


def _split_on_titles(lines: list[str]) -> list[list[str]]:
    """Cut a banner-less paste at each card title. The title line stays as the
    first line of its own block, which is where _parse_block expects it."""
    blocks: list[list[str]] = []
    current: list[str] | None = None
    for line in lines:
        if _CARD_TITLE_RE.match(line):
            if current is not None:
                blocks.append(current)
            current = [line]
            continue
        if current is not None:
            current.append(line)
    if current is not None:
        blocks.append(current)
    return blocks


def _parse_block(message_id: int, lines: list[str], reference_date: datetime) -> ParsedCard:
    non_empty = [ln for ln in lines if ln.strip()]
    title = non_empty[0].strip() if non_empty else ""

    dashboard_idx = next((i for i, ln in enumerate(lines) if _DASHBOARD_LINE in ln), None)
    footer_idx = next((i for i, ln in enumerate(lines) if _FOOTER_MARKER in ln), len(lines))

    description_lines = lines[1:dashboard_idx] if dashboard_idx is not None else lines[1:footer_idx]
    description = "\n".join(ln for ln in description_lines if ln.strip())

    fields: dict[str, str] = {}
    if dashboard_idx is not None:
        field_lines = lines[dashboard_idx + 1 : footer_idx]
        i = 0
        while i < len(field_lines):
            name = field_lines[i].strip()
            if name in _KNOWN_MULTILINE_FIELDS:
                value_lines: list[str] = []
                j = i + 1
                while j < len(field_lines):
                    candidate = field_lines[j].strip()
                    if not candidate or candidate in _ALL_FIELD_NAMES:
                        break
                    value_lines.append(candidate)
                    j += 1
                if value_lines:
                    fields[name] = "\n".join(value_lines)
                    i = j
                    continue
            if name in _KNOWN_SIMPLE_FIELDS and i + 1 < len(field_lines):
                value = field_lines[i + 1].strip()
                if value:
                    fields[name] = value
                    i += 2
                    continue
            i += 1

    footer = lines[footer_idx].split("•")[0].strip() if footer_idx < len(lines) else ""
    timestamp = _parse_timestamp(lines[footer_idx], reference_date) if footer_idx < len(lines) else reference_date

    return ParsedCard(
        message_id=message_id,
        title=title,
        description=description,
        fields=fields,
        footer=footer,
        timestamp=timestamp,
    )
