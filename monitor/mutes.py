"""Per-project mutes (🔕 Mute 1h), stored in SQLite so they survive restarts.

While a project is muted its incidents are still tracked, but no Telegram message
is sent. An incident still open when the mute ends is alerted then.
"""
from __future__ import annotations

import sqlite3
from contextlib import closing
from datetime import datetime, timedelta, timezone

MUTE_CHOICES = ((3600, "1h"), (4 * 3600, "4h"), (24 * 3600, "24h"))


def _connect(database_path: str) -> sqlite3.Connection:
    database = sqlite3.connect(database_path, timeout=10)
    database.execute("CREATE TABLE IF NOT EXISTS mutes (project TEXT PRIMARY KEY, until TEXT NOT NULL, label TEXT NOT NULL)")
    return database


def mute(database_path: str, project: str, seconds: int, label: str, now: datetime | None = None) -> datetime:
    until = (now or datetime.now(timezone.utc)) + timedelta(seconds=seconds)
    with closing(_connect(database_path)) as database:
        database.execute("INSERT OR REPLACE INTO mutes (project, until, label) VALUES (?, ?, ?)", (project, until.isoformat(), label))
        database.commit()
    return until


def unmute(database_path: str, project: str) -> None:
    with closing(_connect(database_path)) as database:
        database.execute("DELETE FROM mutes WHERE project=?", (project,))
        database.commit()


def active_mutes(database_path: str, now: datetime | None = None) -> dict[str, dict]:
    """project -> {'until': datetime, 'label': str} for mutes that have not ended yet."""
    now = now or datetime.now(timezone.utc)
    with closing(_connect(database_path)) as database:
        rows = database.execute("SELECT project, until, label FROM mutes").fetchall()
    mutes = {}
    for project, until, label in rows:
        ends = datetime.fromisoformat(until)
        if ends > now:
            mutes[project] = {"until": ends, "label": label}
    return mutes
