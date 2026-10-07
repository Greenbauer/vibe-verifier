"""The real collector output must render owner capacity without invented readiness."""
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

from dashboard.collector import refresh
from dashboard.collector_view import join_runner_jobs
from dashboard.config import Config
from dashboard.telemetry import read_telemetry
from test_dashboard_collector import config, quota, remote_host

NOW = datetime(2026, 9, 30, 12, tzinfo=timezone.utc)
STAMP = '2026-09-30T12:00:00Z'


class CollectorIntegration(unittest.TestCase):
    def test_real_collector_contract_projects_combined_slots_caps_and_actual_quota(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'telemetry.json'
            host = remote_host()
            host['slots']['limit'] = 16
            refresh(config(), path, fetch=lambda *_: {'host': host, 'quota': {'status': 'ok', 'rate_limits': quota()}}, now=STAMP)
            parsed = read_telemetry(Config('example-ci', ('example-ci/repo',), {}, path), NOW)
        self.assertTrue(parsed['available'])
        self.assertEqual(len(parsed['capacity']['lanes']), 16)
        self.assertTrue(all(lane['state'] == 'provisionable' and lane['registered'] is False for lane in parsed['capacity']['lanes']))
        self.assertEqual(parsed['capacity']['limits']['qae_concurrency'], 2)
        self.assertEqual(parsed['capacity']['limits']['memory_max_bytes'], 100)
        self.assertEqual(parsed['capacity']['host']['memory_total_bytes'], 1000)
        account = parsed['usage']['accounts'][0]
        self.assertEqual(account['id'], 'collector:codex')
        self.assertEqual(account['label'], 'Codex')
        self.assertEqual(account['provider'], 'OpenAI')
        window = account['quota_windows'][0]
        self.assertEqual(window['name'], '7 days')
        self.assertEqual(window['used_percent'], 22)
        self.assertIsNone(window['allowance_tokens'])
        self.assertEqual(window['window_minutes'], 10080)
        self.assertEqual(parsed['usage']['samples'], [])

    def test_each_metered_plan_is_its_own_account_and_windows_keep_their_length(self):
        limits = [
            {"limit_id": "codex", "windows": [
                {"name": "secondary", "duration_minutes": 10080, "used_percent": 70, "resets_at": 1791383461},
                {"name": "primary", "duration_minutes": 300, "used_percent": 0, "resets_at": 1791383461}]},
            {"limit_id": "gpt-5.4", "windows": [
                {"name": "primary", "duration_minutes": 1440, "used_percent": 10, "resets_at": 1791383461}]}]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'telemetry.json'
            refresh(config(), path, fetch=lambda *_: {
                'host': remote_host(), 'quota': {'status': 'ok', 'rate_limits': limits}}, now=STAMP)
            parsed = read_telemetry(Config('example-ci', ('example-ci/repo',), {}, path), NOW)
        accounts = parsed['usage']['accounts']
        self.assertEqual([(account['id'], account['label']) for account in accounts],
                         [('collector:codex', 'Codex'), ('collector:gpt-5.4', 'gpt-5.4')])
        self.assertTrue(all(account['provider'] == 'OpenAI' for account in accounts))
        self.assertEqual([window['name'] for window in accounts[0]['quota_windows']], ['7 days', '5 hours'])
        self.assertEqual(accounts[1]['quota_windows'][0]['name'], '1 day')

    def test_stale_snapshot_never_claims_slots_are_ready(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'telemetry.json'
            refresh(config(), path, fetch=lambda *_: {'host': remote_host(), 'quota': {'status': 'ok', 'rate_limits': quota()}}, now=STAMP)
            selected = Config('example-ci', ('example-ci/repo',), {}, path)
            parsed = read_telemetry(selected, NOW + timedelta(minutes=6))
            self.assertTrue(parsed['capacity']['stale'])
            self.assertTrue(all(row['state'] == 'unknown' for row in parsed['capacity']['lanes']))
            self.assertFalse(parsed['bots']['available'])
            self.assertEqual(parsed['capacity']['sampled_at'], STAMP)
            value = json.loads(path.read_text()); value['owner'] = 'other'
            path.write_text(json.dumps(value))
            self.assertFalse(read_telemetry(selected, NOW)['available'])

    def test_allocation_becomes_busy_only_for_same_owner_matching_active_runner(self):
        lane = {'state': 'allocated', 'runner_id': 42, 'job': {'repository': 'example/repo', 'name': 'Allocated', 'url': None}}
        telemetry = {'capacity': {'available': True, 'stale': False, 'lanes': [lane]}}
        job = {'runner_id': 42, 'status': 'queued', 'name': 'Build', 'html_url': 'https://github.com/example/repo/actions/runs/1/job/2'}
        repository = {'repository': 'example/repo', 'pulls': [{'runs': [{'jobs': [job]}]}]}
        github = {'repositories': [repository]}
        join_runner_jobs(telemetry, github)
        self.assertEqual(lane['state'], 'allocated')
        job['status'] = 'in_progress'; repository['repository'] = 'other/repo'
        join_runner_jobs(telemetry, github)
        self.assertEqual(lane['state'], 'allocated')
        repository['repository'] = 'example/repo'
        join_runner_jobs(telemetry, github)
        self.assertEqual(lane['state'], 'busy')
        self.assertEqual(lane['job']['name'], 'Build')

    def test_an_organization_scope_allocation_takes_its_repository_from_the_matching_runner_job(self):
        occupied = [{'kind': 'ci', 'index': 1, 'state': 'allocated', 'unit': {'active_state': 'active', 'sub_state': 'running'},
                     'target_repository': None, 'set_id': 2, 'runner_id': 42, 'runner_name': 'runner-ci-1',
                     'allocated_at': STAMP}]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'telemetry.json'
            refresh(config(), path, fetch=lambda *_: {'host': remote_host(occupied), 'quota': {'status': 'ok', 'rate_limits': quota()}}, now=STAMP)
            telemetry = read_telemetry(Config('example-ci', ('example-ci/repo',), {}, path), NOW)
        lane = telemetry['capacity']['lanes'][0]
        self.assertEqual((lane['state'], lane['runner_id']), ('allocated', 42))
        self.assertNotIn('job', lane)
        job = {'runner_id': 42, 'status': 'in_progress', 'name': 'Build', 'html_url': 'https://github.com/example-ci/repo/actions/runs/1/job/2'}
        join_runner_jobs(telemetry, {'repositories': [{'repository': 'example-ci/repo', 'pulls': [{'runs': [{'jobs': [job]}]}]}]})
        self.assertEqual(lane['state'], 'busy')
        self.assertEqual(lane['job'], {'repository': 'example-ci/repo', 'name': 'Build', 'url': job['html_url']})
