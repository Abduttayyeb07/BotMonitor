from __future__ import annotations

import logging
from typing import Any

import docker

log = logging.getLogger(__name__)


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
