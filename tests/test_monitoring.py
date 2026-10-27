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
from monitor.reports import bots_report_message, collect_bots_report, frontend_report_items, frontend_report_message, highbuy_monitor_results, mdf_tracker_results, nawa_finance_redeemer_results, nawa_valdora_results, parse_log_freshness, parse_zig_whale_logs, sheets_sync_results, should_send_daily_report, stake_unstake_results, system_service_results, tokenx_vault_results, usdt_backfill_results, wallet_monitor_results, zigchain_bot_results
from systemd.collector import watchman_activity
from monitor.service import collapse_project_failures, OutageWindow
from monitor.telegram_bot import TelegramBotPanel
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo


def requests_error():
    import requests
    return requests.ConnectionError('down')


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

    def test_reports_are_due_at_eleven_am_and_pm_pakistan_time(self):
        times = ['11:00', '23:00']
        pkt = ZoneInfo('Asia/Karachi')
        before = datetime(2026, 10, 5, 10, 59, tzinfo=pkt)
        morning = datetime(2026, 10, 5, 11, 0, tzinfo=pkt)
        evening = datetime(2026, 10, 5, 23, 0, tzinfo=pkt)
        self.assertEqual(should_send_daily_report(before, times, None), (False, '2026-10-05 11:00'))
        self.assertEqual(should_send_daily_report(morning, times, None), (True, '2026-10-05 11:00'))
        self.assertEqual(should_send_daily_report(morning, times, '2026-10-05 11:00'), (False, '2026-10-05 11:00'))
        self.assertEqual(should_send_daily_report(evening, times, '2026-10-05 11:00'), (True, '2026-10-05 23:00'))
        self.assertEqual(should_send_daily_report(evening, times, '2026-10-05 23:00'), (False, '2026-10-05 23:00'))
        self.assertEqual(should_send_daily_report(evening.astimezone(timezone.utc), times, None),
                         (True, '2026-10-05 23:00'))

    def test_report_delivery_slots_survive_restart_and_are_separate_by_type(self):
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / 'incidents.db')
            store = IncidentStore(path)
            store.mark_report_sent('frontend', '2026-10-05 11:00')
            store.db.close()
            restarted = IncidentStore(path)
            self.assertEqual(restarted.last_report_key('frontend'), '2026-10-05 11:00')
            self.assertIsNone(restarted.last_report_key('bots'))
            restarted.mark_report_sent('bots', '2026-10-05 11:00')
            restarted.mark_report_sent('frontend', '2026-10-05 23:00')
            self.assertEqual(restarted.last_report_key('frontend'), '2026-10-05 23:00')
            self.assertEqual(restarted.last_report_key('bots'), '2026-10-05 11:00')
            restarted.db.close()

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

    # --- Bot info on the Telegram pages ------------------------------------------

    @staticmethod
    def fake_container(env, files=None, working_dir='/app', name='sol-usdt-monitor'):
        import io, tarfile, docker
        files = files or {}
        def get_archive(path):
            if path not in files:
                raise docker.errors.NotFound('missing')
            data = files[path].encode()
            buffer = io.BytesIO()
            with tarfile.open(fileobj=buffer, mode='w') as archive:
                member = tarfile.TarInfo(path.rsplit('/', 1)[-1])
                member.size = len(data)
                archive.addfile(member, io.BytesIO(data))
            return iter([buffer.getvalue()]), {'size': len(data)}
        return SimpleNamespace(
            id=name + '-id', name=name, status='running', get_archive=Mock(side_effect=get_archive),
            image=SimpleNamespace(attrs={'Created': '2026-10-12T09:02:00.123456789Z', 'Size': 412_000_000}),
            attrs={'Created': '2026-10-12T09:03:12.987654321Z', 'RestartCount': 0,
                   'State': {'Status': 'running', 'StartedAt': '2026-10-20T04:15:00Z'},
                   'Config': {'Env': env, 'WorkingDir': working_dir, 'Image': 'sol-usdt-bot-monitor',
                              'Labels': {'com.docker.compose.project': 'sol-usdt-bot', 'com.docker.compose.service': 'monitor'}}})

    def test_bot_info_reads_node_version_and_counts_package_json_libraries(self):
        from monitor.bot_info import container_info, detail_lines, summary_line
        package = json.dumps({'dependencies': {f'lib{i}': '^1.0.0' for i in range(8)}, 'devDependencies': {'jest': '^29'}})
        info = container_info(self.fake_container(['NODE_VERSION=20.11.1', 'TELEGRAM_BOT_TOKEN=secret'], {'/app/package.json': package}))
        self.assertEqual(info['runtime'], 'Node.js 20.11.1')
        self.assertEqual((info['libraries']['libraries'], info['libraries']['dev_libraries']), (8, 1))
        self.assertEqual(summary_line(info), '🧱 Node.js 20.11.1  •  📦 8 libraries (+1 dev)  •  📅 12 Oct 2026')
        rows = dict(detail_lines(info))
        self.assertEqual(rows['Created'], '12 Oct 2026, 14:03 PKT')
        self.assertEqual(rows['Image'], 'sol-usdt-bot-monitor (412 MB)')
        self.assertEqual(rows['Compose'], 'sol-usdt-bot / monitor')

    def test_bot_info_counts_python_requirements_and_skips_options_and_comments(self):
        from monitor.bot_info import container_info
        requirements = 'docker>=7.0\n# comment\n-r base.txt\n\npsutil==6.0  # pinned\nrequests\n'
        info = container_info(self.fake_container(['PYTHON_VERSION=3.12.4'], {'/srv/requirements.txt': requirements}, working_dir='/srv'))
        self.assertEqual(info['runtime'], 'Python 3.12.4')
        self.assertEqual(info['libraries']['libraries'], 3)

    def test_bot_info_for_database_skips_library_lookup_and_unknowns_are_left_out(self):
        from monitor.bot_info import container_info, summary_line
        database = self.fake_container(['PG_VERSION=16.2'], working_dir='')
        info = container_info(database)
        self.assertEqual(info['runtime'], 'PostgreSQL 16.2')
        self.assertIsNone(info['libraries'])
        database.get_archive.assert_not_called()
        self.assertEqual(summary_line(info), '🧱 PostgreSQL 16.2  •  📅 12 Oct 2026')
        bare = container_info(self.fake_container([], working_dir='/'))
        self.assertEqual(summary_line(bare), '📅 12 Oct 2026')

    def test_project_and_container_pages_show_bot_info_but_never_secrets(self):
        package = json.dumps({'dependencies': {f'lib{i}': '1' for i in range(8)}})
        container = self.fake_container(['NODE_VERSION=20.11.1', 'TELEGRAM_BOT_TOKEN=supersecret123'], {'/app/package.json': package})
        container.logs = Mock(return_value=b'2026-10-23T10:00:00Z [BLOCK] Live slot 1\n')
        projects = {'bots_docker': {'items': [{'id': 'sol-usdt', 'name': 'Sol USDT', 'containers': ['sol-usdt-monitor']}]}}
        panel = TelegramBotPanel(None, ['1'], projects)
        with patch.object(panel, 'container', return_value=container), \
             patch.object(panel, 'incident_history', return_value=(0, 0, 0)), \
             patch('monitor.telegram_bot.bot_details', return_value=None):
            project_text, _ = panel.pretty_project_page('bots_docker', 'sol-usdt')
            container_text, _ = panel.container_page('bots_docker', 'sol-usdt', 0)
        self.assertIn('├ Node.js: v20.11.1', project_text)
        self.assertIn('├ Libraries: 8', project_text)
        self.assertIn('├ Dependencies: 8', container_text)
        self.assertIn('├ Built: 12 Oct 2026, 14:02 PKT', container_text)
        for text in (project_text, container_text):
            self.assertNotIn('supersecret123', text)
            self.assertNotIn('TELEGRAM_BOT_TOKEN', text)
        self.assertLess(len(container_text), 4096)
        self.assertEqual(container.get_archive.call_count, 1, 'manifest is read once and cached')

    def test_live_usage_matches_docker_stats_math_and_shows_on_container_page_only(self):
        from monitor.bot_info import live_usage, usage_lines
        stats = {
            'memory_stats': {'usage': 100 * 1_048_576, 'limit': 8 * 1_073_741_824, 'stats': {'inactive_file': 16 * 1_048_576}},
            'cpu_stats': {'cpu_usage': {'total_usage': 2_000_000_000}, 'system_cpu_usage': 500_000_000_000, 'online_cpus': 4},
            'precpu_stats': {'cpu_usage': {'total_usage': 1_850_000_000}, 'system_cpu_usage': 450_000_000_000},
        }
        container = self.fake_container(['NODE_VERSION=20.11.1'])
        container.stats = Mock(return_value=stats)
        self.assertEqual(usage_lines(live_usage(container)), [('Memory', '84 MB of 8.0 GB'), ('CPU', '1.2%')])
        container.status = 'exited'
        self.assertIsNone(live_usage(container))
        container.status = 'running'
        container.stats = Mock(side_effect=RuntimeError('stats unavailable'))
        self.assertEqual(usage_lines(live_usage(container)), [])
        # Shown on the container page; the project page must not pay the 1-2 s stats cost.
        container.stats = Mock(return_value=stats)
        container.logs = Mock(return_value=b'')
        projects = {'bots_docker': {'items': [{'id': 'sol-usdt', 'name': 'Sol USDT', 'containers': ['sol-usdt-monitor']}]}}
        panel = TelegramBotPanel(None, ['1'], projects)
        with patch.object(panel, 'container', return_value=container), \
             patch.object(panel, 'incident_history', return_value=(0, 0, 0)), \
             patch('monitor.telegram_bot.bot_details', return_value=None):
            project_text, _ = panel.pretty_project_page('bots_docker', 'sol-usdt')
            container.stats.assert_not_called()
            container_text, _ = panel.container_page('bots_docker', 'sol-usdt', 0)
        self.assertNotIn('Memory', project_text)
        self.assertIn('<b>📊 System Resources</b>\n├ Memory: 84 MB / 8.0 GB\n└ CPU: 1.2%', container_text)

    # --- Panels: cards for every bot and frontend, a Bot Info page per bot, logs in a code box ---

    WHALE_LOG = ('2026-10-23T11:14:32.123456789Z [2026-10-23T11:14:32.120Z] [Gate.io] monitoring — '
                 'largest buy this cycle=6.7K ZIG threshold=1.0M ZIG 24hVol=95.1K USDT')

    def panel_for(self, projects, containers, **kwargs):
        panel = TelegramBotPanel('token', ['1'], projects, **kwargs)
        return panel, patch.object(panel, 'container', side_effect=lambda name: containers.get(name))

    def test_cards_use_tree_lines_and_skip_missing_values(self):
        from monitor.view import card, grouped_card
        self.assertEqual(card('📦 Status', [('A', 1), ('B', None), None, ('Image', 'a<b>', 'code'), ('C', 3)]),
                         '<blockquote><b>📦 Status</b>\n├ A: 1\n├ Image: <code>a&lt;b&gt;</code>\n└ C: 3</blockquote>')
        self.assertEqual(card('Empty', [('A', None)]), '')
        self.assertEqual(grouped_card('🐳 web', [('⚙️ Runtime', [('Status', 'up')]), ('🧩 Env', [('X', None)])], expandable=True),
                         '<blockquote expandable><b>🐳 web</b>\n⚙️ Runtime\n└ Status: up</blockquote>')

    def test_every_container_of_every_project_gets_the_same_card(self):
        web = self.fake_container(['NODE_VERSION=20.11.1'], {'/app/package.json': '{"dependencies": {"a": "1"}, "devDependencies": {"b": "1"}}'}, name='web')
        db = self.fake_container(['PG_VERSION=16.2'], working_dir='', name='db')
        projects = {'frontend': {'items': [{'id': 'lp', 'name': 'Liquidity Provider', 'containers': ['web', 'db', 'gone']}]}}
        panel, containers = self.panel_for(projects, {'web': web, 'db': db})
        with containers, patch.object(panel, 'incident_history', return_value=(3, 1, 2)):
            text, keyboard = panel.pretty_project_page('frontend', 'lp')
        self.assertTrue(text.startswith('📊 <b>Liquidity Provider</b>\n<i>⚠️ 2/3 containers running</i>'))
        self.assertIn('<blockquote expandable><b>🐳 web</b>\n⚙️ Runtime\n├ Status: 🟢 Running\n├ Uptime:', text)
        self.assertIn('🧩 Environment\n├ Node.js: v20.11.1\n├ Libraries: 1 (+1 dev)\n└ Updated: 12 Oct 2026</blockquote>', text)
        self.assertIn('<b>🐳 db</b>', text)
        self.assertIn('🧩 Environment\n├ PostgreSQL: 16.2\n└ Updated: 12 Oct 2026', text)
        self.assertIn('<b>🐳 gone</b>\n⚙️ Runtime\n└ Status: 🔴 Stopped or unavailable', text)
        self.assertEqual(text.count('<blockquote expandable>'), 3, 'three containers -> collapsible cards')
        self.assertIn('<blockquote><b>📚 Incident History</b>\n├ Total: 3\n├ Active: 1\n└ Recovered: 2</blockquote>', text)
        self.assertNotIn('Frontend Health', text, 'bot-specific details live behind the Bot Info button')
        self.assertTrue(text.endswith('PKT</i>'))
        cards = [part.split('</blockquote>')[0] for part in text.split('<blockquote')[1:]]
        self.assertTrue(cards and all('\n\n' not in body for body in cards), 'no blank lines inside a card')
        callbacks = [row[0]['callback_data'] for row in keyboard]
        self.assertEqual(callbacks, ['container:frontend:lp:0', 'container:frontend:lp:1', 'container:frontend:lp:2',
                                     'botinfo:frontend:lp', 'rm-confirm:frontend:lp', 'group:frontend'])
        self.assertEqual(keyboard[3][0]['text'], '🌐 Frontend Info', 'frontends get Frontend Info, not Bot Info')

    def test_one_or_two_containers_are_shown_open(self):
        web = self.fake_container(['NODE_VERSION=24.22.1'], name='zig-whale-bot')
        panel, containers = self.panel_for({'bots_docker': {'items': [{'id': 'marketzig', 'name': 'Zig Whale Bot', 'containers': ['zig-whale-bot']}]}},
                                           {'zig-whale-bot': web})
        with containers, patch.object(panel, 'incident_history', return_value=(0, 0, 0)):
            text, _ = panel.pretty_project_page('bots_docker', 'marketzig')
        self.assertIn('<i>✅ 1/1 containers running</i>', text)
        self.assertIn('<blockquote><b>🐳 zig-whale-bot</b>', text)
        self.assertNotIn('expandable', text)

    def test_bot_info_page_shows_this_bots_own_details(self):
        projects = {'bots_docker': {'items': [{'id': 'marketzig', 'name': 'Zig Whale Bot', 'containers': ['zig-whale-bot']}]},
                    'frontend': {'items': [{'id': 'arkive', 'name': 'Arkive', 'containers': ['arkive']}]}}
        panel = TelegramBotPanel('token', ['1'], projects)
        with patch('monitor.telegram_bot.bot_details', return_value='<blockquote><b>🩺 Bot Health</b>\n└ Status: 🟢 Healthy</blockquote>') as bot, \
             patch('monitor.telegram_bot.frontend_details', return_value='<blockquote><b>🌐 Frontend Health</b>\n└ Status: 🟢 Reachable</blockquote>') as front:
            bot_text, bot_keyboard = panel.bot_info_page('bots_docker', 'marketzig')
            front_text, _ = panel.bot_info_page('frontend', 'arkive')
        self.assertTrue(bot_text.startswith('ℹ️ <b>Zig Whale Bot</b>\n<i>Bot Info</i>'))
        self.assertIn('🩺 Bot Health', bot_text)
        self.assertIn('🌐 Frontend Health', front_text)
        self.assertTrue(front_text.startswith('🌐 <b>Arkive</b>\n<i>Frontend Info</i>'))
        bot.assert_called_once()
        front.assert_called_once()
        self.assertEqual([row[0]['callback_data'] for row in bot_keyboard], ['botinfo:bots_docker:marketzig', 'project:bots_docker:marketzig'])
        with patch('monitor.telegram_bot.bot_details', return_value=None):
            empty, _ = panel.bot_info_page('bots_docker', 'marketzig')
        self.assertIn('No bot-specific check is set up for this one yet.', empty)

    def test_service_info_page_shows_the_services_own_health_and_activity(self):
        projects = {'bots_systemd': {'items': [{'id': 'system-services', 'name': 'System Services', 'services': [
            {'name': 'wallet-watchman', 'unit': 'wallet-watchman.service'}]}]}}
        panel = TelegramBotPanel('token', ['1'], projects, bot_report_config={'system_services': {}})
        snapshot = {'services': {
            'wallet-watchman.service': {'active_state': 'active', 'sub_state': 'running',
                                        'activity': {'wallet_count': 5329, 'last_reload_at': '2026-10-27T10:00:00Z', 'rpc_failures_10m': 0}},
            'zigchain-exporter.service': {'active_state': 'active', 'sub_state': 'running'}}}
        status = snapshot['services']['wallet-watchman.service']
        with patch('monitor.reports.read_systemd_snapshot', return_value=snapshot), \
             patch.object(panel, 'systemd_status', return_value={**status, 'recent_logs': 'Oct 27 10:00:00 host watchman[1]: ok'}):
            info, info_buttons = panel.service_info_page('bots_systemd', 'system-services', 0)
            page, page_buttons = panel.service_page('bots_systemd', 'system-services', 0)
        self.assertTrue(info.startswith('ℹ️ <b>wallet-watchman</b>\n<i>Service Info</i>'))
        self.assertIn('<b>🩺 Service Health</b>\n├ Status: 🟢 Healthy\n└ Summary: running; 5329 wallets', info)
        self.assertIn('<b>👛 Wallet Activity</b>\n├ Wallets: 5329', info)
        self.assertNotIn('Wallet Activity', page, 'the specifics live behind the Service Info button')
        self.assertIn({'text': 'ℹ️ Service Info', 'callback_data': 'svcinfo:bots_systemd:system-services:0'},
                      [button for row in page_buttons for button in row])
        self.assertEqual([row[0]['callback_data'] for row in info_buttons],
                         ['svcinfo:bots_systemd:system-services:0', 'service:bots_systemd:system-services:0'])

    def test_each_bots_details_are_its_own(self):
        from monitor import bot_details as details
        whale = details.zig_whale([
            {'exchange': 'MEXC', 'ok': True, 'age_seconds': 41, 'largest_buy': '5.5K ZIG', 'volume': '183.2K USDT'},
            {'exchange': 'KuCoin', 'ok': False, 'age_seconds': None, 'error': 'No recent monitoring line found'}])
        self.assertIn('<b>🩺 Bot Health</b>\n├ Status: 🔴 Needs attention\n└ Exchanges fresh: 1/2', whale)
        self.assertIn('🟢 <b>MEXC</b>\n├ Last data: 41s ago\n├ Largest buy: 5.5K ZIG\n└ 24h volume: 183.2K USDT', whale)
        self.assertIn('🔴 <b>KuCoin</b>\n└ Issue: No recent monitoring line found', whale)
        self.assertEqual(details.sol_usdt({'ok': True, 'height': 459277691, 'head': 459277693, 'lag': 2, 'age_seconds': 0, 'error': ''}),
                         '<blockquote><b>🩺 Bot Health</b>\n├ Status: 🟢 Healthy\n├ Bot slot: 459,277,691\n├ Chain head: 459,277,693\n'
                         '├ Behind: 2 slots\n└ Last slot logged: 0s ago</blockquote>')
        nawa = details.nawa_valdora({'ok': True, 'height': 13093142, 'internal_gap': 0, 'error': '', 'rpcs': [
            {'name': 'Internal RPC', 'height': 13093142}, {'name': 'ZigScan', 'height': None}]})
        self.assertIn('├ Internal height: 13,093,142\n└ Max gap: 0 blocks', nawa)
        self.assertIn('<b>🔗 RPC Heights</b>\n├ 🟢 Internal RPC: 13,093,142\n└ 🔴 ZigScan: unavailable', nawa)
        tokenx = details.tokenx_vault({'ok': False, 'postgres': 'running/healthy', 'error': 'bsc USDC: backlog 900 exceeds 500', 'streams': [
            {'name': 'bsc USDT', 'backlog': 12, 'end': 1000, 'ok': True}, {'name': 'bsc USDC', 'backlog': 900, 'end': 900, 'ok': False}]})
        self.assertIn('├ 🟢 bsc USDT: 12 behind · block 1,000\n└ 🔴 bsc USDC: 900 behind · block 900', tokenx)
        self.assertIn('└ Issue: bsc USDC: backlog 900 exceeds 500', tokenx)
        self.assertIn('├ Vaults updated: 3/3', details.sheets_sync({'ok': True, 'updated': 3, 'total': 3, 'last_completed': '20:13 PKT', 'age_seconds': 60}))
        self.assertIn('Last TX processed: not found in recent logs', details.freshness('Last TX processed')({'ok': False, 'age_seconds': None}))

    def test_bot_check_is_found_by_project_id_or_by_container_and_skipped_when_removed(self):
        from monitor.bot_details import check_key
        self.assertEqual(check_key({'id': 'marketzig', 'containers': ['zig-whale-bot']}, {}), 'zig_whale')
        # The server's config.yaml calls the Zig USD Alerts project 'zig-only-alerts'.
        self.assertEqual(check_key({'id': 'zig-only-alerts', 'containers': []}, {}), 'zig_usd_alerts')
        self.assertEqual(check_key({'id': 'zig-usd-only-alerts', 'containers': []}, {}), 'zig_usd_alerts')
        renamed = {'id': 'zig-alert-bot', 'containers': ['zig-usd-only-alerts-zig-only-alerts-1']}
        self.assertEqual(check_key(renamed, {'zig_usd_alerts': {'container': 'zig-usd-only-alerts-zig-only-alerts-1'}}), 'zig_usd_alerts')
        self.assertIsNone(check_key({'id': 'marketzig', 'containers': []}, {'zig_whale': {'enabled': False}}))
        self.assertIsNone(check_key({'id': 'unknown', 'containers': ['x']}, {}))

    def test_bot_info_survives_a_failing_check(self):
        from monitor.bot_details import bot_details
        with patch('monitor.bot_details.CHECKS', {'zig_whale': (Mock(side_effect=RuntimeError('docker down')), Mock())}):
            text = bot_details({'id': 'marketzig', 'containers': []}, {})
        self.assertEqual(text, '<blockquote><b>🩺 Bot Health</b>\n├ Status: ⚪ Check unavailable\n└ Reason: RuntimeError: docker down</blockquote>')

    def test_frontend_bot_info_shows_its_health_endpoint(self):
        from monitor.bot_details import frontend_details
        result = [{'name': 'Arkive', 'url': 'http://127.0.0.1:8043/health', 'ok': True, 'status_code': 200, 'response_ms': 34, 'error': ''}]
        with patch('monitor.bot_details.frontend_endpoint_results', return_value=result):
            text = frontend_details({'id': 'arkive'}, {'items': [{'id': 'arkive', 'name': 'Arkive', 'health_url': 'http://127.0.0.1:8043/health'}]}, {})
        self.assertEqual(text, '<blockquote><b>🌐 Frontend Health</b>\n├ Status: 🟢 Reachable\n├ URL: <code>http://127.0.0.1:8043/health</code>\n'
                               '├ HTTP status: 200\n└ Response time: 34 ms</blockquote>')
        self.assertIsNone(frontend_details({'id': 'not-a-frontend'}, {'items': [{'id': 'arkive', 'health_url': 'http://x'}]}, {}))

    def test_frontend_info_matches_ids_and_names_loosely_or_uses_the_projects_own_health_url(self):
        from monitor.bot_details import frontend_item
        report = {'items': [{'id': 'liquidity-provider', 'name': 'Liquidity Provider', 'health_url': 'http://127.0.0.1:5173/health'}]}
        # The server's menu id differs from the report id: still found, by id or by name.
        self.assertEqual(frontend_item({'id': 'liquidityprovider', 'name': 'LP'}, report, {})['health_url'], 'http://127.0.0.1:5173/health')
        self.assertEqual(frontend_item({'id': 'lp', 'name': 'Liquidity  provider'}, report, {})['health_url'], 'http://127.0.0.1:5173/health')
        self.assertIsNone(frontend_item({'id': 'other', 'name': 'Other'}, report, {}))
        own = frontend_item({'id': 'other', 'name': 'Other', 'health_url': 'http://127.0.0.1:9000/health'}, report, {})
        self.assertEqual(own['health_url'], 'http://127.0.0.1:9000/health', 'a health_url on the project wins')
        panel = TelegramBotPanel('token', ['1'], {'frontend': {'items': [{'id': 'other', 'name': 'Other', 'containers': []}]}},
                                 frontend_report_config=report)
        text, _ = panel.bot_info_page('frontend', 'other')
        self.assertIn('No health URL found for this frontend.', text)
        self.assertIn('<code>other</code>', text, 'says which project id to add it to')

    def test_details_page_has_cards_and_logs_in_the_code_box(self):
        container = self.fake_container(['NODE_VERSION=24.22.1'], {'/app/package.json': '{"dependencies": {"a": "1", "b": "1"}, "devDependencies": {"c": "1", "d": "1", "e": "1"}}'})
        container.logs = Mock(return_value='\n'.join([self.WHALE_LOG] * 12).encode())
        container.stats = Mock(return_value={})
        projects = {'bots_docker': {'items': [{'id': 'marketzig', 'name': 'Zig Whale Bot', 'containers': ['zig-whale-bot']}]}}
        panel, containers = self.panel_for(projects, {'zig-whale-bot': container}, allow_restart=True)
        with containers:
            text, keyboard = panel.container_page('bots_docker', 'marketzig', 0)
            full = panel.full_logs('bots_docker', 'marketzig', 0)
        for heading in ('📦 Container Status', '🧩 Application', '🚀 Deployment'):
            self.assertIn(f'<blockquote><b>{heading}</b>', text)
        self.assertNotIn('System Resources', text, 'no stats -> card left out rather than showing zeros')
        self.assertIn('├ Stack: Node.js 24.22.1\n├ Dependencies: 2 + 3 dev', text)
        logs = text.split('📜 <b>Recent Logs</b>\n')[1]
        self.assertTrue(logs.startswith('<pre>') and '</pre>' in logs, 'logs are in the dark code box')
        self.assertIn('[Gate.io] monitoring — largest buy this cycle=6.7K ZIG threshold=1.0M ZIG 24hVol=95.1K USDT', logs,
                      'log text is shown exactly as logged')
        # The date is shown too when the line is from an earlier day, so allow either form.
        self.assertRegex(logs, r'^<pre>(23 Oct )?16:14:32  \[2026-10-23T11:14:32\.120Z\]', 'Docker UTC stamp becomes PKT time')
        self.assertLessEqual(len(text), 4096)
        callbacks = [button['callback_data'] for row in keyboard for button in row]
        for expected in ('container:bots_docker:marketzig:0', 'logs:bots_docker:marketzig:0',
                         'restart-confirm:bots_docker:marketzig:0', 'project:bots_docker:marketzig'):
            self.assertIn(expected, callbacks)
        self.assertTrue(all(len(part) <= 4096 and part.count('<pre>') == part.count('</pre>') == 1 for part in full))
        self.assertEqual(sum(part.count('Gate.io') for part in full), 12, 'full logs include every line')

    def test_log_lines_keep_text_and_only_shorten_the_time(self):
        from monitor.view import log_text, code_box
        today = datetime(2026, 10, 23, 12, 0, tzinfo=timezone.utc)
        self.assertEqual(log_text('2026-10-23T11:37:11.123456789Z 127.0.0.1 - - "GET /api/health HTTP/1.1" 200 -', today),
                         '16:37:11  127.0.0.1 - - "GET /api/health HTTP/1.1" 200 -')
        self.assertEqual(log_text('2026-10-21T11:37:11Z older line', today), '21 Oct 16:37:11  older line', 'older days keep their date')
        self.assertEqual(log_text('Traceback (most recent call last):', today), 'Traceback (most recent call last):')
        self.assertEqual(log_text('2026-13-40T99:00:00Z broken', today), '2026-13-40T99:00:00Z broken')
        self.assertEqual(code_box(['a <b> & c']), '<pre>a &lt;b&gt; &amp; c</pre>', 'special characters are escaped')

    def test_long_logs_are_split_into_code_boxes_never_cut(self):
        from monitor.view import log_messages, newest_lines_that_fit
        lines = [f'{index:02d} ' + 'x' * 300 for index in range(40)] + ['huge ' + 'y' * 9000 + ' <end>']
        messages = log_messages('📜 <b>bot</b>', lines)
        self.assertGreater(len(messages), 1)
        self.assertTrue(all(len(message) <= 4096 for message in messages))
        self.assertTrue(all(message.count('<pre>') == 1 and message.count('</pre>') == 1 for message in messages))
        joined = ''.join(messages)
        self.assertTrue(all(f'{index:02d} ' in joined for index in range(40)))
        self.assertEqual(joined.count('y'), 9000, 'a huge line is split across boxes, not dropped')
        self.assertIn('&lt;end&gt;', joined)
        kept = newest_lines_that_fit(lines[:40], 1000)
        self.assertEqual(kept, lines[:40][-len(kept):], 'newest lines, oldest first')
        self.assertLess(len(kept), 40)

    def test_usage_lists_top_ten_projects_by_memory(self):
        projects = {'bots_docker': {'items': [{'id': f'p{i}', 'name': f'Project {i}', 'containers': [f'c{i}']} for i in range(12)]}}
        containers = {}
        for i in range(12):
            container = self.fake_container([], name=f'c{i}')
            container.stats = Mock(return_value={
                'memory_stats': {'usage': (i + 1) * 100 * 1024 ** 2, 'limit': 8 * 1024 ** 3, 'stats': {}},
                'cpu_stats': {'cpu_usage': {'total_usage': 2}, 'system_cpu_usage': 200, 'online_cpus': 1},
                'precpu_stats': {'cpu_usage': {'total_usage': 1}, 'system_cpu_usage': 100}})
            containers[f'c{i}'] = container
        GB = 1024 ** 3
        snapshot = {
            'containers': {f'c{i}': {'image_id': 'img', 'data': 10 * 1024 ** 2, 'volumes': ['vol11'] if i == 11 else []} for i in range(12)},
            'images': {'img': 1 * GB}, 'volumes': {'vol11': 5 * GB}, 'layers': 1 * GB}
        panel, patched = self.panel_for(projects, containers)
        with patched, patch('monitor.telegram_bot.psutil.virtual_memory', return_value=SimpleNamespace(total=16 * GB, available=8 * GB)), \
             patch('monitor.telegram_bot.psutil.disk_usage', return_value=SimpleNamespace(total=1.4 * 1024 * GB, used=765 * GB, percent=57.0)), \
             patch('monitor.telegram_bot.docker_disk', return_value=snapshot) as disk, \
             patch('monitor.telegram_bot.log_size', side_effect=lambda container: 90 * 1024 ** 2 if container.name == 'c11' else None):
            text = panel.usage_text()
            panel.usage_text()
        disk.assert_called_once()  # cached for the second /usage
        self.assertIn('<blockquote><b>🥇 Project 11</b>\n├ RAM: 1.2 GB · 7.3%\n├ CPU: 1.0%\n├ Disk: 6.1 GB\n'
                      '├ Disk split: vol 5.0 GB · img 1.0 GB · logs 90 MB · data 10 MB\n└ Containers: 1/1 running</blockquote>', text)
        self.assertIn('├ Disk: 1.0 GB\n├ Disk split: img 1.0 GB · data 10 MB', text, 'unknown logs are left out, not shown as 0')
        self.assertIn('<b>🥈 Project 10</b>', text)
        self.assertIn('<b>10. Project 2</b>', text)
        self.assertNotIn('Project 1<', text)
        self.assertNotIn('Project 0<', text)
        self.assertIn('├ RAM: 8.0 GB of 16.0 GB (50%)\n├ Disk: 765.0 GB of 1.4 TB (57%)\n├ Docker disk: 6.2 GB', text)
        self.assertNotIn('Log files not counted', text)
        self.assertLessEqual(len(text), 4096)

    def test_cpu_alert_needs_sustained_high_usage(self):
        from monitor.checks import host_checks
        thresholds = {'cpu_warning_percent': 90, 'cpu_sustain_checks': 3}
        state = {}
        memory = SimpleNamespace(percent=10.0)
        disk = SimpleNamespace(percent=30.0)
        def run(cpu):
            with patch('monitor.checks.psutil.virtual_memory', return_value=memory), \
                 patch('monitor.checks.psutil.disk_usage', return_value=disk), \
                 patch('monitor.checks.psutil.cpu_percent', return_value=cpu):
                return [item['type'] for item in host_checks(thresholds, state)]
        self.assertEqual(run(97.0), [], 'one spike does not alert')
        self.assertEqual(run(95.0), [], 'two in a row still do not')
        self.assertEqual(run(30.0), [], 'a normal sample resets the count')
        self.assertEqual(run(95.0), [])
        self.assertEqual(run(96.0), [])
        self.assertEqual(run(99.0), ['HIGH_CPU'], 'third high sample in a row alerts')
        self.assertEqual(run(92.0), ['HIGH_CPU'], 'stays open while CPU stays high')
        self.assertEqual(run(40.0), [], 'clears once CPU drops')
        with patch('monitor.checks.psutil.virtual_memory', return_value=memory), \
             patch('monitor.checks.psutil.disk_usage', return_value=disk), \
             patch('monitor.checks.psutil.cpu_percent', return_value=95.0):
            self.assertEqual([item['type'] for item in host_checks({'cpu_warning_percent': 90})], ['HIGH_CPU'],
                             'without state (old callers) a single sample still alerts')

    # --- Memory alerts, Docker events, mutes ------------------------------------------

    def memory_config(self, **overrides):
        item = {'name': 'arkive', 'project': 'arkive', **overrides}
        return {'docker': {'containers': [item, {'name': 'small', 'project': 'small'}]},
                'thresholds': {'container_memory_warning_mb': 1536, 'container_memory_critical_mb': 3072,
                               'container_memory_sustain_checks': 3, 'container_memory_interval_seconds': 0}}

    def test_memory_alert_needs_three_high_readings_and_uses_overrides(self):
        from monitor.watchers import MemoryWatch
        GB = 1024 ** 3
        readings = {'arkive': 2 * GB, 'small': 100 * 1024 ** 2}
        containers = {name: SimpleNamespace(name=name, status='running') for name in readings}
        watch = MemoryWatch(read_inventory=lambda: containers, read_memory=lambda c: readings[c.name])
        config = self.memory_config()
        self.assertEqual(watch.findings(config), [])
        self.assertEqual(watch.findings(config), [])
        found = watch.findings(config)
        self.assertEqual([(f['service'], f['type'], f['severity']) for f in found], [('arkive', 'CONTAINER_HIGH_MEMORY', 'WARNING')])
        self.assertEqual(found[0]['message'], 'Container memory is 2.0 GB (warning limit 1.5 GB)')
        readings['arkive'] = int(3.5 * GB)
        self.assertEqual(watch.findings(config)[0]['severity'], 'CRITICAL')
        # A per-container override raises Arkive's limit: no alert at 3.5 GB.
        override = MemoryWatch(read_inventory=lambda: containers, read_memory=lambda c: readings[c.name])
        big = self.memory_config(memory_warning_mb=4096, memory_critical_mb=6144)
        for _ in range(4):
            self.assertEqual(override.findings(big), [])
        # One normal reading resets the count and the alert clears.
        readings['arkive'] = 1 * GB
        self.assertEqual(watch.findings(config), [])

    def test_memory_alert_survives_a_failed_read_and_resets_when_stopped(self):
        from monitor.watchers import MemoryWatch
        GB = 1024 ** 3
        state = {'value': 2 * GB}
        containers = {'arkive': SimpleNamespace(name='arkive', status='running')}
        watch = MemoryWatch(read_inventory=lambda: containers, read_memory=lambda c: state['value'])
        config = self.memory_config()
        for _ in range(3):
            found = watch.findings(config)
        self.assertEqual(len(found), 1)
        state['value'] = None
        self.assertEqual(watch.findings(config), found, 'an unknown reading keeps the incident open')
        broken = MemoryWatch(read_inventory=Mock(side_effect=RuntimeError('docker down')))
        broken.current = {'arkive': found[0]}
        self.assertEqual(broken.findings(config), found, 'Docker unavailable keeps the incident open')
        containers['arkive'].status = 'exited'
        self.assertEqual(watch.findings(config), [], 'a stopped container is reported by the container check')
        self.assertEqual(watch.over, {})

    def test_memory_check_runs_once_per_interval(self):
        from monitor.watchers import MemoryWatch
        read = Mock(return_value=1024)
        watch = MemoryWatch(read_inventory=lambda: {'arkive': SimpleNamespace(name='arkive', status='running')}, read_memory=read)
        config = self.memory_config()
        config['thresholds']['container_memory_interval_seconds'] = 60
        watch.findings(config)
        watch.findings(config)
        self.assertEqual(read.call_count, 1)

    def test_high_memory_incident_keeps_one_fingerprint_as_numbers_change(self):
        self.assertEqual(fingerprint('arkive', 'arkive', 'CONTAINER_HIGH_MEMORY', 'Container memory is 2.0 GB (warning limit 1.5 GB)'),
                         fingerprint('arkive', 'arkive', 'CONTAINER_HIGH_MEMORY', 'Container memory is 3.4 GB (critical limit 3.0 GB)'))

    def test_docker_events_record_real_crashes_only_and_wake_the_loop(self):
        import threading
        from monitor.watchers import DockerEvents
        wake = threading.Event()
        events = DockerEvents(wake)
        def event(action, name, **attributes):
            return {'Action': action, 'time': 1761560000, 'Actor': {'Attributes': {'name': name, **attributes}}}
        events.handle(event('die', 'bot', exitCode='1'), now=100)
        self.assertTrue(wake.is_set())
        wake.clear()
        # A requested stop (docker stop / compose down) is not a crash.
        events.handle(event('kill', 'web', signal='15'), now=100)
        events.handle(event('die', 'web', exitCode='143'), now=101)
        # A clean exit is not a crash either.
        events.handle(event('die', 'job', exitCode='0'), now=101)
        # Out of memory: the oom event, then the die that follows.
        events.handle(event('oom', 'db'), now=102)
        events.handle(event('die', 'db', exitCode='137'), now=102)
        wake.clear()
        events.handle(event('health_status: healthy', 'bot'), now=103)
        self.assertFalse(wake.is_set(), 'routine healthy checks do not wake the loop')
        crashes = {crash['name']: crash for crash in events.take_crashes()}
        self.assertEqual(sorted(crashes), ['bot', 'db'])
        self.assertEqual((crashes['bot']['exit_code'], crashes['bot']['oom']), (1, False))
        self.assertEqual((crashes['db']['exit_code'], crashes['db']['oom']), (137, True))
        self.assertEqual(events.take_crashes(), [], 'taken once')

    def test_crash_notice_waits_for_restart_and_skips_containers_that_stay_down(self):
        from monitor.watchers import CrashNotices
        from datetime import datetime, timezone
        notices = CrashNotices(cooldown=600)
        crash = lambda name: {'name': name, 'exit_code': 1, 'oom': False, 'count': 1, 'at': datetime(2026, 10, 27, tzinfo=timezone.utc)}
        notices.add([crash('bot'), crash('dead'), crash('stranger')])
        monitored = {'bot': 'marketzig', 'dead': 'deadproj'}
        self.assertEqual(notices.ready(monitored, running=set(), down=set(), now=0), [], 'still restarting')
        self.assertIn('bot', notices.waiting)
        self.assertNotIn('stranger', notices.waiting, 'not a monitored container')
        ready = notices.ready(monitored, running={'bot'}, down={'dead'}, now=1)
        self.assertEqual([(item['name'], item['project']) for item in ready], [('bot', 'marketzig')])
        self.assertEqual(notices.waiting, {}, 'the down alert covers "dead"')
        notices.add([crash('bot'), crash('bot')])
        self.assertEqual(notices.ready(monitored, running={'bot'}, down=set(), now=60), [], 'cooldown')
        notices.add([crash('bot')])
        later = notices.ready(monitored, running={'bot'}, down=set(), now=700)
        self.assertEqual(later[0]['count'], 3, 'crashes during the cooldown are counted, not lost')

    def test_crash_message(self):
        from monitor.service import crash_message
        from datetime import datetime, timezone
        text = crash_message({'name': 'zig<bot>', 'project': 'marketzig', 'exit_code': None, 'oom': True, 'count': 2,
                              'at': datetime(2026, 10, 27, 10, 18, 13, tzinfo=timezone.utc)})
        self.assertIn('🔁 <b>CONTAINER CRASHED AND RESTARTED</b>', text)
        self.assertIn('<code>zig&lt;bot&gt;</code>', text)
        self.assertIn('Killed for running out of memory (OOM)', text)
        self.assertIn('<b>Crashes:</b> 2 since the last notice', text)
        self.assertIn('27 Oct 2026, 15:18:13 PKT', text)

    def test_mutes_persist_and_expire(self):
        from monitor.mutes import active_mutes, mute, unmute
        from datetime import datetime, timedelta, timezone
        now = datetime(2026, 10, 27, 10, 0, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / 'incidents.db')
            until = mute(path, 'host', 3600, 'Server (host)', now=now)
            self.assertEqual(until, now + timedelta(hours=1))
            self.assertIn('host', active_mutes(path, now=now + timedelta(minutes=59)))
            self.assertNotIn('host', active_mutes(path, now=now + timedelta(minutes=61)), 'expired')
            mute(path, 'marketzig', 3600, 'Zig Whale Bot', now=now)
            unmute(path, 'marketzig')
            self.assertEqual(active_mutes(path, now=now), {'host': {'until': until, 'label': 'Server (host)'}})

    def test_muted_projects_get_no_messages_and_their_recoveries_are_dropped(self):
        from monitor.service import without_muted
        with tempfile.TemporaryDirectory() as directory:
            store = IncidentStore(str(Path(directory) / 'incidents.db'))
            store.db.execute("INSERT INTO recovery_queue VALUES ('abc', '{}')")
            store.db.commit()
            notifications = [('k1', 'open host', 'host', None), ('recovery:abc', 'recovered host', 'host', None),
                             ('k2', 'open bot', 'marketzig', None), (None, 'crash bot', 'marketzig', None)]
            kept = without_muted(notifications, {'host': {}}, store)
            self.assertEqual([item[1] for item in kept], ['open bot', 'crash bot'])
            self.assertEqual(store.db.execute('SELECT COUNT(*) FROM recovery_queue').fetchone()[0], 0)
            store.db.close()

    def test_alerts_carry_a_mute_button(self):
        from monitor.notifier import TelegramNotifier
        from monitor.service import mute_keyboard
        self.assertEqual(mute_keyboard('host'), [[{'text': '🔕 Mute 1h', 'callback_data': 'mutep:host:3600'}]])
        self.assertIsNone(mute_keyboard('x' * 80), 'over the 64-byte callback limit: no button')
        sent = []
        with patch('monitor.notifier.requests.post', side_effect=lambda url, json, timeout: sent.append(json) or SimpleNamespace(ok=True)):
            self.assertTrue(TelegramNotifier('t', ['1']).send('hi', mute_keyboard('host')))
        self.assertEqual(sent[0]['reply_markup'], {'inline_keyboard': [[{'text': '🔕 Mute 1h', 'callback_data': 'mutep:host:3600'}]]})

    def test_mute_from_an_alert_and_from_the_bot_page(self):
        from monitor.mutes import active_mutes
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / 'incidents.db')
            projects = {'bots_docker': {'items': [{'id': 'marketzig', 'name': 'Zig Whale Bot', 'containers': []}]}}
            panel = TelegramBotPanel('token', ['1'], projects, database_path=path)
            calls = []
            with patch.object(panel, 'api', side_effect=lambda method, payload, **_: calls.append((method, payload)) or {'result': {}}):
                panel.handle({'callback_query': {'id': 'q1', 'data': 'mutep:host:3600', 'message': {'message_id': 7, 'chat': {'id': 1}}}})
            self.assertIn('host', active_mutes(path))
            toast = next(payload for method, payload in calls if method == 'answerCallbackQuery')
            self.assertTrue(toast['text'].startswith('🔕 Server (host) muted until'))
            markup = next(payload for method, payload in calls if method == 'editMessageReplyMarkup')
            self.assertEqual(markup['reply_markup']['inline_keyboard'][0][0]['callback_data'], 'unmutep:host')
            self.assertFalse(any(method == 'editMessageText' for method, _ in calls), 'the alert text is kept')
            # From the bot page: mute for 4h, then the page shows it and offers Unmute.
            with patch.object(panel, 'incident_history', return_value=(0, 0, 0)):
                text, keyboard = panel.mute_menu_page('bots_docker', 'marketzig')
                self.assertEqual([button['callback_data'] for button in keyboard[0]],
                                 ['mute:bots_docker:marketzig:3600', 'mute:bots_docker:marketzig:14400', 'mute:bots_docker:marketzig:86400'])
                with patch.object(panel, 'api', return_value={'result': {}}):
                    panel.handle({'callback_query': {'id': 'q2', 'data': 'mute:bots_docker:marketzig:14400', 'message': {'message_id': 8, 'chat': {'id': 1}}}})
                page, buttons = panel.pretty_project_page('bots_docker', 'marketzig')
            self.assertIn('🔕 <i>Alerts muted until', page)
            self.assertIn({'text': '🔔 Unmute', 'callback_data': 'mutemenu:bots_docker:marketzig'}, [b for row in buttons for b in row])
            muted_text, muted_buttons = panel.muted_page()
            self.assertIn('Server (host)', muted_text)
            self.assertIn('Zig Whale Bot', muted_text)
            self.assertEqual(len(muted_buttons), 2)

    # --- Automatic discovery of new containers ---------------------------------------

    @staticmethod
    def running(name, compose=None, image='img', status='running', working_dir='/root/saad/x'):
        labels = {'com.docker.compose.project': compose, 'com.docker.compose.project.working_dir': working_dir} if compose else {}
        return SimpleNamespace(name=name, status=status, attrs={'Config': {'Labels': labels, 'Image': image}})

    def discovery_base(self):
        return {'docker': {'containers': [{'name': 'zig-whale-bot', 'project': 'marketzig'}]},
                'projects': {'bots_docker': {'title': 'Bots (Docker)', 'items': [
                    {'id': 'marketzig', 'name': 'Zig Whale Bot', 'containers': ['zig-whale-bot']}]},
                    'frontend': {'title': 'Frontend', 'items': []}}}

    def test_unknown_containers_are_grouped_by_compose_project(self):
        from monitor.additions import unknown_groups
        containers = {
            'zig-whale-bot': self.running('zig-whale-bot', 'marketzig'),
            'sol-usdt-monitor': self.running('sol-usdt-monitor', 'sol-usdt-bot', image='sol-usdt-bot-monitor'),
            'sol-usdt-db': self.running('sol-usdt-db', 'sol-usdt-bot', image='postgres:16'),
            'loose': self.running('loose'),
            'stopped': self.running('stopped', 'old', status='exited'),
            'central-monitor': self.running('central-monitor', 'botmonitor'),
            'buildx_buildkit_builder0': self.running('buildx_buildkit_builder0'),
        }
        groups = unknown_groups(containers, {'zig-whale-bot'})
        self.assertEqual(sorted(groups), ['compose:sol-usdt-bot', 'container:loose'])
        sol = groups['compose:sol-usdt-bot']
        self.assertEqual(sorted(sol['containers']), ['sol-usdt-db', 'sol-usdt-monitor'])
        self.assertEqual(sol['details']['images'], ['sol-usdt-bot-monitor', 'postgres:16'])
        self.assertEqual(sol['details']['working_dir'], '/root/saad/x')

    def test_each_new_container_is_asked_about_only_once(self):
        from monitor.additions import discovered, set_status, sync_discovered
        base = self.discovery_base()
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / 'incidents.db')
            # Already running at startup: recorded silently, no question.
            self.assertEqual([info['name'] for info in sync_discovered(path, base, {'old': self.running('old', 'oldproj')}, announce=False)], ['oldproj'])
            self.assertEqual(discovered(path)['compose:oldproj']['status'], 'seen')
            containers = {'old': self.running('old', 'oldproj'), 'sol-usdt-monitor': self.running('sol-usdt-monitor', 'sol-usdt-bot')}
            new = sync_discovered(path, base, containers, announce=True)
            self.assertEqual([info['name'] for info in new], ['sol-usdt-bot'], 'only the new one is announced')
            self.assertEqual(discovered(path)['compose:sol-usdt-bot']['status'], 'announced')
            # Restarts / redeploys / compose up: never asked again, answered or not.
            for _ in range(3):
                self.assertEqual(sync_discovered(path, base, containers, announce=True), [])
            set_status(path, new[0]['id'], 'ignored')
            self.assertEqual(sync_discovered(path, base, containers, announce=True), [], 'ignored stays quiet')
            self.assertEqual(discovered(path)['compose:sol-usdt-bot']['status'], 'ignored')

    def test_added_project_joins_the_menu_and_checks_and_picks_up_new_containers(self):
        from monitor.additions import config_version, discovered, effective_config, set_status, sync_discovered
        from monitor.removals import project_key, remove_item
        base = self.discovery_base()
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / 'incidents.db')
            before = config_version(path)
            info = sync_discovered(path, base, {'sol-usdt-monitor': self.running('sol-usdt-monitor', 'sol-usdt-bot')}, announce=True)[0]
            set_status(path, info['id'], 'added', 'bots_docker')
            self.assertNotEqual(config_version(path), before, 'the main loop sees the change')
            config, _ = effective_config(base, path)
            item = config['projects']['bots_docker']['items'][-1]
            self.assertEqual((item['id'], item['name'], item['containers']), ('sol-usdt-bot', 'Sol Usdt Bot', ['sol-usdt-monitor']))
            self.assertIn({'name': 'sol-usdt-monitor', 'project': 'sol-usdt-bot', 'critical': True}, config['docker']['containers'])
            self.assertEqual(len(base['docker']['containers']), 1, 'config.yaml data is not modified')
            # A new service in the same compose project is added automatically, without asking.
            grown = {'sol-usdt-monitor': self.running('sol-usdt-monitor', 'sol-usdt-bot'), 'sol-usdt-db': self.running('sol-usdt-db', 'sol-usdt-bot')}
            self.assertEqual(sync_discovered(path, base, grown, announce=True), [])
            self.assertEqual(discovered(path)['compose:sol-usdt-bot']['containers'], ['sol-usdt-db', 'sol-usdt-monitor'])
            # Removing an added bot from Telegram works like any other bot.
            remove_item(path, project_key('bots_docker', 'sol-usdt-bot'), 'Sol Usdt Bot')
            config, _ = effective_config(base, path)
            self.assertNotIn('sol-usdt-bot', [i['id'] for i in config['projects']['bots_docker']['items']])
            self.assertNotIn('sol-usdt-monitor', [c['name'] for c in config['docker']['containers']])
            # A container that config.yaml lists but was removed in Telegram is not "new".
            self.assertEqual(sync_discovered(path, base, {'zig-whale-bot': self.running('zig-whale-bot', 'marketzig')}, announce=True), [])

    def test_new_container_alert_asks_once_with_three_buttons(self):
        from monitor.service import new_container_alert
        text, keyboard = new_container_alert({'id': 7, 'name': 'sol-usdt-bot', 'containers': ['sol-usdt-monitor'],
                                              'details': {'images': ['sol<usdt>'], 'working_dir': '/root/saad/Sol-USDT-bot'}})
        self.assertIn('🆕 <b>NEW CONTAINER DETECTED</b>', text)
        self.assertIn('<code>sol&lt;usdt&gt;</code>', text)
        self.assertIn('only be asked once', text)
        self.assertEqual([button['callback_data'] for row in keyboard for button in row],
                         ['disc:add:7:bots_docker', 'disc:add:7:frontend', 'disc:ign:7'])

    def test_add_and_ignore_buttons_and_scan(self):
        from monitor.additions import discovered, sync_discovered
        base = self.discovery_base()
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / 'incidents.db')
            containers = {'sol-usdt-monitor': self.running('sol-usdt-monitor', 'sol-usdt-bot'),
                          'web': self.running('web', 'newsite'), 'junk': self.running('junk', 'junkproj')}
            sync_discovered(path, base, containers, announce=False)
            ids = {row['name']: row['id'] for row in discovered(path).values()}
            panel = TelegramBotPanel('token', ['1'], base['projects'], database_path=path, base_config=base)
            with patch('monitor.telegram_bot.inventory', return_value=containers):
                text, keyboard = panel.scan_page()
                self.assertIn('3 project(s)', text)
                for name in ('Sol Usdt Bot', 'Newsite', 'Junkproj'):
                    self.assertIn(name, text)
                # Add from the "New container detected" message.
                answer, buttons = panel.new_container_answer(f"add:{ids['sol-usdt-bot']}:bots_docker")
                self.assertIn('added as a Bot', answer)
                self.assertEqual(buttons[0][0]['callback_data'], 'project:bots_docker:sol-usdt-bot')
                self.assertIn('sol-usdt-bot', [item['id'] for item in panel.projects['bots_docker']['items']], 'menu updated at once')
                # Add as a frontend and ignore from /scan.
                panel.discovery_action(f"add:{ids['newsite']}:frontend")
                self.assertIn('newsite', [item['id'] for item in panel.projects['frontend']['items']])
                answer, buttons = panel.new_container_answer(f"ign:{ids['junkproj']}")
                self.assertIn('will not be asked about it again', answer)
                text, keyboard = panel.scan_page()
                self.assertIn('Every running container is monitored or ignored.', text)
                self.assertEqual(keyboard, [[{'text': '🙈 Ignored (1)', 'callback_data': 'scan:ignored'}]])
                text, keyboard = panel.scan_page(show_ignored=True)
                self.assertIn('Junkproj', text)
                self.assertEqual(keyboard[0][0]['callback_data'], f"scan:add:{ids['junkproj']}:bots_docker", 'ignored can still be added later')
            self.assertEqual(panel.discovery_action('add:999:bots_docker')[0], '⚠️ That item no longer exists.')

    def test_docker_events_report_started_containers_for_discovery(self):
        import threading
        from monitor.watchers import DockerEvents
        events = DockerEvents(threading.Event())
        events.handle({'Action': 'start', 'Actor': {'Attributes': {'name': 'sol-usdt-monitor'}}}, now=1)
        self.assertEqual(events.take_started(), {'sol-usdt-monitor'})
        self.assertEqual(events.take_started(), set())

    def test_small_disk_parts_show_in_kb_and_empty_ones_are_hidden(self):
        from monitor.telegram_bot import disk_split
        from monitor.view import memory_size
        self.assertEqual(memory_size(300 * 1024), '300 KB')
        self.assertEqual(memory_size(5 * 1024 ** 2), '5 MB')
        self.assertEqual(disk_split({'volumes': 10 * 1024 ** 3, 'images': 0, 'logs': 200 * 1024, 'data': 500}),
                         'vol 10.0 GB · logs 200 KB', 'no "0 MB" parts: tiny ones in KB, under 1 KB hidden')
        self.assertIsNone(disk_split({'volumes': 0, 'images': 0, 'logs': None, 'data': 0}))

    def test_usage_without_log_mount_or_docker_disk_says_so(self):
        container = self.fake_container([], name='c0')
        container.stats = Mock(return_value={'memory_stats': {'usage': 1024 ** 3, 'stats': {}}, 'cpu_stats': {}, 'precpu_stats': {}})
        panel, patched = self.panel_for({'bots_docker': {'items': [{'id': 'p', 'name': 'P', 'containers': ['c0']}]}}, {'c0': container})
        snapshot = {'containers': {'c0': {'image_id': 'i', 'data': 0, 'volumes': []}}, 'images': {'i': 1024 ** 3}, 'volumes': {}, 'layers': 1024 ** 3}
        with patched, patch('monitor.telegram_bot.docker_disk', return_value=snapshot), patch('monitor.telegram_bot.log_size', return_value=None):
            text = panel.usage_text()
        self.assertIn('Log files not counted', text)
        panel.disk_cache = None
        with patched, patch('monitor.telegram_bot.docker_disk', side_effect=RuntimeError('df failed')):
            text = panel.usage_text()
        self.assertIn('├ Docker disk: unavailable', text)
        self.assertIn('├ RAM: 1.0 GB', text, 'RAM still shown when disk data is missing')

    def test_project_disk_counts_shared_images_and_volumes_once_and_unknown_volumes_not_at_all(self):
        from monitor.disk_usage import project_disk, docker_total
        snapshot = {'containers': {'a': {'image_id': 'i', 'data': 5, 'volumes': ['v', 'unmeasured']},
                                   'b': {'image_id': 'i', 'data': 7, 'volumes': ['v']}},
                    'images': {'i': 100}, 'volumes': {'v': 1000, 'unmeasured': -1}, 'layers': 100}
        self.assertEqual(project_disk(['a', 'b'], snapshot, {'a': 3, 'b': None}),
                         {'volumes': 1000, 'images': 100, 'data': 12, 'logs': 3, 'total': 1115})
        self.assertIsNone(project_disk(['a'], snapshot, {})['logs'], 'no log data -> unknown, not 0')
        self.assertEqual(docker_total(snapshot, {'a': 3}), 100 + 12 + 1000 + 3)

    def test_log_size_adds_rotated_files_and_is_unknown_without_the_mount(self):
        from monitor.disk_usage import log_size
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'abc-json.log'
            path.write_bytes(b'x' * 100)
            (Path(directory) / 'abc-json.log.1').write_bytes(b'x' * 50)
            self.assertEqual(log_size(SimpleNamespace(attrs={'LogPath': str(path)})), 150)
        self.assertIsNone(log_size(SimpleNamespace(attrs={'LogPath': '/nope/missing-json.log'})))
        self.assertIsNone(log_size(SimpleNamespace(attrs={})))

    def test_long_pages_are_sent_in_parts_with_buttons_kept(self):
        panel = TelegramBotPanel('token', ['1'], {})
        calls = []
        def api(method, payload, **_):
            calls.append((method, payload))
            return {'result': {'message_id': len(calls)}}
        long_text = '\n\n'.join(f'<blockquote><b>card {i}</b>\n└ ' + 'x' * 400 + '</blockquote>' for i in range(20))
        with patch.object(panel, 'api', side_effect=api):
            last_id = panel.send('1', long_text, [[{'text': 'b', 'callback_data': 'home'}]])
            sends = [payload for method, payload in calls]
            calls.clear()
            panel.edit('1', 9, long_text, [[{'text': 'b', 'callback_data': 'home'}]])
        self.assertGreater(len(sends), 1)
        self.assertTrue(all(len(payload['text']) <= 4096 for payload in sends))
        self.assertTrue(all(payload['text'].count('<blockquote>') == payload['text'].count('</blockquote>') for payload in sends))
        self.assertEqual(['reply_markup' in payload for payload in sends], [False] * (len(sends) - 1) + [True])
        self.assertEqual(last_id, len(sends))
        self.assertEqual(calls[0][0], 'editMessageText')
        self.assertIn('reply_markup', calls[0][1], 'the edited page keeps its buttons')
        self.assertTrue(all(method == 'sendMessage' for method, _ in calls[1:]))

    def test_telegram_timeout_logs_one_line_without_traceback_but_other_errors_keep_it(self):
        import requests
        panel = TelegramBotPanel('token', ['1'], {})
        with patch('monitor.telegram_bot.requests.post', side_effect=requests.ReadTimeout('read timed out')), \
             self.assertLogs('monitor.telegram_bot', level='WARNING') as logs:
            self.assertIsNone(panel.api('getUpdates', {}))
        self.assertEqual(len(logs.records), 1)
        self.assertEqual(logs.records[0].levelname, 'WARNING')
        self.assertIsNone(logs.records[0].exc_info)
        with patch('monitor.telegram_bot.requests.post', side_effect=requests.HTTPError('500')), \
             self.assertLogs('monitor.telegram_bot', level='ERROR') as logs:
            self.assertIsNone(panel.api('sendMessage', {}))
        self.assertIsNotNone(logs.records[0].exc_info)

    def test_nawa_falls_back_to_cc_status_then_abci_info_when_status_is_forbidden(self):
        import requests
        blocked_cc_status = False
        def rpc_response(url, **_kwargs):
            public = 'zigscan' in url or 'cryptocomics' in url
            if public and url.endswith('/status') and not url.endswith('/cc/status'):
                raise requests.HTTPError('403 Forbidden')
            if public and url.endswith('/cc/status'):
                if blocked_cc_status:
                    raise requests.HTTPError('403 Forbidden')
                return SimpleNamespace(raise_for_status=Mock(), json=lambda: {'result': {'sync_info': {
                    'latest_block_height': '12680020', 'catching_up': False}}})
            if url.endswith('/abci_info'):
                return SimpleNamespace(raise_for_status=Mock(), json=lambda: {'result': {'response': {'last_block_height': '12680021'}}})
            return SimpleNamespace(raise_for_status=Mock(), json=lambda: {'result': {'sync_info': {
                'latest_block_height': '12680019', 'catching_up': False}}})
        state = {'running': True, 'status': 'running', 'health': None}
        with patch('monitor.reports._container_state', return_value=state), \
             patch('monitor.reports.requests.get', side_effect=rpc_response):
            result = nawa_valdora_results()
        self.assertTrue(result['ok'], result['error'])
        self.assertEqual(result['unreachable_public'], [])
        self.assertEqual({row['name']: row['height'] for row in result['rpcs']}['ZigScan'], 12680020)
        blocked_cc_status = True
        with patch('monitor.reports._container_state', return_value=state), \
             patch('monitor.reports.requests.get', side_effect=rpc_response):
            result = nawa_valdora_results()
        self.assertTrue(result['ok'], result['error'])
        self.assertEqual({row['name']: row['height'] for row in result['rpcs']}['CryptoComics'], 12680021)

    def test_nawa_asks_zigscan_and_cryptocomics_on_cc_status_first(self):
        calls = []
        def rpc_response(url, **_kwargs):
            calls.append(url)
            return SimpleNamespace(raise_for_status=Mock(), json=lambda: {'result': {'sync_info': {
                'latest_block_height': '12680019', 'catching_up': False}}})
        with patch('monitor.reports._container_state', return_value={'running': True, 'status': 'running', 'health': None}), \
             patch('monitor.reports.requests.get', side_effect=rpc_response):
            result = nawa_valdora_results()
        self.assertTrue(result['ok'], result['error'])
        self.assertIn('https://zigchain-mainnet.zigscan.net/cc/status', calls)
        self.assertIn('https://cryptocomics-rpc.wickhub.cc/cc/status', calls)
        self.assertNotIn('https://zigchain-mainnet.zigscan.net/status', calls)
        self.assertIn('http://internal-bots-rpc.wickhub.cc/cc/status', calls)
        self.assertNotIn('http://internal-bots-rpc.wickhub.cc/status', calls)

    def test_nawa_internal_rpc_falls_back_to_status_until_cc_status_exists(self):
        import requests
        def rpc_response(url, **_kwargs):
            if 'internal-bots-rpc' in url and url.endswith('/cc/status'):
                raise requests.HTTPError('404 Not Found')
            return SimpleNamespace(raise_for_status=Mock(), json=lambda: {'result': {'sync_info': {
                'latest_block_height': '12680019', 'catching_up': False}}})
        with patch('monitor.reports._container_state', return_value={'running': True, 'status': 'running', 'health': None}), \
             patch('monitor.reports.requests.get', side_effect=rpc_response):
            result = nawa_valdora_results()
        self.assertTrue(result['ok'], result['error'])
        self.assertEqual(result['height'], 12680019)

    def test_nawa_ignores_unreachable_public_rpcs_but_not_a_missing_internal_rpc(self):
        import requests
        heights = {'internal-bots-rpc': 12680019, 'numia': 12680021}
        def rpc_response(url, **_kwargs):
            match = next((value for key, value in heights.items() if key in url), None)
            if match is None:
                raise requests.ConnectionError('blocked')
            return SimpleNamespace(raise_for_status=Mock(), json=lambda: {'result': {'sync_info': {
                'latest_block_height': str(match), 'catching_up': False}}})
        state = {'running': True, 'status': 'running', 'health': None}
        with patch('monitor.reports._container_state', return_value=state), \
             patch('monitor.reports.requests.get', side_effect=rpc_response):
            result = nawa_valdora_results()
        self.assertTrue(result['ok'], result['error'])
        self.assertEqual(result['unreachable_public'], ['ZigScan', 'CryptoComics'])
        self.assertEqual(result['internal_gap'], 2)
        self.assertIn('unreachable, not counted', bots_report_message([], nawa_results=result))
        del heights['internal-bots-rpc']
        with patch('monitor.reports._container_state', return_value=state), \
             patch('monitor.reports.requests.get', side_effect=rpc_response):
            down = nawa_valdora_results()
        self.assertFalse(down['ok'])
        self.assertIn('Internal RPC height unavailable', down['error'])
        heights.clear()
        with patch('monitor.reports._container_state', return_value=state), \
             patch('monitor.reports.requests.get', side_effect=rpc_response):
            nothing = nawa_valdora_results()
        self.assertFalse(nothing['ok'])

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

    # --- Sol USDT bot -----------------------------------------------------------

    @staticmethod
    def sol_logs(last_slot, seconds_ago, extra=''):
        now = datetime.now(timezone.utc)
        lines = [f"{(now - timedelta(seconds=seconds_ago + (last_slot - slot) * 0.4)).isoformat()} sol-usdt-monitor  | [BLOCK] Live slot {slot}"
                 for slot in range(last_slot - 20, last_slot + 1)]
        return '\n'.join(lines) + extra

    def sol_result(self, logs, heads, running=True):
        from monitor.reports import sol_usdt_results
        heads = list(heads)
        with patch('monitor.reports._container_state', return_value={'running': running, 'status': 'running', 'health': None}), \
             patch('monitor.reports._container_logs', return_value=logs), \
             patch('monitor.reports._solana_head', side_effect=heads):
            return sol_usdt_results({'rpcs': ['a', 'b'][:len(heads)]})

    def test_sol_usdt_is_healthy_when_live_and_close_to_head(self):
        result = self.sol_result(self.sol_logs(455995881, 1), [455995880, 455995883])
        self.assertTrue(result['ok'], result['error'])
        self.assertEqual(result['lag'], 2)
        self.assertEqual(result['sync_problem'], '')

    def test_sol_usdt_small_drift_is_not_an_alert_but_100_behind_is(self):
        small = self.sol_result(self.sol_logs(455995881, 1), [455995881 + 99])
        self.assertTrue(small['ok'])
        far = self.sol_result(self.sol_logs(455995881, 1), [455995881 + 100])
        self.assertFalse(far['ok'])
        self.assertIn('100 slots behind', far['sync_problem'])

    def test_sol_usdt_stalled_log_is_flagged(self):
        result = self.sol_result(self.sol_logs(455995881, 120), [455995881])
        self.assertIn('Stalled', result['sync_problem'])

    def test_sol_usdt_uses_the_bots_own_sync_line(self):
        now = datetime.now(timezone.utc).isoformat()
        synced = self.sol_result(self.sol_logs(455995881, 1, f"\n{now} x | [SYNC] SYNCED | ws slot 455995881 | rpc slot 455995880 | behind -1"), [455995881])
        self.assertTrue(synced['ok'])
        self.assertIn('synced', synced['summary'])
        behind = self.sol_result(self.sol_logs(455995881, 1, f"\n{now} x | [SYNC] BEHIND | ws slot 455995881 | rpc slot 455996001 | behind 120"), [455995881])
        self.assertIn('120 slots behind its RPC', behind['sync_problem'])

    def test_sol_usdt_rpc_outage_is_reported_but_not_a_sync_alert(self):
        from monitor.reports import sol_usdt_results
        with patch('monitor.reports._container_state', return_value={'running': True, 'status': 'running', 'health': None}), \
             patch('monitor.reports._container_logs', return_value=self.sol_logs(455995881, 1)), \
             patch('monitor.reports._solana_head', side_effect=requests_error()):
            result = sol_usdt_results({'rpcs': ['a']})
        self.assertFalse(result['ok'])
        self.assertEqual(result['sync_problem'], '')

    def test_sol_watch_needs_two_bad_checks_then_holds_until_clear(self):
        from monitor.service import SolSyncWatch
        watch = SolSyncWatch()
        config = {'bot_reports': {'sol_usdt': {'check_interval_seconds': 0}}}
        bad = {'running': True, 'sync_problem': '150 slots behind', 'height': 1, 'lag': 150}
        good = {'running': True, 'sync_problem': '', 'height': 1, 'lag': 1}
        with patch('monitor.service.sol_usdt_results', side_effect=[bad, bad, good]):
            self.assertEqual(watch.findings(config), [])
            self.assertEqual(watch.findings(config)[0]['type'], 'SOL_SYNC_LAG')
            self.assertEqual(watch.findings(config), [])
        self.assertEqual(fingerprint('sol-usdt', 's', 'SOL_SYNC_LAG', '150 slots behind 456000000'),
                         fingerprint('sol-usdt', 's', 'SOL_SYNC_LAG', '210 slots behind 456000999'))

    def test_removed_sol_bot_stops_the_watch(self):
        from monitor.removals import apply_removals, project_key
        from monitor.service import SolSyncWatch
        base = {'bot_reports': {'sol_usdt': {'container': 'sol-usdt-monitor'}},
                'projects': {'bots_docker': {'items': [{'id': 'sol-usdt', 'name': 'Sol', 'containers': ['sol-usdt-monitor']}]}}}
        config = apply_removals(base, {project_key('bots_docker', 'sol-usdt')})
        with patch('monitor.service.sol_usdt_results') as check:
            self.assertEqual(SolSyncWatch().findings(config), [])
        check.assert_not_called()

    def test_sol_usdt_is_added_to_menu_for_old_configs_but_not_duplicated(self):
        old = 'projects:\n  bots_docker:\n    title: Bots\n    items:\n      - {id: other, name: Other, containers: [other]}\ndocker:\n  containers:\n    - {name: other, project: other}\n'
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'config.yaml'
            path.write_text(old, encoding='utf-8')
            config = load_config(str(path))
            self.assertEqual([i['id'] for i in config['projects']['bots_docker']['items']], ['other', 'sol-usdt', 'nawa-finance-redeemer'])
            self.assertEqual(config['bot_reports']['nawa_finance_redeemer']['container'], 'nawa-finance-redeemer')
            self.assertIn('sol-usdt-monitor', [c['name'] for c in config['docker']['containers']])
            self.assertEqual(config['bot_reports']['sol_usdt']['max_slot_lag'], 100)
            self.assertEqual(config['docker']['containers'][-2]['project'], 'sol-usdt')
            path.write_text(old.replace('Other, containers: [other]', 'Other, containers: [other]}\n      - {id: sol-usdt, name: Mine, containers: [sol-usdt-monitor]'), encoding='utf-8')
            again = load_config(str(path))
            self.assertEqual([i['name'] for i in again['projects']['bots_docker']['items']], ['Other', 'Mine', 'NAWA Finance Redeemer'])

    # --- Remove from monitoring -------------------------------------------------

    @staticmethod
    def removal_config():
        return {
            'docker': {'containers': [
                {'name': 'web', 'project': 'site', 'health_url': 'http://x'},
                {'name': 'db', 'project': 'site'},
                {'name': 'zig-whale-bot', 'project': 'marketzig'},
            ]},
            'systemd': {'services': [
                {'name': 'wallet-watchman', 'project': 'wallet-monitor', 'unit': 'wallet-watchman.service'},
                {'name': 'zigchain-exporter', 'project': 'zigchain-exporter', 'unit': 'zigchain-exporter.service'},
            ]},
            'daily_reports': {'frontend': {'enabled': True, 'items': [
                {'id': 'site', 'name': 'Site', 'health_url': 'http://x'}]}},
            'projects': {
                'frontend': {'items': [{'id': 'site', 'name': 'Site', 'containers': ['web', 'db']}]},
                'bots_docker': {'items': [{'id': 'marketzig', 'name': 'Whale', 'containers': ['zig-whale-bot']}]},
                'bots_systemd': {'items': [{'id': 'system-services', 'name': 'System Services', 'services': [
                    {'name': 'wallet-watchman', 'unit': 'wallet-watchman.service'},
                    {'name': 'zigchain-exporter', 'unit': 'zigchain-exporter.service'}]}]},
            },
            'bot_reports': {'zig_whale': {'container': 'zig-whale-bot'}},
        }

    def test_removed_frontend_project_leaves_every_check_and_report(self):
        from monitor.removals import apply_removals, project_key
        base = self.removal_config()
        config = apply_removals(base, {project_key('frontend', 'site')})
        self.assertEqual([c['name'] for c in config['docker']['containers']], ['zig-whale-bot'])
        self.assertEqual(config['projects']['frontend']['items'], [])
        self.assertEqual(config['daily_reports']['frontend']['items'], [])
        self.assertFalse(config['daily_reports']['frontend']['enabled'])
        self.assertEqual(frontend_report_items(config['daily_reports']['frontend'], config['projects']), [])
        self.assertEqual(len(base['docker']['containers']), 3, 'base config must stay untouched')

    def test_nawa_finance_redeemer_is_healthy_with_fresh_zig_heartbeat(self):
        logs = "2026-10-19T10:00:00Z [INFO] [ZIG] mempool contains 0 txs\n"
        with patch("monitor.reports._container_state", return_value={"running": True, "status": "running", "health": None}), \
             patch("monitor.reports._container_logs", return_value=logs), \
             patch("monitor.reports.datetime") as dt:
            dt.now.return_value = datetime(2026, 10, 19, 10, 1, tzinfo=timezone.utc)
            dt.fromisoformat.side_effect = datetime.fromisoformat
            result = nawa_finance_redeemer_results({"container": "nawa-finance-redeemer"})
        self.assertTrue(result["ok"])
        self.assertEqual(result["zig_errors"], 0)
        self.assertEqual(result["eth_errors"], 0)
        self.assertEqual(result["tg_errors"], 0)
        self.assertIn("Zig heartbeat", result["summary"])

    def test_nawa_finance_redeemer_flags_stale_zig_heartbeat(self):
        logs = "2026-10-19T10:00:00Z [INFO] [ZIG] mempool contains 0 txs\n"
        with patch("monitor.reports._container_state", return_value={"running": True, "status": "running", "health": None}), \
             patch("monitor.reports._container_logs", return_value=logs), \
             patch("monitor.reports.datetime") as dt:
            dt.now.return_value = datetime(2026, 10, 19, 10, 4, tzinfo=timezone.utc)
            dt.fromisoformat.side_effect = datetime.fromisoformat
            result = nawa_finance_redeemer_results({
                "container": "nawa-finance-redeemer",
                "stale_after_seconds": 180,
            })
        self.assertFalse(result["ok"])
        self.assertIn("No fresh '[ZIG] mempool contains'", result["error"])

    def test_nawa_finance_redeemer_flags_eth_error_threshold(self):
        logs = "\n".join([
            "2026-10-19T10:05:00Z [INFO] [ZIG] mempool contains 0 txs",
            "2026-10-19T10:06:00Z [WARN] [ETH] websocket error",
            "2026-10-19T10:07:00Z [WARN] [ETH] websocket disconnected, reconnecting",
            "2026-10-19T10:08:00Z [WARN] [ETH] pong timeout, terminating socket",
        ])
        with patch("monitor.reports._container_state", return_value={"running": True, "status": "running", "health": None}), \
             patch("monitor.reports._container_logs", return_value=logs), \
             patch("monitor.reports.datetime") as dt:
            dt.now.return_value = datetime(2026, 10, 19, 10, 9, tzinfo=timezone.utc)
            dt.fromisoformat.side_effect = datetime.fromisoformat
            result = nawa_finance_redeemer_results({
                "container": "nawa-finance-redeemer",
                "error_window_seconds": 600,
                "error_threshold": 3,
            })
        self.assertFalse(result["ok"])
        self.assertEqual(result["eth_errors"], 3)
        self.assertIn("ETH errors 3/600s", result["error"])

    def test_nawa_finance_redeemer_flags_zig_error_threshold(self):
        logs = "\n".join([
            "2026-10-19T10:05:00Z [INFO] [ZIG] mempool contains 0 txs",
            "2026-10-19T10:06:00Z [WARN] [ZIG] rpc timeout, retrying",
            "2026-10-19T10:07:00Z [WARN] [ZIG] mempool poll failed, retrying",
            "2026-10-19T10:08:00Z [WARN] [ZIG] rpc timeout, retrying",
        ])
        with patch("monitor.reports._container_state", return_value={"running": True, "status": "running", "health": None}), \
             patch("monitor.reports._container_logs", return_value=logs), \
             patch("monitor.reports.datetime") as dt:
            dt.now.return_value = datetime(2026, 10, 19, 10, 9, tzinfo=timezone.utc)
            dt.fromisoformat.side_effect = datetime.fromisoformat
            result = nawa_finance_redeemer_results({
                "container": "nawa-finance-redeemer",
                "error_window_seconds": 600,
                "error_threshold": 3,
            })
        self.assertFalse(result["ok"])
        self.assertEqual(result["zig_errors"], 3)
        self.assertIn("ZIG errors 3/600s", result["error"])

    def test_nawa_finance_redeemer_flags_telegram_error_threshold(self):
        logs = "\n".join([
            "2026-10-19T10:05:00Z [INFO] [ZIG] mempool contains 0 txs",
            "2026-10-19T10:06:00Z [WARN] [TG] getUpdates failed, retrying",
            "2026-10-19T10:07:00Z [WARN] [TG] getUpdates failed, retrying",
            "2026-10-19T10:08:00Z [WARN] [TG] getUpdates failed, retrying",
        ])
        with patch("monitor.reports._container_state", return_value={"running": True, "status": "running", "health": None}), \
             patch("monitor.reports._container_logs", return_value=logs), \
             patch("monitor.reports.datetime") as dt:
            dt.now.return_value = datetime(2026, 10, 19, 10, 9, tzinfo=timezone.utc)
            dt.fromisoformat.side_effect = datetime.fromisoformat
            result = nawa_finance_redeemer_results({
                "container": "nawa-finance-redeemer",
                "error_window_seconds": 600,
                "error_threshold": 3,
            })
        self.assertFalse(result["ok"])
        self.assertEqual(result["tg_errors"], 3)
        self.assertIn("TG errors 3/600s", result["error"])

    def test_nawa_finance_redeemer_flags_stopped_container(self):
        logs = "2026-10-19T10:05:00Z [INFO] [ZIG] mempool contains 0 txs\n"
        with patch("monitor.reports._container_state", return_value={"running": False, "status": "exited", "health": None}), \
             patch("monitor.reports._container_logs", return_value=logs), \
             patch("monitor.reports.datetime") as dt:
            dt.now.return_value = datetime(2026, 10, 19, 10, 6, tzinfo=timezone.utc)
            dt.fromisoformat.side_effect = datetime.fromisoformat
            result = nawa_finance_redeemer_results({"container": "nawa-finance-redeemer"})
        self.assertFalse(result["ok"])
        self.assertIn("Container is exited", result["error"])

    def test_nawa_finance_redeemer_appears_in_combined_report(self):
        result = {
            "name": "NAWA Finance Redeemer",
            "ok": True,
            "summary": "container running; Zig heartbeat 30s ago; ZIG errors 0/600s; ETH errors 0/600s; TG errors 0/600s",
            "error": "",
        }
        message = bots_report_message([], nawa_finance_results=result)
        self.assertIn("NAWA Finance Redeemer", message)
        self.assertIn("Zig heartbeat 30s ago", message)

    def test_removed_docker_bot_is_dropped_from_bots_report(self):
        from monitor.removals import apply_removals, project_key
        config = apply_removals(self.removal_config(), {project_key('bots_docker', 'marketzig')})
        self.assertFalse(config['bot_reports']['zig_whale']['enabled'])
        with patch('monitor.reports.zig_whale_exchange_results') as whale:
            message = collect_bots_report({**config['bot_reports'], **{
                key: {'enabled': False} for key in ('zig_usd_alerts', 'wallet_monitor', 'zigchain_bot', 'stake_unstake',
                                                    'mdf_tracker', 'highbuy_monitor', 'nawa_valdora', 'nawa_finance_redeemer', 'tokenx_vault',
                                                    'bep20_usdt', 'eth_usdt', 'sheets_sync', 'system_services', 'sol_usdt')}})
        whale.assert_not_called()
        self.assertIn('No bots are being monitored', message)
        self.assertNotIn('Zig Whale', message)

    def test_removed_system_service_is_dropped_from_checks_and_report(self):
        from monitor.removals import apply_removals, service_key
        config = apply_removals(self.removal_config(), {service_key('bots_systemd', 'system-services', 'wallet-watchman')})
        self.assertEqual([s['name'] for s in config['systemd']['services']], ['zigchain-exporter'])
        self.assertEqual([s['name'] for s in config['projects']['bots_systemd']['items'][0]['services']], ['zigchain-exporter'])
        snapshot = {'services': {'zigchain-exporter.service': {'active_state': 'active', 'sub_state': 'running'}}}
        with patch('monitor.reports.read_systemd_snapshot', return_value=snapshot):
            result = system_service_results(config['bot_reports']['system_services'])
        self.assertEqual([row['name'] for row in result['services']], ['Zigchain Exporter'])
        both = apply_removals(self.removal_config(), {service_key('bots_systemd', 'system-services', n) for n in ('wallet-watchman', 'zigchain-exporter')})
        self.assertEqual(both['projects']['bots_systemd']['items'], [])
        self.assertFalse(both['bot_reports']['system_services']['enabled'])

    def test_discard_closes_removed_incidents_silently_without_touching_others(self):
        with tempfile.TemporaryDirectory() as directory:
            store = IncidentStore(str(Path(directory) / 'incidents.db'))
            for project, service, kind in (('site', 'web', 'CONTAINER_DOWN'), ('site', 'site-project', 'PROJECT_DOWN'),
                                           ('wallet-monitor', 'wallet-watchman', 'SYSTEMD_DOWN'),
                                           ('wallet-monitor', 'wallet-monitor', 'CONTAINER_DOWN')):
                store.observe(fingerprint(project, service, kind, kind), project, service, kind, 'CRITICAL', kind, 0)
            closed = store.discard({'site'}, {'web'}, {'wallet-watchman'})
            self.assertEqual(closed, 3)
            still_open = [row['service'] for row in store.db.execute("SELECT service FROM incidents WHERE status='OPEN'")]
            self.assertEqual(still_open, ['wallet-monitor'])
            self.assertEqual(store.db.execute('SELECT COUNT(*) FROM recovery_queue').fetchone()[0], 0)
            store.db.close()

    def test_panel_remove_persists_hides_item_and_can_restore(self):
        base = self.removal_config()
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / 'incidents.db')
            panel = TelegramBotPanel(None, ['1'], base['projects'], database_path=path, base_config=base)
            text, keyboard = panel.remove_confirm_page('frontend', 'site')
            self.assertIn('Remove from monitoring?', text)
            self.assertIn('rm:frontend:site', keyboard[0][0]['callback_data'])
            panel.remove_item('frontend', 'site')
            self.assertEqual(panel.projects['frontend']['items'], [])
            # A fresh panel (monitor restart) still sees the removal.
            restarted = TelegramBotPanel(None, ['1'], base['projects'], database_path=path, base_config=base)
            restarted.refresh()
            self.assertEqual(restarted.projects['frontend']['items'], [])
            text, keyboard = restarted.removed_page()
            self.assertIn('Site', text)
            restarted.restore(int(keyboard[0][0]['callback_data'].split(':')[1]))
            self.assertEqual(len(restarted.projects['frontend']['items']), 1)

    def test_panel_remove_service_uses_name_not_position(self):
        base = self.removal_config()
        with tempfile.TemporaryDirectory() as directory:
            panel = TelegramBotPanel(None, ['1'], base['projects'], database_path=str(Path(directory) / 'incidents.db'), base_config=base)
            panel.remove_item('bots_systemd', 'system-services', 'wallet-watchman')
            names = [s['name'] for s in panel.projects['bots_systemd']['items'][0]['services']]
            self.assertEqual(names, ['zigchain-exporter'])
            text, _ = panel.remove_item('bots_systemd', 'system-services', 'wallet-watchman')
            self.assertIn('Item not found', text)


if __name__ == '__main__':
    unittest.main()
