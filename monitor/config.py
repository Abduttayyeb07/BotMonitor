from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml


def load_config(path: str = "config.yaml") -> dict[str, Any]:
    config_path = Path(path)
    if not config_path.exists():
        raise FileNotFoundError(f"Missing {config_path}; copy config.example.yaml to config.yaml")
    with config_path.open("r", encoding="utf-8") as handle:
        data = yaml.safe_load(handle) or {}
    return data


def env(name: str, required: bool = False) -> str | None:
    value = os.getenv(name)
    if required and not value:
        raise RuntimeError(f"Required environment variable is missing: {name}")
    return value
