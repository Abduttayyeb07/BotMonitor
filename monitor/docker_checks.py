from __future__ import annotations

import logging
import re
import time
from typing import Any

import docker

log = logging.getLogger(__name__)

DEFAULT_ERROR_PATTERNS = (
    r"\btraceback\b", r"\b(fatal|critical|panic|exception)\b",
    r"\b(unhandled|uncaught)\b", r"\b(error|err)\b",
    r"connection refused", r"connection reset", r"timeout", r"timed out",
    r"database.*(failed|error|refused|unavailable)", r"rpc.*(failed|error|timeout)",
    r"promise rejection", r"out of memory", r"oom killed",
)


def log_checks(config: dict[str, Any], cursors: dict[str, int]) -> list[dict[str, str]]:
    """Read only new Docker log lines and turn actionable errors into findings."""
    if not config.get("enabled", True) or not config.get("log_monitoring", {}).get("enabled", True):
        return []
    findings = []
    log_config = config.get("log_monitoring", {})
    patterns = [re.compile(pattern, re.IGNORECASE) for pattern in log_config.get("error_patterns", DEFAULT_ERROR_PATTERNS)]
    global_ignores = [re.compile(pattern, re.IGNORECASE) for pattern in log_config.get("ignore_patterns", [])]
    try:
        client = docker.from_env()
        configured = {item["name"]: item for item in (config.get("containers") or [])}
        for container in client.containers.list(all=False):
            item = configured.get(container.name)
            if item is None:
                continue
            item_patterns = [re.compile(pattern, re.IGNORECASE) for pattern in item.get("log_error_patterns", [])] or patterns
            ignores = global_ignores + [re.compile(pattern, re.IGNORECASE) for pattern in item.get("ignore_log_patterns", [])]
            cursor = cursors.setdefault(container.name, int(time.time()))
            raw = container.logs(since=cursor, timestamps=True, tail=200).decode("utf-8", errors="replace")
            cursors[container.name] = int(time.time())
            for line in raw.splitlines():
                clean = re.sub(r"^\S+\s+", "", line).strip()
                if not clean or any(pattern.search(clean) for pattern in ignores):
                    continue
                if not any(pattern.search(clean) for pattern in item_patterns):
                    continue
                findings.append({
                    "project": item.get("project", container.name),
                    "service": container.name,
                    "type": "APPLICATION_LOG_ERROR",
                    "severity": item.get("log_severity", "ERROR"),
                    "message": clean[-1600:],
                })
    except Exception:
        log.exception("Docker log inspection failed")
    return findings


def checks(config: dict[str, Any]) -> list[dict[str, str]]:
    findings = []
    if not config.get("enabled", True):
        return findings
    try:
        client = docker.from_env()
        configured = {item["name"]: item for item in (config.get("containers") or [])}
        containers = client.containers.list(all=True)
        for container in containers:
            item = configured.get(container.name)
            if item is None:
                continue
            attrs = container.attrs
            state = attrs.get("State", {})
            status = state.get("Status", "unknown")
            project = item.get("project", container.name)
            severity = "CRITICAL" if item.get("critical", True) else "ERROR"
            if status != "running":
                findings.append({"project": project, "service": container.name, "type": "CONTAINER_DOWN", "severity": severity, "message": f"Container state is {status}; exit code={state.get('ExitCode')}"})
                continue
            health = state.get("Health", {}).get("Status")
            if health in {"unhealthy", "starting"}:
                health_log = state.get("Health", {}).get("Log", [])
                output = health_log[-1].get("Output", "").strip() if health_log else ""
                detail = f"; health-check output: {output[-1200:]}" if output else ""
                findings.append({"project": project, "service": container.name, "type": "CONTAINER_UNHEALTHY", "severity": "ERROR", "message": f"Docker health status is {health}{detail}"})
    except Exception as exc:
        log.exception("Docker inspection failed")
        findings.append({"project": "host", "service": "docker", "type": "DOCKER_UNAVAILABLE", "severity": "CRITICAL", "message": str(exc)})
    return findings
