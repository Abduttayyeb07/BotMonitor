from __future__ import annotations

import logging
import json
from pathlib import Path
import shutil
import subprocess
import time
from typing import Any

import psutil
import requests

log = logging.getLogger(__name__)


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
        snapshot = json.loads(Path(status_path).read_text(encoding="utf-8"))
        services = snapshot.get("services", {})
    except (FileNotFoundError, json.JSONDecodeError, OSError) as exc:
        return [{"project": "host", "service": "systemd-collector", "type": "SYSTEMD_COLLECTOR_MISSING", "severity": "CRITICAL", "message": f"Cannot read {status_path}: {exc}"}]
    for item in (config.get("services") or []):
        unit = item["unit"]
        current = services.get(unit, {})
        status = current.get("active_state", "unknown")
        if status != "active":
            findings.append({"project": item.get("project", item["name"]), "service": item["name"], "type": "SYSTEMD_DOWN", "severity": "CRITICAL" if item.get("critical", True) else "ERROR", "message": f"systemd unit {unit} is {status}"})
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
