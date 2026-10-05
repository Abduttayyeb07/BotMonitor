import tempfile
import json
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from monitor.checks import systemd_checks
from monitor.config import load_config
from monitor.docker_checks import checks, inventory
from monitor.incident_store import IncidentStore, fingerprint
from monitor.reports import collect_bots_report, frontend_report_items, frontend_report_message, highbuy_monitor_results, mdf_tracker_results, nawa_valdora_results, parse_log_freshness, parse_zig_whale_logs, sheets_sync_results, should_send_daily_report, stake_unstake_results, system_service_results, tokenx_vault_results, usdt_backfill_results, wallet_monitor_results, zigchain_bot_results
from systemd.collector import watchman_activity
from monitor.service import collapse_project_failures, OutageWindow
from monitor.telegram_bot import TelegramBotPanel
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo


class MonitoringRegressionTests(unittest.TestCase):
    def test_inventory_closes_client_without_context_manager_support(self):
        container = SimpleNamespace(name='a', reload=Mock())
        client = SimpleNamespace(containers=SimpleNamespace(list=Mock(return_value=[container])), close=Mock())
        with patch('monitor.docker_checks.docker.from_env', return_value=client):
            self.assertEqual(inventory(), {'a': container})
        container.reload.assert_called_once_with()
        client.close.assert_called_once_with()

    def test_inventory_closes_client_on_api_failure(self):
        client = SimpleNamespace(containers=SimpleNamespace(list=Mock(side_effect=RuntimeError('API unavailable'))), close=Mock())
        with patch('monitor.docker_checks.docker.from_env', return_value=client):
            with self.assertRaisesRegex(RuntimeError, 'API unavailable'):
                inventory()
        client.close.assert_called_once_with()

    def test_removed_container_is_down(self):
        with patch('monitor.docker_checks.inventory', return_value={}):
            findings = checks({'containers': [{'name': 'zigchain-wallet-monitor', 'project': 'nawavaldora'}]})
        self.assertEqual(findings[0]['type'], 'CONTAINER_DOWN')

    def test_project_group_uses_ui_inventory(self):
        config = load_config('config.example.yaml')
        project = next(item for item in config['projects']['frontend']['items'] if item['id'] == 'liquidity-provider')
        members = [item for item in config['docker']['containers'] if item['name'] in project['containers']]
        self.assertEqual({item['project'] for item in members}, {'liquidity-provider'})
        with patch('monitor.docker_checks.inventory', return_value={}):
            findings = checks({'containers': members})
        grouped = collapse_project_failures(findings, {'docker': {'containers': members}})
        self.assertEqual(len(grouped), 1)
        self.assertEqual(grouped[0]['type'], 'PROJECT_DOWN')

    def test_partial_project_retains_container_alert(self):
        config = {'docker': {'containers': [{'name': 'a', 'project': 'p'}, {'name': 'b', 'project': 'p'}]}}
        finding = {'service': 'a', 'project': 'p', 'type': 'CONTAINER_DOWN'}
        self.assertEqual(collapse_project_failures([finding], config), [finding])

    def test_starting_is_not_unhealthy(self):
        container = SimpleNamespace(name='a', attrs={'State': {'Status': 'running', 'Health': {'Status': 'starting'}}})
        with patch('monitor.docker_checks.inventory', return_value={'a': container}):
            self.assertEqual(checks({'containers': [{'name': 'a'}]}), [])

    def test_stopped_and_unhealthy_stack_is_one_project_incident(self):
        config = {'docker': {'containers': [{'name': 'web', 'project': 'p'}, {'name': 'db', 'project': 'p'}]}}
        findings = [{'project': 'p', 'service': 'web', 'type': 'CONTAINER_DOWN'},
                    {'project': 'p', 'service': 'db', 'type': 'CONTAINER_UNHEALTHY'}]
        grouped = collapse_project_failures(findings, config)
        self.assertEqual([item['type'] for item in grouped], ['PROJECT_DOWN'])

    def test_panel_reuses_inventory_for_multiple_buttons(self):
        panel = TelegramBotPanel(None, [], {})
        first, second = SimpleNamespace(name='a'), SimpleNamespace(name='b')
        with patch('monitor.telegram_bot.inventory', return_value={'a': first, 'b': second}) as lookup:
            self.assertIs(panel.container('a'), first)
            self.assertIs(panel.container('b'), second)
            self.assertEqual(lookup.call_count, 1)

    def test_state_fingerprint_survives_changed_error_details(self):
        self.assertEqual(fingerprint('p', 'a', 'CONTAINER_DOWN', 'exited exit=1'), fingerprint('p', 'a', 'CONTAINER_DOWN', 'missing'))

    def test_failed_delivery_retries_and_delivered_incident_is_silent(self):
        with tempfile.TemporaryDirectory() as directory:
            store = IncidentStore(str(Path(directory) / 'incidents.db'))
            key = fingerprint('p', 'a', 'CONTAINER_DOWN', '')
            args = (key, 'p', 'a', 'CONTAINER_DOWN', 'CRITICAL', 'down', 900)
            self.assertTrue(store.observe(*args)[1])
            self.assertTrue(store.observe(*args)[1])
            store.mark_alert_sent(key)
            self.assertFalse(store.observe(*args)[1])
            self.assertEqual(store.recover_stale(set(), {'a'}), [])
            self.assertEqual(len(store.recover_stale(set())), 1)
            self.assertTrue(store.observe(*args)[1])
            store.db.close()

    def test_history_matches_legacy_container_records(self):
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / 'incidents.db')
            store = IncidentStore(path)
            store.observe('key', 'nawavaldora', 'zigchain-wallet-monitor', 'CONTAINER_DOWN', 'CRITICAL', 'down', 900)
            panel = TelegramBotPanel(None, [], {}, database_path=path)
            self.assertEqual(panel.incident_history('Nawa Valdora', ['zigchain-wallet-monitor']), (1, 1, 0))
            store.db.close()

    def test_shutdown_window_holds_partial_alerts(self):
        window = OutageWindow()
        finding = {'project': 'p', 'service': 'a', 'type': 'CONTAINER_DOWN'}
        with patch('monitor.service.time.monotonic', return_value=0):
            visible, protected = window.apply([finding], {'project_correlation_seconds': 10})
        self.assertEqual(visible, [])
        self.assertEqual(protected, {'a'})
        with patch('monitor.service.time.monotonic', return_value=11):
            self.assertEqual(window.apply([finding], {'project_correlation_seconds': 10})[0], [finding])

    def test_frontend_report_message_uses_html_sections(self):
        message = frontend_report_message([
            {'name': 'Good App', 'url': 'http://ok', 'ok': True, 'status_code': 200, 'response_ms': 12, 'error': ''},
            {'name': 'Bad App', 'url': 'http://bad', 'ok': False, 'status_code': 'error', 'response_ms': 5000, 'error': 'timeout'},
        ], datetime(2026, 10, 5, 9, 0, tzinfo=ZoneInfo('Asia/Karachi')))
        self.assertIn('<b>Daily Frontend Report</b>', message)
        self.assertIn('<pre>', message)
        self.assertIn('1/2 online', message)
        self.assertIn('Bad App', message)

    def test_daily_report_is_sent_once_after_configured_time(self):
        before = datetime(2026, 10, 5, 8, 59, tzinfo=ZoneInfo('Asia/Karachi'))
        after = datetime(2026, 10, 5, 9, 0, tzinfo=ZoneInfo('Asia/Karachi'))
        self.assertEqual(should_send_daily_report(before, '09:00', None), (False, '2026-10-05'))
        self.assertEqual(should_send_daily_report(after, '09:00', None), (True, '2026-10-05'))
        self.assertEqual(should_send_daily_report(after, '09:00', '2026-10-05'), (False, '2026-10-05'))

    def test_frontend_health_command_sends_report(self):
        panel = TelegramBotPanel(None, ['1'], {}, frontend_report_config={'items': [{'name': 'Frontend', 'health_url': 'http://ok'}]})
        with patch('monitor.telegram_bot.frontend_endpoint_results', return_value=[
            {'name': 'Frontend', 'url': 'http://ok', 'ok': True, 'status_code': 200, 'response_ms': 5, 'error': ''},
        ]), patch.object(panel, 'send') as send:
            panel.handle({'message': {'chat': {'id': 1}, 'text': '/frontendhealth'}})
        self.assertIn('Daily Frontend Report', send.call_args.args[1])

    def test_bots_health_command_sends_combined_report(self):
        panel = TelegramBotPanel(None, ['1'], {}, bot_report_config={'zig_whale': {'container': 'zig-whale-bot'}})
        with patch('monitor.telegram_bot.collect_bots_report', return_value='<b>Daily Bots Report</b>'), \
             patch.object(panel, 'send') as send:
            panel.handle({'message': {'chat': {'id': 1}, 'text': '/botshealth'}})
        self.assertIn('Daily Bots Report', send.call_args.args[1])

    def test_frontend_report_items_fill_known_urls_from_project_list(self):
        items = frontend_report_items({}, {'frontend': {'items': [
            {'id': 'beencointernalcomms', 'name': 'Beencointernalcomms'},
            {'id': 'zigexchange', 'name': 'ZigExchange'},
        ]}})
        self.assertEqual(items[0]['health_url'], 'http://127.0.0.1:4173/health')
        self.assertEqual(items[1]['health_url'], 'http://127.0.0.1:4090/')

    def test_frontend_report_items_normalize_old_host_gateway_urls(self):
        items = frontend_report_items({'items': [
            {'name': 'Old URL', 'health_url': 'http://host.docker.internal:4173/health'},
        ]})
        self.assertEqual(items[0]['health_url'], 'http://127.0.0.1:4173/health')

    def test_zig_whale_parser_tracks_exchange_freshness(self):
        logs = "\n".join([
            "2026-10-05T09:33:41.371833443Z [2026-10-05T09:33:41.371Z] [MEXC] monitoring — largest buy this cycle=5.5K ZIG threshold=1.0M ZIG 24hVol=183.2K USDT",
            "2026-10-05T09:34:41.607090620Z [2026-10-05T09:34:41.603Z] [Bybit] monitoring — largest buy this cycle=0 ZIG threshold=1.0M ZIG 24hVol=1.1M USDT",
        ])
        now = datetime(2026, 10, 5, 9, 35, tzinfo=ZoneInfo('UTC'))
        results = parse_zig_whale_logs(logs, ['MEXC', 'Bybit', 'KuCoin'], 180, now)
        self.assertTrue(results[0]['ok'])
        self.assertEqual(results[0]['largest_buy'], '5.5K ZIG')
        self.assertTrue(results[1]['ok'])
        self.assertFalse(results[2]['ok'])
        self.assertEqual(results[2]['error'], 'No recent monitoring line found')

    def test_log_freshness_finds_recent_processing_marker(self):
        logs = "2026-10-05T10:08:38.152275907Z [PROCESSING] TX: D57B\n"
        now = datetime(2026, 10, 5, 10, 9, tzinfo=ZoneInfo('UTC'))
        result = parse_log_freshness(logs, '[PROCESSING] TX:', 300, now)
        self.assertTrue(result['ok'])
        self.assertLess(result['age_seconds'], 300)

    def test_wallet_monitor_flags_recent_telegram_errors(self):
        logs = "\n".join([
            "2026-10-05T10:00:00Z [ws] Connected",
            "2026-10-05T10:00:01Z [ws] subscription acknowledged for tx-query",
            '2026-10-05T10:02:00Z error: [polling_error] {"code":"EFATAL","message":"EFATAL: AggregateError"}',
        ])
        with patch('monitor.reports._container_logs', return_value=logs), \
             patch('monitor.reports._container_state', return_value={'running': True, 'status': 'running', 'health': None}), \
             patch('monitor.reports.datetime') as dt:
            dt.now.return_value = datetime(2026, 10, 5, 10, 3, tzinfo=timezone.utc)
            dt.fromisoformat.side_effect = datetime.fromisoformat
            result = wallet_monitor_results({'container': 'wallet-monitor'})
        self.assertFalse(result['ok'])
        self.assertIn('EFATAL', result['error'])

    def test_wallet_monitor_does_not_require_repeated_startup_ws_logs(self):
        logs = "2026-10-05T08:00:00Z [ws] Connected\n2026-10-05T08:00:01Z [ws] subscription acknowledged\n"
        with patch('monitor.reports._container_logs', return_value=logs), \
             patch('monitor.reports._container_state', return_value={'running': True, 'status': 'running', 'health': None}), \
             patch('monitor.reports.datetime') as dt:
            dt.now.return_value = datetime(2026, 10, 5, 10, 30, tzinfo=timezone.utc)
            dt.fromisoformat.side_effect = datetime.fromisoformat
            result = wallet_monitor_results()
        self.assertTrue(result['ok'])

    def test_zigchain_bot_ignores_ssh_errors_when_status_is_fresh(self):
        logs = "\n".join([
            "2026-10-05T10:26:15Z [2026-10-05 10:26:15] INFO: [] Status collection complete",
            "2026-10-05T10:26:16Z [2026-10-05 10:26:16] ERROR: [] SSH connection error",
            '2026-10-05T10:26:16Z     err: "All configured authentication methods failed"',
        ])
        with patch('monitor.reports._container_logs', return_value=logs), patch('monitor.reports.datetime') as dt:
            dt.now.return_value = datetime(2026, 10, 5, 10, 27, tzinfo=ZoneInfo('UTC'))
            dt.fromisoformat.side_effect = datetime.fromisoformat
            result = zigchain_bot_results({'container': 'zigchain-bot'})
        self.assertTrue(result['ok'])

    def test_stake_unstake_requires_fresh_block_logs_and_flags_errors(self):
        logs = "\n".join([
            "2026-10-05T10:30:39.698790446Z [processor] catching up blocks 12680100 -> 12680102",
            "2026-10-05T10:30:40Z ERROR unexpected processor failure",
        ])
        with patch('monitor.reports._container_logs', return_value=logs), patch('monitor.reports.datetime') as dt:
            dt.now.return_value = datetime(2026, 10, 5, 10, 31, tzinfo=ZoneInfo('UTC'))
            dt.fromisoformat.side_effect = datetime.fromisoformat
            result = stake_unstake_results({'container': 'zigchain-monitor'})
        self.assertFalse(result['ok'])
        self.assertIn('unexpected processor failure', result['error'])

    def test_mdf_tracker_accepts_startup_and_subscription_logs(self):
        logs = "\n".join([
            "2026-10-05T10:35:21.962Z [INFO] [TG] Telegram bot started (polling)",
            "2026-10-05T10:35:23.311Z [INFO] [WS] Connected",
            "2026-10-05T10:35:23.312Z [INFO] [WS] Subscribed to MDF create_denom: tm.event='Tx'",
            "2026-10-05T10:35:23.313Z [INFO] [WS] Subscribed to CreatePairAndProvideLiquidity: tm.event='Tx'",
        ])
        with patch('monitor.reports._container_logs', return_value=logs), \
             patch('monitor.reports._container_state', return_value={'running': True, 'status': 'running', 'health': None}), \
             patch('monitor.reports.datetime') as dt:
            dt.now.return_value = datetime(2026, 10, 5, 10, 36, tzinfo=ZoneInfo('UTC'))
            dt.fromisoformat.side_effect = datetime.fromisoformat
            result = mdf_tracker_results({'container': 'mdf-tracker'})
        self.assertTrue(result['ok'])

    def test_mdf_tracker_flags_telegram_network_errors(self):
        logs = "\n".join([
            "2026-10-05T10:35:21.962Z [INFO] [TG] Telegram bot started (polling)",
            "2026-10-05T10:35:23.311Z [INFO] [WS] Connected",
            "2026-10-05T10:35:23.312Z [INFO] [WS] Subscribed to MDF create_denom: tm.event='Tx'",
            "2026-10-05T10:35:23.313Z [INFO] [WS] Subscribed to CreatePairAndProvideLiquidity: tm.event='Tx'",
            "2026-10-05T10:58:40.285Z [ERROR] [TG] Cannot reach api.telegram.org - network/DNS issue.",
            "2026-10-05T10:58:41.285Z [ERROR] [TG] Cannot reach api.telegram.org - network/DNS issue.",
            "2026-10-05T10:58:42.285Z [ERROR] [TG] Cannot reach api.telegram.org - network/DNS issue.",
        ])
        with patch('monitor.reports._container_logs', return_value=logs), \
             patch('monitor.reports._container_state', return_value={'running': True, 'status': 'running', 'health': None}), \
             patch('monitor.reports.datetime') as dt:
            dt.now.return_value = datetime(2026, 10, 5, 10, 59, tzinfo=timezone.utc)
            dt.fromisoformat.side_effect = datetime.fromisoformat
            result = mdf_tracker_results({'container': 'mdf-tracker'})
        self.assertFalse(result['ok'])
        self.assertIn('Cannot reach api.telegram.org', result['error'])

    def test_mdf_tracker_running_is_healthy_after_startup_logs_age_out(self):
        with patch('monitor.reports._container_logs', return_value=''), \
             patch('monitor.reports._container_state', return_value={'running': True, 'status': 'running', 'health': None}):
            result = mdf_tracker_results()
        self.assertTrue(result['ok'])
        self.assertIn('outside tail', result['summary'])

    def test_highbuy_reconnect_then_subscription_is_healthy(self):
        logs = "\n".join([
            "2026-10-05T10:21:29Z WebSocket closed (code=1006). Reconnecting in 1s...",
            "2026-10-05T10:21:30Z WebSocket connected",
            "2026-10-05T10:21:31Z Subscription confirmed by RPC",
        ])
        with patch('monitor.reports._container_logs', return_value=logs), \
             patch('monitor.reports._container_state', return_value={'running': True, 'status': 'running', 'health': None}), \
             patch('monitor.reports.datetime') as dt:
            dt.now.return_value = datetime(2026, 10, 5, 10, 22, tzinfo=ZoneInfo('UTC'))
            dt.fromisoformat.side_effect = datetime.fromisoformat
            result = highbuy_monitor_results()
        self.assertTrue(result['ok'])

    def test_highbuy_no_new_buy_is_not_a_failure(self):
        with patch('monitor.reports._container_logs', return_value=''), \
             patch('monitor.reports._container_state', return_value={'running': True, 'status': 'running', 'health': None}):
            result = highbuy_monitor_results()
        self.assertTrue(result['ok'])

    def test_combined_bots_report_survives_docker_api_failure(self):
        with patch('monitor.reports.docker.from_env', side_effect=RuntimeError('Docker unavailable')):
            message = collect_bots_report()
        self.assertIn('Daily Bots Report', message)
        self.assertIn('Nawa Valdora', message)
        self.assertIn('TokenX Vault', message)
        self.assertIn('Check unavailable', message)
        self.assertLessEqual(len(message), 4096)

    def test_nawa_internal_rpc_gap_compares_three_public_rpcs(self):
        heights = dict(zip(('internal-bots-rpc', 'zigscan', 'cryptocomics', 'numia'), (12680019, 12680020, 12680018, 12680045)))
        def rpc_response(url, **_kwargs):
            height = next(value for key, value in heights.items() if key in url)
            return SimpleNamespace(raise_for_status=Mock(), json=lambda: {'result': {'sync_info': {'latest_block_height': str(height)}}})
        with patch('monitor.reports._container_state', return_value={'running': True, 'status': 'running', 'health': None}), \
             patch('monitor.reports.requests.get', side_effect=rpc_response):
            result = nawa_valdora_results()
        self.assertFalse(result['ok'])
        self.assertEqual(result['internal_gap'], 26)
        self.assertIn('26 blocks', result['error'])

    def test_nawa_accepts_internal_and_three_synced_public_rpcs(self):
        heights = dict(zip(('internal-bots-rpc', 'zigscan', 'cryptocomics', 'numia'), (12680019, 12680020, 12680018, 12680021)))
        def rpc_response(url, **_kwargs):
            height = next(value for key, value in heights.items() if key in url)
            return SimpleNamespace(raise_for_status=Mock(), json=lambda: {'result': {'sync_info': {
                'latest_block_height': str(height), 'catching_up': False}}})
        with patch('monitor.reports._container_state', return_value={'running': True, 'status': 'running', 'health': None}), \
             patch('monitor.reports.requests.get', side_effect=rpc_response):
            result = nawa_valdora_results()
        self.assertTrue(result['ok'])
        self.assertEqual(result['internal_gap'], 2)

    def test_tokenx_flags_backlog_over_500_and_checks_postgres_health(self):
        lines = []
        for chain, token, end, latest, backlog in [
            ('bsc', 'USDT', 125853537, 125853822, 290),
            ('bsc', 'USDC', 125853515, 125853867, 357),
            ('ethereum', 'USDT', 26125636, 26125636, 4),
            ('ethereum', 'USDC', 26125636, 26125636, 501),
        ]:
            lines.append(f'2026-10-05T10:41:31Z HTTP backfill {chain} {token} {end-9}-{end-5}; latest={latest}; backlog={backlog+5}')
            lines.append(f'2026-10-05T10:41:32Z HTTP backfill {chain} {token} {end-4}-{end}; latest={latest}; backlog={backlog}')
        db = SimpleNamespace(attrs={'State': {'Status': 'running', 'Health': {'Status': 'healthy'}}}, reload=Mock())
        client = SimpleNamespace(containers=SimpleNamespace(get=Mock(return_value=db)), close=Mock())
        with patch('monitor.reports._container_logs', return_value='\n'.join(lines)), \
             patch('monitor.reports.docker.from_env', return_value=client), \
             patch('monitor.reports.datetime') as dt:
            dt.now.return_value = datetime(2026, 10, 5, 10, 42, tzinfo=ZoneInfo('UTC'))
            dt.fromisoformat.side_effect = datetime.fromisoformat
            result = tokenx_vault_results()
        self.assertFalse(result['ok'])
        self.assertIn('backlog 501 exceeds 500', result['error'])
        self.assertEqual(sum(row['ok'] for row in result['streams']), 3)
        self.assertEqual(result['postgres'], 'running/healthy')

    def test_usdt_backfill_checks_live_rpc_when_log_is_recent(self):
        logs = ('2026-10-05T09:40:00Z Last HTTP block: 26077000\n'
                '2026-10-05T10:40:00Z Last HTTP block: 26077300\n')
        rpc = SimpleNamespace(raise_for_status=Mock(), json=lambda: {'result': hex(26077320)})
        with patch('monitor.reports._container_logs', return_value=logs), \
             patch('monitor.reports._container_state', return_value={'running': True, 'status': 'running', 'health': None}), \
             patch('monitor.reports.requests.post', return_value=rpc), patch('monitor.reports.datetime') as dt:
            dt.now.return_value = datetime(2026, 10, 5, 10, 41, tzinfo=ZoneInfo('UTC'))
            dt.fromisoformat.side_effect = datetime.fromisoformat
            result = usdt_backfill_results('ETH USDT', {}, 'eth-usdt-telegram-monitor')
        self.assertTrue(result['ok'])
        self.assertEqual(result['height'], 26077300)
        self.assertEqual(result['backlog'], 20)

    def test_usdt_backfill_does_not_compare_old_log_to_live_head(self):
        logs = '2026-10-05T09:40:00Z Last HTTP block: 26077000\n'
        with patch('monitor.reports._container_logs', return_value=logs), \
             patch('monitor.reports._container_state', return_value={'running': True, 'status': 'running', 'health': None}), \
             patch('monitor.reports.requests.post') as rpc, patch('monitor.reports.datetime') as dt:
            dt.now.return_value = datetime(2026, 10, 5, 10, 41, tzinfo=ZoneInfo('UTC'))
            dt.fromisoformat.side_effect = datetime.fromisoformat
            result = usdt_backfill_results('ETH USDT', {}, 'eth-usdt-telegram-monitor')
        self.assertIsNone(result['backlog'])
        rpc.assert_not_called()

    def test_bep20_live_scan_shows_known_tatum_backlog_without_false_alert(self):
        logs = "\n".join([
            "2026-10-05T12:18:00Z Live scan [122427306-122427310, 122427311-122427315]; latest=125866752; backlog=3439446 block(s)",
            "2026-10-05T12:18:03Z Live scan result: 0 matching USDT transfer(s) across 2 worker(s) | WebSocket decoded total=570444885, matched total=15, last WS block=125866758",
            "2026-10-05T12:18:03Z Worker 1 failed: Error: server response 402 Payment Required. Retrying this range next tick.",
            "2026-10-05T12:18:06Z Live scan [122427311-122427315, 122427316-122427320]; latest=125866758; backlog=3439447 block(s)",
            "2026-10-05T12:18:08Z Live scan result: 0 matching USDT transfer(s) across 2 worker(s) | WebSocket decoded total=570446133, matched total=15, last WS block=125866770",
        ])
        with patch('monitor.reports._container_logs', return_value=logs), \
             patch('monitor.reports._container_state', return_value={'running': True, 'status': 'running', 'health': None}), \
             patch('monitor.reports.datetime') as dt:
            dt.now.return_value = datetime(2026, 10, 5, 12, 18, 12, tzinfo=timezone.utc)
            dt.fromisoformat.side_effect = datetime.fromisoformat
            result = usdt_backfill_results('BEP20 USDT', {}, 'bep20-usdt-telegram-monitor')
        self.assertTrue(result['ok'])
        self.assertEqual(result['backlog'], 3439447)
        self.assertIn('known Tatum 402', result['summary'])

    def test_eth_live_scan_requires_ws_and_chain_progress(self):
        logs = "\n".join([
            "2026-10-05T12:17:37Z Live scan result: 0 matching USDT transfer(s) | WebSocket decoded total=82230, matched total=0, last WS block=26126114",
            "2026-10-05T12:17:37Z Live scan 26126112-26126113; latest=26126114; backlog=2 block(s)",
            "2026-10-05T12:18:02Z Live scan result: 0 matching USDT transfer(s) | WebSocket decoded total=82418, matched total=0, last WS block=26126116",
            "2026-10-05T12:18:03Z Live scan 26126115-26126115; latest=26126116; backlog=1 block(s)",
        ])
        with patch('monitor.reports._container_logs', return_value=logs), \
             patch('monitor.reports._container_state', return_value={'running': True, 'status': 'running', 'health': None}), \
             patch('monitor.reports.datetime') as dt:
            dt.now.return_value = datetime(2026, 10, 5, 12, 18, 12, tzinfo=timezone.utc)
            dt.fromisoformat.side_effect = datetime.fromisoformat
            result = usdt_backfill_results('ETH USDT', {}, 'eth-usdt-telegram-monitor')
        self.assertTrue(result['ok'])
        self.assertEqual(result['backlog'], 1)
        self.assertIn('WS live', result['summary'])

    def test_known_tatum_failure_does_not_hide_stalled_websocket(self):
        logs = "\n".join([
            "2026-10-05T12:18:00Z Live scan 122427306-122427310; latest=125866752; backlog=3439446 block(s)",
            "2026-10-05T12:18:03Z Live scan result: WebSocket decoded total=570444885, matched total=15, last WS block=125866758",
            "2026-10-05T12:18:04Z Worker failed: 402 Payment Required",
            "2026-10-05T12:18:06Z Live scan 122427311-122427315; latest=125866758; backlog=3439447 block(s)",
            "2026-10-05T12:18:08Z Live scan result: WebSocket decoded total=570445000, matched total=15, last WS block=125866758",
        ])
        with patch('monitor.reports._container_logs', return_value=logs), \
             patch('monitor.reports._container_state', return_value={'running': True, 'status': 'running', 'health': None}), \
             patch('monitor.reports.datetime') as dt:
            dt.now.return_value = datetime(2026, 10, 5, 12, 18, 12, tzinfo=timezone.utc)
            dt.fromisoformat.side_effect = datetime.fromisoformat
            result = usdt_backfill_results('BEP20 USDT', {}, 'bep20-usdt-telegram-monitor')
        self.assertFalse(result['ok'])
        self.assertIn('WebSocket block height is not advancing', result['error'])

    def test_live_scan_bot_stopped_output_is_reported_even_if_container_runs(self):
        with patch('monitor.reports._container_logs', return_value=''), \
             patch('monitor.reports._container_state', return_value={'running': True, 'status': 'running', 'health': None}):
            result = usdt_backfill_results('ETH USDT', {}, 'eth-usdt-telegram-monitor')
        self.assertFalse(result['ok'])
        self.assertIn('No Live scan', result['error'])

    def test_sheets_sync_requires_three_vault_updates_each_hour(self):
        vaults = ('Stablecoin Yield Vault', 'USDC Opportunistic Credit Vault', 'USDC Core Income Vault')
        logs = []
        for hour in (9, 10):
            logs.append(f'2026-10-05T{hour:02d}:28:18Z Running sync...')
            logs.extend(f"2026-10-05T{hour:02d}:28:2{index}Z [{vault}] Update today's row (row 157, 2026-10-05) -> in=0 out=0"
                        for index, vault in enumerate(vaults))
        with patch('monitor.reports._container_logs', return_value='\n'.join(logs)), \
             patch('monitor.reports.datetime') as dt:
            dt.now.return_value = datetime(2026, 10, 5, 10, 35, tzinfo=ZoneInfo('UTC'))
            dt.fromisoformat.side_effect = datetime.fromisoformat
            result = sheets_sync_results()
        self.assertTrue(result['ok'])
        self.assertEqual(result['updated'], 3)
        self.assertEqual(result['last_completed'], '15:28 PKT')

    def test_sheets_sync_flags_missing_vault_after_grace_period(self):
        logs = "\n".join([
            '2026-10-05T09:28:18Z Running sync...',
            "2026-10-05T09:28:21Z [Stablecoin Yield Vault] Update today's row (row 177, 2026-10-05) -> in=0 out=0",
            "2026-10-05T09:28:22Z [USDC Opportunistic Credit Vault] Update today's row (row 157, 2026-10-05) -> in=0 out=0",
            "2026-10-05T09:28:23Z [USDC Core Income Vault] Update today's row (row 154, 2026-10-05) -> in=0 out=0",
            '2026-10-05T10:28:18Z Running sync...',
            "2026-10-05T10:28:21Z [Stablecoin Yield Vault] Update today's row (row 177, 2026-10-05) -> in=0 out=0",
        ])
        with patch('monitor.reports._container_logs', return_value=logs), patch('monitor.reports.datetime') as dt:
            dt.now.return_value = datetime(2026, 10, 5, 10, 35, tzinfo=ZoneInfo('UTC'))
            dt.fromisoformat.side_effect = datetime.fromisoformat
            result = sheets_sync_results()
        self.assertFalse(result['ok'])
        self.assertIn('USDC Core Income Vault', result['error'])

    def test_sheets_sync_flags_missed_hour(self):
        vaults = ('Stablecoin Yield Vault', 'USDC Opportunistic Credit Vault', 'USDC Core Income Vault')
        logs = []
        for hour in (8, 10):
            logs.append(f'2026-10-05T{hour:02d}:28:18Z Running sync...')
            logs.extend(f"2026-10-05T{hour:02d}:28:2{index}Z [{vault}] Update today's row (row 157, 2026-10-05) -> in=0 out=0"
                        for index, vault in enumerate(vaults))
        with patch('monitor.reports._container_logs', return_value='\n'.join(logs)), \
             patch('monitor.reports.datetime') as dt:
            dt.now.return_value = datetime(2026, 10, 5, 10, 35, tzinfo=ZoneInfo('UTC'))
            dt.fromisoformat.side_effect = datetime.fromisoformat
            result = sheets_sync_results()
        self.assertFalse(result['ok'])
        self.assertIn('120m apart', result['error'])

    def test_watchman_collector_ignores_old_single_rpc_timeout(self):
        logs = "\n".join([
            "2026-10-05T11:58:42+02:00 vmi python[1]: RPC [http://internal-bots-rpc.wickhub.cc] failed: Connection timed out",
            "2026-10-05T12:48:29+02:00 vmi python[1]: Reloaded 5329 wallets from DB",
            "2026-10-05T12:53:29+02:00 vmi python[1]: Reloaded 5329 wallets from DB",
        ])
        result = watchman_activity(logs, datetime(2026, 10, 5, 10, 54, tzinfo=timezone.utc))
        self.assertEqual(result['wallet_count'], 5329)
        self.assertEqual(result['rpc_failures_10m'], 0)

    def test_watchman_collector_counts_repeated_rpc_failures(self):
        logs = "\n".join(
            f"2026-10-05T12:{minute}:00+02:00 vmi python[1]: RPC [http://internal-bots-rpc.wickhub.cc] failed: timeout"
            for minute in ('51', '52', '53')
        )
        result = watchman_activity(logs, datetime(2026, 10, 5, 10, 54, tzinfo=timezone.utc))
        self.assertEqual(result['rpc_failures_10m'], 3)

    def test_systemd_check_alerts_on_repeated_rpc_failure_but_not_single_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            status_path = Path(directory) / 'systemd-status.json'
            snapshot = {'updated_at': datetime.now(timezone.utc).isoformat(), 'services': {
                'wallet-watchman.service': {'active_state': 'active', 'sub_state': 'running', 'activity': {
                    'last_reload_at': (datetime.now(timezone.utc) - timedelta(minutes=2)).isoformat(),
                    'wallet_count': 5329, 'rpc_failures_10m': 1}},
            }}
            status_path.write_text(json.dumps(snapshot), encoding='utf-8')
            config = {'services': [{'name': 'wallet-watchman', 'unit': 'wallet-watchman.service'}]}
            self.assertEqual(systemd_checks(config, str(status_path)), [])
            snapshot['services']['wallet-watchman.service']['activity']['rpc_failures_10m'] = 3
            status_path.write_text(json.dumps(snapshot), encoding='utf-8')
            findings = systemd_checks(config, str(status_path))
            self.assertEqual([item['type'] for item in findings], ['SYSTEMD_RPC_FAILURE'])

    def test_systemd_check_rejects_stale_collector_snapshot(self):
        with tempfile.TemporaryDirectory() as directory:
            status_path = Path(directory) / 'systemd-status.json'
            status_path.write_text(json.dumps({'updated_at': (datetime.now(timezone.utc) - timedelta(minutes=3)).isoformat(),
                                               'services': {}}), encoding='utf-8')
            findings = systemd_checks({'services': []}, str(status_path))
        self.assertEqual(findings[0]['type'], 'SYSTEMD_COLLECTOR_MISSING')

    def test_systemd_check_does_not_require_watchman_reload_heartbeat(self):
        with tempfile.TemporaryDirectory() as directory:
            status_path = Path(directory) / 'systemd-status.json'
            snapshot = {'updated_at': datetime.now(timezone.utc).isoformat(), 'services': {
                'wallet-watchman.service': {'active_state': 'active', 'sub_state': 'running', 'activity': {
                    'last_reload_at': (datetime.now(timezone.utc) - timedelta(minutes=16)).isoformat(),
                    'wallet_count': 5329, 'rpc_failures_10m': 0}},
            }}
            status_path.write_text(json.dumps(snapshot), encoding='utf-8')
            findings = systemd_checks({'services': [{'name': 'wallet-watchman', 'unit': 'wallet-watchman.service'}]}, str(status_path))
        self.assertEqual(findings, [])

    def test_rpc_failure_incident_fingerprint_ignores_count(self):
        self.assertEqual(
            fingerprint('wallet-monitor', 'wallet-watchman', 'SYSTEMD_RPC_FAILURE', '3 RPC failures in 10 minutes'),
            fingerprint('wallet-monitor', 'wallet-watchman', 'SYSTEMD_RPC_FAILURE', '4 RPC failures in 10 minutes'),
        )

    def test_system_service_report_uses_host_snapshot(self):
        snapshot = {'services': {
            'wallet-watchman.service': {'active_state': 'active', 'sub_state': 'running', 'activity': {
                'last_reload_at': (datetime.now(timezone.utc) - timedelta(minutes=4)).isoformat(),
                'wallet_count': 5329, 'rpc_failures_10m': 1}},
            'zigchain-exporter.service': {'active_state': 'active', 'sub_state': 'running'},
        }}
        with patch('monitor.reports.read_systemd_snapshot', return_value=snapshot):
            result = system_service_results()
        self.assertTrue(result['ok'])
        self.assertIn('5329 wallets', result['services'][0]['summary'])
        self.assertIn('metrics not verified', result['services'][1]['summary'])

    def test_watchman_without_collector_activity_is_running_but_unverified(self):
        snapshot = {'services': {
            'wallet-watchman.service': {'active_state': 'active', 'sub_state': 'running'},
            'zigchain-exporter.service': {'active_state': 'active', 'sub_state': 'running'},
        }}
        with patch('monitor.reports.read_systemd_snapshot', return_value=snapshot):
            result = system_service_results()
        self.assertTrue(result['ok'])
        self.assertIn('RPC error check unavailable', result['services'][0]['summary'])

    def test_system_service_report_flags_empty_exporter_endpoint(self):
        snapshot = {'services': {
            'wallet-watchman.service': {'active_state': 'active', 'sub_state': 'running', 'activity': {
                'last_reload_at': datetime.now(timezone.utc).isoformat(), 'wallet_count': 5329, 'rpc_failures_10m': 0}},
            'zigchain-exporter.service': {'active_state': 'active', 'sub_state': 'running'},
        }}
        response = SimpleNamespace(raise_for_status=Mock(), text='')
        with patch('monitor.reports.read_systemd_snapshot', return_value=snapshot), \
             patch('monitor.reports.requests.get', return_value=response):
            result = system_service_results({'exporter_metrics_url': 'http://127.0.0.1:9100/metrics'})
        self.assertFalse(result['ok'])
        self.assertIn('empty response', result['services'][1]['error'])


if __name__ == '__main__':
    unittest.main()
