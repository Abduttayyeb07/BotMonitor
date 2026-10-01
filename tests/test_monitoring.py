import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from monitor.config import load_config
from monitor.docker_checks import checks
from monitor.incident_store import IncidentStore, fingerprint
from monitor.service import collapse_project_failures, OutageWindow
from monitor.telegram_bot import TelegramBotPanel


class MonitoringRegressionTests(unittest.TestCase):
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


if __name__ == '__main__':
    unittest.main()
