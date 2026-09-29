#!/usr/bin/env python3
"""Small host-side systemd collector for the Dockerized central monitor."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

UNITS = ("wallet-watchman.service", "zigchain-exporter.service")
OUTPUT = Path(os.getenv("MONITOR_STATUS_FILE", "/root/saad/BotMonitor/data/systemd-status.json"))
INTERVAL = int(os.getenv("MONITOR_SYSTEMD_INTERVAL", "15"))
running = True


def stop(_signum: int, _frame: object) -> None:
    global running
    running = False


def collect_unit(unit: str) -> dict[str, str]:
    result = subprocess.run(
        ["systemctl", "show", unit, "--property=ActiveState,SubState,ActiveEnterTimestamp,ExecMainStatus"],
        capture_output=True, text=True, timeout=10,
    )
    values = {}
    for line in result.stdout.splitlines():
        if "=" in line:
            key, value = line.split("=", 1)
            values[key] = value
    logs = subprocess.run(
        ["journalctl", "-u", unit, "-n", "12", "--no-pager", "-o", "short-iso"],
        capture_output=True, text=True, timeout=10,
    )
    return {
        "unit": unit,
        "active_state": values.get("ActiveState", "unknown"),
        "sub_state": values.get("SubState", "unknown"),
        "active_since": values.get("ActiveEnterTimestamp", ""),
        "exit_code": values.get("ExecMainStatus", ""),
        "recent_logs": logs.stdout[-5000:] or logs.stderr[-5000:],
    }


def write_snapshot() -> None:
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    snapshot = {"updated_at": datetime.now(timezone.utc).isoformat(), "services": {}}
    for unit in UNITS:
        try:
            snapshot["services"][unit] = collect_unit(unit)
        except Exception as exc:
            snapshot["services"][unit] = {"unit": unit, "active_state": "unknown", "sub_state": "error", "recent_logs": str(exc)}
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=OUTPUT.parent, delete=False) as handle:
        json.dump(snapshot, handle, indent=2)
        temp_name = handle.name
    os.replace(temp_name, OUTPUT)


signal.signal(signal.SIGTERM, stop)
signal.signal(signal.SIGINT, stop)
while running:
    write_snapshot()
    time.sleep(INTERVAL)
