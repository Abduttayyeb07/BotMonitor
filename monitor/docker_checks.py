from __future__ import annotations

import logging
import re
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import docker

log = logging.getLogger(__name__)


def inventory() -> dict[str, Any]:
    """Inspect containers concurrently; fail the whole snapshot on API errors."""
    with docker.from_env(timeout=5) as client:
        containers = client.containers.list(all=True, sparse=True)
        def reload(container):
            try:
                container.reload()
            except docker.errors.NotFound:
                return None
            return container
        with ThreadPoolExecutor(max_workers=8) as pool:
            inspected = list(pool.map(reload, containers))
        return {container.name: container for container in inspected if container is not None}

DEFAULT_ERROR_PATTERNS = (
    r"\btraceback\b", r"\b(fatal|critical|panic|exception)\b",
    r"\b(unhandled|uncaught)\b", r"\b(error|err)\b",
    r"connection refused", r"connection reset", r"timeout", r"timed out",
    r"database.*(failed|error|refused|unavailable)", r"rpc.*(failed|error|timeout)",
    r"promise rejection", r"out of memory", r"oom killed",
)

# Expected provider/fallback noise. These are intentionally built into the
# monitor so a deployment cannot accidentally alert on them because of a
# missing or stale YAML ignore rule.
BUILTIN_IGNORE_PATTERNS = (
    r"402\s+Payment\s+Required",
    r"You have used all your credits",
    r"account is expired",
    r"Retrying this range next tick",
    r"(?:WS|WebSocket) reconnecting",
    r"WebSocket reconnected successfully",
    r"WebSocket appears stalled",
    r"Trying fallback WebSocket endpoint",
    r"WebSocket subscription/endpoint issue",
    r"received result for unknown id",
    r"Subscription timed out",
    r"WebSocket decoded:",
    r"WebSocket matched:",
    r"HTTP backfill scanned:",
    r"HTTP backfill matched:",
    r"Alerts delivered:",
    r"ETH USDT Monitor Health",
    r"Ethereum USDC WebSocket decoded:",
    r"BNB Smart Chain USDT WebSocket decoded:",
)


def log_checks(config: dict[str, Any], cursors: dict[str, int]) -> list[dict[str, str]]:
    """Read only new Docker log lines and turn actionable errors into findings."""
    if not config.get("enabled", True) or not config.get("log_monitoring", {}).get("enabled", True):
        return []
    findings = []
    log_config = config.get("log_monitoring", {})
    patterns = [re.compile(pattern, re.IGNORECASE) for pattern in log_config.get("error_patterns", DEFAULT_ERROR_PATTERNS)]
    global_ignores = [re.compile(pattern, re.IGNORECASE) for pattern in (*BUILTIN_IGNORE_PATTERNS, *log_config.get("ignore_patterns", []))]
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
            seen_lines: set[str] = set()
            for line in raw.splitlines():
                clean = re.sub(r"^\S+\s+", "", line).strip()
                if not clean or clean in seen_lines or any(pattern.search(clean) for pattern in ignores):
                    continue
                seen_lines.add(clean)
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
        configured = {item["name"]: item for item in (config.get("containers") or [])}
        containers = inventory()
        for name, item in configured.items():
            project = item.get('project', name)
            severity = 'CRITICAL' if item.get('critical', True) else 'ERROR'
            container = containers.get(name)
            if container is None:
                findings.append({'project': project, 'service': name, 'type': 'CONTAINER_DOWN', 'severity': severity, 'message': 'Configured container is missing or removed'})
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
            if health == "unhealthy":
                health_log = state.get("Health", {}).get("Log", [])
                output = health_log[-1].get("Output", "").strip() if health_log else ""
                detail = f"; health-check output: {output[-1200:]}" if output else ""
                findings.append({"project": project, "service": container.name, "type": "CONTAINER_UNHEALTHY", "severity": "ERROR", "message": f"Docker health status is {health}{detail}"})
    except Exception as exc:
        log.exception("Docker inspection failed")
        findings.append({"project": "host", "service": "docker", "type": "DOCKER_UNAVAILABLE", "severity": "CRITICAL", "message": str(exc)})
    return findings


def snapshot(config: dict[str, Any]) -> dict[str, int]:
    """Return a lightweight inventory summary for operational logging."""
    configured = {item["name"] for item in (config.get("containers") or [])}
    result = {"configured": len(configured), "running": 0, "healthy": 0, "unhealthy": 0, "stopped": 0, "missing": 0}
    if not config.get("enabled", True):
        return result
    try:
        containers = inventory()
        for name in configured:
            container = containers.get(name)
            if container is None:
                result["missing"] += 1
                continue
            state = container.attrs.get("State", {})
            if state.get("Status") != "running":
                result["stopped"] += 1
                continue
            result["running"] += 1
            health = state.get("Health", {}).get("Status")
            if health == "unhealthy":
                result["unhealthy"] += 1
            else:
                result["healthy"] += 1
    except Exception:
        log.exception("Docker summary collection failed")
    return result
