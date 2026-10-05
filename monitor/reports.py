from __future__ import annotations

import html
import json
import logging
import re
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo

import docker
import requests

from .checks import read_systemd_snapshot

log = logging.getLogger(__name__)

PKT = ZoneInfo("Asia/Karachi")

DEFAULT_ZIG_WHALE_EXCHANGES = ["MEXC", "Bybit", "KuCoin", "Gate.io", "Bitget"]
DEFAULT_ZIG_USD_CONTAINER = "zig-usd-only-alerts-zig-only-alerts-1"
DEFAULT_WALLET_MONITOR_CONTAINER = "wallet-monitor"
DEFAULT_ZIGCHAIN_BOT_CONTAINER = "zigchain-bot"
DEFAULT_STAKE_UNSTAKE_CONTAINER = "zigchain-monitor"
DEFAULT_MDF_TRACKER_CONTAINER = "mdf-tracker"
DEFAULT_HIGHBUY_MONITOR_CONTAINER = "highbuy-monitor"
DEFAULT_NAWA_CONTAINER = "zigchain-wallet-monitor"
DEFAULT_TOKENX_CONTAINER = "bsc-and-eth-token-monitor"
DEFAULT_BEP20_CONTAINER = "bep20-usdt-telegram-monitor"
DEFAULT_ETH_USDT_CONTAINER = "eth-usdt-telegram-monitor"
DEFAULT_SHEETS_SYNC_CONTAINER = "sheets-sync"
SHEETS_SYNC_VAULTS = (
    "Stablecoin Yield Vault",
    "USDC Opportunistic Credit Vault",
    "USDC Core Income Vault",
)
SHEETS_SYNC_UPDATE = re.compile(r"\[(?P<vault>[^\]]+)\]\s+Update today's row\b")
DEFAULT_USDT_RPCS = {
    DEFAULT_BEP20_CONTAINER: "https://bsc-rpc.publicnode.com",
    DEFAULT_ETH_USDT_CONTAINER: "https://ethereum-rpc.publicnode.com",
}
ZIGCHAIN_RPCS = (
    ("Internal RPC", "http://internal-bots-rpc.wickhub.cc"),
    ("ZigScan", "https://zigchain-mainnet.zigscan.net"),
    ("CryptoComics", "https://cryptocomics-rpc.wickhub.cc"),
    ("Numia", "https://public-zigchain-rpc.numia.xyz/"),
)
BACKFILL_LINE = re.compile(
    r"HTTP backfill\s+(?P<chain>bsc|ethereum)\s+(?P<token>USDT|USDC)\s+"
    r"(?P<start>\d+)-(?P<end>\d+);\s*latest=(?P<latest>\d+);\s*backlog=(?P<backlog>\d+)", re.IGNORECASE
)
HTTP_BLOCK_LINE = re.compile(r"Last HTTP block:\s*(\d+)", re.IGNORECASE)
LIVE_SCAN_LINE = re.compile(r"\bLive scan\s+.+?;\s*latest=(?P<latest>\d+);\s*backlog=(?P<backlog>\d+)\s+block\(s\)", re.IGNORECASE)
LIVE_SCAN_RESULT = re.compile(r"\bLive scan result:.*?WebSocket decoded total=(?P<decoded>\d+),\s*matched total=\d+,\s*last WS block=(?P<height>\d+)", re.IGNORECASE)
TATUM_CREDIT_FAILURE = re.compile(r"(?:402 Payment Required|You have used all your credits|account is expired)", re.IGNORECASE)

DEFAULT_FRONTEND_REPORT_ITEMS = [
    {"id": "beencointernalcomms", "name": "Beenco Internal Comms", "health_url": "http://127.0.0.1:4173/health"},
    {"id": "beencomindshub", "name": "Beencomindshub", "health_url": "http://127.0.0.1:3000/health"},
    {"id": "liquidity-provider", "name": "Liquidity Provider", "health_url": "http://127.0.0.1:5173/health"},
    {"id": "vault-automator", "name": "VaultAutomator", "health_url": "http://127.0.0.1:4560/"},
    {"id": "ethzigliquid", "name": "EthZigLiquid", "health_url": "http://127.0.0.1:18300/"},
    {"id": "arkive", "name": "Arkive", "health_url": "http://127.0.0.1:8043/health"},
    {"id": "zigexchange", "name": "ZigExchange", "health_url": "http://127.0.0.1:4090/"},
]


def frontend_report_items(config: dict[str, Any], projects: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    configured = config.get("items") or []
    if configured and all(item.get("health_url") for item in configured):
        return [_normalize_frontend_url(item) for item in configured]

    by_id = {item["id"]: item for item in DEFAULT_FRONTEND_REPORT_ITEMS}
    by_name = {item["name"].lower(): item for item in DEFAULT_FRONTEND_REPORT_ITEMS}
    resolved = []
    for item in configured or (projects or {}).get("frontend", {}).get("items", []):
        default = by_id.get(item.get("id")) or by_name.get(str(item.get("name", "")).lower())
        if default:
            resolved.append(_normalize_frontend_url({**default, **{key: value for key, value in item.items() if key != "health_url"}}))
        elif item.get("health_url"):
            resolved.append(_normalize_frontend_url(item))

    return resolved or DEFAULT_FRONTEND_REPORT_ITEMS


def _normalize_frontend_url(item: dict[str, Any]) -> dict[str, Any]:
    health_url = str(item.get("health_url", ""))
    if "host.docker.internal" in health_url:
        item = dict(item)
        item["health_url"] = health_url.replace("host.docker.internal", "127.0.0.1")
    return item


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


ZIG_WHALE_LINE = re.compile(
    r"^(?P<stamp>\S+)\s+\[[^\]]+\]\s+\[(?P<exchange>[^\]]+)\]\s+monitoring\s+"
    r".*?largest buy this cycle=(?P<largest_buy>.*?)\s+threshold=.*?24hVol=(?P<volume>.*)$"
)


def parse_zig_whale_logs(log_text: str, exchanges: list[str] | None = None, stale_after_seconds: int = 180,
                         now: datetime | None = None) -> list[dict[str, Any]]:
    expected = exchanges or DEFAULT_ZIG_WHALE_EXCHANGES
    now = now or datetime.now(timezone.utc)
    latest: dict[str, dict[str, Any]] = {}
    for line in log_text.splitlines():
        match = ZIG_WHALE_LINE.search(line.strip())
        if not match:
            continue
        try:
            stamp = datetime.fromisoformat(match.group("stamp").replace("Z", "+00:00"))
        except ValueError:
            continue
        exchange = match.group("exchange")
        if exchange not in expected:
            continue
        if exchange not in latest or stamp > latest[exchange]["last_seen"]:
            latest[exchange] = {
                "exchange": exchange,
                "last_seen": stamp,
                "largest_buy": match.group("largest_buy").strip(),
                "volume": match.group("volume").strip(),
            }

    results = []
    for exchange in expected:
        item = latest.get(exchange)
        if not item:
            results.append({"exchange": exchange, "ok": False, "age_seconds": None, "error": "No recent monitoring line found"})
            continue
        age = max(0, int((now - item["last_seen"].astimezone(timezone.utc)).total_seconds()))
        item["age_seconds"] = age
        item["ok"] = age <= stale_after_seconds
        item["error"] = "" if item["ok"] else f"No fresh data for {age}s"
        results.append(item)
    return results


def zig_whale_exchange_results(config: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    config = config or {}
    container_name = config.get("container", "zig-whale-bot")
    tail = int(config.get("log_tail_lines", 500))
    exchanges = config.get("exchanges") or DEFAULT_ZIG_WHALE_EXCHANGES
    stale_after = int(config.get("stale_after_seconds", 180))
    client = docker.from_env(timeout=5)
    try:
        container = client.containers.get(container_name)
        raw_logs = container.logs(tail=tail, timestamps=True).decode("utf-8", errors="replace")
    finally:
        client.close()
    return parse_zig_whale_logs(raw_logs, exchanges, stale_after)


def _container_logs(container_name: str, tail: int = 500) -> str:
    client = docker.from_env(timeout=5)
    try:
        container = client.containers.get(container_name)
        return container.logs(tail=tail, timestamps=True).decode("utf-8", errors="replace")
    finally:
        client.close()


def _container_state(container_name: str) -> dict[str, Any]:
    client = docker.from_env(timeout=5)
    try:
        container = client.containers.get(container_name)
        container.reload()
        state = container.attrs.get("State") or {}
        status = state.get("Status", "unknown")
        health = (state.get("Health") or {}).get("Status")
        return {"running": status == "running" and health != "unhealthy",
                "status": status, "health": health}
    finally:
        client.close()


def _recent_matching_lines(log_text: str, pattern: re.Pattern[str], window_seconds: int = 600) -> list[str]:
    now = datetime.now(timezone.utc)
    recent = []
    for line in log_text.splitlines():
        stamp = _docker_timestamp(line)
        if stamp and pattern.search(line) and 0 <= (now - stamp.astimezone(timezone.utc)).total_seconds() <= window_seconds:
            recent.append(line)
    return recent


def _docker_timestamp(line: str) -> datetime | None:
    first = line.split(maxsplit=1)[0] if line.strip() else ""
    try:
        return datetime.fromisoformat(first.replace("Z", "+00:00"))
    except ValueError:
        return None


def parse_log_freshness(log_text: str, marker: str, stale_after_seconds: int = 300,
                        now: datetime | None = None) -> dict[str, Any]:
    now = now or datetime.now(timezone.utc)
    last_seen = None
    last_line = ""
    for line in log_text.splitlines():
        if marker not in line:
            continue
        stamp = _docker_timestamp(line)
        if not stamp:
            continue
        if last_seen is None or stamp > last_seen:
            last_seen = stamp
            last_line = line
    if last_seen is None:
        return {"ok": False, "age_seconds": None, "last_line": "", "error": f"No '{marker}' log line found"}
    age = max(0, int((now - last_seen.astimezone(timezone.utc)).total_seconds()))
    return {
        "ok": age <= stale_after_seconds,
        "age_seconds": age,
        "last_line": last_line,
        "error": "" if age <= stale_after_seconds else f"No fresh '{marker}' log line for {age}s",
    }


def parse_any_log_freshness(log_text: str, markers: list[str], stale_after_seconds: int = 300,
                            now: datetime | None = None) -> dict[str, Any]:
    now = now or datetime.now(timezone.utc)
    last_seen = None
    last_line = ""
    last_marker = ""
    for line in log_text.splitlines():
        marker = next((candidate for candidate in markers if candidate in line), "")
        if not marker:
            continue
        stamp = _docker_timestamp(line)
        if not stamp:
            continue
        if last_seen is None or stamp > last_seen:
            last_seen = stamp
            last_line = line
            last_marker = marker
    if last_seen is None:
        return {"ok": False, "age_seconds": None, "last_line": "", "marker": "", "error": f"No expected log line found: {', '.join(markers)}"}
    age = max(0, int((now - last_seen.astimezone(timezone.utc)).total_seconds()))
    return {
        "ok": age <= stale_after_seconds,
        "age_seconds": age,
        "last_line": last_line,
        "marker": last_marker,
        "error": "" if age <= stale_after_seconds else f"No fresh expected log line for {age}s",
    }


def zig_usd_alerts_results(config: dict[str, Any] | None = None) -> dict[str, Any]:
    config = config or {}
    container_name = config.get("container", DEFAULT_ZIG_USD_CONTAINER)
    logs = _container_logs(container_name, int(config.get("log_tail_lines", 500)))
    freshness = parse_log_freshness(logs, config.get("marker", "[PROCESSING] TX:"), int(config.get("stale_after_seconds", 300)))
    return {"name": "Zig USD Only Alerts", "container": container_name, **freshness}


WALLET_MONITOR_ERROR = re.compile(r"(EFATAL|polling_error|ETELEGRAM|Bad Gateway|AggregateError)", re.IGNORECASE)
WALLET_MONITOR_READY = ("[ws] Connected", "subscription acknowledged")
TELEGRAM_BOT_ERROR = re.compile(r"(polling error|ETELEGRAM|Bad Gateway|Cannot reach api\.telegram\.org|network/DNS issue)", re.IGNORECASE)


def wallet_monitor_results(config: dict[str, Any] | None = None) -> dict[str, Any]:
    config = config or {}
    container_name = config.get("container", DEFAULT_WALLET_MONITOR_CONTAINER)
    state = _container_state(container_name)
    logs = _container_logs(container_name, int(config.get("log_tail_lines", 500)))
    ready = any(marker in logs for marker in WALLET_MONITOR_READY)
    errors = _recent_matching_lines(logs, WALLET_MONITOR_ERROR)
    fatal = any("EFATAL" in line or "AggregateError" in line for line in errors)
    failing = fatal or len(errors) >= int(config.get("telegram_error_threshold", 3))
    ok = state["running"] and not failing
    if not state["running"]:
        error = f"Container is {state['status']} (health {state['health'] or 'not configured'})"
    elif failing:
        error = errors[-1].split(maxsplit=1)[-1][-500:]
    else:
        error = ""
    return {
        "name": "Wallet Monitor",
        "container": container_name,
        "ok": ok,
        "ready": ready,
        "status": state["status"],
        "summary": "running; startup confirmed" if ready else "running; startup log outside tail",
        "error_count": len(errors),
        "error": "" if ok else error,
    }


def zigchain_bot_results(config: dict[str, Any] | None = None) -> dict[str, Any]:
    config = config or {}
    container_name = config.get("container", DEFAULT_ZIGCHAIN_BOT_CONTAINER)
    logs = _container_logs(container_name, int(config.get("log_tail_lines", 500)))
    markers = config.get("markers") or ["Status collection complete", "PG height fetched"]
    freshness = parse_any_log_freshness(logs, markers, int(config.get("stale_after_seconds", 300)))
    return {"name": "Zigchain Bot", "container": container_name, **freshness}


STAKE_UNSTAKE_ERROR = re.compile(r"\b(error|fatal|panic|exception|unhandled|uncaught)\b", re.IGNORECASE)
STAKE_UNSTAKE_IGNORES = (
    re.compile(r"eth-rpc.*failed over to", re.IGNORECASE),
)


def stake_unstake_results(config: dict[str, Any] | None = None) -> dict[str, Any]:
    config = config or {}
    container_name = config.get("container", DEFAULT_STAKE_UNSTAKE_CONTAINER)
    logs = _container_logs(container_name, int(config.get("log_tail_lines", 500)))
    markers = config.get("markers") or ["[processor] catching up blocks", "[status] live at block"]
    freshness = parse_any_log_freshness(logs, markers, int(config.get("stale_after_seconds", 300)))
    errors = []
    for line in logs.splitlines():
        if not STAKE_UNSTAKE_ERROR.search(line):
            continue
        if any(pattern.search(line) for pattern in STAKE_UNSTAKE_IGNORES):
            continue
        errors.append(line)
    ok = freshness["ok"] and not errors
    error = errors[-1].split(maxsplit=1)[-1][-500:] if errors else freshness["error"]
    return {
        "name": "Stake Unstake",
        "container": container_name,
        "ok": ok,
        "age_seconds": freshness.get("age_seconds"),
        "marker": freshness.get("marker"),
        "error_count": len(errors),
        "error": "" if ok else error,
    }


def mdf_tracker_results(config: dict[str, Any] | None = None) -> dict[str, Any]:
    config = config or {}
    container_name = config.get("container", DEFAULT_MDF_TRACKER_CONTAINER)
    state = _container_state(container_name)
    logs = _container_logs(container_name, int(config.get("log_tail_lines", 500)))
    markers = config.get("markers") or [
        "[TG] Telegram bot started",
        "[WS] Connected",
        "[WS] Subscribed to MDF create_denom",
        "[WS] Subscribed to CreatePairAndProvideLiquidity",
    ]
    ready = all(marker in logs for marker in markers)
    errors = _recent_matching_lines(logs, TELEGRAM_BOT_ERROR)
    failing = len(errors) >= int(config.get("telegram_error_threshold", 3))
    ok = state["running"] and not failing
    if not state["running"]:
        error = f"Container is {state['status']} (health {state['health'] or 'not configured'})"
    elif failing:
        error = errors[-1].split(maxsplit=1)[-1][-500:]
    else:
        error = ""
    return {
        "name": "MDF Tracker",
        "container": container_name,
        "ok": ok,
        "ready": ready,
        "status": state["status"],
        "summary": "running; subscriptions logged" if ready else "running; startup log outside tail",
        "error_count": len(errors),
        "error": "" if ok else error,
    }


HIGHBUY_ERROR = re.compile(r"\b(error|fatal|panic|exception|unhandled|uncaught)\b", re.IGNORECASE)


def highbuy_monitor_results(config: dict[str, Any] | None = None) -> dict[str, Any]:
    config = config or {}
    container_name = config.get("container", DEFAULT_HIGHBUY_MONITOR_CONTAINER)
    state = _container_state(container_name)
    logs = _container_logs(container_name, int(config.get("log_tail_lines", 500)))
    ready = "WebSocket connected" in logs and "Subscription confirmed by RPC" in logs
    errors = _recent_matching_lines(logs, HIGHBUY_ERROR)
    failing = any("fatal" in line.lower() or "panic" in line.lower() for line in errors) or len(errors) >= int(config.get("error_threshold", 3))
    ok = state["running"] and not failing
    if not state["running"]:
        error = f"Container is {state['status']} (health {state['health'] or 'not configured'})"
    elif failing:
        error = errors[-1].split(maxsplit=1)[-1][-500:]
    else:
        error = ""
    return {
        "name": "HighBuy Monitor",
        "container": container_name,
        "ok": ok,
        "ready": ready,
        "summary": "running; RPC subscribed" if ready else "running; startup log outside tail",
        "error_count": len(errors),
        "error": "" if ok else error,
    }


def nawa_valdora_results(config: dict[str, Any] | None = None) -> dict[str, Any]:
    config = config or {}
    state = _container_state(config.get("container", DEFAULT_NAWA_CONTAINER))

    def read_rpc(label: str, url: str) -> dict[str, Any]:
        try:
            response = requests.get(url.rstrip("/") + "/status", timeout=5)
            response.raise_for_status()
            sync_info = response.json()["result"]["sync_info"]
            height = int(sync_info["latest_block_height"])
            return {"name": label, "height": height, "catching_up": sync_info.get("catching_up", False), "error": ""}
        except (requests.RequestException, ValueError, KeyError, TypeError) as exc:
            return {"name": label, "height": None, "catching_up": None, "error": str(exc)[:150]}

    rpcs = config.get("rpcs", ZIGCHAIN_RPCS)
    with ThreadPoolExecutor(max_workers=3) as pool:
        rpc_results = list(pool.map(lambda item: read_rpc(*item), rpcs))

    heights = [item["height"] for item in rpc_results if item["height"] is not None]
    internal = next((item["height"] for item in rpc_results if item["name"] == "Internal RPC"), None)
    public_heights = [item["height"] for item in rpc_results if item["name"] != "Internal RPC" and item["height"] is not None]
    max_gap = int(config.get("max_block_gap", 20))
    rpc_spread = max(heights) - min(heights) if len(heights) == len(rpc_results) and heights else None
    internal_gap = max(abs(internal - height) for height in public_heights) if internal is not None and len(public_heights) == len(rpc_results) - 1 else None
    errors = []
    if not state["running"]:
        errors.append(f"Container is {state['status']} (health {state['health'] or 'not configured'})")
    if len(heights) != len(rpc_results):
        errors.append("RPC status unavailable: " + ", ".join(item["name"] for item in rpc_results if item["height"] is None))
    if internal is None:
        errors.append("Internal RPC height unavailable")
    if any(item["catching_up"] for item in rpc_results):
        errors.append("RPC still syncing: " + ", ".join(item["name"] for item in rpc_results if item["catching_up"]))
    if rpc_spread is not None and rpc_spread > max_gap:
        errors.append(f"RPC heights differ by {rpc_spread} blocks (limit {max_gap})")
    if internal_gap is not None and internal_gap > max_gap:
        errors.append(f"Internal RPC differs from a public RPC by {internal_gap} blocks (limit {max_gap})")
    return {"name": "Nawa Valdora", "ok": not errors, "height": internal,
            "rpcs": rpc_results, "rpc_spread": rpc_spread, "internal_gap": internal_gap, "error": "; ".join(errors)}


def _last_advance_age(samples: list[dict[str, Any]], field: str, now: datetime) -> int | None:
    last_advance = None
    for previous, current in zip(samples, samples[1:]):
        if current[field] > previous[field]:
            last_advance = current["stamp"]
    return None if last_advance is None else max(0, int((now - last_advance.astimezone(timezone.utc)).total_seconds()))


def tokenx_vault_results(config: dict[str, Any] | None = None) -> dict[str, Any]:
    config = config or {}
    logs = _container_logs(config.get("container", DEFAULT_TOKENX_CONTAINER), int(config.get("log_tail_lines", 2000)))
    now = datetime.now(timezone.utc)
    expected = (("bsc", "USDT"), ("bsc", "USDC"), ("ethereum", "USDT"), ("ethereum", "USDC"))
    streams: dict[tuple[str, str], list[dict[str, Any]]] = {key: [] for key in expected}
    for line in logs.splitlines():
        match, stamp = BACKFILL_LINE.search(line), _docker_timestamp(line)
        if match and stamp:
            key = (match["chain"].lower(), match["token"].upper())
            if key in streams:
                streams[key].append({"stamp": stamp, "end": int(match["end"]), "latest": int(match["latest"]),
                                     "backlog": int(match["backlog"])})

    max_backlog = int(config.get("max_backlog_blocks", 500))
    stale_after = int(config.get("stale_after_seconds", 300))
    rows = []
    errors = []
    for key, samples in streams.items():
        label = f"{key[0]} {key[1]}"
        if not samples:
            rows.append({"name": label, "backlog": None, "end": None, "age_seconds": None, "ok": False})
            errors.append(f"{label}: no backfill progress logs")
            continue
        samples.sort(key=lambda item: item["stamp"])
        latest = samples[-1]
        age = max(0, int((now - latest["stamp"].astimezone(timezone.utc)).total_seconds()))
        progress_age = _last_advance_age(samples, "end", now)
        progressed = progress_age is not None and progress_age <= stale_after
        ok = age <= stale_after and latest["backlog"] <= max_backlog and progressed
        rows.append({"name": label, "backlog": latest["backlog"], "end": latest["end"],
                     "age_seconds": age, "ok": ok})
        if age > stale_after:
            errors.append(f"{label}: last backfill log {age}s ago")
        if latest["backlog"] > max_backlog:
            errors.append(f"{label}: backlog {latest['backlog']} exceeds {max_backlog}")
        if not progressed:
            errors.append(f"{label}: no confirmed backfill progress in last {stale_after}s")

    db_name = config.get("postgres_container", "tokenx-vault-postgres")
    try:
        client = docker.from_env(timeout=5)
        try:
            db = client.containers.get(db_name)
            db.reload()
            state = db.attrs.get("State") or {}
            db_status = state.get("Status", "unknown")
            db_health = (state.get("Health") or {}).get("Status", "unknown")
        finally:
            client.close()
    except Exception as exc:
        db_status, db_health = "unavailable", "unknown"
        errors.append(f"Postgres inspection failed: {type(exc).__name__}")
    if db_status != "running" or db_health != "healthy":
        errors.append(f"Postgres: {db_status}, health {db_health}")
    return {"name": "TokenX Vault", "ok": not errors, "streams": rows,
            "postgres": f"{db_status}/{db_health}", "error": "; ".join(errors)}


def usdt_backfill_results(name: str, config: dict[str, Any], container: str) -> dict[str, Any]:
    container_name = config.get("container", container)
    state = _container_state(container_name)
    logs = _container_logs(container_name, int(config.get("log_tail_lines", 1000)))
    now = datetime.now(timezone.utc)
    scans = []
    websocket = []
    samples = []
    for line in logs.splitlines():
        stamp = _docker_timestamp(line)
        if not stamp:
            continue
        scan = LIVE_SCAN_LINE.search(line)
        ws_result = LIVE_SCAN_RESULT.search(line)
        if scan:
            scans.append({"stamp": stamp, "height": int(scan["latest"]), "backlog": int(scan["backlog"])})
        if ws_result:
            websocket.append({"stamp": stamp, "height": int(ws_result["height"]), "decoded": int(ws_result["decoded"])})
        full = BACKFILL_LINE.search(line)
        simple = HTTP_BLOCK_LINE.search(line)
        if full:
            samples.append({"stamp": stamp, "height": int(full["end"]), "backlog": int(full["backlog"])})
        elif simple:
            samples.append({"stamp": stamp, "height": int(simple.group(1)), "backlog": None})
    if scans:
        scans.sort(key=lambda item: item["stamp"])
        websocket.sort(key=lambda item: item["stamp"])
        latest_scan = scans[-1]
        age = max(0, int((now - latest_scan["stamp"].astimezone(timezone.utc)).total_seconds()))
        stale_after = int(config.get("live_scan_stale_seconds", 300))
        errors = []
        if not state["running"]:
            errors.append(f"Container is {state['status']} (health {state['health'] or 'not configured'})")
        if age > stale_after or _last_advance_age(scans, "height", now) is None or _last_advance_age(scans, "height", now) > stale_after:
            errors.append("Live scan is stale or chain height is not advancing")
        ws_progress_age = _last_advance_age(websocket, "height", now)
        if not websocket or ws_progress_age is None or ws_progress_age > stale_after:
            errors.append("WebSocket block height is not advancing")
        known_provider_issue = container == DEFAULT_BEP20_CONTAINER and bool(_recent_matching_lines(logs, TATUM_CREDIT_FAILURE, stale_after))
        backlog = latest_scan["backlog"]
        if backlog > int(config.get("max_backlog_blocks", 500)) and not known_provider_issue:
            errors.append(f"Backlog {backlog} exceeds {config.get('max_backlog_blocks', 500)}")
        summary = f"WS live; HTTP backlog {backlog:,}"
        if known_provider_issue and backlog > int(config.get("max_backlog_blocks", 500)):
            summary += " (known Tatum 402)"
        return {"name": name, "ok": not errors, "height": latest_scan["height"], "backlog": backlog,
                "age_seconds": age, "status": state["status"], "verified": True,
                "summary": summary, "error": "; ".join(errors)}
    samples.sort(key=lambda item: item["stamp"])
    if not samples:
        return {"name": name, "ok": False, "height": None, "backlog": None,
                "age_seconds": None, "status": state["status"], "verified": False,
                "summary": "no scan activity found" if state["running"] else "container stopped",
                "error": "No Live scan or HTTP block lines found in recent logs" if state["running"]
                         else f"Container is {state['status']} (health {state['health'] or 'not configured'})"}
    latest = samples[-1]
    age = max(0, int((now - latest["stamp"].astimezone(timezone.utc)).total_seconds()))
    max_backlog = int(config.get("max_backlog_blocks", 500))
    errors = []
    if not state["running"]:
        errors.append(f"Container is {state['status']} (health {state['health'] or 'not configured'})")
    if age > int(config.get("stale_after_seconds", 7200)):
        errors.append(f"HTTP backfill status is stale ({age}s old)")
    progress_age = _last_advance_age(samples, "height", now)
    if progress_age is None or progress_age > int(config.get("stale_after_seconds", 7200)):
        errors.append("No confirmed HTTP backfill progress in recent logs")
    backlog = latest["backlog"]
    if backlog is None and age <= int(config.get("rpc_comparison_max_age_seconds", 120)):
        try:
            rpc_url = config.get("rpc_url", DEFAULT_USDT_RPCS[container])
            response = requests.post(rpc_url, json={"jsonrpc": "2.0", "method": "eth_blockNumber", "params": [], "id": 1}, timeout=5)
            response.raise_for_status()
            head = int(response.json()["result"], 16)
            backlog = max(0, head - latest["height"])
        except (requests.RequestException, ValueError, KeyError, TypeError) as exc:
            errors.append(f"Cannot verify chain head: {type(exc).__name__}")
    if backlog is not None and backlog > max_backlog:
        errors.append(f"Backlog {backlog} exceeds {max_backlog}")
    return {"name": name, "ok": not errors, "height": latest["height"],
            "backlog": backlog, "age_seconds": age, "status": state["status"], "verified": True,
            "error": "; ".join(errors)}


def sheets_sync_results(config: dict[str, Any] | None = None) -> dict[str, Any]:
    config = config or {}
    logs = _container_logs(config.get("container", DEFAULT_SHEETS_SYNC_CONTAINER),
                           int(config.get("log_tail_lines", 500)))
    now = datetime.now(timezone.utc)
    required = set(SHEETS_SYNC_VAULTS)
    runs: list[dict[str, Any]] = []
    for line in logs.splitlines():
        stamp = _docker_timestamp(line)
        if stamp is None:
            continue
        if "Running sync..." in line:
            runs.append({"started": stamp, "updated": set(), "completed": None})
        elif runs:
            match = SHEETS_SYNC_UPDATE.search(line)
            if match and match["vault"] in required:
                run = runs[-1]
                run["updated"].add(match["vault"])
                if run["updated"] == required and run["completed"] is None:
                    run["completed"] = stamp

    max_gap = int(config.get("max_gap_seconds", 4500))
    grace = int(config.get("completion_grace_seconds", 300))
    completed = [run for run in runs if run["completed"] is not None]
    latest = runs[-1] if runs else None
    last_complete = completed[-1] if completed else None
    age = None if last_complete is None else max(0, int((now - last_complete["completed"].astimezone(timezone.utc)).total_seconds()))
    errors = []
    if not last_complete:
        errors.append("No complete hourly sync found for all three vaults")
    elif age > max_gap:
        errors.append(f"Last complete sync was {age // 60}m ago (limit {max_gap // 60}m)")
    if latest and latest["completed"] is None:
        start_age = max(0, int((now - latest["started"].astimezone(timezone.utc)).total_seconds()))
        if start_age > grace:
            missing = required - latest["updated"]
            errors.append("Latest sync unfinished; missing " + ", ".join(sorted(missing)))
    if len(completed) >= 2:
        gap = int((completed[-1]["started"] - completed[-2]["started"]).total_seconds())
        if gap > max_gap:
            errors.append(f"Sync runs were {gap // 60}m apart (limit {max_gap // 60}m)")
    return {"name": "Sheets Sync", "ok": not errors, "updated": len(last_complete["updated"]) if last_complete else 0,
            "total": len(required), "age_seconds": age,
            "last_completed": last_complete["completed"].astimezone(PKT).strftime("%H:%M PKT") if last_complete else None,
            "error": "; ".join(errors)}


def system_service_results(config: dict[str, Any] | None = None) -> dict[str, Any]:
    config = config or {}
    try:
        snapshot = read_systemd_snapshot(config.get("status_path", "/data/systemd-status.json"),
                                         int(config.get("collector_max_age_seconds", 60)))
    except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
        error = f"Host collector unavailable: {type(exc).__name__}: {exc}"[:200]
        return {"name": "System Services", "ok": False, "services": [
            {"name": name, "ok": False, "summary": "collector unavailable", "error": error}
            for name in ("Wallet Watchman", "Zigchain Exporter")], "error": error}

    services = snapshot.get("services", {})
    watchman = services.get("wallet-watchman.service") or {}
    activity = watchman.get("activity") or {}
    watchman_active = watchman.get("active_state") == "active" and watchman.get("sub_state") == "running"
    wallets = activity.get("wallet_count")
    failures = int(activity.get("rpc_failures_10m") or 0)
    rpc_limit = int(config.get("rpc_failure_threshold", 3))
    watchman_issues = []
    if not watchman_active:
        watchman_issues.append("systemd unit is not running")
    if failures >= rpc_limit:
        watchman_issues.append(f"{failures} RPC failures in the last 10 minutes")
    watchman_row = {
        "name": "Wallet Watchman", "ok": not watchman_issues,
        "summary": (f"running; {f'{wallets} wallets' if wallets else 'wallet count unknown'}; "
                    f"{'RPC failures ' + str(failures) + '/10m' if activity else 'RPC error check unavailable'}"
                    if watchman_active else "systemd not running"),
        "error": "; ".join(watchman_issues),
    }

    exporter = services.get("zigchain-exporter.service") or {}
    exporter_active = exporter.get("active_state") == "active" and exporter.get("sub_state") == "running"
    metrics_url = config.get("exporter_metrics_url")
    metrics_ok = None
    metrics_error = ""
    if exporter_active and metrics_url:
        try:
            response = requests.get(metrics_url, timeout=5)
            response.raise_for_status()
            metrics_ok = bool(response.text.strip())
            if not metrics_ok:
                metrics_error = "Exporter endpoint returned an empty response"
        except requests.RequestException as exc:
            metrics_ok = False
            metrics_error = f"Exporter endpoint failed: {type(exc).__name__}"
    exporter_row = {
        "name": "Zigchain Exporter", "ok": exporter_active and metrics_ok is not False,
        "summary": ("systemd running; endpoint responding" if metrics_ok else
                    "systemd running; metrics not verified" if exporter_active and metrics_ok is None else
                    "systemd running; endpoint failed" if exporter_active else "systemd not running"),
        "error": "zigchain-exporter.service is not running" if not exporter_active else metrics_error,
    }
    rows = [watchman_row, exporter_row]
    return {"name": "System Services", "ok": all(row["ok"] for row in rows),
            "services": rows, "error": "; ".join(row["error"] for row in rows if row["error"])}


def collect_bots_report(config: dict[str, Any] | None = None) -> str:
    """Keep one unavailable bot from hiding the rest of the daily report."""
    config = config or {}

    def check(name: str, function, *args) -> dict[str, Any]:
        try:
            return function(*args)
        except Exception as exc:
            log.warning("bots report check failed: %s (%s: %s)", name, type(exc).__name__, exc)
            return {"name": name, "ok": False, "error": f"Check unavailable: {type(exc).__name__}: {exc}"[:300]}

    specs = [
        ("Zig USD Only Alerts", zig_usd_alerts_results, config.get("zig_usd_alerts", {})),
        ("Wallet Monitor", wallet_monitor_results, config.get("wallet_monitor", {})),
        ("Zigchain Bot", zigchain_bot_results, config.get("zigchain_bot", {})),
        ("Stake Unstake", stake_unstake_results, config.get("stake_unstake", {})),
        ("MDF Tracker", mdf_tracker_results, config.get("mdf_tracker", {})),
        ("HighBuy Monitor", highbuy_monitor_results, config.get("highbuy_monitor", {})),
        ("Nawa Valdora", nawa_valdora_results, config.get("nawa_valdora", {})),
        ("TokenX Vault", tokenx_vault_results, config.get("tokenx_vault", {})),
        ("BEP20 USDT", usdt_backfill_results, "BEP20 USDT", config.get("bep20_usdt", {}), DEFAULT_BEP20_CONTAINER),
        ("ETH USDT", usdt_backfill_results, "ETH USDT", config.get("eth_usdt", {}), DEFAULT_ETH_USDT_CONTAINER),
        ("Sheets Sync", sheets_sync_results, config.get("sheets_sync", {})),
        ("System Services", system_service_results, config.get("system_services", {})),
    ]
    with ThreadPoolExecutor(max_workers=8) as pool:
        whale_future = pool.submit(zig_whale_exchange_results, config.get("zig_whale", {}))
        jobs = [pool.submit(check, *spec) for spec in specs]
        results = [job.result() for job in jobs]
        try:
            whale = whale_future.result()
        except Exception as exc:
            log.warning("bots report check failed: Zig Whale (%s: %s)", type(exc).__name__, exc)
            whale = [{"exchange": exchange, "ok": False, "error": f"Check unavailable: {type(exc).__name__}: {exc}"[:300]}
                     for exchange in DEFAULT_ZIG_WHALE_EXCHANGES]
    return bots_report_message(whale, *results)


def zig_whale_report_message(results: list[dict[str, Any]], generated_at: datetime | None = None) -> str:
    generated_at = generated_at or datetime.now(PKT)
    total = len(results)
    ok_count = sum(1 for item in results if item["ok"])
    failed = total - ok_count
    status_line = "All exchanges are producing data" if failed == 0 else f"{failed} exchange(s) need attention"

    lines = [
        "🐋 <b>Zig Whale Bot Report</b>",
        "",
        f"<b>Date:</b> {generated_at.astimezone(PKT).strftime('%d %b %Y, %H:%M PKT')}",
        f"<b>Status:</b> {html.escape(status_line)}",
        f"<b>Exchange Data:</b> {ok_count}/{total} fresh",
        "",
        "— <b>Exchange Freshness</b> —",
        "",
    ]

    rows = []
    for item in results:
        icon = _status_icon(item["ok"])
        age = "missing" if item.get("age_seconds") is None else f"{item['age_seconds']}s ago"
        largest_buy = item.get("largest_buy", "-")
        volume = item.get("volume", "-")
        rows.append(
            f"{icon} {item['exchange']}\n"
            f"Last data: {age}\n"
            f"Largest buy: {largest_buy}\n"
            f"24h volume: {volume}"
        )
    lines.append("<pre>" + html.escape("\n\n".join(rows)) + "</pre>")

    failures = [item for item in results if not item["ok"]]
    if failures:
        lines.extend(["", "— <b>Issues</b> —", ""])
        issue_rows = [f"{_status_icon(False)} {item['exchange']}\n{item['error']}" for item in failures]
        lines.append("<pre>" + html.escape("\n\n".join(issue_rows)) + "</pre>")

    return "\n".join(lines)


def bots_report_message(zig_whale_results: list[dict[str, Any]], zig_usd_results: dict[str, Any] | None = None,
                        wallet_results: dict[str, Any] | None = None,
                        zigchain_results: dict[str, Any] | None = None,
                        stake_results: dict[str, Any] | None = None,
                        mdf_results: dict[str, Any] | None = None,
                        highbuy_results: dict[str, Any] | None = None,
                        nawa_results: dict[str, Any] | None = None,
                        tokenx_results: dict[str, Any] | None = None,
                        bep20_results: dict[str, Any] | None = None,
                        eth_results: dict[str, Any] | None = None,
                        sheets_results: dict[str, Any] | None = None,
                        system_results: dict[str, Any] | None = None,
                        generated_at: datetime | None = None) -> str:
    generated_at = generated_at or datetime.now(PKT)
    zig_ok = sum(1 for item in zig_whale_results if item["ok"])
    zig_total = len(zig_whale_results)
    bot_checks = [{"name": "Zig Whale Bot", "ok": zig_ok == zig_total, "summary": f"{zig_ok}/{zig_total} exchanges fresh"}]
    if zig_usd_results is not None:
        age = "missing" if zig_usd_results.get("age_seconds") is None else f"{zig_usd_results['age_seconds']}s ago"
        bot_checks.append({"name": "Zig USD Only Alerts", "ok": zig_usd_results["ok"], "summary": f"last TX processing {age}"})
    if wallet_results is not None:
        if wallet_results["ok"]:
            wallet_summary = wallet_results.get("summary", "container running")
        else:
            wallet_summary = wallet_results.get("error", "needs attention")[:80]
        bot_checks.append({"name": "Wallet Monitor", "ok": wallet_results["ok"], "summary": wallet_summary})
    if zigchain_results is not None:
        age = "missing" if zigchain_results.get("age_seconds") is None else f"{zigchain_results['age_seconds']}s ago"
        bot_checks.append({"name": "Zigchain Bot", "ok": zigchain_results["ok"], "summary": f"DB status logs {age}"})
    if stake_results is not None:
        age = "missing" if stake_results.get("age_seconds") is None else f"{stake_results['age_seconds']}s ago"
        bot_checks.append({"name": "Stake Unstake", "ok": stake_results["ok"], "summary": f"block processor logs {age}"})
    if mdf_results is not None:
        if mdf_results["ok"]:
            mdf_summary = mdf_results.get("summary", "container running")
        else:
            mdf_summary = mdf_results.get("error", "needs attention")[:80]
        bot_checks.append({"name": "MDF Tracker", "ok": mdf_results["ok"], "summary": mdf_summary})
    if highbuy_results is not None:
        bot_checks.append({"name": "HighBuy Monitor", "ok": highbuy_results["ok"],
                           "summary": highbuy_results.get("summary", "container running" if highbuy_results["ok"] else highbuy_results.get("error", "needs attention"))})
    if nawa_results is not None:
        gap = "unknown" if nawa_results.get("internal_gap") is None else str(nawa_results["internal_gap"])
        bot_checks.append({"name": "Nawa Valdora", "ok": nawa_results["ok"], "summary": f"internal RPC {nawa_results.get('height') or '-'}, max gap {gap}"})
    if tokenx_results is not None:
        streams = tokenx_results.get("streams", [])
        healthy = sum(1 for item in streams if item["ok"])
        bot_checks.append({"name": "TokenX Vault", "ok": tokenx_results["ok"], "summary": f"{healthy}/{len(streams)} backfills, DB {tokenx_results.get('postgres', 'unknown')}"})
    for result in (bep20_results, eth_results):
        if result is not None:
            lag = result.get("backlog")
            lag_text = "lag unverified" if lag is None else f"backlog {lag}"
            bot_checks.append({"name": result["name"], "ok": result["ok"],
                               "summary": result.get("summary") or f"HTTP block {result.get('height') or '-'}, {lag_text}"})
    if sheets_results is not None:
        last = sheets_results.get("last_completed") or "never"
        bot_checks.append({"name": "Sheets Sync", "ok": sheets_results["ok"],
                           "summary": f"{sheets_results.get('updated', 0)}/{sheets_results.get('total', 3)} vaults, last {last}"})
    if system_results is not None:
        for item in system_results.get("services", []):
            bot_checks.append({"name": item["name"], "ok": item["ok"], "summary": item["summary"]})
        if not system_results.get("services"):
            bot_checks.append({"name": "System Services", "ok": False, "summary": "check unavailable"})
    ok_count = sum(1 for item in bot_checks if item["ok"])
    failed = len(bot_checks) - ok_count
    status_line = "All bot checks healthy" if failed == 0 else f"{failed} bot check(s) need attention"

    lines = [
        "🤖 <b>Daily Bots Report</b>",
        "",
        f"<b>Date:</b> {generated_at.astimezone(PKT).strftime('%d %b %Y, %H:%M PKT')}",
        f"<b>Status:</b> {html.escape(status_line)}",
        f"<b>Bot Checks:</b> {ok_count}/{len(bot_checks)} healthy",
        "",
        "— <b>Bot Summary</b> —",
        "",
        "<pre>" + html.escape("\n".join(
            f"{_status_icon(item['ok'])} {item['name']}: {item['summary']}" for item in bot_checks
        )) + "</pre>",
        "",
        "— <b>Zig Whale Exchanges</b> —",
        "",
    ]

    rows = []
    for item in zig_whale_results:
        age = "missing" if item.get("age_seconds") is None else f"{item['age_seconds']}s ago"
        rows.append(
            f"{_status_icon(item['ok'])} {item['exchange']}\n"
            f"Last data: {age}\n"
            f"Largest buy: {item.get('largest_buy', '-')}\n"
            f"24h volume: {item.get('volume', '-')}"
        )
    lines.append("<pre>" + html.escape("\n\n".join(rows)) + "</pre>")

    if nawa_results is not None:
        rpc_rows = [f"Internal RPC: {nawa_results.get('height') or '-'}  Max gap: {nawa_results.get('internal_gap') if nawa_results.get('internal_gap') is not None else '-'}"]
        rpc_rows.extend(f"{row['name']}: {row['height'] if row['height'] is not None else 'unavailable'}{' (syncing)' if row.get('catching_up') else ''}"
                        for row in nawa_results.get("rpcs", []))
        lines.extend(["", "— <b>Nawa Valdora Heights</b> —", "<pre>" + html.escape("\n".join(rpc_rows)) + "</pre>"])
    if tokenx_results is not None:
        stream_rows = [f"{item['name']}: {item['backlog'] if item['backlog'] is not None else '-'} behind, block {item['end'] or '-'}"
                       for item in tokenx_results.get("streams", [])]
        stream_rows.append(f"Postgres: {tokenx_results.get('postgres', 'unknown')}")
        lines.extend(["", "— <b>TokenX Backfill</b> —", "<pre>" + html.escape("\n".join(stream_rows)) + "</pre>"])

    failures = [item for item in zig_whale_results if not item["ok"]]
    other_failures = []
    if zig_usd_results is not None and not zig_usd_results["ok"]:
        other_failures.append(("Zig USD Only Alerts", zig_usd_results["error"]))
    if wallet_results is not None and not wallet_results["ok"]:
        other_failures.append(("Wallet Monitor", wallet_results["error"]))
    if zigchain_results is not None and not zigchain_results["ok"]:
        other_failures.append(("Zigchain Bot", zigchain_results["error"]))
    if stake_results is not None and not stake_results["ok"]:
        other_failures.append(("Stake Unstake", stake_results["error"]))
    if mdf_results is not None and not mdf_results["ok"]:
        other_failures.append(("MDF Tracker", mdf_results["error"]))
    if highbuy_results is not None and not highbuy_results["ok"]:
        other_failures.append(("HighBuy Monitor", highbuy_results["error"]))
    for result in (nawa_results, tokenx_results, bep20_results, eth_results, sheets_results):
        if result is not None and not result["ok"]:
            other_failures.append((result["name"], result["error"]))
    if system_results is not None and not system_results["ok"]:
        if system_results.get("services"):
            other_failures.extend((item["name"], item["error"]) for item in system_results["services"] if not item["ok"])
        else:
            other_failures.append(("System Services", system_results["error"]))
    if failures or other_failures:
        lines.extend(["", "— <b>Issues</b> —", ""])
        issue_rows = [f"{_status_icon(False)} Zig Whale / {item['exchange']}\n{item['error'][:140]}" for item in failures]
        issue_rows.extend(f"{_status_icon(False)} {name}\n{error[:140]}" for name, error in other_failures)
        lines.append("<pre>" + html.escape("\n\n".join(issue_rows)) + "</pre>")

    lines.extend(["", "<i>Next report in 24h</i>"])
    return "\n".join(lines)
