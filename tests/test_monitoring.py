import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from monitor.config import load_config
from monitor.docker_checks import checks, inventory
from monitor.incident_store import IncidentStore, fingerprint
from monitor.reports import frontend_report_message, should_send_daily_report
from monitor.service import collapse_project_failures, OutageWindow
from monitor.telegram_bot import TelegramBotPanel
from datetime import datetime
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


if __name__ == '__main__':
    unittest.main()
