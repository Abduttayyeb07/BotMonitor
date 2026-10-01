from __future__ import annotations

import hashlib
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def normalize(message: str) -> str:
    value = message.lower().strip()
    value = re.sub(r"\bblocks?\s+\d+(?:\s*[-–]\s*\d+)?\b", "block <range>", value)
    value = re.sub(r"\bblock(?:height|number)?[=: ]+\d+\b", "block <number>", value)
    value = re.sub(r"\b[0-9a-f]{8}-[0-9a-f-]{27,}\b", "<uuid>", value)
    value = re.sub(r"\b0x[0-9a-f]+\b", "<hex>", value)
    value = re.sub(r"\b\d{10,}\b", "<number>", value)
    value = re.sub(r"\b\d+\s*[-–]\s*\d+\b", "<range>", value)
    value = re.sub(r"\b\d{1,3}(?:\.\d{1,3}){3}\b", "<ip>", value)
    return re.sub(r"\s+", " ", value)


def fingerprint(project: str, service: str, incident_type: str, message: str) -> str:
    if incident_type in {'CONTAINER_DOWN', 'CONTAINER_UNHEALTHY', 'PROJECT_DOWN', 'SYSTEMD_DOWN'}:
        message = incident_type
    raw = "|".join((project, service, incident_type, normalize(message)))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


class IncidentStore:
    def __init__(self, database_path: str):
        Path(database_path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(database_path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute("""
            CREATE TABLE IF NOT EXISTS incidents (
              fingerprint TEXT PRIMARY KEY, project TEXT NOT NULL, service TEXT NOT NULL,
              incident_type TEXT NOT NULL, severity TEXT NOT NULL, message TEXT NOT NULL,
              first_seen TEXT NOT NULL, last_seen TEXT NOT NULL, occurrences INTEGER NOT NULL,
              status TEXT NOT NULL, last_alert_at TEXT, resolved_at TEXT
            )
        """)
        self.db.execute('CREATE TABLE IF NOT EXISTS recovery_queue (fingerprint TEXT PRIMARY KEY, payload TEXT NOT NULL)')
        self.db.commit()

    def observe(self, key: str, project: str, service: str, incident_type: str,
                severity: str, message: str, cooldown_seconds: int) -> tuple[dict[str, Any], bool]:
        current = datetime.now(timezone.utc)
        row = self.db.execute("SELECT * FROM incidents WHERE fingerprint = ?", (key,)).fetchone()
        if row is None:
            stamp = current.isoformat()
            self.db.execute("INSERT INTO incidents VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                             (key, project, service, incident_type, severity, message, stamp,
                              stamp, 1, "OPEN", None, None))
            self.db.commit()
            return dict(self.db.execute("SELECT * FROM incidents WHERE fingerprint = ?", (key,)).fetchone()), True

        # One notification per incident lifecycle. Repeated observations stay
        # active silently until recovery, preventing reminder spam.
        should_alert = row["status"] == "RECOVERED" or row['last_alert_at'] is None
        self.db.execute("""UPDATE incidents SET last_seen=?, occurrences=occurrences+1,
                           status='OPEN', message=?, resolved_at=NULL,
                           last_alert_at=CASE WHEN ? THEN NULL ELSE last_alert_at END
                           WHERE fingerprint=?""",
                        (current.isoformat(), message, int(row['status'] == 'RECOVERED'), key))
        self.db.commit()
        return dict(self.db.execute("SELECT * FROM incidents WHERE fingerprint = ?", (key,)).fetchone()), should_alert

    def mark_alert_sent(self, key: str) -> None:
        self.db.execute('UPDATE incidents SET last_alert_at=? WHERE fingerprint=?', (now(), key))
        self.db.commit()

    def recover_stale(self, active_keys: set[str], protected_services: set[str] | None = None) -> list[dict[str, Any]]:
        rows = self.db.execute("SELECT * FROM incidents WHERE status='OPEN'").fetchall()
        recovered = []
        for row in rows:
            if row["fingerprint"] not in active_keys and row['service'] not in (protected_services or set()):
                stamp = now()
                self.db.execute("UPDATE incidents SET status='RECOVERED', resolved_at=? WHERE fingerprint=?",
                                 (stamp, row["fingerprint"]))
                recovered_row = dict(row)
                recovered_row["resolved_at"] = stamp
                recovered_row['status'] = 'RECOVERED'
                recovered.append(recovered_row)
        if recovered:
            self.db.commit()
        return recovered
