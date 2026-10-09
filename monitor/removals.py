"""Persistent "stop monitoring this" list, applied as a filter over config.yaml.

config.yaml is mounted read-only, so removals are stored in SQLite and applied
to the loaded config every time they change. Restoring an item deletes its row.
"""
from __future__ import annotations

import copy
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from typing import Any

# Project id -> bot_reports section that checks the same bot.
BOT_REPORT_KEYS = {
    "marketzig": "zig_whale",
    "zig-usd-only-alerts": "zig_usd_alerts",
    "wallet-monitor": "wallet_monitor",
    "patronicluster": "zigchain_bot",
    "stakeunstake": "stake_unstake",
    "launches": "mdf_tracker",
    "highlaunches": "highbuy_monitor",
    "nawavaldora": "nawa_valdora",
    "tokenx-vault": "tokenx_vault",
    "bep20": "bep20_usdt",
    "ethtethertoken": "eth_usdt",
    "sheetszahoor": "sheets_sync",
}
SYSTEM_SERVICE_UNITS = ("wallet-watchman.service", "zigchain-exporter.service")


def project_key(group_id: str, project_id: str) -> str:
    return f"project:{group_id}:{project_id}"


def service_key(group_id: str, project_id: str, name: str) -> str:
    return f"service:{group_id}:{project_id}:{name}"


def _connect(database_path: str) -> sqlite3.Connection:
    database = sqlite3.connect(database_path, timeout=10)
    database.execute("""CREATE TABLE IF NOT EXISTS removed_items (
        id INTEGER PRIMARY KEY AUTOINCREMENT, item_key TEXT NOT NULL UNIQUE,
        label TEXT NOT NULL, removed_at TEXT NOT NULL)""")
    return database


def removed_items(database_path: str) -> list[dict[str, Any]]:
    with closing(_connect(database_path)) as database:
        rows = database.execute("SELECT id, item_key, label FROM removed_items ORDER BY id").fetchall()
    return [{"id": row[0], "key": row[1], "label": row[2]} for row in rows]


def removed_keys(database_path: str) -> frozenset[str]:
    return frozenset(item["key"] for item in removed_items(database_path))


def remove_item(database_path: str, key: str, label: str) -> None:
    with closing(_connect(database_path)) as database:
        database.execute("INSERT OR IGNORE INTO removed_items (item_key, label, removed_at) VALUES (?, ?, ?)",
                         (key, label, datetime.now(timezone.utc).isoformat()))
        database.commit()


def restore_item(database_path: str, item_id: int) -> str | None:
    with closing(_connect(database_path)) as database:
        row = database.execute("SELECT label FROM removed_items WHERE id=?", (item_id,)).fetchone()
        if row is None:
            return None
        database.execute("DELETE FROM removed_items WHERE id=?", (item_id,))
        database.commit()
    return row[0]


def apply_removals(config: dict[str, Any], keys: frozenset[str] | set[str]) -> dict[str, Any]:
    """Return a copy of config with every removed item no longer monitored.

    The result carries a ``_removed`` summary (project ids, containers, service
    names) so callers can discard the matching open incidents.
    """
    if not keys:
        return config
    result = copy.deepcopy(config)
    projects = result.get("projects") or {}
    project_ids: set[str] = set()
    frontend_ids: set[str] = set()
    containers: set[str] = set()
    units: set[str] = set()
    service_names: set[str] = set()

    for key in sorted(keys):
        kind, _, rest = key.partition(":")
        if kind == "project":
            group_id, _, project_id = rest.partition(":")
            group = projects.get(group_id) or {}
            for item in group.get("items", []):
                if item.get("id") == project_id:
                    project_ids.add(project_id)
                    if group_id == "frontend":
                        frontend_ids.add(project_id)
                    containers.update(item.get("containers", []))
                    for service in item.get("services", []):
                        units.add(service["unit"])
                        service_names.add(service["name"])
            if group:
                group["items"] = [item for item in group.get("items", []) if item.get("id") != project_id]
        elif kind == "service":
            group_id, project_id, name = rest.split(":", 2)
            for item in (projects.get(group_id) or {}).get("items", []):
                if item.get("id") != project_id:
                    continue
                for service in item.get("services", []):
                    if service["name"] == name:
                        units.add(service["unit"])
                        service_names.add(name)
                item["services"] = [service for service in item.get("services", []) if service["name"] != name]
            group = projects.get(group_id) or {}
            # A service-only item with nothing left to monitor disappears from the menu.
            group["items"] = [item for item in group.get("items", [])
                              if item.get("id") != project_id or item.get("containers") or item.get("services")]

    still_monitored = {name for group in projects.values() for item in group.get("items", [])
                       for name in item.get("containers", [])}
    containers -= still_monitored

    docker_config = result.get("docker") or {}
    if docker_config.get("containers"):
        docker_config["containers"] = [item for item in docker_config["containers"] if item["name"] not in containers]
    systemd_config = result.get("systemd") or {}
    if systemd_config.get("services"):
        systemd_config["services"] = [item for item in systemd_config["services"] if item["unit"] not in units]

    frontend_report = (result.get("daily_reports") or {}).get("frontend")
    if frontend_report is not None:
        if frontend_report.get("items"):
            frontend_report["items"] = [item for item in frontend_report["items"] if item.get("id") not in frontend_ids]
        if not (projects.get("frontend") or {}).get("items"):
            frontend_report["enabled"] = False

    bot_reports = result.setdefault("bot_reports", {})
    for project_id in project_ids:
        if project_id in BOT_REPORT_KEYS:
            bot_reports.setdefault(BOT_REPORT_KEYS[project_id], {})["enabled"] = False
    for section in bot_reports.values():
        if isinstance(section, dict) and section.get("container") in containers:
            section["enabled"] = False
    if units:
        system_services = bot_reports.setdefault("system_services", {})
        system_services["disabled_units"] = sorted(units)
        if all(unit in units for unit in SYSTEM_SERVICE_UNITS):
            system_services["enabled"] = False

    result["_removed"] = {"projects": sorted(project_ids), "containers": sorted(containers),
                          "services": sorted(service_names)}
    return result
