from __future__ import annotations

import logging
import html
import json
import os
import signal
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from zoneinfo import ZoneInfo
from pathlib import Path

from .checks import endpoint_checks, host_checks, systemd_checks
from .config import env, load_config
from .docker_checks import checks as docker_checks, log_checks, snapshot as docker_snapshot
from .incident_store import IncidentStore, fingerprint
from .notifier import TelegramNotifier
from .reports import collect_bots_report, frontend_endpoint_results, frontend_report_items, frontend_report_message, should_send_daily_report
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
        "PROJECT_DOWN": incident['message'],
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
            f"<b>Project:</b> <code>{html.escape(str(incident['project']))}</code>\n"
            f"<b>Service:</b> <code>{html.escape(str(incident['service']))}</code>\n"
            f"<b>Incident:</b> {html.escape(incident_title(incident['incident_type']))}\n"
            f"<b>Severity:</b> {html.escape(str(incident['severity']))}\n\n"
            f"<b>Issue:</b> {html.escape(issue_text(incident))}\n\n"
            f"<b>Status:</b> {status}\n"
            f"<b>Occurrences:</b> {incident['occurrences']}\n\n"
            f"<b>First Detected:</b> {local_time(incident['first_seen'])}\n"
            f"<b>{ending_label}:</b> {local_time(ending_value)}")


def collapse_project_failures(findings: list[dict], config: dict) -> list[dict]:
    """Collapse a complete Docker project outage into one incident."""
    configured = config.get("docker", {}).get("containers") or []
    by_project: dict[str, set[str]] = {}
    for item in configured:
        by_project.setdefault(item.get("project", item["name"]), set()).add(item["name"])
    result = []
    collapsed: set[tuple[str, str]] = set()
    for project, names in by_project.items():
        down = {finding["service"] for finding in findings
                if finding["project"] == project and finding["type"] in {'CONTAINER_DOWN', 'CONTAINER_UNHEALTHY'}}
        if len(names) > 1 and names.issubset(down):
            collapsed.add((project, "CONTAINER_DOWN"))
            collapsed.add((project, 'CONTAINER_UNHEALTHY'))
            result.append({
                "project": project,
                "service": f"{project}-project",
                "type": "PROJECT_DOWN",
                "severity": "CRITICAL",
                "message": f"All {len(names)} configured containers are stopped, missing, or unhealthy: {', '.join(sorted(names))}",
            })
    for finding in findings:
        if (finding["project"], finding["type"]) in collapsed:
            continue
        result.append(finding)
    return result


class OutageWindow:
    """Allow a short window for a stack shutdown to settle before notifying."""
    def __init__(self):
        self.pending = {}

    def apply(self, findings, config):
        now = time.monotonic()
        delay = config.get('project_correlation_seconds', 10)
        projects = {item['project'] for item in findings if item['type'] == 'CONTAINER_DOWN'}
        self.pending = {project: stamp for project, stamp in self.pending.items() if project in projects}
        held = set()
        for project in projects:
            stamp = self.pending.setdefault(project, now)
            if now - stamp < delay:
                held.add(project)
        protected = {item['service'] for item in findings if item['project'] in held}
        return [item for item in findings if not (item['project'] in held and item['type'] == 'CONTAINER_DOWN')], protected


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
                             config.get("projects", {}), config.get("allow_container_restart", False),
                             config.get('database_path', 'data/incidents.db'),
                             config.get("daily_reports", {}).get("frontend", {}),
                             config.get("bot_reports", {}))
    running = True
    def stop(_signum, _frame):
        nonlocal running
        running = False
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    interval = max(1, int(config.get("poll_interval_seconds", 5)))
    configured_containers = len(config.get("docker", {}).get("containers") or [])
    configured_systemd = len(config.get("systemd", {}).get("services") or [])
    log.info("central monitor started: poll_interval=%ss, docker_containers=%s, systemd_services=%s, telegram_chats=%s",
             interval, configured_containers, configured_systemd, len(chat_ids))
    groups = config.get("projects", {})
    log.info("monitoring groups: %s", ", ".join(group.get("title", name) for name, group in groups.items()))
    log.info("monitoring mode: container_state=true, docker_health=true, systemd=true, application_log_alerts=%s, container_restart=%s",
             config.get("docker", {}).get("log_monitoring", {}).get("enabled", False),
             config.get("allow_container_restart", False))
    panel_thread = threading.Thread(target=panel.run, name="telegram-panel", daemon=True)
    panel_thread.start()
    log_cursors: dict[str, int] = {}
    poll_number = 0
    summary_every = max(1, int(config.get("summary_interval_seconds", 300) / max(interval, 1)))
    outage_window = OutageWindow()
    report_config = config.get("daily_reports", {}).get("frontend", {"enabled": True, "time": "09:00"})
    bots_report_config = config.get("daily_reports", {}).get("bots", {"enabled": True, "time": "09:05"})
    last_frontend_report_key: str | None = None
    last_bots_report_key: str | None = None
    while running:
        cycle_started = time.monotonic()
        poll_number += 1
        active = []
        findings = host_checks(config.get("thresholds", {}))
        findings += docker_checks(config.get("docker", {}))
        findings += log_checks(config.get("docker", {}), log_cursors)
        findings += systemd_checks(config.get("systemd", {}), config.get("systemd_status_path", "/data/systemd-status.json"))
        findings += endpoint_checks(config.get("docker", {}).get("containers") or [])
        findings = collapse_project_failures(findings, config)
        # A project outage resolves only when all its members are healthy.
        protected = set()
        grouped_active = set()
        for row in store.db.execute("SELECT project, service FROM incidents WHERE status='OPEN' AND incident_type='PROJECT_DOWN'"):
            if any(item['project'] == row['project'] for item in findings):
                protected.add(row['service'])
                grouped_active.add(row['project'])
        findings = [item for item in findings if not (item['project'] in grouped_active and item['type'] in {'CONTAINER_DOWN', 'CONTAINER_UNHEALTHY'})]
        findings, held = outage_window.apply(findings, config)
        protected.update(held)
        # Never resolve container outages while Docker itself cannot be read.
        if any(item['type'] == 'DOCKER_UNAVAILABLE' for item in findings):
            protected.update(item['name'] for item in config.get('docker', {}).get('containers', []))
            protected.update(row[0] for row in store.db.execute("SELECT service FROM incidents WHERE status='OPEN' AND incident_type='PROJECT_DOWN'"))
        notifications = []
        cooldowns = config.get("cooldowns", {})
        for finding in findings:
            key = fingerprint(finding["project"], finding["service"], finding["type"], finding["message"])
            active.append(key)
            cooldown = cooldowns.get("critical_seconds", 300) if finding["severity"] == "CRITICAL" else cooldowns.get("default_seconds", 900)
            incident, should_alert = store.observe(key, finding["project"], finding["service"], finding["type"], finding["severity"], finding["message"], int(cooldown))
            if should_alert:
                notifications.append((key, message_for("🚨 INCIDENT OPEN / STILL ACTIVE", incident)))
                log.warning('incident active: project=%s service=%s type=%s', finding['project'], finding['service'], finding['type'])
        project_down = {item['project'] for item in findings if item['type'] == 'PROJECT_DOWN'}
        for incident in store.recover_stale(set(active), protected):
            # Grouped container findings are superseded, not recovered.
            if incident['project'] in project_down:
                continue
            store.db.execute('INSERT OR REPLACE INTO recovery_queue VALUES (?, ?)', (incident['fingerprint'], json.dumps(incident)))
            log.info('incident recovered: project=%s service=%s', incident['project'], incident['service'])
        store.db.commit()
        for row in store.db.execute('SELECT fingerprint, payload FROM recovery_queue'):
            notifications.append(('recovery:' + row[0], message_for('✅ INCIDENT RECOVERED', json.loads(row[1]))))
        with ThreadPoolExecutor(max_workers=4) as delivery_pool:
            jobs = [(key, delivery_pool.submit(notifier.send, message)) for key, message in notifications]
            for key, job in jobs:
                if job.result() and key:
                    if key.startswith('recovery:'):
                        store.db.execute('DELETE FROM recovery_queue WHERE fingerprint=?', (key.split(':', 1)[1],))
                        store.db.commit()
                    else:
                        store.mark_alert_sent(key)
        if report_config.get("enabled", False):
            try:
                due, report_key = should_send_daily_report(datetime.now(PKT), report_config.get("time", "09:00"), last_frontend_report_key)
                if due:
                    report_items = frontend_report_items(report_config, config.get("projects", {}))
                    report_message = frontend_report_message(frontend_endpoint_results(report_items))
                    if notifier.send(report_message):
                        last_frontend_report_key = report_key
                        log.info("daily frontend report delivered: items=%s", len(report_items))
            except Exception:
                log.exception("daily frontend report failed")
        if bots_report_config.get("enabled", False):
            try:
                due, report_key = should_send_daily_report(datetime.now(PKT), bots_report_config.get("time", "09:05"), last_bots_report_key)
                if due:
                    report_message = collect_bots_report(config.get("bot_reports", {}))
                    if notifier.send(report_message):
                        last_bots_report_key = report_key
                        log.info("daily bots report delivered")
            except Exception:
                log.exception("daily bots report failed")
        if poll_number == 1 or poll_number % summary_every == 0:
            summary = docker_snapshot(config.get("docker", {}))
            active_count = store.db.execute("SELECT COUNT(*) FROM incidents WHERE status='OPEN'").fetchone()[0]
            log.info("monitoring summary: running=%s healthy=%s unhealthy=%s stopped=%s missing=%s active_incidents=%s findings_this_poll=%s",
                     summary["running"], summary["healthy"], summary["unhealthy"], summary["stopped"], summary["missing"], active_count, len(findings))
        time.sleep(max(0, interval - (time.monotonic() - cycle_started)))
    panel.stop()
