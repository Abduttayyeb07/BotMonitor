from __future__ import annotations

import html
import logging
import sqlite3
import threading
import time
from datetime import datetime, timezone
from typing import Any
from contextlib import closing

from concurrent.futures import ThreadPoolExecutor

import docker
import psutil
import requests
from .bot_details import bot_details, frontend_details
from .bot_info import container_info, live_usage, local_date
from .disk_usage import docker_disk, docker_total, log_size, project_disk
from .view import (MAX_MESSAGE, Page, card, code_box, grouped_card, log_lines, log_messages, memory_size,
                   newest_lines_that_fit, split_html)
from .docker_checks import inventory
from .checks import read_systemd_snapshot
from .removals import apply_removals, project_key, remove_item, removed_items, removed_keys, restore_item, service_key
from .reports import collect_bots_report, frontend_endpoint_results, frontend_report_items, frontend_report_message

log = logging.getLogger(__name__)


def esc(value: Any) -> str:
    return html.escape(str(value), quote=False)


def disk_split(disk: dict[str, Any]) -> str | None:
    """'vol 11.1 GB · img 1.1 GB · logs 90 MB' — only parts of at least 1 KB, biggest first."""
    parts = [(label, disk[key]) for key, label in (("volumes", "vol"), ("images", "img"), ("logs", "logs"), ("data", "data"))
             if (disk.get(key) or 0) >= 1024]
    return " · ".join(f"{label} {memory_size(size)}" for label, size in sorted(parts, key=lambda part: -part[1])) or None


def short_status(container: Any) -> tuple[str, str]:
    state = container.attrs.get("State", {})
    status = state.get("Status", "unknown")
    health = state.get("Health", {}).get("Status")
    if status != "running":
        return "🔴", status.upper()
    if health == "unhealthy":
        return "🟠", "UNHEALTHY"
    if health == "starting":
        return "🟡", "STARTING"
    return "🟢", "RUNNING"


def uptime(started_at: str | None) -> str:
    if not started_at or started_at.startswith("0001"):
        return "unknown"
    try:
        started = datetime.fromisoformat(started_at.replace("Z", "+00:00"))
        seconds = max(0, int((datetime.now(timezone.utc) - started).total_seconds()))
        days, seconds = divmod(seconds, 86400)
        hours, seconds = divmod(seconds, 3600)
        minutes = seconds // 60
        if days:
            return f"{days}d {hours}h"
        if hours:
            return f"{hours}h {minutes}m"
        return f"{minutes}m"
    except (TypeError, ValueError):
        return "unknown"


class TelegramBotPanel:
    def __init__(self, token: str | None, allowed_chat_ids: list[str], projects: dict[str, Any], allow_restart: bool = False, database_path: str = '/data/incidents.db', frontend_report_config: dict[str, Any] | None = None, bot_report_config: dict[str, Any] | None = None, base_config: dict[str, Any] | None = None):
        self.base_config = base_config
        self.token = token
        self.allowed_chat_ids = set(allowed_chat_ids)
        self.projects = projects or {}
        self.allow_restart = allow_restart
        self.database_path = database_path
        self.frontend_report_config = frontend_report_config or {}
        self.bot_report_config = bot_report_config or {}
        self.inventory_cache = {}
        self.inventory_at = 0.0
        self.info_cache: dict[str, dict[str, Any]] = {}
        self.disk_cache: tuple[float, dict[str, Any]] | None = None
        self.stop_event = threading.Event()
        self.offset = 0
        self.api_url = f"https://api.telegram.org/bot{token}" if token else ""

    def refresh(self) -> None:
        """Re-read the removed-items list so menus and reports match what is monitored."""
        if self.base_config is None:
            return
        try:
            config = apply_removals(self.base_config, removed_keys(self.database_path))
        except (sqlite3.Error, OSError):
            log.exception("Could not read removed items")
            return
        self.projects = config.get("projects") or {}
        self.frontend_report_config = (config.get("daily_reports") or {}).get("frontend", {})
        self.bot_report_config = config.get("bot_reports") or {}

    def remove_confirm_page(self, group_id: str, project_id: str, service_name: str | None = None) -> tuple[str, list[list[dict[str, str]]]]:
        project = self.project_by_id(group_id, project_id)
        service = next((s for s in (project or {}).get("services", []) if s["name"] == service_name), None) if service_name else None
        if not project or (service_name and not service):
            return "<b>Item not found</b>\n\nIt may already have been removed.", [[{"text": "⬅️ Groups", "callback_data": "home"}]]
        if service:
            label, back = service.get("menu_name", service["name"]), f"service:{group_id}:{project_id}:{project['services'].index(service)}"
            yes = f"rmsvc:{group_id}:{project_id}:{service_name}"
        else:
            label, back = project.get("menu_name", project["name"]), f"project:{group_id}:{project_id}"
            yes = f"rm:{group_id}:{project_id}"
        return (f"🗑 <b>Remove from monitoring?</b>\n\n<code>{esc(label)}</code> will no longer be checked, reported, or alerted on, "
                "and its open incidents will be closed without a notification.\n\n"
                "The application itself is not touched. Use /removed to bring it back."), [
                    [{"text": "✅ Yes, remove", "callback_data": yes}, {"text": "❌ Cancel", "callback_data": back}]]

    def remove_item(self, group_id: str, project_id: str, service_name: str | None = None) -> tuple[str, list[list[dict[str, str]]]]:
        project = self.project_by_id(group_id, project_id)
        service = next((s for s in (project or {}).get("services", []) if s["name"] == service_name), None) if service_name else None
        if not project or (service_name and not service):
            return "<b>Item not found</b>\n\nIt may already have been removed.", [[{"text": "⬅️ Groups", "callback_data": "home"}]]
        if service:
            key, label = service_key(group_id, project_id, service_name), service.get("menu_name", service["name"])
        else:
            key, label = project_key(group_id, project_id), project.get("menu_name", project["name"])
        try:
            remove_item(self.database_path, key, label)
        except (sqlite3.Error, OSError) as exc:
            log.exception("Could not remove %s", key)
            return f"❌ <b>Removal failed</b>\n\n<code>{esc(exc)}</code>", [[{"text": "⬅️ Groups", "callback_data": "home"}]]
        self.refresh()
        log.warning("removed from monitoring: %s", key)
        return (f"✅ <b>Removed</b>\n\n<code>{esc(label)}</code> is no longer monitored.\n\nUse /removed to restore it."), [
            [{"text": "⬅️ Back", "callback_data": f"group:{group_id}"}]]

    def removed_page(self, notice: str = "") -> tuple[str, list[list[dict[str, str]]]]:
        try:
            items = removed_items(self.database_path)
        except (sqlite3.Error, OSError):
            items = []
        text = notice + ("🗑 <b>Removed from monitoring</b>\n\n" + "\n".join(f"• {esc(item['label'])}" for item in items)
                         if items else "🗑 <b>Removed from monitoring</b>\n\nNothing has been removed.")
        buttons = [[{"text": f"↩️ Restore {item['label'][:30]}", "callback_data": f"restore:{item['id']}"}] for item in items]
        buttons.append([{"text": "⬅️ Groups", "callback_data": "home"}])
        return text, buttons

    def restore(self, item_id: int) -> tuple[str, list[list[dict[str, str]]]]:
        try:
            label = restore_item(self.database_path, item_id)
        except (sqlite3.Error, OSError):
            log.exception("Could not restore item %s", item_id)
            label = None
        self.refresh()
        notice = f"✅ <b>Restored</b> <code>{esc(label)}</code>. Monitoring resumes within seconds.\n\n" if label else ""
        return self.removed_page(notice)

    def overview_text(self) -> str:
        frontend = self.projects.get("frontend", {}).get("items", [])
        docker_bots = self.projects.get("bots_docker", {}).get("items", [])
        system_services = sum(len(item.get("services", [])) for item in self.projects.get("bots_systemd", {}).get("items", []))
        container_names = [name for group in self.projects.values() for project in group.get("items", []) for name in project.get("containers", [])]
        down = 0
        unhealthy = 0
        for name in container_names:
            container = self.container(name)
            if container is None or container.status != "running":
                down += 1
            elif container.attrs.get("State", {}).get("Health", {}).get("Status") == "unhealthy":
                unhealthy += 1
        try:
            with closing(sqlite3.connect(self.database_path)) as database:
                active_incidents = database.execute("SELECT COUNT(*) FROM incidents WHERE status='OPEN'").fetchone()[0]
        except (sqlite3.Error, OSError):
            active_incidents = "unknown"
        if down or unhealthy or active_incidents not in (0, "unknown"):
            current_status = "⚠️ Attention required"
        else:
            current_status = "🟢 All systems operational"
        page = Page("🤖", "Central Bot Monitor", "Live monitoring for frontends, Docker bots and system services")
        page.add(card("📊 Monitoring", [("Frontends", len(frontend)), ("Docker bots", len(docker_bots)),
                                       ("System services", system_services), ("Containers", len(container_names))]))
        page.add(card("🚨 Status", [("Overall", current_status), ("Down", down), ("Unhealthy", unhealthy),
                                   ("Active incidents", active_incidents)]))
        page.add("<i>Choose a section below 👇</i>")
        return page.footer().html()

    def api(self, method: str, payload: dict[str, Any], timeout: int = 35) -> dict[str, Any] | None:
        try:
            response = requests.post(f"{self.api_url}/{method}", json=payload, timeout=timeout)
            response.raise_for_status()
            data = response.json()
            if not data.get("ok"):
                log.error("Telegram API %s failed: %s", method, data)
                return None
            return data
        except (requests.Timeout, requests.ConnectionError) as exc:
            # Routine network blips (long-poll timeouts, dropped connections): one line, no traceback.
            log.warning("Telegram API %s unreachable (%s), will retry", method, type(exc).__name__)
            return None
        except requests.RequestException:
            log.exception("Telegram API request failed: %s", method)
            return None

    def send(self, chat_id: str, text: str, keyboard: list[list[dict[str, str]]] | None = None) -> int | None:
        """Send a message, split into parts if it is over Telegram's limit (buttons on the last part).

        Returns the ID of the last message sent, so it can be edited later.
        """
        parts = split_html(text)
        message_id = None
        for number, part in enumerate(parts, start=1):
            payload: dict[str, Any] = {"chat_id": chat_id, "text": part, "parse_mode": "HTML"}
            if keyboard and number == len(parts):
                payload["reply_markup"] = {"inline_keyboard": keyboard}
            sent = self.api("sendMessage", payload)
            message_id = ((sent or {}).get("result") or {}).get("message_id", message_id)
        return message_id

    def answer_callback(self, callback_id: str) -> None:
        self.api("answerCallbackQuery", {"callback_query_id": callback_id}, timeout=10)

    def edit(self, chat_id: str, message_id: int, text: str, keyboard: list[list[dict[str, str]]] | None = None) -> None:
        """Edit a message in place; any overflow past Telegram's limit follows as new messages."""
        first, *overflow = split_html(text)
        payload: dict[str, Any] = {"chat_id": chat_id, "message_id": message_id, "text": first, "parse_mode": "HTML"}
        if keyboard:
            payload["reply_markup"] = {"inline_keyboard": keyboard}
        self.api("editMessageText", payload)
        for part in overflow:
            self.send(chat_id, part)

    def group_keyboard(self) -> list[list[dict[str, str]]]:
        buttons = []
        for group_id, group in self.projects.items():
            buttons.append({"text": f"{group.get('icon', '📁')} {group.get('title', group_id)}", "callback_data": f"group:{group_id}"})
        if len(buttons) == 3:
            return [buttons[:2], buttons[2:]]
        return [buttons[index:index + 2] for index in range(0, len(buttons), 2)]

    def group_page(self, group_id: str) -> tuple[str, list[list[dict[str, str]]]]:
        group = self.projects.get(group_id, {})
        service_only = group.get("items") and all((item.get("services") and not item.get("containers")) for item in group["items"])
        label = "service" if service_only else "project"
        return f"{group.get('icon', '📁')} <b>{esc(group.get('title', group_id))}</b>\n\nChoose a {label}:", self.project_keyboard(group_id)

    def command_help(self) -> str:
        return ("🤖 <b>Central Bot Monitor Commands</b>\n\n"
                "/start or /menu — main dashboard\n"
                "/status — current monitoring overview\n"
                "/frontend — frontend projects\n"
                "/docker or /bots — Docker bot projects\n"
                "/services — system services\n"
                "/incidents — active incidents\n"
                "/usage — top 10 projects by memory use\n"
                "/removed — items removed from monitoring (restore)\n"
                "/frontendhealth — frontend health report\n"
                "/botshealth — bot health report\n"
                "/help — show this help")

    def active_incidents_text(self) -> str:
        try:
            with closing(sqlite3.connect(self.database_path)) as database:
                rows = database.execute(
                    "SELECT project, service, incident_type, severity, first_seen, occurrences "
                    "FROM incidents WHERE status='OPEN' ORDER BY first_seen DESC LIMIT 20"
                ).fetchall()
        except sqlite3.Error:
            rows = []
        if not rows:
            return "✅ <b>Active Incidents</b>\n\nNo active incidents."
        lines = ["🚨 <b>Active Incidents</b>", ""]
        for project, service, incident_type, severity, first_seen, occurrences in rows:
            lines.append(f"🔴 <b>{esc(project)}</b> / <code>{esc(service)}</code>\n"
                         f"   {esc(incident_type.replace('_', ' ').title())} · {esc(severity)} · {occurrences} occurrence(s)")
        return "\n\n".join(lines)

    def register_commands(self) -> None:
        self.api("setMyCommands", {"commands": [
            {"command": "start", "description": "Open monitoring dashboard"},
            {"command": "status", "description": "Show current overview"},
            {"command": "frontend", "description": "Show frontend projects"},
            {"command": "docker", "description": "Show Docker bot projects"},
            {"command": "services", "description": "Show system services"},
            {"command": "incidents", "description": "Show active incidents"},
            {"command": "usage", "description": "Top 10 projects by memory use"},
            {"command": "removed", "description": "Restore removed items"},
            {"command": "frontendhealth", "description": "Show frontend health report"},
            {"command": "botshealth", "description": "Show bot health report"},
            {"command": "help", "description": "Show available commands"},
        ]}, timeout=15)

    def frontend_health_report_text(self) -> str:
        items = frontend_report_items(self.frontend_report_config, self.projects)
        return frontend_report_message(frontend_endpoint_results(items))

    def bots_health_report_text(self) -> str:
        return collect_bots_report(self.bot_report_config)

    def project_keyboard(self, group_id: str) -> list[list[dict[str, str]]]:
        group = self.projects.get(group_id, {})
        service_only = group.get("items") and all((item.get("services") and not item.get("containers")) for item in group["items"])
        if service_only:
            buttons = []
            for project in group["items"]:
                for index, service in enumerate(project.get("services", [])):
                    buttons.append({"text": f"⚙️ {service.get('menu_name', service['name'])}", "callback_data": f"service:{group_id}:{project['id']}:{index}"})
            rows = [buttons[index:index + 2] for index in range(0, len(buttons), 2)]
            rows.append([{"text": "⬅️ Groups", "callback_data": "home"}])
            return rows
        buttons = []
        for project in group.get("items", []):
            buttons.append({"text": f"📊 {project.get('menu_name', project['name'])}", "callback_data": f"project:{group_id}:{project['id']}"})
        rows = [buttons[index:index + 2] for index in range(0, len(buttons), 2)]
        rows.append([{"text": "⬅️ Groups", "callback_data": "home"}])
        return rows

    def project_by_id(self, group_id: str, project_id: str) -> dict[str, Any] | None:
        group = self.projects.get(group_id, {})
        return next((item for item in group.get("items", []) if item.get("id") == project_id), None)

    def container(self, name: str) -> Any | None:
        try:
            if time.monotonic() - self.inventory_at > 2:
                self.inventory_cache = inventory()
                self.inventory_at = time.monotonic()
            return self.inventory_cache.get(name)
        except Exception:
            log.error('Telegram container inventory unavailable')
            return None

    def bot_info(self, container: Any) -> dict[str, Any]:
        """Stack/library facts, cached per container ID (a recreated container gets a new ID)."""
        key = getattr(container, "id", None) or container.name
        if key not in self.info_cache:
            try:
                self.info_cache[key] = container_info(container)
            except Exception:
                log.exception("Could not read bot info for %s", getattr(container, "name", key))
                return {}
        return self.info_cache[key]

    def systemd_status(self, unit: str) -> dict[str, Any]:
        try:
            return read_systemd_snapshot("/data/systemd-status.json").get("services", {}).get(unit, {})
        except (OSError, ValueError, KeyError, TypeError):
            return {}

    def incident_history(self, project_name: str, services: list[str] | None = None) -> tuple[int, int, int]:
        try:
            services = services or []
            clause = 'project=?'
            if services:
                clause += ' OR service IN (' + ','.join('?' for _ in services) + ')'
            with closing(sqlite3.connect(self.database_path)) as database:
                total, active, recovered = database.execute(
                    "SELECT COUNT(*), SUM(status='OPEN'), SUM(status='RECOVERED') FROM incidents WHERE " + clause,
                    (project_name, *services),
                ).fetchone()
                return total or 0, active or 0, recovered or 0
        except (sqlite3.Error, OSError):
            return 0, 0, 0

    @staticmethod
    def health_bar(total: int, running: int, unhealthy: int) -> str:
        if not total:
            return "⬜⬜⬜⬜⬜⬜⬜⬜⬜⬜"
        blocks = []
        for index in range(10):
            threshold = (index + 1) * total / 10
            if threshold <= running:
                blocks.append("🟦")
            else:
                blocks.append("🟥")
        return "".join(blocks)

    def pretty_project_page(self, group_id: str, project_id: str) -> tuple[str, list[list[dict[str, str]]]]:
        project = self.project_by_id(group_id, project_id)
        if not project:
            return "<b>Project not found</b>", [[{"text": "⬅️ Groups", "callback_data": "home"}]]
        containers = project.get("containers") or []
        found = [self.container(name) for name in containers]
        running = sum(1 for container in found if container is not None and short_status(container)[1] == "RUNNING")
        mark = "✅" if running == len(containers) else "⚠️" if running else "🔴"
        page = Page("📊", project["name"], f"{mark} {running}/{len(containers)} containers running" if containers else None)
        # One card per container, the same for every bot and frontend. Long projects start collapsed.
        for name, container in zip(containers, found):
            page.add(self.container_card(name, container, expandable=len(containers) > 2))
        services = project.get("services") or []
        if services:
            states = [(service["name"], self.systemd_status(service["unit"]).get("active_state", "unknown")) for service in services]
            page.add(card("⚙️ System Services", [(name, f"{'🟢' if state == 'active' else '🔴'} {state.upper()}") for name, state in states]))
        total, active, recovered = self.incident_history(project_id, containers)
        page.add(card("📚 Incident History", [("Total", total), ("Active", active), ("Recovered", recovered)]))
        page.footer()
        buttons = [[{"text": f"🔎 {name[:30]}", "callback_data": f"container:{group_id}:{project_id}:{index}"}]
                   for index, name in enumerate(containers)]
        buttons += [[{"text": f"⚙️ {service['name'][:30]}", "callback_data": f"service:{group_id}:{project_id}:{index}"}]
                    for index, service in enumerate(services)]
        # One Bot Info button per bot or frontend, right under its containers.
        buttons.append([{"text": "ℹ️ Bot Info", "callback_data": f"botinfo:{group_id}:{project_id}"}])
        buttons.append([{"text": "🗑 Remove from Monitoring", "callback_data": f"rm-confirm:{group_id}:{project_id}"}])
        buttons.append([{"text": "⬅️ Projects", "callback_data": f"group:{group_id}"}])
        return page.html(), buttons

    def container_card(self, name: str, container: Any, expandable: bool = False) -> str:
        """A container's Runtime and Environment as one card with tree lines."""
        if container is None:
            return grouped_card(f"🐳 {esc(name)}", [("⚙️ Runtime", [("Status", "🔴 Stopped or unavailable")])], expandable)
        icon, status = short_status(container)
        state = container.attrs.get("State", {})
        info = self.bot_info(container)
        runtime_name, _, version = (info.get("runtime") or "").partition(" ")
        if runtime_name == "Node.js" and version and not version.startswith("v"):
            version = f"v{version}"  # as `node --version` prints it
        libraries = info.get("libraries")
        return grouped_card(f"🐳 {esc(name)}", [
            ("⚙️ Runtime", [("Status", f"{icon} {status.title()}"), ("Uptime", uptime(state.get("StartedAt"))),
                           ("Restarts", container.attrs.get("RestartCount", 0))]),
            ("🧩 Environment", [
                (runtime_name, version) if version else ("Stack", runtime_name or None),
                ("Libraries", f"{libraries['libraries']}" + (f" (+{libraries['dev_libraries']} dev)" if libraries["dev_libraries"] else "")) if libraries else None,
                ("Updated", local_date(info.get("created"), with_time=False))]),
        ], expandable)

    def bot_info_page(self, group_id: str, project_id: str) -> tuple[str, list[list[dict[str, str]]]]:
        """What is specific to this bot (exchanges, slots, RPC heights, ...) or frontend (health endpoint)."""
        project = self.project_by_id(group_id, project_id)
        if not project:
            return "<b>Project not found</b>", [[{"text": "⬅️ Groups", "callback_data": "home"}]]
        if group_id == "frontend":
            details = frontend_details(project, self.frontend_report_config, self.projects)
        else:
            details = bot_details(project, self.bot_report_config)
        page = Page("ℹ️", project["name"], "Bot Info")
        page.add(details or "<i>No bot-specific check is set up for this one yet.</i>")
        return page.footer().html(), [
            [{"text": "🔄 Refresh", "callback_data": f"botinfo:{group_id}:{project_id}"}],
            [{"text": "⬅️ Project", "callback_data": f"project:{group_id}:{project_id}"}]]

    def project_page(self, group_id: str, project_id: str) -> tuple[str, list[list[dict[str, str]]]]:
        project = self.project_by_id(group_id, project_id)
        if not project:
            return "<b>Project not found</b>", [[{"text": "⬅️ Groups", "callback_data": "home"}]]
        lines = [f"📊 <b>{esc(project['name'])}</b>", "<code>Container                  Status       Uptime</code>"]
        for name in project.get("containers", []):
            container = self.container(name)
            if container is None:
                lines.append(f"🔴 <code>{esc(name[:25]):25} DOWN         unknown</code>")
                continue
            icon, status = short_status(container)
            state = container.attrs.get("State", {})
            lines.append(f"{icon} <code>{esc(name[:25]):25} {status:11} {uptime(state.get('StartedAt'))}</code>")
        services = project.get("services") or []
        if services:
            lines.extend(["", "<b>Systemd services</b>"])
            for service in services:
                status = self.systemd_status(service["unit"]).get("active_state", "unknown")
                icon = "🟢" if status == "active" else "🔴"
                lines.append(f"{icon} <code>{esc(service['name'][:25]):25} {esc(status.upper())}</code>")
        lines.append(f"\n<b>Containers:</b> {len(project.get('containers', []))}")
        buttons = [[{"text": f"🔎 {name[:24]}", "callback_data": f"container:{group_id}:{project_id}:{index}"}] for index, name in enumerate(project.get("containers", []))]
        buttons.extend([[{"text": f"⚙️ {service['name'][:24]}", "callback_data": f"service:{group_id}:{project_id}:{index}"}] for index, service in enumerate(services)])
        buttons.append([{"text": "⬅️ Projects", "callback_data": f"group:{group_id}"}])
        return "\n".join(lines), buttons

    def container_page(self, group_id: str, project_id: str, index: int) -> tuple[str, list[list[dict[str, str]]]]:
        project = self.project_by_id(group_id, project_id)
        names = project.get("containers", []) if project else []
        if not project or index < 0 or index >= len(names):
            return "<b>Container not found</b>", [[{"text": "⬅️ Groups", "callback_data": "home"}]]
        name = names[index]
        container = self.container(name)
        if container is None:
            page = Page("🔴", name, "Container not found").add(card("📦 Container Status", [("Status", "DOWN")]))
            text = page.footer().html()
        else:
            icon, status = short_status(container)
            state = container.attrs.get("State", {})
            info = self.bot_info(container)
            usage = live_usage(container) or {}
            libraries = info.get("libraries")
            page = Page(icon, name)
            page.add(card("📦 Container Status", [
                ("Status", status), ("Health", (state.get("Health", {}).get("Status") or "not configured").capitalize()),
                ("Uptime", uptime(state.get("StartedAt"))), ("Restarts", container.attrs.get("RestartCount", 0)),
                ("Exit Code", state.get("ExitCode", "unknown"))]))
            page.add(card("📊 System Resources", [
                ("Memory", f"{memory_size(usage['memory'])}" + (f" / {memory_size(usage['memory_limit'])}" if usage.get("memory_limit") else "")
                 if usage.get("memory") is not None else None),
                ("CPU", f"{usage['cpu_percent']:.1f}%" if usage.get("cpu_percent") is not None else None)]))
            page.add(card("🧩 Application", [
                ("Stack", info.get("runtime")),
                ("Dependencies", f"{libraries['libraries']} + {libraries['dev_libraries']} dev" if libraries and libraries["dev_libraries"]
                 else f"{libraries['libraries']}" if libraries else None),
                ("Image", info.get("image"), "code"),
                ("Size", f"{info['image_size'] / 1_000_000:.0f} MB" if info.get("image_size") else None)]))
            page.add(card("🚀 Deployment", [
                ("Built", local_date(info.get("built"))), ("Created", local_date(info.get("created"))),
                ("Compose", f"{info['compose_project']} / {info.get('compose_service') or '-'}" if info.get("compose_project") else None, "code")]))
            page.add(self.recent_logs(container, room=MAX_MESSAGE - len(page.html()) - 120))
            text = page.footer().html()
        buttons = [[{"text": "🔄 Refresh", "callback_data": f"container:{group_id}:{project_id}:{index}"},
                    {"text": "📜 Full logs", "callback_data": f"logs:{group_id}:{project_id}:{index}"}]]
        if self.allow_restart:
            buttons.append([{"text": "♻️ Restart container", "callback_data": f"restart-confirm:{group_id}:{project_id}:{index}"}])
        buttons.append([{"text": "⬅️ Project", "callback_data": f"project:{group_id}:{project_id}"}])
        return text, buttons

    def recent_logs(self, container: Any, room: int) -> str:
        """Recent log lines in the dark code box: newest lines that fit, and a note when some don't."""
        try:
            raw = container.logs(tail=12, timestamps=True).decode("utf-8", errors="replace")
        except Exception as exc:
            return f"📜 <b>Recent Logs</b>\n<i>Unable to read logs: {esc(exc)}</i>"
        lines = log_lines(raw)
        if not lines:
            return "📜 <b>Recent Logs</b>\n<i>No recent logs.</i>"
        kept = newest_lines_that_fit(lines, max(room, 0))
        if not kept:
            return "📜 <b>Recent Logs</b>\n<i>The latest line is too long for this page · tap 📜 Full logs</i>"
        note = f"\n<i>Latest {len(kept)} of {len(lines)} lines · 📜 Full logs for all</i>" if len(kept) < len(lines) else ""
        return f"📜 <b>Recent Logs</b>\n{code_box(kept)}{note}"

    def full_logs(self, group_id: str, project_id: str, index: int, tail: int = 60) -> list[str]:
        """The last `tail` log lines in code boxes, as many messages as they need; nothing is cut."""
        project = self.project_by_id(group_id, project_id)
        names = project.get("containers", []) if project else []
        if not project or index < 0 or index >= len(names):
            return ["<b>Container not found</b>"]
        title = f"📜 <b>{esc(names[index])}</b>"
        container = self.container(names[index])
        if container is None:
            return [f"{title}\n<i>Container could not be found.</i>"]
        try:
            raw = container.logs(tail=tail, timestamps=True).decode("utf-8", errors="replace")
        except Exception as exc:
            return [f"{title}\n<i>Unable to read logs: {esc(exc)}</i>"]
        lines = log_lines(raw)
        return log_messages(f"{title}\n<i>Last {len(lines)} log lines</i>", lines)

    def usage_text(self, top: int = 10) -> str:
        """Projects ranked by live memory (all of a project's containers added up)."""
        projects = [item for group in self.projects.values() for item in group.get("items", []) if item.get("containers")]
        names = sorted({name for item in projects for name in item["containers"]})
        containers = {name: self.container(name) for name in names}
        live = [name for name, container in containers.items() if container is not None and container.status == "running"]
        # Docker needs about a second per container to measure CPU, so all are measured at once.
        with ThreadPoolExecutor(max_workers=10) as pool:
            measured = {name: usage for name, usage in zip(live, pool.map(lambda name: live_usage(containers[name]), live))
                        if usage and usage.get("memory") is not None}
        snapshot = self.disk_snapshot()
        logs = {name: log_size(container) for name, container in containers.items() if container is not None}
        rows = []
        for item in projects:
            parts = [measured[name] for name in item["containers"] if name in measured]
            if parts:
                cpu = [part["cpu_percent"] for part in parts if part.get("cpu_percent") is not None]
                rows.append((sum(part["memory"] for part in parts), sum(cpu) if cpu else None,
                             sum(1 for name in item["containers"] if name in live), len(item["containers"]),
                             item.get("name") or item["id"],
                             project_disk(item["containers"], snapshot, logs) if snapshot else None))
        rows.sort(key=lambda row: row[0], reverse=True)
        ram = psutil.virtual_memory()
        ram_used = ram.total - ram.available
        disk = psutil.disk_usage("/")
        page = Page("💾", "Resource Usage", f"Top {top} projects by RAM")
        page.add(card("🖥 Server", [
            ("RAM", f"{memory_size(ram_used)} of {memory_size(ram.total)} ({ram_used / ram.total * 100:.0f}%)"),
            ("Disk", f"{memory_size(disk.used)} of {memory_size(disk.total)} ({disk.percent:.0f}%)"),
            ("Docker disk", memory_size(docker_total(snapshot, logs)) if snapshot else "unavailable"),
            ("Monitored bots", f"{memory_size(sum(u['memory'] for u in measured.values()))} RAM · {len(live)} containers")]))
        if not rows:
            page.add("<i>Could not read memory for any monitored container.</i>")
        for rank, (memory, cpu, up, total, name, used_disk) in enumerate(rows[:top], start=1):
            badge = {1: "🥇", 2: "🥈", 3: "🥉"}.get(rank, f"{rank}.")
            page.add(card(f"{badge} {esc(name)}", [
                ("RAM", f"{memory_size(memory)} · {memory / ram.total * 100:.1f}%"),
                ("CPU", f"{cpu:.1f}%" if cpu is not None else None),
                ("Disk", memory_size(used_disk["total"]) if used_disk else None),
                ("Disk split", disk_split(used_disk) if used_disk else None),
                ("Containers", f"{up}/{total} running")]))
        if snapshot and not any(size is not None for size in logs.values()):
            page.add("<i>Log files not counted: the Docker log folder is not mounted (see docker-compose.yml).</i>")
        return page.footer().html()

    def disk_snapshot(self) -> dict[str, Any] | None:
        """`docker system df` data, cached for 10 minutes because it takes a few seconds to collect."""
        if self.disk_cache and time.monotonic() - self.disk_cache[0] < 600:
            return self.disk_cache[1]
        try:
            snapshot = docker_disk()
        except Exception:
            log.exception("Docker disk usage unavailable")
            return self.disk_cache[1] if self.disk_cache else None
        self.disk_cache = (time.monotonic(), snapshot)
        return snapshot

    def restart_confirm_page(self, group_id: str, project_id: str, index: int) -> tuple[str, list[list[dict[str, str]]]]:
        project = self.project_by_id(group_id, project_id)
        names = project.get("containers", []) if project else []
        if not project or index < 0 or index >= len(names):
            return "<b>Container not found</b>", [[{"text": "⬅️ Groups", "callback_data": "home"}]]
        name = html.escape(names[index])
        return (f"⚠️ <b>Confirm restart</b>\n\nRestart <code>{name}</code>?\n\n"
                "This will briefly interrupt the container."), [
                    [{"text": "✅ Yes, restart", "callback_data": f"restart:{group_id}:{project_id}:{index}"},
                     {"text": "❌ Cancel", "callback_data": f"container:{group_id}:{project_id}:{index}"}]
                ]

    def restart_container(self, group_id: str, project_id: str, index: int) -> tuple[str, list[list[dict[str, str]]]]:
        project = self.project_by_id(group_id, project_id)
        names = project.get("containers", []) if project else []
        if not project or index < 0 or index >= len(names):
            return "<b>Container not found</b>", [[{"text": "⬅️ Groups", "callback_data": "home"}]]
        name = names[index]
        try:
            container = docker.from_env().containers.get(name)
            container.restart(timeout=20)
            text = f"✅ <b>Restart requested</b>\n\nContainer <code>{html.escape(name)}</code> is restarting."
        except Exception as exc:
            text = f"❌ <b>Restart failed</b>\n\n<code>{html.escape(str(exc))}</code>"
        return text, [[{"text": "🔄 Refresh", "callback_data": f"container:{group_id}:{project_id}:{index}"}],
                      [{"text": "⬅️ Project", "callback_data": f"project:{group_id}:{project_id}"}]]

    def service_page(self, group_id: str, project_id: str, index: int) -> tuple[str, list[list[dict[str, str]]]]:
        project = self.project_by_id(group_id, project_id)
        services = project.get("services", []) if project else []
        if not project or index < 0 or index >= len(services):
            return "<b>Service not found</b>", [[{"text": "⬅️ Groups", "callback_data": "home"}]]
        service = services[index]
        details = self.systemd_status(service["unit"])
        status = details.get("active_state", "unknown")
        page = Page("🟢" if status == "active" else "🔴", service["name"], "System service")
        page.add(card("⚙️ Service Status", [("Unit", service["unit"], "code"), ("Status", status.upper()),
                                           ("Sub-state", details.get("sub_state"))]))
        if service["unit"] == "wallet-watchman.service":
            activity = details.get("activity") or {}
            page.add(card("👛 Wallet Activity", [("Wallets", activity.get("wallet_count") or "unknown"),
                                                ("Last reload", activity.get("last_reload_at") or "unknown"),
                                                ("RPC failures (10m)", activity.get("rpc_failures_10m") or 0)]))
        # Journal lines are shown exactly as journald wrote them, newest that fit.
        journal = [line.rstrip() for line in (details.get("recent_logs") or "").splitlines() if line.strip()]
        kept = newest_lines_that_fit(journal, MAX_MESSAGE - len(page.html()) - 120)
        if not journal:
            page.add("📜 <b>Recent Journal</b>\n<i>No recent journal logs.</i>")
        else:
            note = f"\n<i>Latest {len(kept)} of {len(journal)} lines</i>" if len(kept) < len(journal) else ""
            page.add(f"📜 <b>Recent Journal</b>\n{code_box(kept)}{note}")
        text = page.footer().html()
        return text, [[{"text": "🔄 Refresh", "callback_data": f"service:{group_id}:{project_id}:{index}"}],
                      [{"text": "🗑 Remove from monitoring", "callback_data": f"rmsvc-confirm:{group_id}:{project_id}:{service['name']}"}],
                      [{"text": "⬅️ Services" if group_id == "bots_systemd" else "⬅️ Project",
                        "callback_data": f"group:{group_id}" if group_id == "bots_systemd" else f"project:{group_id}:{project_id}"}]]

    def handle(self, update: dict[str, Any]) -> None:
        message = update.get("message")
        callback = update.get("callback_query")
        chat_id = str((message or callback or {}).get("chat", {}).get("id", ""))
        if callback:
            chat_id = str(callback.get("message", {}).get("chat", {}).get("id", ""))
        if chat_id not in self.allowed_chat_ids:
            log.warning("Ignoring Telegram update from unauthorized chat %s", chat_id)
            return
        self.refresh()
        if message:
            words = (message.get('text') or '').split()
            command = words[0].lower().split('@')[0] if words else ''
            if command in {"/start", "/menu", "/status"}:
                self.send(chat_id, self.overview_text(), self.group_keyboard())
            elif command in {"/frontend"}:
                text, keyboard = self.group_page("frontend")
                self.send(chat_id, text, keyboard)
            elif command in {"/docker", "/bots"}:
                text, keyboard = self.group_page("bots_docker")
                self.send(chat_id, text, keyboard)
            elif command in {"/services"}:
                text, keyboard = self.group_page("bots_systemd")
                self.send(chat_id, text, keyboard)
            elif command == "/incidents":
                self.send(chat_id, self.active_incidents_text())
            elif command == "/usage":
                # Measuring every container takes a few seconds: show that, then replace it with the result.
                waiting = self.send(chat_id, "⏳ <i>Measuring memory and CPU of all monitored projects…</i>")
                if waiting:
                    self.edit(chat_id, waiting, self.usage_text())
                else:
                    self.send(chat_id, self.usage_text())
            elif command == "/removed":
                text, keyboard = self.removed_page()
                self.send(chat_id, text, keyboard)
            elif command in {"/frontendhealth", "/frontendreport"}:
                self.send(chat_id, self.frontend_health_report_text())
            elif command in {"/botshealth", "/botsreport", "/zigwhalehealth", "/zigwhale"}:
                self.send(chat_id, self.bots_health_report_text())
            elif command == "/help":
                self.send(chat_id, self.command_help())
            return
        if not callback:
            return
        self.answer_callback(callback.get("id", ""))
        data = callback.get("data", "")
        message_id = callback.get("message", {}).get("message_id")
        if data == "home":
            text, keyboard = "🤖 <b>Central Bot Monitor</b>\n\nChoose a group:", self.group_keyboard()
        elif data.startswith("group:"):
            group_id = data.split(":", 1)[1]
            group = self.projects.get(group_id, {})
            service_only = group.get("items") and all((item.get("services") and not item.get("containers")) for item in group["items"])
            label = "service" if service_only else "project"
            text, keyboard = f"📁 <b>{esc(group.get('title', group_id))}</b>\n\nChoose a {label}:", self.project_keyboard(group_id)
        elif data.startswith("project:"):
            _, group_id, project_id = data.split(":", 2)
            text, keyboard = self.pretty_project_page(group_id, project_id)
        elif data.startswith("container:"):
            _, group_id, project_id, index = data.split(":", 3)
            text, keyboard = self.container_page(group_id, project_id, int(index))
        elif data.startswith("botinfo:"):
            _, group_id, project_id = data.split(":", 2)
            text, keyboard = self.bot_info_page(group_id, project_id)
        elif data.startswith("logs:"):
            # Sent as new messages under the page, which stays as it is.
            _, group_id, project_id, index = data.split(":", 3)
            for part in self.full_logs(group_id, project_id, int(index)):
                self.send(chat_id, part)
            return
        elif data.startswith("restart-confirm:") and self.allow_restart:
            _, group_id, project_id, index = data.split(":", 3)
            text, keyboard = self.restart_confirm_page(group_id, project_id, int(index))
        elif data.startswith("restart:") and self.allow_restart:
            _, group_id, project_id, index = data.split(":", 3)
            text, keyboard = self.restart_container(group_id, project_id, int(index))
        elif data.startswith("service:"):
            _, group_id, project_id, index = data.split(":", 3)
            text, keyboard = self.service_page(group_id, project_id, int(index))
        elif data.startswith("rm-confirm:"):
            _, group_id, project_id = data.split(":", 2)
            text, keyboard = self.remove_confirm_page(group_id, project_id)
        elif data.startswith("rm:"):
            _, group_id, project_id = data.split(":", 2)
            text, keyboard = self.remove_item(group_id, project_id)
        elif data.startswith("rmsvc-confirm:"):
            _, group_id, project_id, name = data.split(":", 3)
            text, keyboard = self.remove_confirm_page(group_id, project_id, name)
        elif data.startswith("rmsvc:"):
            _, group_id, project_id, name = data.split(":", 3)
            text, keyboard = self.remove_item(group_id, project_id, name)
        elif data.startswith("restore:") and data[8:].isdigit():
            text, keyboard = self.restore(int(data[8:]))
        else:
            return
        if message_id:
            self.edit(chat_id, message_id, text, keyboard)

    def run(self) -> None:
        if not self.token or not self.allowed_chat_ids:
            log.warning("Telegram panel disabled: token or authorized chat IDs are missing")
            return
        log.info("Telegram panel started: groups=%s, authorized_chats=%s", len(self.projects), len(self.allowed_chat_ids))
        self.register_commands()
        while not self.stop_event.is_set():
            response = self.api("getUpdates", {"offset": self.offset, "timeout": 25, "allowed_updates": ["message", "callback_query"]}, timeout=35)
            if not response:
                time.sleep(2)
                continue
            for update in response.get("result", []):
                self.offset = update["update_id"] + 1
                self.handle(update)

    def stop(self) -> None:
        self.stop_event.set()
