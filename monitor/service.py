from __future__ import annotations

import logging
import html
import os
import signal
import threading
import time
from datetime import datetime
from zoneinfo import ZoneInfo
from pathlib import Path

from .checks import endpoint_checks, host_checks, systemd_checks
from .config import env, load_config
from .docker_checks import checks as docker_checks
from .incident_store import IncidentStore, fingerprint
from .notifier import TelegramNotifier
from .telegram_bot import TelegramBotPanel

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)


PKT = ZoneInfo("Asia/Karachi")


def local_time(value: str | None) -> str:
    if not value:
        return "Unknown"
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed.astimezone(PKT).strftime("%d %b %Y, %H:%M:%S PKT")
    except ValueError:
        return value


def incident_title(incident_type: str) -> str:
    return incident_type.replace("_", " ").title()


def issue_text(incident: dict) -> str:
    if incident["incident_type"] == "CONTAINER_UNHEALTHY":
        detail = incident["message"].split("; health-check output:", 1)
        if len(detail) == 2 and detail[1].strip():
            return f"Docker reported the container health status as unhealthy. Health output: {detail[1].strip()}"
        return "Docker reported the container health status as unhealthy."
    messages = {
        "CONTAINER_DOWN": "Docker reported that the container is down.",
        "SYSTEMD_DOWN": "systemd reported that the service is not active.",
        "HIGH_MEMORY": "Host memory usage exceeded the configured threshold.",
        "DISK_FULL": "Host disk usage exceeded the configured threshold.",
        "HIGH_CPU": "Host CPU usage exceeded the configured threshold.",
    }
    return messages.get(incident["incident_type"], incident["message"])


def message_for(prefix: str, incident: dict) -> str:
    recovered = prefix.startswith("✅") or incident.get("status") == "RECOVERED"
    heading = "✅ <b>INCIDENT RECOVERED</b>" if recovered else "🚨 <b>INCIDENT OPEN</b>"
    status = "✅ Recovered" if recovered else "🔴 Active"
    ending_label = "Recovered At" if recovered else "Last Detected"
    ending_value = incident.get("resolved_at") if recovered else incident.get("last_seen")
    return (f"{heading}\n\n"
            f"<b>Project</b>  <code>{html.escape(str(incident['project']))}</code>\n"
            f"<b>Service</b>  <code>{html.escape(str(incident['service']))}</code>\n"
            f"<b>Incident</b>  {html.escape(incident_title(incident['incident_type']))}\n"
            f"<b>Severity</b>  {html.escape(str(incident['severity']))}\n"
            f"<b>Issue</b>  {html.escape(issue_text(incident))}\n"
            f"<b>Status</b>  {status}\n"
            f"<b>Occurrences</b>  {incident['occurrences']}\n"
            f"<b>First Detected</b>  {local_time(incident['first_seen'])}\n"
            f"<b>{ending_label}</b>  {local_time(ending_value)}")


def main() -> None:
    config = load_config(os.getenv("MONITOR_CONFIG", "config.yaml"))
    store = IncidentStore(config.get("database_path", "data/incidents.db"))
    telegram_config = config.get("telegram", {})
    chat_ids_value = env(telegram_config.get("chat_ids_env", "TELEGRAM_CHAT_IDS"))
    if not chat_ids_value:
        chat_ids_value = env(telegram_config.get("chat_id_env", "TELEGRAM_CHAT_ID"))
    chat_ids = [value.strip() for value in (chat_ids_value or "").split(",") if value.strip()]
    notifier = TelegramNotifier(env(telegram_config.get("bot_token_env", "TELEGRAM_BOT_TOKEN")),
                                chat_ids,
                                telegram_config.get("enabled", True))
    panel = TelegramBotPanel(env(telegram_config.get("bot_token_env", "TELEGRAM_BOT_TOKEN")), chat_ids,
                             config.get("projects", {}), config.get("allow_container_restart", False))
    running = True
    def stop(_signum, _frame):
        nonlocal running
        running = False
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    interval = int(config.get("poll_interval_seconds", 30))
    configured_containers = len(config.get("docker", {}).get("containers") or [])
    configured_systemd = len(config.get("systemd", {}).get("services") or [])
    log.info("central monitor started: poll_interval=%ss, docker_containers=%s, systemd_services=%s, telegram_chats=%s",
             interval, configured_containers, configured_systemd, len(chat_ids))
    panel_thread = threading.Thread(target=panel.run, name="telegram-panel", daemon=True)
    panel_thread.start()
    while running:
        active = []
        findings = host_checks(config.get("thresholds", {}))
        findings += docker_checks(config.get("docker", {}))
        findings += systemd_checks(config.get("systemd", {}), config.get("systemd_status_path", "/data/systemd-status.json"))
        findings += endpoint_checks(config.get("docker", {}).get("containers") or [])
        cooldowns = config.get("cooldowns", {})
        for finding in findings:
            key = fingerprint(finding["project"], finding["service"], finding["type"], finding["message"])
            active.append(key)
            cooldown = cooldowns.get("critical_seconds", 300) if finding["severity"] == "CRITICAL" else cooldowns.get("default_seconds", 900)
            incident, should_alert = store.observe(key, finding["project"], finding["service"], finding["type"], finding["severity"], finding["message"], int(cooldown))
            if should_alert:
                notifier.send(message_for("🚨 INCIDENT OPEN / STILL ACTIVE", incident))
        for incident in store.recover_stale(set(active)):
            notifier.send(message_for("✅ INCIDENT RECOVERED", incident))
        time.sleep(interval)
    panel.stop()
