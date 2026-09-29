from __future__ import annotations

import logging
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


def systemd_checks(config: dict[str, Any]) -> list[dict[str, str]]:
    findings = []
    if not config.get("enabled", True):
        return findings
    for item in config.get("services", []):
        unit = item["unit"]
        result = subprocess.run(["systemctl", "is-active", unit], capture_output=True, text=True, timeout=10)
        if result.stdout.strip() != "active":
            findings.append({"project": item.get("project", item["name"]), "service": item["name"], "type": "SYSTEMD_DOWN", "severity": "CRITICAL" if item.get("critical", True) else "ERROR", "message": f"systemd unit {unit} is {result.stdout.strip() or 'unknown'}"})
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
