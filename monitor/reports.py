from __future__ import annotations

import html
import logging
import time
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

import requests

log = logging.getLogger(__name__)

PKT = ZoneInfo("Asia/Karachi")


def frontend_endpoint_results(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    results = []
    for item in items:
        url = item.get("health_url")
        started = time.monotonic()
        if not url:
            results.append({
                "name": item.get("name", item.get("id", "unknown")),
                "url": "",
                "ok": False,
                "status_code": "missing",
                "response_ms": None,
                "error": "No health_url configured",
            })
            continue
        try:
            response = requests.get(url, timeout=item.get("timeout_seconds", 5), allow_redirects=True)
            elapsed_ms = int((time.monotonic() - started) * 1000)
            ok = response.status_code < 400
            results.append({
                "name": item.get("name", item.get("id", url)),
                "url": url,
                "ok": ok,
                "status_code": response.status_code,
                "response_ms": elapsed_ms,
                "error": "" if ok else f"HTTP {response.status_code}",
            })
        except requests.RequestException as exc:
            elapsed_ms = int((time.monotonic() - started) * 1000)
            results.append({
                "name": item.get("name", item.get("id", url)),
                "url": url,
                "ok": False,
                "status_code": "error",
                "response_ms": elapsed_ms,
                "error": str(exc),
            })
    return results


def _status_icon(ok: bool) -> str:
    return "🟢" if ok else "🔴"


def frontend_report_message(results: list[dict[str, Any]], generated_at: datetime | None = None) -> str:
    generated_at = generated_at or datetime.now(PKT)
    total = len(results)
    ok_count = sum(1 for item in results if item["ok"])
    failed = total - ok_count
    status_line = "All frontends reachable" if failed == 0 else f"{failed} frontend(s) need attention"

    lines = [
        "📊 <b>Daily Frontend Report</b>",
        "",
        f"<b>Date:</b> {generated_at.astimezone(PKT).strftime('%d %b %Y, %H:%M PKT')}",
        f"<b>Status:</b> {html.escape(status_line)}",
        f"<b>Reachability:</b> {ok_count}/{total} online",
        "",
        "— <b>Frontend Health</b> —",
        "",
    ]

    rows = []
    for result in results:
        code = str(result["status_code"])
        response_ms = "-" if result["response_ms"] is None else f"{result['response_ms']}ms"
        rows.append(
            f"{_status_icon(result['ok'])} {result['name']}\n"
            f"HTTP: {code}  Time: {response_ms}\n"
            f"URL: {result['url'] or 'not configured'}"
        )
    lines.append("<pre>" + html.escape("\n\n".join(rows)) + "</pre>")

    failures = [item for item in results if not item["ok"]]
    if failures:
        lines.extend(["", "— <b>Issues</b> —", ""])
        issue_rows = []
        for item in failures:
            issue_rows.append(f"🔴 {item['name']}\n{item['error'][:300]}")
        lines.append("<pre>" + html.escape("\n\n".join(issue_rows)) + "</pre>")

    lines.extend(["", "<i>Next report in 24h</i>"])
    return "\n".join(lines)


def should_send_daily_report(now: datetime, report_time: str, last_sent_key: str | None) -> tuple[bool, str]:
    hour, minute = [int(part) for part in report_time.split(":", 1)]
    today_key = now.astimezone(PKT).strftime("%Y-%m-%d")
    if last_sent_key == today_key:
        return False, today_key
    current = now.astimezone(PKT)
    return (current.hour, current.minute) >= (hour, minute), today_key
