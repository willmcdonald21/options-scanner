from __future__ import annotations

import re
from datetime import datetime, timedelta

from bot.parser import ParsedEmbed

_BLOCK_MARKER = "SWIFT TRADES · LIVE DESK"
_DASHBOARD_LINE = "Open Live Dashboard"
_FOOTER_MARKER = "financial advice"

# Only fields the parser actually reads off ParsedEmbed.fields; every other
# field (Trim Targets, News backdrop, Realized, Best fill, Peak, ...) is
# real but unused by parse_embed(), so it's fine to skip capturing it here.
_KNOWN_SIMPLE_FIELDS = {"Entry", "Contracts", "Cost", "New Avg", "Total Contracts", "Entry → avg exit"}

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

    This exists to validate/backtest the parser against real historical
    chat logs. Production ingestion should prefer real discord.Embed
    objects (message.embeds[i].to_dict()) via the live bot instead, since
    field names are then keyed directly rather than re-derived from
    rendered text -- but the two normalize into the same ParsedEmbed shape
    parse_embed() consumes, so the parsing logic itself is identical either way.
    """
    reference_date = reference_date or datetime.now()
    lines = text.splitlines()
    blocks: list[list[str]] = []
    current: list[str] | None = None
    for line in lines:
        if _BLOCK_MARKER in line:
            if current is not None:
                blocks.append(current)
            current = []
            continue
        if current is not None:
            current.append(line)
    if current is not None:
        blocks.append(current)

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
