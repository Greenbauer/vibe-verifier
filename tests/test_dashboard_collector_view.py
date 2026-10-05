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
        window = parsed['usage']['accounts'][0]['quota_windows'][0]
        self.assertIn('7d', window['name'])
        self.assertEqual(window['used_percent'], 22)
        self.assertIsNone(window['allowance_tokens'])
        self.assertEqual(window['window_minutes'], 10080)
        self.assertEqual(parsed['usage']['samples'], [])

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
