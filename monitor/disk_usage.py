"""Disk space per container, read-only: what `docker system df -v` reports, plus log files.

- image:  the container's image (layers shared between images are counted for each).
- data:   files the container wrote into its own writable layer.
- volumes: named Docker volumes it mounts (databases usually live here).
- logs:   Docker's json log file and its rotated copies. These live on the host under
          /var/lib/docker/containers, which docker-compose.yml mounts read-only. Without
          that mount the size is unknown (None), not zero.

Folders bind-mounted from the host (e.g. ./data) are not visible to the monitor and
are not counted.
"""
from __future__ import annotations

import glob
import os
from typing import Any

import docker


def docker_disk() -> dict[str, Any]:
    """One `docker system df` snapshot, indexed for per-container lookups. Takes a few seconds."""
    client = docker.from_env(timeout=120)
    try:
        df = client.df()
    finally:
        client.close()
    containers = {}
    for item in df.get("Containers") or []:
        name = ((item.get("Names") or ["?"])[0]).lstrip("/")
        containers[name] = {
            "image_id": item.get("ImageID"),
            "data": max(int(item.get("SizeRw") or 0), 0),
            "volumes": [mount["Name"] for mount in item.get("Mounts") or []
                        if mount.get("Type") == "volume" and mount.get("Name")],
        }
    return {
        "containers": containers,
        "images": {image["Id"]: max(int(image.get("Size") or 0), 0) for image in df.get("Images") or []},
        # Docker reports -1 when it has not measured a volume; treat that as unknown.
        "volumes": {volume["Name"]: (volume.get("UsageData") or {}).get("Size", -1) for volume in df.get("Volumes") or []},
        "layers": max(int(df.get("LayersSize") or 0), 0),
    }


def log_size(container: Any) -> int | None:
    """Size of the container's log file plus rotated copies; None when the log folder is not mounted."""
    path = (getattr(container, "attrs", None) or {}).get("LogPath")
    if not path or not os.path.exists(path):
        return None
    total = 0
    for file in glob.glob(glob.escape(path) + "*"):
        try:
            total += os.path.getsize(file)
        except OSError:
            continue
    return total


def project_disk(names: list[str], snapshot: dict[str, Any], logs: dict[str, int | None]) -> dict[str, Any]:
    """Disk used by a project's containers. Shared images and volumes are counted once per project."""
    images, volumes, data, log_total, log_known = set(), set(), 0, 0, False
    for name in names:
        info = snapshot["containers"].get(name)
        if info:
            data += info["data"]
            if info["image_id"]:
                images.add(info["image_id"])
            volumes.update(info["volumes"])
        if logs.get(name) is not None:
            log_total += logs[name]
            log_known = True
    image_bytes = sum(snapshot["images"].get(image, 0) for image in images)
    volume_bytes = sum(size for size in (snapshot["volumes"].get(volume, -1) for volume in volumes) if size >= 0)
    parts = {"volumes": volume_bytes, "images": image_bytes, "data": data, "logs": log_total if log_known else None}
    return {**parts, "total": sum(value for value in parts.values() if value)}


def docker_total(snapshot: dict[str, Any], logs: dict[str, int | None]) -> int:
    """Everything Docker stores: image layers (each once), container data, volumes and known logs."""
    return (snapshot["layers"] + sum(item["data"] for item in snapshot["containers"].values())
            + sum(size for size in snapshot["volumes"].values() if size >= 0)
            + sum(size for size in logs.values() if size))
