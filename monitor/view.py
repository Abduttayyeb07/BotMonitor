"""Telegram HTML building blocks shared by every page: panels, log entries, splitting.

Everything dynamic is escaped on the way in, and every line closes its own tags,
so text can be split at any line break and still be valid Telegram HTML.
"""
from __future__ import annotations

import html
import re
from datetime import datetime, timezone
from typing import Any, Iterable
from zoneinfo import ZoneInfo

PKT = ZoneInfo("Asia/Karachi")
MAX_MESSAGE = 4096
RULE = "━━━━━━━━━━━━━━━━"
THIN_RULE = "┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄"


def e(value: Any) -> str:
    """Escape any value for Telegram HTML."""
    return html.escape(str(value), quote=False)


Item = tuple  # (label, value) or (label, value, "code")


class Panel:
    """A message built block by block: a titled header, then sections separated by blank lines."""

    def __init__(self, icon: str, name: str):
        self.blocks = [f"{icon} <b>{e(name)}</b>\n{RULE}"]

    def add(self, html_block: str | None) -> "Panel":
        """Append a block that is already valid HTML (callers escape their own values)."""
        if html_block:
            self.blocks.append(html_block)
        return self

    def section(self, heading: str, items: Iterable[Item | None]) -> "Panel":
        """'<b>Heading</b>' and one '• Label: value' line per item; items without a value are skipped."""
        return self.add(section(heading, items))

    def footer(self, now: datetime | None = None) -> "Panel":
        stamp = (now or datetime.now(timezone.utc)).astimezone(PKT)
        return self.add(f"<i>Last checked: {stamp.strftime('%I:%M %p').lstrip('0')} PKT</i>")

    def html(self) -> str:
        return "\n\n".join(self.blocks)


def section(heading: str, items: Iterable[Item | None]) -> str:
    lines = []
    for item in items:
        if not item or item[1] is None or item[1] == "":
            continue
        value = f"<code>{e(item[1])}</code>" if len(item) > 2 and item[2] == "code" else e(item[1])
        lines.append(f"• {e(item[0])}: {value}")
    return f"<b>{e(heading)}</b>\n" + "\n".join(lines) if lines else ""


def ago(seconds: int | None) -> str | None:
    if seconds is None:
        return None
    if seconds < 120:
        return f"{seconds}s ago"
    if seconds < 7200:
        return f"{seconds // 60}m ago"
    return f"{seconds // 3600}h {seconds % 3600 // 60}m ago"


def memory_size(value: float) -> str:
    """Bytes as `docker stats` shows them (1 MB = 1024 * 1024 bytes)."""
    return f"{value / 1024 ** 3:.1f} GB" if value >= 1024 ** 3 else f"{value / 1024 ** 2:.0f} MB"


def number(value: Any) -> str | None:
    return f"{value:,}" if isinstance(value, int) else (None if value is None else str(value))


def split_html(text: str, limit: int = MAX_MESSAGE) -> list[str]:
    """Split a message at blank lines (then single lines) so every part fits Telegram's limit."""
    if len(text) <= limit:
        return [text]
    parts: list[str] = []
    current = ""
    for block in text.split("\n\n"):
        pieces = [block] if len(block) <= limit else _split_lines(block, limit)
        for piece in pieces:
            if current and len(current) + 2 + len(piece) > limit:
                parts.append(current)
                current = piece
            else:
                current = f"{current}\n\n{piece}" if current else piece
    if current:
        parts.append(current)
    return parts


def _split_lines(block: str, limit: int) -> list[str]:
    pieces, current = [], ""
    for line in block.split("\n"):
        if current and len(current) + 1 + len(line) > limit:
            pieces.append(current)
            current = line
        else:
            current = f"{current}\n{line}" if current else line
    return pieces + ([current] if current else [])


# --- Log entries --------------------------------------------------------------------
#
# Docker prefixes every line with an RFC 3339 timestamp. After it, only structure that
# cannot be misread is pulled out: leading [TAG]s, and segments separated by ' | ' or
# '; ', or key=value pairs after ' — '. Anything else is shown exactly as logged.

STAMP = re.compile(r"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2}) ")
APP_STAMP = re.compile(r"^\[\d{4}-\d{2}-\d{2}T[^\]]*\]\s*")
TAG = re.compile(r"^\[([^\[\]\n]{1,40})\]\s*")
# Keys are plain names (latest, backlog, 24hVol); anything with quotes, braces or colons is not a key.
PAIR_KEY = re.compile(r"(?:^|\s)([A-Za-z0-9_][\w.\-/]*)=")
PAIR_START = re.compile(r"^[A-Za-z0-9_][\w.\-/]*=")
RAW_PIECE = 3000


def _pairs(text: str) -> list[tuple[str, str]]:
    """'largest buy this cycle=5.5K ZIG threshold=1.0M ZIG' -> [('largest buy this cycle', '5.5K ZIG'), ...].

    The first key may be several words (everything before the first '='); later keys are single tokens.
    """
    keys = list(PAIR_KEY.finditer(text))
    if not keys:
        return []
    pairs = []
    for index, match in enumerate(keys):
        key = text[:match.end() - 1].strip() if index == 0 else match.group(1)
        end = keys[index + 1].start() if index + 1 < len(keys) else len(text)
        pairs.append((key, text[match.end():end].strip()))
    return pairs


def _segment_line(segment: str) -> str:
    if PAIR_START.match(segment):
        key, _, value = segment.partition("=")
        return f"• {e(key[:1].upper() + key[1:])}: {e(value) if value else '(empty)'}"
    return f"• {e(segment)}"


def _raw(text: str) -> list[str]:
    # Cut before escaping so an entity like &amp; is never split.
    return [f"<code>{e(text[start:start + RAW_PIECE])}</code>" for start in range(0, len(text), RAW_PIECE)] or ["<code></code>"]


def format_log_line(line: str) -> str:
    stamp_match = STAMP.match(line)
    try:
        stamp = datetime.fromisoformat(stamp_match.group(1)).replace(tzinfo=timezone.utc) if stamp_match else None
    except ValueError:
        stamp = None
    if stamp is None:
        return "\n".join(_raw(line))
    rest = APP_STAMP.sub("", line[stamp_match.end():].strip(), count=1)
    tags = []
    while (tag := TAG.match(rest)):
        tags.append(tag.group(1))
        rest = rest[tag.end():]
    out = [f"🕒 <i>{stamp.astimezone(PKT).strftime('%d %b %Y · %H:%M:%S')} PKT</i>"]
    if len(rest) > RAW_PIECE:
        # A huge single line (a dumped payload or stack): keep the time and tags, show the text raw.
        if tags:
            out.append(" ".join(f"<b>{e(tag)}</b>" for tag in tags))
        return "\n".join(out + _raw(rest))
    separator = " | " if " | " in rest else "; " if "; " in rest and "=" in rest else None
    if separator:
        headline, *segments = [part.strip() for part in rest.split(separator)]
        bullets = [_segment_line(segment) for segment in segments if segment]
    elif " — " in rest and "=" in rest.split(" — ", 1)[1]:
        headline, tail = rest.split(" — ", 1)
        bullets = [f"• {e(key[:1].upper() + key[1:])}: {e(value) if value else '(empty)'}" for key, value in _pairs(tail)]
    elif PAIR_START.match(rest):
        headline = ""
        bullets = [f"• {e(key[:1].upper() + key[1:])}: {e(value) if value else '(empty)'}" for key, value in _pairs(rest)]
    else:
        headline, bullets = rest, []
    title_parts = [" ".join(f"<b>{e(tag)}</b>" for tag in tags), e(headline.strip())]
    title = " · ".join(part for part in title_parts if part)
    return "\n".join(out + ([title] if title else []) + bullets)


def log_entries(raw: str) -> list[str]:
    return [format_log_line(line) for line in (raw or "").splitlines() if line.strip()]


def newest_that_fit(entries: list[str], room: int) -> list[str]:
    """The newest whole entries that fit in `room` characters, oldest first. Entries are never cut."""
    kept, used = [], 0
    for entry in reversed(entries):
        cost = len(entry) + (2 if kept else 0)
        if used + cost > room:
            break
        kept.insert(0, entry)
        used += cost
    return kept
