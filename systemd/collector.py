#!/usr/bin/env python3
"""Small host-side systemd collector for the Dockerized central monitor."""

from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

UNITS = ("wallet-watchman.service", "zigchain-exporter.service")
OUTPUT = Path(os.getenv("MONITOR_STATUS_FILE", "/root/saad/BotMonitor/data/systemd-status.json"))
INTERVAL = int(os.getenv("MONITOR_SYSTEMD_INTERVAL", "15"))
running = True
WATCHMAN_RELOAD = re.compile(r"Reloaded\s+(\d+)\s+wallets from DB", re.IGNORECASE)
WATCHMAN_RPC_FAILURE = re.compile(r"\bRPC\s+\[.*?\]\s+failed:", re.IGNORECASE)


def stop(_signum: int, _frame: object) -> None:
    global running
    running = False


def watchman_activity(log_text: str, now: datetime | None = None) -> dict[str, object]:
    now = now or datetime.now(timezone.utc)
    reloads: list[tuple[datetime, int]] = []
    rpc_failures: list[datetime] = []
    for line in log_text.splitlines():
        try:
            stamp = datetime.fromisoformat(line.split(maxsplit=1)[0].replace("Z", "+00:00"))
            if stamp.tzinfo is None:
                continue
        except (ValueError, IndexError):
            continue
        reload_match = WATCHMAN_RELOAD.search(line)
        if reload_match:
            reloads.append((stamp, int(reload_match.group(1))))
        if WATCHMAN_RPC_FAILURE.search(line) and 0 <= (now - stamp).total_seconds() <= 600:
            rpc_failures.append(stamp)
    latest = max(reloads, default=None, key=lambda item: item[0])
    return {
        "last_reload_at": latest[0].isoformat() if latest else None,
        "wallet_count": latest[1] if latest else None,
        "rpc_failures_10m": len(rpc_failures),
        "last_rpc_failure_at": max(rpc_failures).isoformat() if rpc_failures else None,
    }


def collect_unit(unit: str) -> dict[str, Any]:
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
        ["journalctl", "-u", unit, "-n", "120", "--no-pager", "-o", "short-iso"],
        capture_output=True, text=True, timeout=10,
    )
    snapshot = {
        "unit": unit,
        "active_state": values.get("ActiveState", "unknown"),
        "sub_state": values.get("SubState", "unknown"),
        "active_since": values.get("ActiveEnterTimestamp", ""),
        "exit_code": values.get("ExecMainStatus", ""),
        "recent_logs": logs.stdout[-5000:] or logs.stderr[-5000:],
    }
    if unit == "wallet-watchman.service":
        snapshot["activity"] = watchman_activity(logs.stdout)
    return snapshot


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


def main() -> None:
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    while running:
        write_snapshot()
        time.sleep(INTERVAL)


if __name__ == "__main__":
    main()
