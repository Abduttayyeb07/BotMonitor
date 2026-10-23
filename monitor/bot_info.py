"""Read-only facts about a container for the Telegram pages.

Stack and runtime version come from the version variables that official base
images set (NODE_VERSION, PYTHON_VERSION, ...). Only those variables are read;
other environment values hold tokens and keys and are never touched. The
library count comes from the app's package.json / requirements.txt /
pyproject.toml, copied out of the container through Docker's archive API, so
nothing is executed inside the bot.
"""
from __future__ import annotations

import io
import json
import tarfile
import tomllib
from datetime import datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo

PKT = ZoneInfo("Asia/Karachi")

# First match wins, so app runtimes come before servers and databases.
RUNTIME_ENV = (
    ("NODE_VERSION", "Node.js"), ("BUN_VERSION", "Bun"), ("PYTHON_VERSION", "Python"),
    ("GOLANG_VERSION", "Go"), ("JAVA_VERSION", "Java"), ("RUBY_VERSION", "Ruby"), ("PHP_VERSION", "PHP"),
    ("NGINX_VERSION", "nginx"), ("CADDY_VERSION", "Caddy"), ("HTTPD_VERSION", "Apache"),
    ("PG_VERSION", "PostgreSQL"), ("REDIS_VERSION", "Redis"), ("MONGO_VERSION", "MongoDB"), ("MYSQL_VERSION", "MySQL"),
)
# Off-the-shelf servers and databases have no app manifest worth looking for.
NO_APP_RUNTIMES = {"nginx", "Caddy", "Apache", "PostgreSQL", "Redis", "MongoDB", "MySQL"}
MANIFESTS = ("package.json", "requirements.txt", "pyproject.toml")
APP_DIRS = ("/app", "/usr/src/app", "/home/node/app")
MAX_MANIFEST_BYTES = 512_000


def _runtime(env: list[str]) -> str | None:
    values = {}
    for entry in env or []:
        key, _, value = entry.partition("=")
        values[key] = value  # only RUNTIME_ENV keys are ever looked up below
    for key, label in RUNTIME_ENV:
        if values.get(key):
            return f"{label} {values[key]}"
    return None


def _read_file(container: Any, path: str) -> str | None:
    try:
        stream, stat = container.get_archive(path)
        if int(stat.get("size", 0)) > MAX_MANIFEST_BYTES:
            return None
        with tarfile.open(fileobj=io.BytesIO(b"".join(stream))) as archive:
            member = archive.next()
            if member is None or not member.isfile():
                return None
            handle = archive.extractfile(member)
            return handle.read(MAX_MANIFEST_BYTES).decode("utf-8", errors="replace") if handle else None
    except Exception:
        return None


def count_libraries(manifest: str, text: str) -> tuple[int, int] | None:
    """(runtime libraries, dev libraries) declared in a manifest."""
    try:
        if manifest == "package.json":
            data = json.loads(text)
            return len(data.get("dependencies") or {}), len(data.get("devDependencies") or {})
        if manifest == "requirements.txt":
            lines = [line.split("#", 1)[0].strip() for line in text.splitlines()]
            return len([line for line in lines if line and not line.startswith("-")]), 0
        if manifest == "pyproject.toml":
            data = tomllib.loads(text)
            if "dependencies" in data.get("project", {}):
                return len(data["project"]["dependencies"]), 0
            poetry = data.get("tool", {}).get("poetry", {})
            runtime = [name for name in poetry.get("dependencies", {}) if name.lower() != "python"]
            dev = poetry.get("group", {}).get("dev", {}).get("dependencies", {})
            return len(runtime), len(dev)
    except (ValueError, TypeError, AttributeError, tomllib.TOMLDecodeError):
        return None
    return None


def _libraries(container: Any, working_dir: str) -> dict[str, Any] | None:
    dirs = [working_dir] if working_dir and working_dir != "/" else []
    dirs += [path for path in APP_DIRS if path not in dirs]
    for directory in dirs:
        for manifest in MANIFESTS:
            text = _read_file(container, f"{directory.rstrip('/')}/{manifest}")
            counts = count_libraries(manifest, text) if text is not None else None
            if counts is not None:
                return {"manifest": manifest, "libraries": counts[0], "dev_libraries": counts[1]}
    return None


def container_info(container: Any) -> dict[str, Any]:
    attrs = container.attrs or {}
    config = attrs.get("Config") or {}
    labels = config.get("Labels") or {}
    runtime = _runtime(config.get("Env") or [])
    info: dict[str, Any] = {
        "runtime": runtime,
        "created": attrs.get("Created"),
        "image": config.get("Image"),
        "compose_project": labels.get("com.docker.compose.project"),
        "compose_service": labels.get("com.docker.compose.service"),
        "built": None, "image_size": None, "libraries": None,
    }
    try:
        image = container.image.attrs or {}
        info["built"], info["image_size"] = image.get("Created"), image.get("Size")
    except Exception:
        pass
    if not runtime or runtime.split(" ", 1)[0] not in NO_APP_RUNTIMES:
        info["libraries"] = _libraries(container, config.get("WorkingDir") or "")
        if info["libraries"] and not runtime:
            info["runtime"] = "Node.js" if info["libraries"]["manifest"] == "package.json" else "Python"
    return info


def live_usage(container: Any) -> dict[str, Any] | None:
    """Current memory and CPU, as `docker stats` computes them. Not cached: it is live.

    Docker samples twice about a second apart to get a CPU delta, so this takes ~1-2 s.
    """
    if getattr(container, "status", None) != "running":
        return None
    try:
        stats = container.stats(stream=False)
    except Exception:
        return None
    memory = stats.get("memory_stats") or {}
    details = memory.get("stats") or {}
    usage = memory.get("usage")
    if usage is not None:
        # Page cache is reclaimable, so `docker stats` leaves it out (cgroup v2, then v1 names).
        usage -= details.get("inactive_file", details.get("total_inactive_file", 0))
    cpu, previous = stats.get("cpu_stats") or {}, stats.get("precpu_stats") or {}
    cpu_delta = (cpu.get("cpu_usage") or {}).get("total_usage", 0) - (previous.get("cpu_usage") or {}).get("total_usage", 0)
    system_delta = cpu.get("system_cpu_usage", 0) - previous.get("system_cpu_usage", 0)
    cpus = cpu.get("online_cpus") or len((cpu.get("cpu_usage") or {}).get("percpu_usage") or []) or 1
    cpu_percent = cpu_delta / system_delta * cpus * 100 if cpu_delta >= 0 and system_delta > 0 else None
    return {"memory": usage, "memory_limit": memory.get("limit"), "cpu_percent": cpu_percent}


def size_text(value: float) -> str:
    """Memory size the way `docker stats` counts it (1 MB = 1024 * 1024 bytes)."""
    return f"{value / 1_073_741_824:.1f} GB" if value >= 1_073_741_824 else f"{value / 1_048_576:.0f} MB"


def usage_lines(usage: dict[str, Any] | None) -> list[tuple[str, str]]:
    if not usage:
        return []
    rows = []
    if usage.get("memory") is not None:
        limit = f" / {size_text(usage['memory_limit'])}" if usage.get("memory_limit") else ""
        rows.append(("Memory", f"{size_text(usage['memory'])}{limit}"))
    if usage.get("cpu_percent") is not None:
        rows.append(("CPU", f"{usage['cpu_percent']:.1f}%"))
    return rows


def local_date(value: str | None, with_time: bool = True) -> str | None:
    # Docker reports UTC with nanoseconds ("2026-10-12T09:03:12.123456789Z"); seconds are enough.
    if not value or value.startswith("0001"):
        return None
    try:
        parsed = datetime.fromisoformat(value[:19]).replace(tzinfo=timezone.utc)
    except ValueError:
        return None
    return parsed.astimezone(PKT).strftime("%d %b %Y, %H:%M PKT" if with_time else "%d %b %Y")


def library_text(info: dict[str, Any]) -> str | None:
    """'2 (+3 dev)' for the overview; None when no manifest was found."""
    libraries = info.get("libraries")
    if not libraries:
        return None
    return f"{libraries['libraries']}" + (f" (+{libraries['dev_libraries']} dev)" if libraries["dev_libraries"] else "")


def dependency_text(info: dict[str, Any]) -> str | None:
    """'2 production + 3 development' for the container page."""
    libraries = info.get("libraries")
    if not libraries:
        return None
    text = f"{libraries['libraries']} production"
    return text + (f" + {libraries['dev_libraries']} development" if libraries["dev_libraries"] else "")


def runtime_row(info: dict[str, Any]) -> tuple[str, str] | None:
    """('Node.js', '24.22.1') — the runtime as the label, its version as the value."""
    runtime = info.get("runtime")
    if not runtime:
        return None
    name, _, version = runtime.partition(" ")
    return (name, version) if version else ("Stack", name)
