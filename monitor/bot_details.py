"""Bot-specific details for each project panel, built from the monitor's existing checks.

Each bot gets its own section (Zig Whale: exchanges, Sol USDT: slot vs chain head,
Nawa Valdora: RPC heights, ...). The checks in reports.py are only called, never
changed, and a check that fails shows as "unavailable" instead of hiding the panel.
"""
from __future__ import annotations

import logging
from typing import Any, Callable

from .reports import (DEFAULT_BEP20_CONTAINER, DEFAULT_ETH_USDT_CONTAINER, frontend_endpoint_results,
                      frontend_report_items, highbuy_monitor_results, mdf_tracker_results,
                      nawa_finance_redeemer_results, nawa_valdora_results, sheets_sync_results, sol_usdt_results,
                      stake_unstake_results, tokenx_vault_results, usdt_backfill_results, wallet_monitor_results,
                      zig_usd_alerts_results, zig_whale_exchange_results, zigchain_bot_results)
from .view import ago, e, number, section

log = logging.getLogger(__name__)

HEALTHY, ATTENTION = "🟢 Healthy", "🔴 Needs attention"


def _health(result: dict[str, Any], *items) -> str:
    return section("Bot Health", [("Status", HEALTHY if result.get("ok") else ATTENTION), *items,
                                  ("Issue", result.get("error") or None)])


def zig_whale(results: list[dict[str, Any]]) -> str:
    fresh = sum(1 for item in results if item["ok"])
    blocks = [section("Bot Health", [("Status", HEALTHY if fresh == len(results) else ATTENTION),
                                     ("Exchanges fresh", f"{fresh}/{len(results)}")])]
    rows = []
    for item in results:
        lines = [f"{'🟢' if item['ok'] else '🔴'} <b>{e(item['exchange'])}</b>"]
        if item.get("age_seconds") is not None:
            lines.append(f"   • Last data: {e(ago(item['age_seconds']))}")
        if item.get("largest_buy"):
            lines.append(f"   • Largest buy: {e(item['largest_buy'])}")
        if item.get("volume"):
            lines.append(f"   • 24h volume: {e(item['volume'])}")
        if not item["ok"] and item.get("error"):
            lines.append(f"   • Issue: {e(item['error'])}")
        rows.append("\n".join(lines))
    blocks.append("<b>Exchanges</b>\n" + "\n".join(rows))
    return "\n\n".join(blocks)


def sol_usdt(result: dict[str, Any]) -> str:
    return _health(result, ("Bot slot", number(result.get("height"))), ("Chain head", number(result.get("head"))),
                   ("Behind", f"{result['lag']:,} slots" if result.get("lag") is not None else None),
                   ("Last slot logged", ago(result.get("age_seconds"))))


def nawa_valdora(result: dict[str, Any]) -> str:
    rows = []
    for rpc in result.get("rpcs", []):
        if rpc.get("height") is not None:
            rows.append(f"🟢 {e(rpc['name'])}: {e(number(rpc['height']))}" + (" (syncing)" if rpc.get("catching_up") else ""))
        else:
            rows.append(f"🔴 {e(rpc['name'])}: unavailable")
    gap = result.get("internal_gap")
    health = _health(result, ("Internal height", number(result.get("height"))),
                     ("Max gap", f"{gap} blocks" if gap is not None else None))
    return health + ("\n\n<b>RPC Heights</b>\n" + "\n".join(rows) if rows else "")


def tokenx_vault(result: dict[str, Any]) -> str:
    rows = []
    for stream in result.get("streams", []):
        behind = f"{stream['backlog']:,} behind" if stream.get("backlog") is not None else "no progress logs"
        block = f" · block {stream['end']:,}" if stream.get("end") is not None else ""
        rows.append(f"{'🟢' if stream['ok'] else '🔴'} {e(stream['name'])}: {e(behind + block)}")
    health = _health(result, ("Postgres", result.get("postgres")))
    return health + ("\n\n<b>Backfill Streams</b>\n" + "\n".join(rows) if rows else "")


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


def bot_details(project: dict[str, Any], bot_reports: dict[str, Any]) -> str | None:
    key = check_key(project, bot_reports)
    if key is None:
        return None
    check, render = CHECKS[key]
    try:
        return render(check(bot_reports.get(key) or {}))
    except Exception as exc:
        log.warning("bot details check failed: %s (%s: %s)", key, type(exc).__name__, exc)
        return section("Bot Health", [("Status", "⚪ Check unavailable"), ("Reason", f"{type(exc).__name__}: {exc}"[:200])])


def frontend_details(project: dict[str, Any], frontend_config: dict[str, Any], projects: dict[str, Any]) -> str | None:
    item = next((entry for entry in frontend_report_items(frontend_config, projects) if entry.get("id") == project.get("id")), None)
    if item is None:
        return None
    try:
        result = frontend_endpoint_results([item])[0]
    except Exception as exc:
        return section("Frontend Health", [("Status", "⚪ Check unavailable"), ("Reason", f"{type(exc).__name__}: {exc}"[:200])])
    return section("Frontend Health", [
        ("Status", "🟢 Reachable" if result["ok"] else "🔴 Not reachable"),
        ("URL", result.get("url") or "not configured", "code"),
        ("HTTP status", result.get("status_code")),
        ("Response time", f"{result['response_ms']} ms" if result.get("response_ms") is not None else None),
        ("Issue", result.get("error") or None)])
