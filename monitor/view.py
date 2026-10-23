"""Telegram HTML building blocks shared by every page.

Pages are made of short "cards": a <blockquote> with a bold heading and tree lines
(├ / └), the style of well-known Telegram bots. Everything dynamic is escaped on
the way in, and no block contains a blank line, so a long page can be split at
blank lines into several messages without breaking any tag.
"""
from __future__ import annotations

import html
import re
from datetime import datetime, timezone
from typing import Any, Iterable
from zoneinfo import ZoneInfo

PKT = ZoneInfo("Asia/Karachi")
MAX_MESSAGE = 4096

Item = tuple  # (label, value) or (label, value, "code")


def e(value: Any) -> str:
    """Escape any value for Telegram HTML."""
    return html.escape(str(value), quote=False)


def _value(item: Item) -> str:
    return f"<code>{e(item[1])}</code>" if len(item) > 2 and item[2] == "code" else e(item[1])


def tree(items: Iterable[Item | None]) -> list[str]:
    """'├ Label: value' lines, the last one '└'. Items that are None or have no value are left out."""
    kept = [item for item in items if item and item[1] is not None and item[1] != ""]
    return [f"{'└' if index == len(kept) - 1 else '├'} {e(item[0])}: {_value(item)}" for index, item in enumerate(kept)]


def card(heading: str, items: Iterable[Item | None], expandable: bool = False) -> str:
    """A quoted card: bold heading, then tree lines. Empty when no item has a value."""
    lines = tree(items)
    return quote([f"<b>{heading}</b>", *lines], expandable) if lines else ""


def grouped_card(heading: str, groups: Iterable[tuple[str, Iterable[Item | None]]], expandable: bool = False) -> str:
    """One card with several titled groups, e.g. a container's Runtime and Environment."""
    lines = [f"<b>{heading}</b>"]
    for title, items in groups:
        rows = tree(items)
        if rows:
            lines += [title, *rows]
    return quote(lines, expandable) if len(lines) > 1 else ""


def quote(lines: list[str], expandable: bool = False) -> str:
    """Lines are already HTML. Expandable quotes start collapsed (for long lists)."""
    body = "\n".join(line for line in lines if line)
    return f"<blockquote expandable>{body}</blockquote>" if expandable else f"<blockquote>{body}</blockquote>"


class Page:
    """A message: a bold title with an optional italic subtitle, then blocks separated by blank lines."""

    def __init__(self, icon: str, title: str, subtitle: str | None = None):
        self.blocks = [f"{icon} <b>{e(title)}</b>" + (f"\n<i>{e(subtitle)}</i>" if subtitle else "")]

    def add(self, block: str | None) -> "Page":
        if block:
            self.blocks.append(block)
        return self

    def footer(self, now: datetime | None = None) -> "Page":
        stamp = (now or datetime.now(timezone.utc)).astimezone(PKT)
        return self.add(f"<i>🕒 Checked {stamp.strftime('%I:%M %p').lstrip('0')} PKT</i>")

    def html(self) -> str:
        return "\n\n".join(self.blocks)


def ago(seconds: int | None) -> str | None:
    if seconds is None:
        return None
    if seconds < 120:
        return f"{seconds}s ago"
    if seconds < 7200:
        return f"{seconds // 60}m ago"
    return f"{seconds // 3600}h {seconds % 3600 // 60}m ago"


def number(value: Any) -> str | None:
    return f"{value:,}" if isinstance(value, int) else (None if value is None else str(value))


def memory_size(value: float) -> str:
    """Bytes as `docker stats`, `free -h` and `df -h` count them (1 GB = 1024³ bytes)."""
    if value >= 1024 ** 4:
        return f"{value / 1024 ** 4:.1f} TB"
    if value >= 1024 ** 3:
        return f"{value / 1024 ** 3:.1f} GB"
    if value >= 1024 ** 2:
        return f"{value / 1024 ** 2:.0f} MB"
    return f"{value / 1024:.0f} KB"  # small sizes would otherwise read as "0 MB"


def split_html(text: str, limit: int = MAX_MESSAGE) -> list[str]:
    """Split a page into messages at blank lines; blocks never contain one, so tags stay whole."""
    if len(text) <= limit:
        return [text]
    parts: list[str] = []
    for block in text.split("\n\n"):
        if parts and len(parts[-1]) + 2 + len(block) <= limit:
            parts[-1] += "\n\n" + block
        else:
            parts.append(block)
    return parts


# --- Logs: the dark code box ----------------------------------------------------------

STAMP = re.compile(r"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2}) ")
LONG_LINE = 1500


def log_text(line: str, now: datetime | None = None) -> str:
    """One log line for the code box: Docker's UTC stamp shown as short PKT time, text as logged.

    Lines from an earlier day keep their date. Lines without a Docker stamp are left as they are.
    """
    match = STAMP.match(line)
    try:
        stamp = datetime.fromisoformat(match.group(1)).replace(tzinfo=timezone.utc).astimezone(PKT) if match else None
    except ValueError:
        stamp = None
    if stamp is None:
        return line.rstrip()
    today = (now or datetime.now(timezone.utc)).astimezone(PKT).date()
    when = stamp.strftime("%H:%M:%S") if stamp.date() == today else stamp.strftime("%d %b %H:%M:%S")
    return f"{when}  {line[match.end():].rstrip()}"


def log_lines(raw: str, now: datetime | None = None) -> list[str]:
    return [log_text(line, now) for line in (raw or "").splitlines() if line.strip()]


def code_box(lines: list[str]) -> str:
    return "<pre>" + e("\n".join(lines)) + "</pre>"


def newest_lines_that_fit(lines: list[str], room: int) -> list[str]:
    """The newest whole lines whose code box fits in `room` characters, oldest first."""
    kept: list[str] = []
    used = len("<pre></pre>")
    for line in reversed(lines):
        cost = len(e(line)) + (1 if kept else 0)
        if used + cost > room:
            break
        kept.insert(0, line)
        used += cost
    return kept


def log_messages(title_html: str, lines: list[str], limit: int = MAX_MESSAGE) -> list[str]:
    """Lines packed into as few messages as needed, each with its own code box. Nothing is cut:
    a single line too long for one message is split across code boxes, never dropped."""
    pieces: list[str] = []
    for line in lines:
        pieces += [line[start:start + LONG_LINE] for start in range(0, len(line), LONG_LINE)] or [""]
    messages: list[str] = []
    current: list[str] = []
    header = title_html + "\n\n"
    for piece in pieces:
        candidate = current + [piece]
        if current and len((header if not messages else "") + code_box(candidate)) > limit:
            messages.append((header if not messages else "") + code_box(current))
            current = [piece]
        else:
            current = candidate
    if current or not messages:
        messages.append((header if not messages else "") + code_box(current or ["No recent logs."]))
    return messages
