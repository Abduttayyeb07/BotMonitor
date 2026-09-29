from __future__ import annotations

import html
import logging
import threading
import time
from datetime import datetime, timezone
from typing import Any

import docker
import requests

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
    def __init__(self, token: str | None, allowed_chat_ids: list[str], projects: dict[str, Any]):
        self.token = token
        self.allowed_chat_ids = set(allowed_chat_ids)
        self.projects = projects or {}
        self.stop_event = threading.Event()
        self.offset = 0
        self.api_url = f"https://api.telegram.org/bot{token}" if token else ""

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
        rows = []
        for group_id, group in self.projects.items():
            rows.append([{"text": f"📁 {group.get('title', group_id)}", "callback_data": f"group:{group_id}"}])
        return rows

    def project_keyboard(self, group_id: str) -> list[list[dict[str, str]]]:
        group = self.projects.get(group_id, {})
        rows = []
        for project in group.get("items", []):
            rows.append([{"text": f"📊 {project['name']}", "callback_data": f"project:{group_id}:{project['id']}"}])
        rows.append([{"text": "⬅️ Groups", "callback_data": "home"}])
        return rows

    def project_by_id(self, group_id: str, project_id: str) -> dict[str, Any] | None:
        group = self.projects.get(group_id, {})
        return next((item for item in group.get("items", []) if item.get("id") == project_id), None)

    def container(self, name: str) -> Any | None:
        try:
            return docker.from_env().containers.get(name)
        except Exception:
            return None

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
        lines.append(f"\n<b>Containers:</b> {len(project.get('containers', []))}")
        buttons = [[{"text": f"🔎 {name[:24]}", "callback_data": f"container:{group_id}:{project_id}:{index}"}] for index, name in enumerate(project.get("containers", []))]
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
        return text, [[{"text": "🔄 Refresh", "callback_data": f"container:{group_id}:{project_id}:{index}"}],
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
            command = (message.get("text") or "").split()[0].lower()
            if command in {"/start", "/menu", "/status"}:
                self.send(chat_id, "🤖 <b>Central Bot Monitor</b>\n\nChoose a group to view project health:", self.group_keyboard())
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
            text, keyboard = f"📁 <b>{esc(group.get('title', group_id))}</b>\n\nChoose a project:", self.project_keyboard(group_id)
        elif data.startswith("project:"):
            _, group_id, project_id = data.split(":", 2)
            text, keyboard = self.project_page(group_id, project_id)
        elif data.startswith("container:"):
            _, group_id, project_id, index = data.split(":", 3)
            text, keyboard = self.container_page(group_id, project_id, int(index))
        else:
            return
        if message_id:
            self.edit(chat_id, message_id, text, keyboard)

    def run(self) -> None:
        if not self.token or not self.allowed_chat_ids:
            log.warning("Telegram panel disabled: token or authorized chat IDs are missing")
            return
        log.info("Telegram panel started: groups=%s, authorized_chats=%s", len(self.projects), len(self.allowed_chat_ids))
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
