from __future__ import annotations

import re
from datetime import datetime, timedelta

from bot.parser import ParsedEmbed

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

# First line of a card when the header banner is missing entirely (e.g. a
# single alert pasted on its own). Mirrors bot/parser.py's title patterns.
_BARE_TITLE_RE = re.compile(
    r"^\W*(BUY|TRIM|SOLD ALL|EXPIRED|NEW ALERT|AVERAGING DOWN|[+-]?\d+(?:\.\d+)?%)\b",
    re.IGNORECASE,
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


def parse_chat_export(text: str, reference_date: datetime | None = None) -> list[ParsedEmbed]:
    """Splits a plain-text Discord chat export (copy-pasted from the client,
    not the raw API) into one ParsedEmbed per "SWIFT TRADES · LIVE DESK"
    block, skipping every other line in the export (human chat, polls,
    the plain-text daily recap) since those never contain that marker.

    Also used for live ingestion (bot/main.py) -- the source channel isn't
    ours to get bot API access to, so alerts are manually copy-pasted into
    a relay channel the user does own, arriving as plain message.content
    in exactly this shape. Same parsing logic either way.
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

    # No header banner anywhere, but the paste itself opens with a
    # recognizable alert title -- treat the whole thing as one card rather
    # than discarding it. Covers a single alert copied without its header.
    if not blocks:
        non_empty = [ln for ln in lines if ln.strip()]
        if non_empty and _BARE_TITLE_RE.match(non_empty[0].strip()):
            blocks = [lines]

    embeds: list[ParsedEmbed] = []
    for message_id, block in enumerate(blocks):
        embeds.append(_parse_block(message_id, block, reference_date))
    return embeds


def _parse_block(message_id: int, lines: list[str], reference_date: datetime) -> ParsedEmbed:
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

    return ParsedEmbed(
        message_id=message_id,
        title=title,
        description=description,
        fields=fields,
        footer=footer,
        timestamp=timestamp,
    )
