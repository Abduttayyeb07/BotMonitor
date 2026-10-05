from __future__ import annotations

import html
import json
import logging
import sqlite3
import threading
import time
from datetime import datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo
from contextlib import closing

import docker
import requests
from .docker_checks import inventory
from .reports import frontend_endpoint_results, frontend_report_items, frontend_report_message

log = logging.getLogger(__name__)


def esc(value: Any) -> str:
    return html.escape(str(value), quote=False)


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
    def __init__(self, token: str | None, allowed_chat_ids: list[str], projects: dict[str, Any], allow_restart: bool = False, database_path: str = '/data/incidents.db', frontend_report_config: dict[str, Any] | None = None):
        self.token = token
        self.allowed_chat_ids = set(allowed_chat_ids)
        self.projects = projects or {}
        self.allow_restart = allow_restart
        self.database_path = database_path
        self.frontend_report_config = frontend_report_config or {}
        self.inventory_cache = {}
        self.inventory_at = 0.0
        self.stop_event = threading.Event()
        self.offset = 0
        self.api_url = f"https://api.telegram.org/bot{token}" if token else ""

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
        updated = datetime.now(ZoneInfo("Asia/Karachi")).strftime("%d %b %Y, %H:%M PKT")
        return ("🤖 <b>Central Bot Monitor</b>\n\n"
                "Live monitoring and control for frontend applications, Docker workloads, and system services.\n\n"
                "<b>📊 Monitoring Overview</b>\n\n"
                f"🖥 <b>Frontend Projects:</b> {len(frontend)}\n"
                f"🐳 <b>Docker Bot Projects:</b> {len(docker_bots)}\n"
                f"⚙️ <b>System Services:</b> {system_services}\n"
                f"📦 <b>Docker Containers:</b> {len(container_names)}\n\n"
                "<b>🚨 Current Status</b>\n\n"
                f"{current_status}\n"
                f"<b>Active Incidents:</b> {active_incidents}\n\n"
                f"🔄 <b>Last Updated:</b> {updated}\n\n"
                "Select a section below to view:\n\n"
                "• Project health\n"
                "• Uptime and availability\n"
                "• Container status\n"
                "• Logs and errors\n"
                "• Service controls")

    def api(self, method: str, payload: dict[str, Any], timeout: int = 35) -> dict[str, Any] | None:
        try:
            response = requests.post(f"{self.api_url}/{method}", json=payload, timeout=timeout)
            response.raise_for_status()
            data = response.json()
            if not data.get("ok"):
                log.error("Telegram API %s failed: %s", method, data)
                return None
            return data
        except requests.RequestException:
            log.exception("Telegram API request failed: %s", method)
            return None

    def send(self, chat_id: str, text: str, keyboard: list[list[dict[str, str]]] | None = None) -> None:
        payload: dict[str, Any] = {"chat_id": chat_id, "text": text, "parse_mode": "HTML"}
        if keyboard:
            payload["reply_markup"] = {"inline_keyboard": keyboard}
        self.api("sendMessage", payload)

    def answer_callback(self, callback_id: str) -> None:
        self.api("answerCallbackQuery", {"callback_query_id": callback_id}, timeout=10)

    def edit(self, chat_id: str, message_id: int, text: str, keyboard: list[list[dict[str, str]]] | None = None) -> None:
        payload: dict[str, Any] = {"chat_id": chat_id, "message_id": message_id, "text": text, "parse_mode": "HTML"}
        if keyboard:
            payload["reply_markup"] = {"inline_keyboard": keyboard}
        self.api("editMessageText", payload)

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
                "/frontendhealth — frontend health report\n"
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
            {"command": "frontendhealth", "description": "Show frontend health report"},
            {"command": "help", "description": "Show available commands"},
        ]}, timeout=15)

    def frontend_health_report_text(self) -> str:
        items = frontend_report_items(self.frontend_report_config, self.projects)
        return frontend_report_message(frontend_endpoint_results(items))

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

    def systemd_status(self, unit: str) -> dict[str, Any]:
        try:
            with open("/data/systemd-status.json", encoding="utf-8") as handle:
                return json.load(handle).get("services", {}).get(unit, {})
        except (FileNotFoundError, json.JSONDecodeError, OSError):
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
        rows = []
        running = 0
        unhealthy = 0
        for name in containers:
            container = self.container(name)
            if container is None:
                rows.append(f"🟥 <b>{esc(name)}</b>\n   <i>Stopped or unavailable</i>")
                continue
            icon, status = short_status(container)
            state = container.attrs.get("State", {})
            if status == "RUNNING":
                running += 1
            if status == "UNHEALTHY":
                unhealthy += 1
            rows.append(
                f"{icon} <b>{esc(name)}</b>\n"
                f"   <i>{esc(status.title())}  •  Uptime {uptime(state.get('StartedAt'))}  •  Restarts {container.attrs.get('RestartCount', 0)}</i>"
            )
        total, active, recovered = self.incident_history(project_id, containers)
        lines = [
            f"📊 <b>{esc(project['name'])}</b>",
            f"{self.health_bar(len(containers), running, unhealthy)}  <b>{running}/{len(containers)} running</b>",
            "",
            "\n\n".join(rows) if rows else "<i>No Docker containers configured.</i>",
            "",
            f"📚 <b>Incident history:</b> {total} total  •  {active} active  •  {recovered} recovered",
        ]
        services = project.get("services") or []
        if services:
            lines.append("\n<b>Systemd services</b>")
            for service in services:
                details = self.systemd_status(service["unit"])
                status = details.get("active_state", "unknown")
                icon = "🟢" if status == "active" else "🔴"
                lines.append(f"{icon} <b>{esc(service['name'])}</b>  <i>{esc(status.upper())}</i>")
        container_buttons = [
            {"text": f"🔎 {name[:22]}", "callback_data": f"container:{group_id}:{project_id}:{index}"}
            for index, name in enumerate(containers)
        ]
        buttons = [container_buttons[index:index + 2] for index in range(0, len(container_buttons), 2)]
        service_buttons = [
            {"text": f"⚙️ {service['name'][:22]}", "callback_data": f"service:{group_id}:{project_id}:{index}"}
            for index, service in enumerate(services)
        ]
        buttons.extend([service_buttons[index:index + 2] for index in range(0, len(service_buttons), 2)])
        buttons.append([{"text": "⬅️ Projects", "callback_data": f"group:{group_id}"}])
        return "\n".join(lines), buttons

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
            text = f"🔴 <b>{esc(name)}</b>\n\n<b>Status:</b> DOWN\nContainer could not be found."
        else:
            icon, status = short_status(container)
            state = container.attrs.get("State", {})
            health = state.get("Health", {}).get("Status", "not configured")
            restarts = container.attrs.get("RestartCount", 0)
            try:
                raw_logs = container.logs(tail=12, timestamps=True).decode("utf-8", errors="replace")
            except Exception as exc:
                raw_logs = f"Unable to read logs: {exc}"
            logs = esc(raw_logs[-2800:] or "No recent logs.")
            text = (f"{icon} <b>{esc(name)}</b>\n\n"
                    f"<b>Status:</b> {esc(status)}\n"
                    f"<b>Health:</b> {esc(health)}\n"
                    f"<b>Uptime:</b> {esc(uptime(state.get('StartedAt')))}\n"
                    f"<b>Restarts:</b> {restarts}\n"
                    f"<b>Exit code:</b> {state.get('ExitCode', 'unknown')}\n\n"
                    f"<b>Recent logs</b>\n<pre>{logs}</pre>")
        buttons = [[{"text": "🔄 Refresh", "callback_data": f"container:{group_id}:{project_id}:{index}"}]]
        if self.allow_restart:
            buttons.append([{"text": "♻️ Restart container", "callback_data": f"restart-confirm:{group_id}:{project_id}:{index}"}])
        buttons.append([{"text": "⬅️ Project", "callback_data": f"project:{group_id}:{project_id}"}])
        return text, buttons

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
        logs = details.get("recent_logs", "No recent journal logs.")
        icon = "🟢" if status == "active" else "🔴"
        text = (f"{icon} <b>{esc(service['name'])}</b>\n\n"
                f"<b>Unit:</b> <code>{esc(service['unit'])}</code>\n"
                f"<b>Status:</b> {esc(status.upper())}\n\n"
                f"<b>Recent journal</b>\n<pre>{esc(logs)}</pre>")
        return text, [[{"text": "🔄 Refresh", "callback_data": f"service:{group_id}:{project_id}:{index}"}],
                      [{"text": "⬅️ Project", "callback_data": f"project:{group_id}:{project_id}"}]]

    def handle(self, update: dict[str, Any]) -> None:
        message = update.get("message")
        callback = update.get("callback_query")
        chat_id = str((message or callback or {}).get("chat", {}).get("id", ""))
        if callback:
            chat_id = str(callback.get("message", {}).get("chat", {}).get("id", ""))
        if chat_id not in self.allowed_chat_ids:
            log.warning("Ignoring Telegram update from unauthorized chat %s", chat_id)
            return
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
            elif command in {"/frontendhealth", "/frontendreport"}:
                self.send(chat_id, self.frontend_health_report_text())
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
        elif data.startswith("restart-confirm:") and self.allow_restart:
            _, group_id, project_id, index = data.split(":", 3)
            text, keyboard = self.restart_confirm_page(group_id, project_id, int(index))
        elif data.startswith("restart:") and self.allow_restart:
            _, group_id, project_id, index = data.split(":", 3)
            text, keyboard = self.restart_container(group_id, project_id, int(index))
        elif data.startswith("service:"):
            _, group_id, project_id, index = data.split(":", 3)
            text, keyboard = self.service_page(group_id, project_id, int(index))
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
