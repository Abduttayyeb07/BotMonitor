"""Bot-specific details for the "ℹ️ Bot Info" page, built from the monitor's existing checks.

Each bot gets its own cards (Zig Whale: exchanges, Sol USDT: slot vs chain head,
Nawa Valdora: RPC heights, ...). The checks in reports.py are only called, never
changed, and a check that fails shows as "unavailable" instead of breaking the page.
"""
from __future__ import annotations

import logging
import re
from typing import Any, Callable

from .reports import (DEFAULT_BEP20_CONTAINER, DEFAULT_ETH_USDT_CONTAINER, check_failure, frontend_endpoint_results,
                      frontend_report_items, highbuy_monitor_results, mdf_tracker_results,
                      nawa_finance_redeemer_results, nawa_valdora_results, sheets_sync_results, sol_usdt_results,
                      stake_unstake_results, system_service_results, tokenx_vault_results, usdt_backfill_results,
                      wallet_monitor_results,
                      zig_usd_alerts_results, zig_whale_exchange_results, zigchain_bot_results)
from .view import ago, card, e, number, quote

log = logging.getLogger(__name__)

HEALTHY, ATTENTION = "🟢 Healthy", "🔴 Needs attention"


def _health(result: dict[str, Any], *items) -> str:
    return card("🩺 Bot Health", [("Status", HEALTHY if result.get("ok") else ATTENTION), *items,
                                 ("Issue", result.get("error") or None)])


def _list_card(heading: str, rows: list[str], expandable: bool = False) -> str:
    return quote([f"<b>{heading}</b>", *rows], expandable) if rows else ""


def zig_whale(results: list[dict[str, Any]]) -> str:
    fresh = sum(1 for item in results if item["ok"])
    health = card("🩺 Bot Health", [("Status", HEALTHY if fresh == len(results) else ATTENTION),
                                   ("Exchanges fresh", f"{fresh}/{len(results)}")])
    rows = []
    for item in results:
        details = [("Last data", ago(item.get("age_seconds"))), ("Largest buy", item.get("largest_buy")),
                   ("24h volume", item.get("volume")), ("Issue", item.get("error") if not item["ok"] else None)]
        details = [detail for detail in details if detail[1]]
        rows.append(f"{'🟢' if item['ok'] else '🔴'} <b>{e(item['exchange'])}</b>")
        rows += [f"{'└' if index == len(details) - 1 else '├'} {e(label)}: {e(value)}" for index, (label, value) in enumerate(details)]
    return "\n\n".join(block for block in (health, _list_card("💱 Exchanges", rows, expandable=len(results) > 3)) if block)


def sol_usdt(result: dict[str, Any]) -> str:
    return _health(result, ("Bot slot", number(result.get("height"))), ("Chain head", number(result.get("head"))),
                   ("Behind", f"{result['lag']:,} slots" if result.get("lag") is not None else None),
                   ("Last slot logged", ago(result.get("age_seconds"))))


def nawa_valdora(result: dict[str, Any]) -> str:
    gap = result.get("internal_gap")
    health = _health(result, ("Internal height", number(result.get("height"))),
                     ("Max gap", f"{gap} blocks" if gap is not None else None))
    rpcs = result.get("rpcs", [])
    rows = [f"{'└' if index == len(rpcs) - 1 else '├'} {'🟢' if rpc.get('height') is not None else '🔴'} {e(rpc['name'])}: "
            + (e(number(rpc["height"])) + (" (syncing)" if rpc.get("catching_up") else "") if rpc.get("height") is not None else "unavailable")
            for index, rpc in enumerate(rpcs)]
    return "\n\n".join(block for block in (health, _list_card("🔗 RPC Heights", rows)) if block)


def tokenx_vault(result: dict[str, Any]) -> str:
    streams = result.get("streams", [])
    rows = []
    for index, stream in enumerate(streams):
        behind = f"{stream['backlog']:,} behind" if stream.get("backlog") is not None else "no progress logs"
        block = f" · block {stream['end']:,}" if stream.get("end") is not None else ""
        rows.append(f"{'└' if index == len(streams) - 1 else '├'} {'🟢' if stream['ok'] else '🔴'} {e(stream['name'])}: {e(behind + block)}")
    health = _health(result, ("Postgres", result.get("postgres")))
    return "\n\n".join(block for block in (health, _list_card("🔄 Backfill Streams", rows)) if block)


def usdt_scan(result: dict[str, Any]) -> str:
    backlog = result.get("backlog")
    return _health(result, ("Latest block", number(result.get("height"))),
                   ("HTTP backlog", f"{backlog:,} blocks" if backlog is not None else None),
                   ("Last scan", ago(result.get("age_seconds"))), ("Summary", result.get("summary")))


def sheets_sync(result: dict[str, Any]) -> str:
    return _health(result, ("Vaults updated", f"{result.get('updated', 0)}/{result.get('total', 0)}"),
                   ("Last complete run", result.get("last_completed")), ("Last run", ago(result.get("age_seconds"))))


def freshness(label: str) -> Callable[[dict[str, Any]], str]:
    def render(result: dict[str, Any]) -> str:
        return _health(result, (label, ago(result.get("age_seconds")) or "not found in recent logs"))
    return render


def stake_unstake(result: dict[str, Any]) -> str:
    return _health(result, ("Last block log", ago(result.get("age_seconds")) or "not found in recent logs"),
                   ("Error lines in logs", result.get("error_count")))


def telegram_bot(result: dict[str, Any]) -> str:
    return _health(result, ("Startup", "confirmed" if result.get("ready") else "not in recent logs"),
                   ("Recent errors (10m)", result.get("error_count")))


def redeemer(result: dict[str, Any]) -> str:
    return _health(result, ("ZIG heartbeat", ago(result.get("age_seconds")) or "missing"),
                   ("ZIG errors (10m)", result.get("zig_errors")), ("ETH errors (10m)", result.get("eth_errors")),
                   ("Telegram errors (10m)", result.get("tg_errors")))


# bot_reports key -> (check, renderer). The key also names the bot's settings in config.yaml.
CHECKS: dict[str, tuple[Callable[[dict[str, Any]], Any], Callable[[Any], str]]] = {
    "zig_whale": (zig_whale_exchange_results, zig_whale),
    "sol_usdt": (sol_usdt_results, sol_usdt),
    "nawa_valdora": (nawa_valdora_results, nawa_valdora),
    "nawa_finance_redeemer": (nawa_finance_redeemer_results, redeemer),
    "tokenx_vault": (tokenx_vault_results, tokenx_vault),
    "bep20_usdt": (lambda config: usdt_backfill_results("BEP20 USDT", config, DEFAULT_BEP20_CONTAINER), usdt_scan),
    "eth_usdt": (lambda config: usdt_backfill_results("ETH USDT", config, DEFAULT_ETH_USDT_CONTAINER), usdt_scan),
    "sheets_sync": (sheets_sync_results, sheets_sync),
    "zig_usd_alerts": (zig_usd_alerts_results, freshness("Last TX processed")),
    "zigchain_bot": (zigchain_bot_results, freshness("Last status log")),
    "stake_unstake": (stake_unstake_results, stake_unstake),
    "wallet_monitor": (wallet_monitor_results, telegram_bot),
    "mdf_tracker": (mdf_tracker_results, telegram_bot),
    "highbuy_monitor": (highbuy_monitor_results, telegram_bot),
}
# Project id (config.example.yaml) -> bot_reports key, for bots whose settings name no container.
PROJECT_CHECKS = {
    "marketzig": "zig_whale", "sol-usdt": "sol_usdt", "nawavaldora": "nawa_valdora",
    "nawa-finance-redeemer": "nawa_finance_redeemer", "tokenx-vault": "tokenx_vault", "bep20": "bep20_usdt",
    "ethtethertoken": "eth_usdt", "sheetszahoor": "sheets_sync", "zig-usd-only-alerts": "zig_usd_alerts",
    "zig-only-alerts": "zig_usd_alerts",  # the project id used in the server's config.yaml
    "patronicluster": "zigchain_bot", "stakeunstake": "stake_unstake", "wallet-monitor": "wallet_monitor",
    "launches": "mdf_tracker", "highlaunches": "highbuy_monitor",
}


def check_key(project: dict[str, Any], bot_reports: dict[str, Any]) -> str | None:
    """Which bot check belongs to this project: by project id, else by a container it monitors."""
    key = PROJECT_CHECKS.get(project.get("id", ""))
    if key and (bot_reports.get(key) or {}).get("enabled", True):
        return key
    containers = set(project.get("containers") or [])
    for candidate, settings in bot_reports.items():
        if candidate in CHECKS and isinstance(settings, dict) and settings.get("enabled", True) \
                and settings.get("container") in containers:
            return candidate
    return None


def unavailable(heading: str, exc: Exception) -> str:
    reason = check_failure(exc).removeprefix("Check unavailable: ")  # the Status line already says so
    return card(heading, [("Status", "⚪ Check unavailable"), ("Reason", reason[:200])])


def bot_details(project: dict[str, Any], bot_reports: dict[str, Any]) -> str | None:
    key = check_key(project, bot_reports)
    if key is None:
        return None
    check, render = CHECKS[key]
    try:
        return render(check(bot_reports.get(key) or {}))
    except Exception as exc:
        log.warning("bot details check failed: %s (%s: %s)", key, type(exc).__name__, exc)
        return unavailable("🩺 Bot Health", exc)


SERVICE_CHECK_NAMES = {"wallet-watchman.service": "Wallet Watchman", "zigchain-exporter.service": "Zigchain Exporter"}


def service_details(unit: str, config: dict[str, Any], status: dict[str, Any]) -> str | None:
    """A system service's own health (from the system service check) and, for Wallet Watchman, its activity."""
    blocks = []
    name = SERVICE_CHECK_NAMES.get(unit)
    if name:
        try:
            result = system_service_results(config)
        except Exception as exc:
            log.warning("service details check failed: %s (%s: %s)", unit, type(exc).__name__, exc)
            blocks.append(unavailable("🩺 Service Health", exc))
        else:
            row = next((item for item in result.get("services", []) if item["name"] == name), None)
            if row:
                blocks.append(card("🩺 Service Health", [("Status", HEALTHY if row["ok"] else ATTENTION),
                                                         ("Summary", row.get("summary")), ("Issue", row.get("error") or None)]))
    if unit == "wallet-watchman.service":
        activity = status.get("activity") or {}
        blocks.append(card("👛 Wallet Activity", [("Wallets", activity.get("wallet_count") or "unknown"),
                                                 ("Last reload", activity.get("last_reload_at") or "unknown"),
                                                 ("RPC failures (10m)", activity.get("rpc_failures_10m") or 0)]))
    return "\n\n".join(block for block in blocks if block) or None


def _same_name(value: Any) -> str:
    """'Liquidity Provider', 'liquidity-provider' and 'liquidityprovider' all compare equal."""
    return re.sub(r"[^a-z0-9]", "", str(value or "").lower())


def frontend_item(project: dict[str, Any], frontend_config: dict[str, Any], projects: dict[str, Any]) -> dict[str, Any] | None:
    """The health endpoint for a frontend: a health_url on the project itself, else the
    daily-report entry whose id or name matches the project's (ignoring case, dashes, spaces)."""
    if project.get("health_url"):
        return {"id": project.get("id"), "name": project.get("name"), "health_url": project["health_url"]}
    wanted = {_same_name(project.get(key)) for key in ("id", "name", "menu_name")} - {""}
    return next((entry for entry in frontend_report_items(frontend_config, projects)
                 if {_same_name(entry.get("id")), _same_name(entry.get("name"))} & wanted), None)


def frontend_details(project: dict[str, Any], frontend_config: dict[str, Any], projects: dict[str, Any]) -> str | None:
    item = frontend_item(project, frontend_config, projects)
    if item is None:
        return None
    try:
        result = frontend_endpoint_results([item])[0]
    except Exception as exc:
        return unavailable("🌐 Frontend Health", exc)
    return card("🌐 Frontend Health", [
        ("Status", "🟢 Reachable" if result["ok"] else "🔴 Not reachable"),
        ("URL", result.get("url") or "not configured", "code"),
        ("HTTP status", result.get("status_code")),
        ("Response time", f"{result['response_ms']} ms" if result.get("response_ms") is not None else None),
        ("Issue", result.get("error") or None)])
