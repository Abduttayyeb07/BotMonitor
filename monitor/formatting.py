"""Shared Telegram (parse_mode=HTML) layout helpers: titles, sections, logs, splitting.

Every dynamic value is escaped here, and every rendered line closes its own tags,
so a message can be split at any line break without breaking Telegram's HTML.
"""
from __future__ import annotations

import html
import re
from datetime import datetime, timezone
from typing import Any, Iterable
from zoneinfo import ZoneInfo

PKT = ZoneInfo("Asia/Karachi")
TELEGRAM_LIMIT = 4096
DIVIDER = "━━━━━━━━━━━━━━━━"
EMPTY_VALUE = "(empty)"


def esc(value: Any) -> str:
    return html.escape(str(value), quote=False)


def title(icon: str, text: str) -> str:
    return f"{icon} <b>{esc(text)}</b>\n{DIVIDER}"


def section(heading: str, rows: Iterable[tuple[str, Any] | tuple[str, Any, bool] | None]) -> str:
    """'<b>Heading</b>' then one '• Label: value' line per row.

    A row is (label, value) or (label, value, as_code). Rows that are None or have a
    None value are left out, and a section with no rows renders as an empty string.
    """
    lines = []
    for row in rows:
        if not row or row[1] is None:
            continue
        label, value = row[0], row[1]
        rendered = f"<code>{esc(value)}</code>" if len(row) > 2 and row[2] else esc(value)
        lines.append(f"• {esc(label)}: {rendered}")
    return f"<b>{esc(heading)}</b>\n" + "\n".join(lines) if lines else ""


def join_blocks(*blocks: str) -> str:
    """Join non-empty blocks with one blank line between them."""
    return "\n\n".join(block for block in blocks if block)


def checked_footer(now: datetime | None = None) -> str:
    now = now or datetime.now(timezone.utc)
    return f"<i>Last checked: {now.astimezone(PKT).strftime('%I:%M %p PKT').lstrip('0')}</i>"


def split_message(blocks: list[str], limit: int = TELEGRAM_LIMIT, separator: str = "\n\n") -> list[str]:
    """Pack blocks into as few messages as possible, each within Telegram's limit.

    Blocks are never cut; a block that is longer than the limit on its own is split
    at its line breaks (each rendered line is self-contained HTML).
    """
    pieces: list[str] = []
    for block in blocks:
        if len(block) <= limit:
            pieces.append(block)
            continue
        current = ""
        for line in block.split("\n"):
            if current and len(current) + 1 + len(line) > limit:
                pieces.append(current)
                current = line
            else:
                current = f"{current}\n{line}" if current else line
        if current:
            pieces.append(current)
    messages: list[str] = []
    for piece in pieces:
        if messages and len(messages[-1]) + len(separator) + len(piece) <= limit:
            messages[-1] += separator + piece
        else:
            messages.append(piece)
    return messages


# --- Logs -------------------------------------------------------------------------

DOCKER_STAMP = re.compile(r"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})\s?")
INNER_STAMP = re.compile(r"^\[\d{4}-\d{2}-\d{2}T[\d:.]+(?:Z|[+-]\d{2}:?\d{2})?\]\s*")
LEADING_TAGS = re.compile(r"^((?:\[[^\[\]\n]{1,40}\]\s*)+)")
# key=value where the key is one token (letters/digits, at least one letter) after whitespace.
KEY_VALUE = re.compile(r"(?:(?<=\s)|^)([A-Za-z0-9][\w.\-/]*)=")
# Separators that end a headline and may start a multi-word first key: "monitoring — largest buy=".
HEADLINE_BREAK = re.compile(r"\s[—–|;,]\s?|:\s|;\s")
WORDS_ONLY = re.compile(r"^[A-Za-z][A-Za-z0-9 ]{0,39}$")
RAW_CHUNK = 3000


def parse_log_line(line: str) -> dict[str, Any]:
    """Split a Docker log line into time, tags, headline and metrics.

    Only structure that is unambiguous is extracted: leading [TAG]s, ' | '-separated
    segments, and key=value pairs. Anything else stays in the headline as written,
    and a line without a Docker timestamp is returned as raw text.
    """
    match = DOCKER_STAMP.match(line)
    if not match:
        return {"raw": line}
    try:
        stamp = datetime.fromisoformat(match.group(1)).replace(tzinfo=timezone.utc)
    except ValueError:
        return {"raw": line}
    rest = INNER_STAMP.sub("", line[match.end():].strip(), count=1)
    tags_match = LEADING_TAGS.match(rest)
    tags = re.findall(r"\[([^\[\]]+)\]", tags_match.group(1)) if tags_match else []
    rest = rest[tags_match.end():].strip() if tags_match else rest
    headline, metrics = rest, []

    if " | " in rest:
        segments = [segment.strip() for segment in rest.split(" | ")]
        headline = segments[0]
        for segment in segments[1:]:
            pair = KEY_VALUE.match(segment)
            if pair:
                metrics.append((pair.group(1), segment[pair.end():].strip()))
            elif ": " in segment:
                key, _, value = segment.partition(": ")
                metrics.append((key.strip(), value.strip()))
            else:
                metrics.append((None, segment))
    else:
        pairs = [pair for pair in KEY_VALUE.finditer(rest) if re.search(r"[A-Za-z]", pair.group(1))]
        if pairs:
            prefix = rest[:pairs[0].start()]
            first_key = pairs[0].group(1)
            breaks = list(HEADLINE_BREAK.finditer(prefix))
            headline = prefix.strip(" ;,")
            if breaks:
                extension = prefix[breaks[-1].end():].strip()
                if not extension:
                    headline = prefix[:breaks[-1].start()].strip()
                elif WORDS_ONLY.match(extension):
                    headline = prefix[:breaks[-1].start()].strip()
                    first_key = f"{extension} {first_key}"
            for index, pair in enumerate(pairs):
                last = index + 1 == len(pairs)
                value = rest[pair.end():len(rest) if last else pairs[index + 1].start()]
                # A ';' or ',' before the next key is a separator; the last value is kept exactly.
                metrics.append((first_key if index == 0 else pair.group(1), value.strip() if last else value.strip(" ;,")))
    return {"time": stamp, "tags": tags, "headline": headline, "metrics": metrics, "message": line[match.end():].strip()}


def _label(key: str) -> str:
    return key[:1].upper() + key[1:] if key[:1].isalpha() else key


def _raw_blocks(text: str) -> list[str]:
    # Escaping happens after chunking so an HTML entity is never cut in half.
    chunks = [text[start:start + RAW_CHUNK] for start in range(0, len(text), RAW_CHUNK)] or [""]
    return [f"<code>{esc(chunk)}</code>" for chunk in chunks]


def render_log_entry(line: str) -> str:
    entry = parse_log_line(line)
    if "raw" in entry:
        return "\n".join(_raw_blocks(entry["raw"]))
    lines = [f"🕒 <i>{entry['time'].astimezone(PKT).strftime('%d %b · %H:%M:%S')}</i>"]
    head = " · ".join(part for part in (
        " ".join(f"<b>{esc(tag)}</b>" for tag in entry["tags"]),
        esc(entry["headline"]) if entry["headline"] else "",
    ) if part)
    if head:
        lines.append(head)
    for key, value in entry["metrics"]:
        value_text = esc(value) if value else EMPTY_VALUE
        lines.append(f"• {esc(_label(key))}: {value_text}" if key else f"• {value_text}")
    if any(len(line) > RAW_CHUNK for line in lines):
        # A huge single line (e.g. a dumped payload): keep the time, show the text raw in safe chunks.
        return "\n".join([lines[0], *_raw_blocks(entry["message"])])
    return "\n".join(lines)


def log_lines(raw: str) -> list[str]:
    return [line for line in (raw or "").splitlines() if line.strip()]


def render_logs(raw: str, budget: int) -> tuple[str, int, int]:
    """Latest log entries that fit in `budget` characters, oldest first.

    Returns (html, shown, total). Entries are dropped whole, from the oldest, never cut.
    """
    entries = [render_log_entry(line) for line in log_lines(raw)]
    kept: list[str] = []
    used = 0
    for rendered in reversed(entries):
        cost = len(rendered) + (2 if kept else 0)
        if used + cost > budget:
            break
        kept.append(rendered)
        used += cost
    kept.reverse()
    return "\n\n".join(kept), len(kept), len(entries)
