"""The remote sampler's own checks. Imported by package name, so the mutation gate's mutants reach them."""
import io
import json
from pathlib import Path
import signal
import sys
import tempfile
import time
import types
import unittest
from unittest import mock
from urllib.error import HTTPError
from urllib.request import HTTPSHandler, build_opener
from urllib.response import addinfourl

from dashboard import collector as COLLECTOR
from dashboard import remote_sampler as REMOTE
from test_dashboard_collector import auth_document, config, listener, quota, raw_quota, remote_host


class RemoteSamplerTests(unittest.TestCase):

    def test_listener_owner_is_casefolded_and_foreign_config_is_denied(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "listener.json"
            path.write_text(json.dumps(listener()))
            self.assertEqual(REMOTE.listener_contract(path, "example-ci", "example-ci"), (8, 2, 0))
            path.write_text(json.dumps(listener("different-owner")))
            with self.assertRaisesRegex(REMOTE.SampleError, "listener_identity_mismatch"):
                REMOTE.listener_contract(path, "example-ci", "example-ci")

    def test_foreign_job_target_is_denied(self):
        with tempfile.TemporaryDirectory() as directory:
            job = Path(directory) / "example-ci" / "ci" / "1" / "job"
            job.parent.mkdir(parents=True)
            job.write_text("different-owner/repo ci 4 9 runner-ci-1\n")
            states = {"example-ci-ci@1.service": {"ActiveState": "active", "SubState": "running"}}
            with self.assertRaisesRegex(REMOTE.SampleError, "foreign_job_target"):
                REMOTE.scan_slots("example-ci", "example-ci", 1, states, directory, ("ci",))

    def test_an_organization_scope_job_names_only_the_owner_and_projects_without_a_repository(self):
        with tempfile.TemporaryDirectory() as directory:
            job = Path(directory) / "example-ci" / "ci" / "1" / "job"
            job.parent.mkdir(parents=True)
            job.write_text("EXAMPLE-CI ci 2 5386 runner-ci-1\n")
            states = {"example-ci-ci@1.service": {"ActiveState": "active", "SubState": "running"}}
            occupied = REMOTE.scan_slots("example-ci", "example-ci", 1, states, directory, ("ci",))
            self.assertEqual(occupied[0]["state"], "allocated")
            self.assertIsNone(occupied[0]["target_repository"])
            self.assertEqual(occupied[0]["runner_id"], 5386)
            projected = COLLECTOR.project_host(remote_host(occupied), config())
            self.assertIsNone(projected["slots"]["occupied"][0]["target_repository"])
            job.write_text("different-owner ci 2 5386 runner-ci-1\n")
            with self.assertRaisesRegex(REMOTE.SampleError, "foreign_job_target"):
                REMOTE.scan_slots("example-ci", "example-ci", 1, states, directory, ("ci",))
        for target in ("example-ci", "different-owner/repo", 7):
            with self.subTest(target), self.assertRaises(ValueError):
                COLLECTOR.project_host(remote_host([dict(occupied[0], target_repository=target)]), config())

    def test_every_unknown_reason_the_sampler_reports_survives_projection(self):
        with tempfile.TemporaryDirectory() as directory:
            unreadable = Path(directory) / "example-ci" / "ci" / "1" / "job"
            unreadable.parent.mkdir(parents=True)
            unreadable.write_text("not a job\n")
            states = {"example-ci-ci@1.service": {"ActiveState": "active", "SubState": "running"},
                      "example-ci-ci@2.service": {"ActiveState": "active", "SubState": "running"}}
            occupied = REMOTE.scan_slots("example-ci", "example-ci", 3, states, directory, ("ci",))
        self.assertEqual([row["reason"] for row in occupied],
                         ["job_unreadable", "active_unit_without_job", "unit_state_unavailable"])
        projected = COLLECTOR.project_host(remote_host(occupied), config())
        self.assertEqual([row["reason"] for row in projected["slots"]["occupied"]],
                         ["job_unreadable", "active_unit_without_job", "unit_state_unavailable"])

    def test_sixteen_candidate_units_still_use_one_combined_budget(self):
        with tempfile.TemporaryDirectory() as directory:
            states = {}
            for unit in REMOTE.slot_units("example-ci", 8):
                states[unit] = {"ActiveState": "inactive", "SubState": "dead"}
            for kind, runner_id in (("ci", 11), ("qae", 12)):
                job = Path(directory) / "example-ci" / kind / "1" / "job"
                job.parent.mkdir(parents=True)
                job.write_text("EXAMPLE-CI/repo %s 4 %d runner-%s-1\n" % (kind, runner_id, kind))
            occupied = REMOTE.scan_slots("example-ci", "example-ci", 8, states, directory)
            projected = COLLECTOR.project_host(remote_host(occupied), config())
            self.assertEqual(len(REMOTE.slot_units("example-ci", 8)), 16)
            self.assertEqual(projected["slots"]["limit"], 8)
            self.assertEqual(projected["slots"]["occupied_count"], 2)
            self.assertEqual(projected["slots"]["remaining_on_demand"], 6)
            self.assertEqual({item["index"] for item in occupied}, {1})

    def test_a_wait_slot_is_its_own_pool_and_a_host_job_is_kept(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "listener.json"
            body = listener()
            body["kinds"] = {"wait": {"slots": 8, "set_name": "example-wait"}}
            path.write_text(json.dumps(body))
            self.assertEqual(REMOTE.listener_contract(path, "example-ci", "example-ci"), (8, 2, 8))
            body["kinds"]["wait"]["slots"] = 0
            path.write_text(json.dumps(body))
            with self.assertRaisesRegex(REMOTE.SampleError, "listener_budget_invalid"):
                REMOTE.listener_contract(path, "example-ci", "example-ci")
            states = {}
            for unit in REMOTE.slot_units("example-ci", 8, wait_slots=8):
                states[unit] = {"ActiveState": "inactive", "SubState": "dead"}
            states["example-ci-wait@3.service"] = {"ActiveState": "active", "SubState": "running"}
            states["example-ci-ci@1.service"] = {"ActiveState": "active", "SubState": "running"}
            wait = Path(directory) / "example-ci" / "wait" / "3"
            wait.mkdir(parents=True)
            wait.joinpath("job").write_text("example-ci wait 9 70 runner-wait-3\n")
            wait.joinpath("assignment").write_text(json.dumps({
                "repository": "example-ci/widgets", "name": "Wait for preview",
                "run_id": 15, "job_id": "16"}) + "\n")
            ci = Path(directory) / "example-ci" / "ci" / "1"
            ci.mkdir(parents=True)
            ci.joinpath("job").write_text("example-ci ci 4 11 runner-ci-1\n")
            occupied = REMOTE.scan_slots("example-ci", "example-ci", 8, states, directory, wait_slots=8)
            host = remote_host(occupied)
            host["slots"]["wait_limit"] = 8
            projected = COLLECTOR.project_host(host, config())
            self.assertEqual([(row["kind"], row["index"]) for row in occupied], [("ci", 1), ("wait", 3)])
            self.assertEqual(projected["slots"]["wait_limit"], 8)
            self.assertEqual(projected["slots"]["occupied_count"], 1)
            self.assertEqual(projected["slots"]["remaining_on_demand"], 7)
            wait_row = projected["slots"]["occupied"][1]
            self.assertEqual(wait_row["job_name"], "Wait for preview")
            self.assertEqual(wait_row["job_repository"], "example-ci/widgets")
            self.assertEqual(wait_row["job_url"], "https://github.com/example-ci/widgets/actions/runs/15/job/16")

    def test_a_foreign_or_oversized_assignment_is_refused(self):
        def occupied(directory, assignment):
            job = Path(directory) / "example-ci" / "ci" / "1"
            job.mkdir(parents=True)
            job.joinpath("job").write_text("example-ci/repo ci 4 9 runner-ci-1\n")
            job.joinpath("assignment").write_text(assignment)
            states = {"example-ci-ci@1.service": {"ActiveState": "active", "SubState": "running"}}
            return REMOTE.scan_slots("example-ci", "example-ci", 1, states, directory, ("ci",))
        with tempfile.TemporaryDirectory() as directory:
            rows = occupied(directory, json.dumps({"repository": "other/repo", "name": "Build"}))
            self.assertEqual(rows[0]["state"], "allocated")
            self.assertNotIn("job_name", rows[0])
        with tempfile.TemporaryDirectory() as directory:
            rows = occupied(directory, json.dumps({"repository": "example-ci/repo", "name": "B" * (REMOTE.MAX_JOB_NAME + 1)}))
            self.assertNotIn("job_name", rows[0])
        with tempfile.TemporaryDirectory() as directory:
            rows = occupied(directory, "x" * (REMOTE.MAX_JOB + 1))
            self.assertNotIn("job_name", rows[0])
        base = {"kind": "ci", "index": 1, "state": "allocated",
                "unit": {"active_state": "active", "sub_state": "running"},
                "target_repository": "example-ci/repo", "set_id": 4, "runner_id": 9,
                "runner_name": "runner-ci-1", "allocated_at": "2026-09-30T12:00:00Z"}
        for extra in ({"job_name": "Build", "job_repository": "other/repo"},
                      {"job_name": "B" * (COLLECTOR.MAX_JOB_NAME + 1), "job_repository": "example-ci/repo"}):
            projected = COLLECTOR.project_host(remote_host([dict(base, **extra)]), config())
            self.assertNotIn("job_name", projected["slots"]["occupied"][0])
            self.assertNotIn("other/repo", json.dumps(projected))

    def test_active_unit_without_job_is_unknown_not_free(self):
        states = {"example-ci-ci@1.service": {"ActiveState": "active", "SubState": "running"}}
        with tempfile.TemporaryDirectory() as directory:
            records = REMOTE.scan_slots("example-ci", "example-ci", 1, states, directory, ("ci",))
        self.assertEqual(records[0]["state"], "unknown")
        self.assertEqual(records[0]["reason"], "active_unit_without_job")

    def test_cpu_excludes_guest_counters_and_collect_uses_workspace(self):
        before = "cpu 100 0 50 800 20 10 20 0 900 80\n"
        after = "cpu 120 0 60 850 20 10 20 0 9999 9999\n"
        self.assertEqual(REMOTE.cpu_busy_percent(before, after), 37.5)
        with tempfile.TemporaryDirectory() as directory:
            listener_path = Path(directory) / "listener.json"
            listener_path.write_text(json.dumps(listener(slots=1, qae=1)))
            samples = iter([before.encode(), after.encode()])
            original_read = REMOTE.read_limited
            def read(path, limit=REMOTE.MAX_FILE):
                if str(path) == "/proc/stat":
                    return next(samples)
                if str(path) == "/proc/meminfo":
                    return b"MemTotal: 1000 kB\nMemAvailable: 500 kB\n"
                return original_read(path, limit)
            seen = []
            def statvfs(path):
                seen.append(path)
                return types.SimpleNamespace(f_blocks=100, f_bavail=25, f_frsize=4096)
            def runner(command, timeout, limit):
                if "Id" in command:
                    return (b"Id=example-ci-ci@1.service\nActiveState=inactive\nSubState=dead\n\n"
                            b"Id=example-ci-qae@1.service\nActiveState=inactive\nSubState=dead\n")
                if command[-1].endswith("listener.service"):
                    return b"ActiveState=failed\nSubState=failed\nExecMainStartTimestamp=\n"
                return (b"MemoryCurrent=1\nMemoryMax=10\nMemoryHigh=8\nCPUUsageNSec=2\n"
                        b"CPUQuotaPerSecUSec=4s\n")
            payload = {"owner": "example-ci", "lane_name": "example-ci",
                       "listener_config_path": str(listener_path),
                       "workspace_path": "/srv/example-ci/work", "collect_quota": False}
            saved = REMOTE.read_limited
            REMOTE.read_limited = read
            try:
                result = REMOTE.collect(payload, run_root=directory, runner=runner,
                                        sleeper=lambda _: None, statvfs=statvfs)
            finally:
                REMOTE.read_limited = saved
            self.assertEqual(result["host"]["listener"]["state"], "down")
            self.assertEqual(seen, ["/srv/example-ci/work"])
            self.assertEqual(result["host"]["workspace"],
                             {"total_bytes": 409600, "free_bytes": 102400})
            self.assertEqual((result["host"]["queue"], result["host"]["jobs"]), (None, None))

    def test_the_listeners_queue_and_newest_hundred_jobs_are_read_and_anything_unsafe_is_absent(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            queue = {"version": 1, "sets": [{"kind": "ci", "assigned": 2, "running": 1, "min_runners": 0}]}
            (base / "queue.json").write_text(json.dumps(queue))
            jobs = {"version": 1, "jobs": [{"runner_name": "r-%d" % index} for index in range(150)]}
            (base / "jobs.json").write_text(json.dumps(jobs))
            self.assertEqual(REMOTE.read_lane_record(base / "queue.json"), queue)
            kept = REMOTE.read_lane_record(base / "jobs.json")
            self.assertEqual((kept["version"], len(kept["jobs"]), kept["jobs"][0]), (1, 100, {"runner_name": "r-50"}))
            (base / "link.json").symlink_to(base / "queue.json")
            (base / "list.json").write_text("[1]")
            (base / "bad.json").write_text("{")
            (base / "big.json").write_text(" " * (REMOTE.MAX_FILE * 16 + 1))
            for name in ("link.json", "list.json", "bad.json", "big.json", "missing.json"):
                with self.subTest(name):
                    self.assertIsNone(REMOTE.read_lane_record(base / name))

    def test_missing_and_malformed_quota_never_become_zero_usage(self):
        malformed = raw_quota()
        malformed["rate_limit"]["primary_window"]["used_percent"] = "bad"
        for payload in ({}, {"rate_limit": None}, {"rate_limit": {
                "primary_window": None, "secondary_window": None}}, malformed):
            with self.subTest(payload=payload):
                with self.assertRaisesRegex(REMOTE.QuotaError, "quota_malformed"):
                    REMOTE.normalize_rate_limits(payload)

    def test_every_malformed_window_field_is_refused(self):
        window = raw_quota()["rate_limit"]["primary_window"]
        for change in ({"used_percent": 101}, {"used_percent": -1}, {"used_percent": True},
                       {"used_percent": float("nan")}, {"limit_window_seconds": 0},
                       {"limit_window_seconds": True}, {"limit_window_seconds": 60.0},
                       {"reset_at": 0}, {"reset_at": True}, {"reset_at": "1791383461"}):
            with self.subTest(change=change):
                payload = raw_quota()
                payload["rate_limit"]["primary_window"] = dict(window, **change)
                with self.assertRaisesRegex(REMOTE.QuotaError, "quota_malformed"):
                    REMOTE.normalize_rate_limits(payload)
        for rate_limit in ("bad", {"primary_window": "bad"}):
            with self.subTest(rate_limit=rate_limit):
                with self.assertRaisesRegex(REMOTE.QuotaError, "quota_malformed"):
                    REMOTE.normalize_rate_limits({"rate_limit": rate_limit})

    def test_window_minutes_round_up_and_both_windows_are_kept(self):
        payload = raw_quota()
        payload["rate_limit"]["secondary_window"] = {
            "used_percent": 100, "limit_window_seconds": 61, "reset_at": 1}
        self.assertEqual(REMOTE.normalize_rate_limits(payload)[0]["windows"], quota()[0]["windows"] + [
            {"name": "secondary", "duration_minutes": 2, "used_percent": 100, "resets_at": 1}])

    def test_run_bounded_returns_output_and_refuses_failure_overflow_and_timeout(self):
        self.assertEqual(REMOTE.run_bounded([sys.executable, "-c", "print('ok')"]), b"ok\n")
        with self.assertRaisesRegex(OSError, "command failed"):
            REMOTE.run_bounded([sys.executable, "-c", "raise SystemExit(3)"])
        with self.assertRaisesRegex(OSError, "command output is too large"):
            REMOTE.run_bounded([sys.executable, "-c", "print('x' * 64)"], limit=8)
        started = time.monotonic()
        with self.assertRaises(TimeoutError):
            REMOTE.run_bounded([sys.executable, "-c", "import time; time.sleep(30)"], timeout=0.3)
        self.assertLess(time.monotonic() - started, 5)

    def run_recorded(self, script, **kwargs):
        """run_bounded on a Python script; returns the processes it started, or what it raised."""
        started, real = [], REMOTE.subprocess.Popen
        def popen(*args, **options):
            started.append(real(*args, **options))
            return started[-1]
        with mock.patch.object(REMOTE.subprocess, "Popen", popen):
            try:
                REMOTE.run_bounded([sys.executable, "-c", script], **kwargs)
            except (OSError, TimeoutError) as error:
                return started, error
        return started, None

    def test_run_bounded_stops_a_process_still_writing_past_the_limit(self):
        started, error = self.run_recorded(
            "import sys\nwhile True: sys.stdout.write('x' * 4096); sys.stdout.flush()", limit=1000)
        self.assertEqual(str(error), "command output is too large")
        self.assertEqual(started[0].returncode, -signal.SIGTERM)

    def test_run_bounded_kills_a_process_that_ignores_terminate(self):
        started, error = self.run_recorded(
            "import signal, time\nsignal.signal(signal.SIGTERM, signal.SIG_IGN)\ntime.sleep(30)", timeout=1.0)
        self.assertIsInstance(error, TimeoutError)
        self.assertEqual(started[0].returncode, -signal.SIGKILL)

    def test_null_bucket_preserves_other_populated_windows(self):
        payload = raw_quota()
        payload["additional_rate_limits"] = [{"metered_feature": "premium",
            "rate_limit": {"primary_window": None, "secondary_window": None}}]
        self.assertEqual(REMOTE.normalize_rate_limits(payload), quota())
        payload["additional_rate_limits"][0]["rate_limit"] = payload["rate_limit"]
        payload["rate_limit"] = None
        result = REMOTE.normalize_rate_limits(payload)
        self.assertEqual(result[0]["limit_id"], "premium")
        self.assertEqual(result[0]["windows"], quota()[0]["windows"])

    def test_quota_get_is_fixed_bounded_and_never_writes_or_starts_codex(self):
        with tempfile.TemporaryDirectory() as directory:
            auth_path = Path(directory) / "auth.json"
            original = json.dumps(auth_document()).encode()
            auth_path.write_bytes(original)
            response = mock.MagicMock()
            response.__enter__.return_value = response
            payload = raw_quota()
            payload["private_metadata"] = "SECRET_UNRELATED"
            response.read.return_value = json.dumps(payload).encode()
            opener = mock.Mock()
            opener.open.return_value = response
            with mock.patch.object(REMOTE.subprocess, "Popen") as process:
                result = REMOTE.quota_read(directory, opener)
            process.assert_not_called()
            self.assertEqual(result, quota())
            self.assertNotIn("SECRET", json.dumps(result))
            request = opener.open.call_args.args[0]
            self.assertEqual(request.full_url, "https://chatgpt.com/backend-api/wham/usage")
            self.assertEqual(request.get_method(), "GET")
            self.assertIsNone(request.data)
            self.assertEqual(request.get_header("Authorization"), "Bearer SECRET_ACCESS_TOKEN")
            self.assertEqual(request.get_header("Chatgpt-account-id"), "SECRET_ACCOUNT")
            opener.open.assert_called_once_with(request, timeout=10)
            response.read.assert_called_once_with(REMOTE.MAX_QUOTA_BODY + 1)
            self.assertEqual(auth_path.read_bytes(), original)
            self.assertEqual(list(Path(directory).iterdir()), [auth_path])

    def test_missing_partial_oversized_and_symlink_auth_fail_before_http(self):
        with tempfile.TemporaryDirectory() as directory:
            auth_path = Path(directory) / "auth.json"
            opener = mock.Mock()
            for content in (None, b'{"auth_mode":', b'{}', b'[]',
                            json.dumps({"auth_mode": "chatgpt", "tokens": {}}).encode(),
                            b" " * (REMOTE.MAX_FILE + 1)):
                with self.subTest(content_type=type(content).__name__):
                    if content is not None:
                        auth_path.write_bytes(content)
                    with self.assertRaisesRegex(REMOTE.QuotaError, "quota_unavailable"):
                        REMOTE.quota_read(directory, opener)
            auth_path.unlink()
            source = Path(directory) / "source.json"
            source.write_text(json.dumps(auth_document()))
            auth_path.symlink_to(source)
            with self.assertRaisesRegex(REMOTE.QuotaError, "quota_unavailable"):
                REMOTE.quota_read(directory, opener)
            opener.open.assert_not_called()

    def test_http_failure_body_and_error_never_expose_credentials(self):
        with tempfile.TemporaryDirectory() as directory:
            (Path(directory) / "auth.json").write_text(json.dumps(auth_document()))
            for error in (HTTPError(REMOTE.QUOTA_URL, 401, "SECRET_SERVER_BODY", {}, None),
                          TimeoutError("SECRET_TIMEOUT")):
                opener = mock.Mock()
                opener.open.side_effect = error
                with self.assertRaises(REMOTE.QuotaError) as caught:
                    REMOTE.quota_read(directory, opener)
                self.assertEqual(str(caught.exception), "quota_unavailable")
                opener.open.assert_called_once()
            for body in (b"SECRET_BAD_JSON", b" " * (REMOTE.MAX_QUOTA_BODY + 1)):
                opener = mock.Mock()
                opener.open.return_value = io.BytesIO(body)
                with self.assertRaisesRegex(REMOTE.QuotaError, "quota_unavailable"):
                    REMOTE.quota_read(directory, opener)

    def test_redirect_is_refused_without_replaying_auth(self):
        requests = []
        class RedirectServer(HTTPSHandler):
            def https_open(self, request):
                requests.append(request)
                response = addinfourl(io.BytesIO(), {"location": "https://example.com/other"},
                                      request.full_url, 302)
                response.msg = "Found"
                return response
        opener = build_opener(RedirectServer(), REMOTE.NoQuotaRedirect())
        with tempfile.TemporaryDirectory() as directory:
            (Path(directory) / "auth.json").write_text(json.dumps(auth_document()))
            with self.assertRaisesRegex(REMOTE.QuotaError, "quota_unavailable"):
                REMOTE.quota_read(directory, opener)
            self.assertEqual(len(requests), 1)
            self.assertEqual(requests[0].full_url, REMOTE.QUOTA_URL)
            with mock.patch.object(REMOTE.urllib.request, "build_opener") as build:
                build.return_value.open.return_value = io.BytesIO(json.dumps(raw_quota()).encode())
                self.assertEqual(REMOTE.quota_read(directory), quota())
            self.assertIsInstance(build.call_args.args[0], REMOTE.NoQuotaRedirect)

    def test_read_only_quota_remains_available_while_qae_is_present(self):
        record = {"kind": "qae", "index": 1, "state": "unknown",
                  "unit": {"active_state": "active", "sub_state": "running"},
                  "reason": "active_unit_without_job"}
        quota_reader = mock.Mock(return_value=quota())
        reads = iter([b"cpu 1 0 0 9 0 0 0 0\n", b"cpu 2 0 0 10 0 0 0 0\n"])
        def read(path, _limit=REMOTE.MAX_FILE):
            if path == "/proc/stat":
                return next(reads)
            return b"MemTotal: 10 kB\nMemAvailable: 5 kB\n"
        disk = types.SimpleNamespace(f_blocks=10, f_bavail=5, f_frsize=1024)
        payload = {"owner": "example-ci", "lane_name": "example-ci",
                   "listener_config_path": "/etc/example-ci/listener.json",
                   "workspace_path": "/srv/example-ci/work", "collect_quota": True,
                   "codex_home": "/var/lib/example-ci/codex"}
        with mock.patch.object(REMOTE, "listener_contract", return_value=(1, 1, 0)), \
             mock.patch.object(REMOTE, "read_limited", side_effect=read), \
             mock.patch.object(REMOTE, "unit_states", return_value={}), \
             mock.patch.object(REMOTE, "scan_slots", return_value=[record]), \
             mock.patch.object(REMOTE, "systemd_properties", return_value=None):
            result = REMOTE.collect(payload, sleeper=lambda _: None,
                                    statvfs=lambda _: disk, quota_reader=quota_reader)
        self.assertEqual(result["quota"], {"status": "ok", "rate_limits": quota()})
        quota_reader.assert_called_once_with("/var/lib/example-ci/codex")


if __name__ == "__main__":
    unittest.main()
