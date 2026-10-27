"""Watchers that run beside the main 5-second poll.

MemoryWatch      per-container RAM alert, checked once a minute.
DockerEvents     Docker's event stream: wakes the main loop the moment a container
                 dies, runs out of memory, starts or turns unhealthy, and catches
                 crashes that restart within seconds (a poll could miss those).
"""
from __future__ import annotations

import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Any

import docker

from .docker_checks import inventory
from .view import memory_size

log = logging.getLogger(__name__)
MB = 1024 * 1024


def container_memory(container: Any) -> int | None:
    """RAM in use, counted like `docker stats` (reclaimable page cache excluded).

    `one_shot` skips the one-second CPU sample, so this is quick enough to run for
    every container each minute.
    """
    try:
        stats = container.stats(stream=False, one_shot=True)
    except Exception:
        return None
    memory = stats.get("memory_stats") or {}
    usage = memory.get("usage")
    if usage is None:
        return None
    details = memory.get("stats") or {}
    return max(0, usage - details.get("inactive_file", details.get("total_inactive_file", 0)))


class MemoryWatch:
    """Alert when one container uses too much RAM for several checks in a row.

    Limits: thresholds.container_memory_warning_mb / _critical_mb (default 1.5 / 3 GB),
    overridable per container with memory_warning_mb / memory_critical_mb. The last
    result is repeated between checks so an open incident is not falsely recovered.
    """

    def __init__(self, read_inventory=inventory, read_memory=container_memory):
        self.read_inventory, self.read_memory = read_inventory, read_memory
        self.last_run = float("-inf")
        self.over: dict[str, int] = {}
        self.current: dict[str, dict[str, str]] = {}

    def findings(self, config: dict[str, Any]) -> list[dict[str, str]]:
        thresholds = config.get("thresholds") or {}
        if not thresholds.get("container_memory_enabled", True) or not (config.get("docker") or {}).get("enabled", True):
            self.over, self.current = {}, {}
            return []
        if time.monotonic() - self.last_run >= int(thresholds.get("container_memory_interval_seconds", 60)):
            self.last_run = time.monotonic()
            self.current = self.measure(config, thresholds)
        return list(self.current.values())

    def measure(self, config: dict[str, Any], thresholds: dict[str, Any]) -> dict[str, dict[str, str]]:
        items = {item["name"]: item for item in ((config.get("docker") or {}).get("containers") or [])}
        try:
            containers = self.read_inventory()
        except Exception:
            log.exception("container memory check: Docker inventory unavailable")
            return self.current  # keep open incidents open rather than recover them on a read failure
        running = [name for name in items if containers.get(name) is not None and containers[name].status == "running"]
        with ThreadPoolExecutor(max_workers=10) as pool:
            usage = dict(zip(running, pool.map(lambda name: self.read_memory(containers[name]), running)))
        sustain = max(1, int(thresholds.get("container_memory_sustain_checks", 3)))
        findings = {}
        for name in items:
            used = usage.get(name)
            if name not in usage:
                self.over.pop(name, None)  # stopped or missing: the container checks report that
                continue
            if used is None:
                if name in self.current:
                    findings[name] = self.current[name]  # unknown this time: keep what we had
                continue
            item = items[name]
            warning = int(item.get("memory_warning_mb", thresholds.get("container_memory_warning_mb", 1536))) * MB
            critical = int(item.get("memory_critical_mb", thresholds.get("container_memory_critical_mb", 3072))) * MB
            self.over[name] = self.over.get(name, 0) + 1 if used >= warning else 0
            if self.over[name] >= sustain:
                level, limit = ("CRITICAL", critical) if used >= critical else ("WARNING", warning)
                findings[name] = {
                    "project": item.get("project", name), "service": name, "type": "CONTAINER_HIGH_MEMORY",
                    "severity": level,
                    "message": f"Container memory is {memory_size(used)} ({level.lower()} limit {memory_size(limit)})"}
        for name in list(self.over):
            if name not in items:
                del self.over[name]
        return findings


class DockerEvents:
    """Background listener on Docker's container events.

    - Wakes the main loop (`wake`) when something important happens, so a check runs
      immediately instead of up to 5 seconds later.
    - Records crashes: a container that dies with a non-zero exit code (or is killed
      for running out of memory) without anyone stopping it. A normal `docker stop` /
      `compose down` sends a 'kill' first and is not a crash.
    """

    WAKE_ACTIONS = {"die", "oom", "start", "destroy", "health_status: unhealthy"}

    def __init__(self, wake: threading.Event, client_factory=None):
        self.wake = wake
        self.client_factory = client_factory or (lambda: docker.from_env())
        self.stop_event = threading.Event()
        self.lock = threading.Lock()
        self.requested_stop: dict[str, float] = {}
        self.pending: dict[str, dict[str, Any]] = {}
        self.started: set[str] = set()  # containers that started since the last look (for discovery)
        self.stream = None

    def run(self) -> None:
        while not self.stop_event.is_set():
            client = None
            try:
                client = self.client_factory()
                self.stream = client.events(decode=True, filters={"type": "container"})
                log.info("Docker events stream connected")
                for event in self.stream:
                    if self.stop_event.is_set():
                        break
                    self.handle(event)
            except Exception as exc:
                if not self.stop_event.is_set():
                    log.warning("Docker events stream lost (%s); reconnecting in 5s", type(exc).__name__)
                    self.stop_event.wait(5)
            finally:
                if client is not None:
                    try:
                        client.close()
                    except Exception:
                        pass

    def stop(self) -> None:
        self.stop_event.set()
        try:
            if self.stream is not None:
                self.stream.close()
        except Exception:
            pass

    def handle(self, event: dict[str, Any], now: float | None = None) -> None:
        now = time.monotonic() if now is None else now
        action = event.get("Action") or event.get("status") or ""
        attributes = (event.get("Actor") or {}).get("Attributes") or {}
        name = attributes.get("name")
        if not name:
            return
        with self.lock:
            if action == "start":
                self.started.add(name)
            if action in ("kill", "stop"):
                self.requested_stop[name] = now
            elif action == "oom":
                self._record(name, None, oom=True, event=event)
            elif action == "die":
                exit_code = int(attributes.get("exitCode") or 0)
                stopped_on_purpose = now - self.requested_stop.get(name, float("-inf")) < 60
                if exit_code != 0 and not stopped_on_purpose:
                    self._record(name, exit_code, oom=False, event=event)
                elif name in self.pending and self.pending[name]["exit_code"] is None:
                    self.pending[name]["exit_code"] = exit_code  # the die that follows an oom
        if action in self.WAKE_ACTIONS:
            self.wake.set()

    def _record(self, name: str, exit_code: int | None, oom: bool, event: dict[str, Any]) -> None:
        seconds = event.get("time")
        when = datetime.fromtimestamp(seconds, timezone.utc) if seconds else datetime.now(timezone.utc)
        entry = self.pending.setdefault(name, {"name": name, "exit_code": exit_code, "oom": False, "count": 0, "at": when})
        entry["count"] += 1
        entry["oom"] = entry["oom"] or oom
        if exit_code is not None:
            entry["exit_code"] = exit_code
        entry["at"] = when

    def take_started(self) -> set[str]:
        with self.lock:
            started, self.started = self.started, set()
        return started

    def take_crashes(self) -> list[dict[str, Any]]:
        with self.lock:
            crashes, self.pending = list(self.pending.values()), {}
        return crashes


class CrashNotices:
    """Turns recorded crashes into "crashed and restarted" messages.

    A crash is held until the container is running again (then a notice is sent) or an
    incident covers it (the container is still down: the normal alert handles it).
    At most one notice per container per `cooldown` seconds; extra crashes are counted.
    """

    def __init__(self, cooldown: int = 600):
        self.cooldown = cooldown
        self.waiting: dict[str, dict[str, Any]] = {}
        self.last_sent: dict[str, float] = {}
        self.suppressed: dict[str, int] = {}

    def add(self, crashes: list[dict[str, Any]]) -> None:
        for crash in crashes:
            known = self.waiting.get(crash["name"])
            if known:
                known["count"] += crash["count"]
                known["oom"] = known["oom"] or crash["oom"]
                known["exit_code"] = crash["exit_code"] if crash["exit_code"] is not None else known["exit_code"]
                known["at"] = crash["at"]
            else:
                self.waiting[crash["name"]] = dict(crash)

    def ready(self, monitored: dict[str, str], running: set[str], down: set[str], now: float | None = None) -> list[dict[str, Any]]:
        """Crashes to announce now. `monitored`: container -> project; `down`: containers with an open problem."""
        now = time.monotonic() if now is None else now
        notices = []
        for name, crash in list(self.waiting.items()):
            if name not in monitored or name in down:
                del self.waiting[name]  # not ours, or the down/unhealthy alert covers it
                continue
            if name not in running:
                continue  # still restarting; decide on a later poll
            del self.waiting[name]
            if now - self.last_sent.get(name, float("-inf")) < self.cooldown:
                self.suppressed[name] = self.suppressed.get(name, 0) + crash["count"]
                continue
            crash["count"] += self.suppressed.pop(name, 0)
            self.last_sent[name] = now
            notices.append({**crash, "project": monitored[name]})
        return notices
