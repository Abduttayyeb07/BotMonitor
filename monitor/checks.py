from __future__ import annotations

import logging
import json
from pathlib import Path
import shutil
import subprocess
import time
from datetime import datetime, timezone
from typing import Any

import psutil
import requests

log = logging.getLogger(__name__)


def read_systemd_snapshot(status_path: str, max_age_seconds: int = 60) -> dict[str, Any]:
    snapshot = json.loads(Path(status_path).read_text(encoding="utf-8"))
    stamp = datetime.fromisoformat(snapshot["updated_at"].replace("Z", "+00:00"))
    if stamp.tzinfo is None:
        raise ValueError("systemd collector timestamp has no timezone")
    age = (datetime.now(timezone.utc) - stamp.astimezone(timezone.utc)).total_seconds()
    if age > max_age_seconds or age < -max_age_seconds:
        raise ValueError(f"systemd collector snapshot is stale ({int(age)}s old)")
    return snapshot


def host_checks(thresholds: dict[str, Any]) -> list[dict[str, str]]:
    findings = []
    memory = psutil.virtual_memory().percent
    disk = psutil.disk_usage("/").percent
    cpu = psutil.cpu_percent(interval=1)
    if memory >= thresholds.get("memory_critical_percent", 95):
        findings.append({"project": "host", "service": "memory", "type": "HIGH_MEMORY", "severity": "CRITICAL", "message": f"Memory usage is {memory:.1f}%"})
    elif memory >= thresholds.get("memory_warning_percent", 85):
        findings.append({"project": "host", "service": "memory", "type": "HIGH_MEMORY", "severity": "WARNING", "message": f"Memory usage is {memory:.1f}%"})
    if disk >= thresholds.get("disk_critical_percent", 90):
        findings.append({"project": "host", "service": "disk", "type": "DISK_FULL", "severity": "CRITICAL", "message": f"Root disk usage is {disk:.1f}%"})
    elif disk >= thresholds.get("disk_warning_percent", 80):
        findings.append({"project": "host", "service": "disk", "type": "DISK_FULL", "severity": "WARNING", "message": f"Root disk usage is {disk:.1f}%"})
    if cpu >= thresholds.get("cpu_warning_percent", 90):
        findings.append({"project": "host", "service": "cpu", "type": "HIGH_CPU", "severity": "WARNING", "message": f"CPU usage is {cpu:.1f}%"})
    return findings


def systemd_checks(config: dict[str, Any], status_path: str = "/data/systemd-status.json") -> list[dict[str, str]]:
    findings = []
    if not config.get("enabled", True):
        return findings
    try:
        snapshot = read_systemd_snapshot(status_path, int(config.get("collector_max_age_seconds", 60)))
        services = snapshot.get("services", {})
    except (FileNotFoundError, json.JSONDecodeError, OSError, ValueError, KeyError) as exc:
        return [{"project": "host", "service": "systemd-collector", "type": "SYSTEMD_COLLECTOR_MISSING", "severity": "CRITICAL", "message": f"Cannot read {status_path}: {exc}"}]
    for item in (config.get("services") or []):
        unit = item["unit"]
        current = services.get(unit, {})
        status = current.get("active_state", "unknown")
        if status != "active" or current.get("sub_state") != "running":
            findings.append({"project": item.get("project", item["name"]), "service": item["name"], "type": "SYSTEMD_DOWN", "severity": "CRITICAL" if item.get("critical", True) else "ERROR", "message": f"systemd unit {unit} is {status}"})
            continue
        if unit == "wallet-watchman.service":
            activity = current.get("activity") or {}
            failures = int(activity.get("rpc_failures_10m") or 0)
            if failures >= int(item.get("rpc_failure_threshold", 3)):
                findings.append({"project": item.get("project", item["name"]), "service": item["name"],
                                 "type": "SYSTEMD_RPC_FAILURE", "severity": "ERROR",
                                 "message": f"Wallet Watchman logged {failures} RPC failures in the last 10 minutes"})
    return findings


def endpoint_checks(items: list[dict[str, Any]]) -> list[dict[str, str]]:
    findings = []
    for item in items:
        url = item.get("health_url")
        if not url:
            continue
        try:
            response = requests.get(url, timeout=item.get("timeout_seconds", 5))
            if response.status_code >= 400:
                raise RuntimeError(f"HTTP {response.status_code}")
        except Exception as exc:
            findings.append({"project": item.get("project", item["name"]), "service": item["name"], "type": "HEALTH_CHECK_FAILED", "severity": "CRITICAL" if item.get("critical", False) else "ERROR", "message": f"Health endpoint failed: {exc}"})
    return findings
