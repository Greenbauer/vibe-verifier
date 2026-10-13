"""The real collector output must render owner capacity without invented readiness."""
import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from dashboard import collector
from dashboard.collector import refresh
from dashboard.collector_view import collector_view, join_runner_jobs
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

    def test_stale_snapshot_keeps_the_last_lanes_and_marks_them_stale(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'telemetry.json'
            refresh(config(), path, fetch=lambda *_: {'host': remote_host(), 'quota': {'status': 'ok', 'rate_limits': quota()}}, now=STAMP)
            selected = Config('example-ci', ('example-ci/repo',), {}, path)
            parsed = read_telemetry(selected, NOW + timedelta(minutes=6))
            self.assertTrue(parsed['capacity']['stale'])
            self.assertTrue(parsed['capacity']['lanes'])
            self.assertTrue(all(row['state'] == 'provisionable' for row in parsed['capacity']['lanes']))
            self.assertTrue(parsed['bots']['stale'])
            self.assertEqual(parsed['bots']['states'][0]['state'], 'idle')
            self.assertEqual(parsed['capacity']['sampled_at'], STAMP)
            value = json.loads(path.read_text()); value['owner'] = 'other'
            path.write_text(json.dumps(value))
            self.assertFalse(read_telemetry(selected, NOW)['available'])

    def view(self, host):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'telemetry.json'
            refresh(config(), path, fetch=lambda *_: {'host': host, 'quota': {'status': 'ok', 'rate_limits': quota()}}, now=STAMP)
            return collector_view(json.loads(path.read_text()), SimpleNamespace(owner='example-ci'), NOW)

    def test_lanes_bot_state_and_usage_come_from_the_snapshot(self):
        slot = {'kind': 'qae', 'index': 1, 'state': 'allocated', 'unit': {'active_state': 'active', 'sub_state': 'running'},
                'target_repository': 'example-ci/widgets', 'set_id': 2, 'runner_id': 42, 'runner_name': 'runner-qae-1',
                'allocated_at': STAMP}
        unknown = {'kind': 'ci', 'index': 2, 'state': 'unknown', 'unit': {'active_state': 'active', 'sub_state': 'running'},
                   'reason': 'job_unreadable'}
        view = self.view(remote_host([slot, unknown]))
        capacity = view['capacity']
        self.assertEqual((capacity['available'], capacity['sampled_at'], capacity['stale'], capacity['listener_state']),
                         (True, STAMP, False, 'up'))
        self.assertEqual(capacity['host'], {'cpu_percent': 25.0, 'memory_used_bytes': 400, 'memory_total_bytes': 1000,
                                            'workspace_disk_free_bytes': 1500, 'workspace_disk_total_bytes': 2000})
        self.assertEqual((capacity['limits']['slots'], capacity['limits']['qae_concurrency'], capacity['limits']['wait_slots']),
                         (8, 2, 0))
        self.assertEqual(capacity['lanes'][:3], [
            {'id': 'qae-1', 'state': 'allocated', 'registered': None, 'labels': ['qae'], 'runner_id': 42, 'allocated_at': STAMP,
             'job': {'repository': 'example-ci/widgets', 'name': 'Runner allocated; job match pending', 'url': None}},
            {'id': 'ci-2', 'state': 'unknown', 'registered': None, 'labels': ['ci']},
            {'id': 'on-demand-1', 'state': 'provisionable', 'registered': False, 'labels': []}])
        self.assertEqual(len(capacity['lanes']), 8)
        self.assertEqual(view['bots']['states'], [{'owner': 'example-ci', 'bot': 'explorer', 'state': 'unknown',
                                                   'detail': "Owner's QAE runner listener"}])
        usage = view['usage']
        self.assertEqual((usage['available'], usage['sampled_at'], usage['stale'], usage['samples']), (True, STAMP, False, []))
        self.assertTrue(usage['completeness'].startswith('Subscription percentage is account-wide'))
        self.assertEqual(usage['accounts'][0]['quota_windows'][0]['resets_at'], '2026-10-07T14:31:01Z')
        self.assertEqual(self.view(remote_host())['bots']['states'][0]['state'], 'idle')
        down = remote_host()
        down['listener']['state'] = 'down'
        view = self.view(down)
        self.assertEqual(view['bots']['states'][0]['state'], 'down')
        self.assertTrue(all(lane['state'] == 'unknown' for lane in view['capacity']['lanes']))

    def test_runner_jobs_skip_unavailable_repositories_and_missing_lists(self):
        lane = {'state': 'allocated', 'runner_id': 42, 'job': {'repository': 'example/repo', 'name': 'Allocated', 'url': None}}
        telemetry = {'capacity': {'available': True, 'stale': False, 'lanes': [lane]}}
        job = {'runner_id': 42, 'status': 'in_progress', 'name': 'Build'}
        join_runner_jobs(telemetry, {'repositories': [
            {'repository': 'example/repo', 'unavailable': True, 'pulls': [{'runs': [{'jobs': [job]}]}]}]})
        join_runner_jobs(telemetry, {})
        self.assertEqual(lane['state'], 'allocated')
        join_runner_jobs(telemetry, {'repositories': [
            {'repository': 'example/repo', 'unavailable': True}, {'repository': 'example/repo'},
            {'repository': 'example/repo', 'pulls': [{}, {'runs': [{}, {'jobs': [job]}]}]}]})
        self.assertEqual(lane['state'], 'busy')
        self.assertEqual(lane['job'], {'repository': 'example/repo', 'name': 'Build', 'url': None})

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

    def test_a_host_job_shows_without_a_pull_request_and_a_missing_record_falls_back(self):
        def slot(kind, index, runner_id, job=None):
            row = {'kind': kind, 'index': index, 'state': 'allocated',
                   'unit': {'active_state': 'active', 'sub_state': 'running'},
                   'target_repository': None, 'set_id': 2, 'runner_id': runner_id,
                   'runner_name': 'runner-%s-%s' % (kind, index), 'allocated_at': STAMP}
            if job:
                row.update(job)
            return row
        host = remote_host([
            slot('ci', 1, 42, {'job_name': 'Default branch', 'job_repository': 'example-ci/widgets',
                               'job_url': 'https://github.com/example-ci/widgets/actions/runs/9/job/8'}),
            slot('ci', 2, 43),
            slot('wait', 1, 70, {'job_name': 'Wait for preview', 'job_repository': 'example-ci/widgets'}),
        ])
        host['slots']['wait_limit'] = 8
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'telemetry.json'
            refresh(config(), path, fetch=lambda *_: {'host': host, 'quota': {'status': 'ok', 'rate_limits': quota()}}, now=STAMP)
            telemetry = read_telemetry(Config('example-ci', ('example-ci/widgets',), {}, path), NOW)
        lanes = {lane['id']: lane for lane in telemetry['capacity']['lanes']}
        self.assertEqual(telemetry['capacity']['limits']['wait_slots'], 8)
        self.assertEqual(lanes['ci-1']['state'], 'busy')
        self.assertEqual(lanes['ci-1']['job']['name'], 'Default branch')
        self.assertEqual(lanes['ci-1']['job']['repository'], 'example-ci/widgets')
        self.assertIsNone(lanes['ci-2'].get('job'))
        self.assertEqual(lanes['wait-1']['job']['name'], 'Wait for preview')
        self.assertEqual(sum(lane['state'] == 'provisionable' for lane in telemetry['capacity']['lanes']), 6)
        github_job = {'runner_id': 43, 'status': 'in_progress', 'name': 'Older run',
                      'html_url': 'https://github.com/example-ci/widgets/actions/runs/3/job/4'}
        join_runner_jobs(telemetry, {'repositories': [{'repository': 'example-ci/widgets', 'pulls': []}]})
        self.assertEqual(lanes['ci-1']['job']['name'], 'Default branch')
        self.assertEqual(lanes['ci-2']['state'], 'allocated')
        join_runner_jobs(telemetry, {'repositories': [{'repository': 'example-ci/widgets', 'pulls': [{'runs': [{'jobs': [github_job]}]}]}]})
        self.assertEqual(lanes['ci-1']['job']['name'], 'Default branch')
        self.assertEqual(lanes['ci-2']['state'], 'busy')
        self.assertEqual(lanes['ci-2']['job']['name'], 'Older run')


class CollectorPackage(unittest.TestCase):
    """collector.py through its package import: slot projection, the SSH read loop and quota kept between attempts."""

    def test_each_slot_is_checked_against_its_own_pool_and_runner_fields(self):
        base = {"kind": "ci", "index": 1, "state": "allocated",
                "unit": {"active_state": "active", "sub_state": "running"},
                "target_repository": "example-ci/repo", "set_id": 1, "runner_id": 1,
                "runner_name": "runner-ci-1", "allocated_at": "2026-09-30T12:00:00Z"}
        def project(*rows, wait_limit=1):
            # An Ellipsis value leaves that field out of the slot.
            host = remote_host([{key: value for key, value in dict(base, **row).items() if value is not ...}
                                for row in rows])
            host["slots"]["wait_limit"] = wait_limit
            return collector.project_host(host, config())["slots"]["occupied"]
        records = project({}, {"kind": "qae", "index": 8, "set_id": 5, "runner_id": 7}, {"kind": "wait"})
        self.assertEqual([(row["kind"], row["index"], row["set_id"], row["runner_id"]) for row in records],
                         [("ci", 1, 1, 1), ("qae", 8, 5, 7), ("wait", 1, 1, 1)])
        self.assertEqual(records[0]["target_repository"], "example-ci/repo")
        for reason in ("active-unit-without-job", "active_unit_without_job", "job_unreadable", "unit_state_unavailable"):
            self.assertEqual(project({"state": "unknown", "reason": reason})[0]["reason"], reason)
        refused = {"empty wait pool": ([{"kind": "wait"}], 0), "past the wait pool": ([{"kind": "wait", "index": 2}], 1),
                   "past the shared pool": ([{"index": 9}], 1), "same slot twice": ([{}, {"runner_id": 2}], 1),
                   "foreign target": ([{"target_repository": "other/repo"}], 1),
                   "bad allocation time": ([{"allocated_at": "yesterday"}], 1), "zero set": ([{"set_id": 0}], 1),
                   "no runner name": ([{"runner_name": ...}], 1), "unknown kind": ([{"kind": "build"}], 1),
                   "unknown state": ([{"state": "free"}], 1)}
        for name, (rows, wait_limit) in refused.items():
            with self.subTest(name=name), self.assertRaises(ValueError):
                project(*rows, wait_limit=wait_limit)

    def test_run_ssh_returns_stdout_and_refuses_failure_overflow_and_timeout(self):
        script = [sys.executable, "-c"]
        echo = "import sys; sys.stderr.write('noise'); sys.stdout.write(sys.stdin.read())"
        self.assertEqual(collector._run_ssh(script + [echo], b"sampler source"), b"sampler source")
        for command, timeout, code in (("raise SystemExit(3)", 5, "ssh_failed"),
                                       ("import time; time.sleep(30)", 0.3, "ssh_timeout")):
            with self.subTest(code=code), self.assertRaisesRegex(collector.CollectorError, code):
                collector._run_ssh(script + [command], b"", timeout=timeout)
        with mock.patch.object(collector, "MAX_REMOTE_OUTPUT", 8), \
                self.assertRaisesRegex(collector.CollectorError, "remote_output_invalid"):
            collector._run_ssh(script + ["print('x' * 64)"], b"")

    def test_quota_between_attempts_keeps_the_last_result_and_its_error(self):
        def accounts(output, *answers):
            for now, answer in answers:
                def fetch(_config, collect_quota, answer=answer):
                    if isinstance(answer, Exception):
                        raise answer
                    return {"host": remote_host(), "quota": answer if collect_quota else None}
                collector.refresh(config(), output, fetch, now)
            return json.loads(output.read_text())["accounts"][0]
        with tempfile.TemporaryDirectory() as directory:
            account = accounts(Path(directory) / "ok.json",
                               ("2026-09-30T12:00:00Z", {"status": "ok", "rate_limits": quota()}),
                               ("2026-09-30T12:01:00Z", {"status": "ok", "rate_limits": []}))
            self.assertEqual((account["available"], account["error"], account["rate_limits"]), (True, None, quota()))
            self.assertEqual((account["observed_at"], account["last_attempt_at"]),
                             ("2026-09-30T12:00:00Z", "2026-09-30T12:00:00Z"))
            for answer, code in ((collector.CollectorError("ssh_timeout"), "ssh_timeout"),
                                 ({"status": "listener_budget_invalid"}, "listener_budget_invalid"),
                                 ({"status": "SECRET_MARKER"}, "quota_unavailable")):
                with self.subTest(code=code):
                    account = accounts(Path(directory) / ("%s.json" % code),
                                       ("2026-09-30T12:00:00Z", {"status": "ok", "rate_limits": quota()}),
                                       ("2026-09-30T12:05:00Z", answer),
                                       ("2026-09-30T12:06:00Z", {"status": "ok", "rate_limits": quota()}))
                    self.assertEqual((account["available"], account["error"], account["observed_at"],
                                      account["last_attempt_at"], account["rate_limits"]),
                                     (False, code, "2026-09-30T12:00:00Z", "2026-09-30T12:05:00Z", quota()))
