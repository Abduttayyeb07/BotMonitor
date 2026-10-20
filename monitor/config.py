from __future__ import annotations

import copy
import os
from pathlib import Path
from typing import Any

import yaml


# Bots every deployment gets in the menu even if config.yaml predates them.
# A bot already present in config.yaml (by project id or container) is left as written.
BUILTIN_DOCKER_BOTS = [{
    "item": {"id": "sol-usdt", "name": "Sol USDT Monitor", "menu_name": "Sol USDT",
             "compose_project": "sol-usdt-bot", "working_dir": "/root/saad/Sol-USDT-bot",
             "compose_file": "/root/saad/Sol-USDT-bot/docker-compose.yml", "containers": ["sol-usdt-monitor"]},
    "report_key": "sol_usdt",
    "report": {"container": "sol-usdt-monitor", "log_tail_lines": 1000, "stall_after_seconds": 60,
               "max_slot_lag": 100, "check_interval_seconds": 30},
}, {
    "item": {"id": "nawa-finance-redeemer", "name": "NAWA Finance Redeemer", "menu_name": "NAWA Finance Redeemer",
             "compose_project": "nawa-finance-redeemer", "working_dir": "/root/saad/nawa-finance-redeemer",
             "compose_file": "/root/saad/nawa-finance-redeemer/docker-compose.yml",
             "containers": ["nawa-finance-redeemer"]},
    "report_key": "nawa_finance_redeemer",
    "report": {"container": "nawa-finance-redeemer", "log_tail_lines": 1000, "stale_after_seconds": 180,
               "error_window_seconds": 600, "error_threshold": 3},
}]


def add_builtin_bots(data: dict[str, Any]) -> None:
    group = (data.get("projects") or {}).get("bots_docker")
    if group is None:
        return
    items = group.setdefault("items", [])
    containers = data.setdefault("docker", {}).setdefault("containers", [])
    for bot in BUILTIN_DOCKER_BOTS:
        item = bot["item"]
        if any(existing.get("id") == item["id"] or set(existing.get("containers", [])) & set(item["containers"])
               for group_items in [items] for existing in group_items):
            continue
        items.append(copy.deepcopy(item))
        for name in item["containers"]:
            if not any(entry["name"] == name for entry in containers):
                containers.append({"name": name, "project": item["id"], "critical": True})
        data.setdefault("bot_reports", {}).setdefault(bot["report_key"], copy.deepcopy(bot["report"]))


def load_config(path: str = "config.yaml") -> dict[str, Any]:
    config_path = Path(path)
    if not config_path.exists():
        raise FileNotFoundError(f"Missing {config_path}; copy config.example.yaml to config.yaml")
    with config_path.open("r", encoding="utf-8") as handle:
        data = yaml.safe_load(handle) or {}
    add_builtin_bots(data)
    # The UI project inventory is the authoritative grouping, including stacks
    # whose containers use multiple Compose project names.
    configured = {item['name']: item for item in (data.get('docker', {}).get('containers') or [])}
    for group in (data.get('projects') or {}).values():
        for project in group.get('items', []):
            for name in project.get('containers', []):
                if name in configured:
                    configured[name]['project'] = project['id']
    return data


def env(name: str, required: bool = False) -> str | None:
    value = os.getenv(name)
    if required and not value:
        raise RuntimeError(f"Required environment variable is missing: {name}")
    return value
