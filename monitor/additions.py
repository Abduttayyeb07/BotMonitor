"""Containers found running that config.yaml does not list, and the ones added from Telegram.

Every unknown container (grouped by its Docker Compose project) gets one row, the
first time it is seen, and is never announced again: restarts, rebuilds and
`compose up` do not ask twice, whatever the answer was (or if there was none).

  status 'seen'       found before the monitor knew about it (startup, /scan): no message
  status 'announced'  a "New container detected" message was sent once
  status 'added'      added from Telegram: merged into the config like config.yaml entries
  status 'ignored'    the user said no; hidden from /scan (shown under "ignored")
"""
from __future__ import annotations

import copy
import fnmatch
import json
import re
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from typing import Any

# Containers that are never worth asking about.
DEFAULT_IGNORE = ("central-monitor", "buildx_buildkit*")


def _connect(database_path: str) -> sqlite3.Connection:
    database = sqlite3.connect(database_path, timeout=10)
    database.execute("""CREATE TABLE IF NOT EXISTS discovered (
        id INTEGER PRIMARY KEY AUTOINCREMENT, item_key TEXT NOT NULL UNIQUE, name TEXT NOT NULL,
        containers TEXT NOT NULL, details TEXT NOT NULL, status TEXT NOT NULL, group_id TEXT,
        first_seen TEXT NOT NULL)""")
    return database


def discovered(database_path: str) -> dict[str, dict[str, Any]]:
    with closing(_connect(database_path)) as database:
        rows = database.execute("SELECT id, item_key, name, containers, details, status, group_id, first_seen FROM discovered ORDER BY id").fetchall()
    return {row[1]: {"id": row[0], "key": row[1], "name": row[2], "containers": json.loads(row[3]),
                     "details": json.loads(row[4]), "status": row[5], "group_id": row[6], "first_seen": row[7]} for row in rows}


def record(database_path: str, info: dict[str, Any], status: str) -> int:
    """Remember an unknown container group once. Returns its id (existing rows are left untouched)."""
    with closing(_connect(database_path)) as database:
        database.execute("INSERT OR IGNORE INTO discovered (item_key, name, containers, details, status, first_seen) VALUES (?, ?, ?, ?, ?, ?)",
                         (info["key"], info["name"], json.dumps(sorted(info["containers"])),
                          json.dumps(info["details"]), status, datetime.now(timezone.utc).isoformat()))
        database.commit()
        return database.execute("SELECT id FROM discovered WHERE item_key=?", (info["key"],)).fetchone()[0]


def set_status(database_path: str, row_id: int, status: str, group_id: str | None = None) -> dict[str, Any] | None:
    with closing(_connect(database_path)) as database:
        database.execute("UPDATE discovered SET status=?, group_id=? WHERE id=?", (status, group_id, row_id))
        database.commit()
    return next((row for row in discovered(database_path).values() if row["id"] == row_id), None)


def extend_containers(database_path: str, key: str, names: list[str]) -> None:
    """An added project gained containers (scaled up, new service): include them."""
    rows = discovered(database_path)
    if key not in rows:
        return
    merged = sorted(set(rows[key]["containers"]) | set(names))
    with closing(_connect(database_path)) as database:
        database.execute("UPDATE discovered SET containers=? WHERE item_key=?", (json.dumps(merged), key))
        database.commit()


def signature(rows: dict[str, dict[str, Any]]) -> tuple:
    """Changes when an addition is made or its containers change, so the config is rebuilt."""
    return tuple(sorted((row["key"], row["group_id"] or "", tuple(row["containers"])) for row in rows.values() if row["status"] == "added"))


def known_containers(config: dict[str, Any], rows: dict[str, dict[str, Any]]) -> set[str]:
    """Every container the monitor already knows: config.yaml (even if removed in Telegram) and additions."""
    names = {item["name"] for item in ((config.get("docker") or {}).get("containers") or [])}
    names |= {name for group in (config.get("projects") or {}).values() for item in group.get("items", []) for name in item.get("containers", [])}
    names |= {name for row in rows.values() if row["status"] == "added" for name in row["containers"]}
    return names


def unknown_groups(containers: dict[str, Any], known: set[str], ignore: tuple[str, ...] = DEFAULT_IGNORE) -> dict[str, dict[str, Any]]:
    """Running containers the monitor does not know, grouped by Compose project (else by name)."""
    groups: dict[str, dict[str, Any]] = {}
    for name, container in containers.items():
        if name in known or getattr(container, "status", None) != "running" or any(fnmatch.fnmatch(name, pattern) for pattern in ignore):
            continue
        attrs = getattr(container, "attrs", None) or {}
        labels = (attrs.get("Config") or {}).get("Labels") or {}
        compose = labels.get("com.docker.compose.project")
        key = f"compose:{compose}" if compose else f"container:{name}"
        group = groups.setdefault(key, {"key": key, "name": compose or name, "containers": [], "details": {
            "compose_project": compose, "working_dir": labels.get("com.docker.compose.project.working_dir"),
            "images": []}})
        group["containers"].append(name)
        image = (attrs.get("Config") or {}).get("Image")
        if image and image not in group["details"]["images"]:
            group["details"]["images"].append(image)
    return groups


def _project_id(name: str, taken: set[str]) -> str:
    base = re.sub(r"[^a-z0-9-]+", "-", name.lower()).strip("-") or "added"
    candidate, number = base, 2
    while candidate in taken:
        candidate, number = f"{base}-{number}", number + 1
    return candidate


def display_name(name: str) -> str:
    return " ".join(part.capitalize() for part in re.split(r"[-_\s]+", name) if part) or name


def apply_additions(config: dict[str, Any], rows: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """A copy of config with every added project in the menu and its containers checked."""
    added = [row for row in rows.values() if row["status"] == "added" and row["group_id"]]
    if not added:
        return config
    result = copy.deepcopy(config)
    projects = result.setdefault("projects", {})
    containers = result.setdefault("docker", {}).setdefault("containers", [])
    taken = {item.get("id") for group in projects.values() for item in group.get("items", [])}
    existing = {item["name"] for item in containers}
    for row in added:
        group = projects.setdefault(row["group_id"], {"title": "Frontend" if row["group_id"] == "frontend" else "Bots (Docker)",
                                                      "icon": "🖥️" if row["group_id"] == "frontend" else "🐳", "items": []})
        project_id = _project_id(row["name"], taken)
        taken.add(project_id)
        details = row["details"]
        item = {"id": project_id, "name": display_name(row["name"]), "menu_name": display_name(row["name"])[:28],
                "containers": list(row["containers"]), "added_from_telegram": True}
        if details.get("compose_project"):
            item["compose_project"] = details["compose_project"]
        if details.get("working_dir"):
            item["working_dir"] = details["working_dir"]
        group.setdefault("items", []).append(item)
        for name in row["containers"]:
            if name not in existing:
                containers.append({"name": name, "project": project_id, "critical": True})
                existing.add(name)
    return result


def effective_config(base_config: dict[str, Any], database_path: str) -> tuple[dict[str, Any], tuple]:
    """config.yaml + containers added from Telegram - items removed from Telegram, and a
    version stamp that changes whenever either list changes."""
    from .bot_details import align_report_containers
    from .removals import apply_removals, removed_keys
    rows = discovered(database_path)
    keys = removed_keys(database_path)
    config = align_report_containers(apply_removals(apply_additions(base_config, rows), keys))
    return config, (keys, signature(rows))


def config_version(database_path: str) -> tuple:
    """Cheap stamp of the removal and addition lists; rebuild the config only when it changes."""
    from .removals import removed_keys
    return removed_keys(database_path), signature(discovered(database_path))


def sync_discovered(database_path: str, base_config: dict[str, Any], containers: dict[str, Any],
                    announce: bool) -> list[dict[str, Any]]:
    """Record unknown container groups the first time they are seen; return the new ones.

    With announce=False (startup, /scan) they are stored as 'seen' and no message is sent.
    A group already stored is never returned again, so nobody is asked twice. New
    containers of a project that was already added join it automatically.
    """
    rows = discovered(database_path)
    ignore = tuple((base_config.get("discovery") or {}).get("ignore") or DEFAULT_IGNORE)
    new = []
    for key, info in unknown_groups(containers, known_containers(base_config, rows), ignore).items():
        row = rows.get(key)
        if row is None:
            info["id"] = record(database_path, info, "announced" if announce else "seen")
            new.append(info)
        elif row["status"] == "added":
            extend_containers(database_path, key, info["containers"])
    return new
