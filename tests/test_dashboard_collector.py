import importlib.util
import io
import json
from pathlib import Path
import stat
import sys
import tempfile
import types
import unittest
from unittest import mock
from urllib.error import HTTPError
from urllib.request import HTTPSHandler, build_opener
from urllib.response import addinfourl

ROOT = Path(__file__).resolve().parents[1]


def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


COLLECTOR = load_module("dashboard_collector", ROOT / "dashboard" / "collector.py")
REMOTE = load_module("dashboard_remote_sampler", ROOT / "dashboard" / "remote_sampler.py")


def config():
    return COLLECTOR.CollectorConfig(
        owner="example-ci", host_label="Shared CI host", ssh_argv=("ssh", "-T"),
        destination="example-ci-host", listener_config_path="/etc/example-ci/listener.json",
        lane_name="example-ci", workspace_path="/srv/example-ci/work",
        codex_home="/var/lib/example-ci/codex")


def listener(owner="Example-CI", slots=8, qae=2):
    return {"name": "example-ci", "github_url": "https://github.com/" + owner,
            "budget": {"slots": slots, "qae_concurrency": qae},
            "app": {"client_id": "SECRET_MARKER", "installation_id": 7,
                    "key_file": "/etc/example-ci/app.pem"}}


def remote_host(occupied=None):
    return {
        "cpu": {"busy_percent": 25.0},
        "memory": {"total_bytes": 1000, "available_bytes": 600},
        "workspace": {"total_bytes": 2000, "free_bytes": 1500},
        "listener": {"state": "up", "active_state": "active", "sub_state": "running",
                     "started_at": "Tue 2026-09-29 12:00:00 UTC"},
        "lane_limits": {"memory_current_bytes": 10, "memory_max_bytes": 100,
                        "memory_high_bytes": 80, "cpu_usage_nsec": 123,
                        "cpu_quota_cores": 4.0},
        "slots": {"limit": 8, "qae_concurrency": 2, "occupied": occupied or []},
    }


def quota():
    return [{"limit_id": "codex", "windows": [
        {"name": "primary", "duration_minutes": 10080,
         "used_percent": 22, "resets_at": 1791383461}]}]


def raw_quota():
    return {"rate_limit": {"primary_window": {
        "used_percent": 22, "limit_window_seconds": 604800, "reset_at": 1791383461},
        "secondary_window": None}}


def auth_document():
    return {"auth_mode": "chatgpt", "tokens": {"access_token": "SECRET_ACCESS_TOKEN",
            "account_id": "SECRET_ACCOUNT", "refresh_token": "SECRET_REFRESH_TOKEN"}}


class RemoteSamplerTests(unittest.TestCase):
    def test_listener_owner_is_casefolded_and_foreign_config_is_denied(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "listener.json"
            path.write_text(json.dumps(listener()))
            self.assertEqual(REMOTE.listener_contract(path, "example-ci", "example-ci"), (8, 2))
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

    def test_missing_and_malformed_quota_never_become_zero_usage(self):
        malformed = raw_quota()
        malformed["rate_limit"]["primary_window"]["used_percent"] = "bad"
        for payload in ({}, {"rate_limit": None}, {"rate_limit": {
                "primary_window": None, "secondary_window": None}}, malformed):
            with self.subTest(payload=payload):
                with self.assertRaisesRegex(REMOTE.QuotaError, "quota_malformed"):
                    REMOTE.normalize_rate_limits(payload)

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
        with mock.patch.object(REMOTE, "listener_contract", return_value=(1, 1)), \
             mock.patch.object(REMOTE, "read_limited", side_effect=read), \
             mock.patch.object(REMOTE, "unit_states", return_value={}), \
             mock.patch.object(REMOTE, "scan_slots", return_value=[record]), \
             mock.patch.object(REMOTE, "systemd_properties", return_value=None):
            result = REMOTE.collect(payload, sleeper=lambda _: None,
                                    statvfs=lambda _: disk, quota_reader=quota_reader)
        self.assertEqual(result["quota"], {"status": "ok", "rate_limits": quota()})
        quota_reader.assert_called_once_with("/var/lib/example-ci/codex")



class LocalCollectorTests(unittest.TestCase):
    def test_atomic_mode_and_failed_refresh_keep_success_timestamp(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "state" / "latest.json"

            def success(_config, collect_quota):
                self.assertTrue(collect_quota)
                return {"host": remote_host(), "quota": {"status": "ok", "rate_limits": quota()}}

            self.assertTrue(COLLECTOR.refresh(config(), output, success, "2026-09-30T12:00:00Z"))
            self.assertEqual(stat.S_IMODE(output.stat().st_mode), 0o600)

            def failure(_config, _collect_quota):
                raise COLLECTOR.CollectorError("ssh_failed")

            self.assertFalse(COLLECTOR.refresh(config(), output, failure, "2026-09-30T12:01:00Z"))
            saved = json.loads(output.read_text())
            self.assertEqual(saved["observed_at"], "2026-09-30T12:01:00Z")
            self.assertEqual(saved["hosts"][0]["observed_at"], "2026-09-30T12:00:00Z")
            self.assertTrue(saved["hosts"][0]["stale"])
            self.assertEqual(len(saved["samples"]), 1)

    def test_allowlist_projection_drops_secrets_and_failed_quota_has_no_false_zero(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "latest.json"
            host = remote_host()
            host["credentials"] = {"private_key": "SECRET_MARKER"}

            def fetch(_config, _collect_quota):
                return {"host": host, "quota": {"status": "quota_unavailable",
                                                  "raw": "SECRET_MARKER"},
                        "listener_config": {"installation_id": "SECRET_MARKER"}}

            COLLECTOR.refresh(config(), output, fetch, "2026-09-30T12:00:00Z")
            text = output.read_text()
            saved = json.loads(text)
            self.assertNotIn("SECRET_MARKER", text)
            self.assertFalse(saved["accounts"][0]["available"])
            self.assertEqual(saved["accounts"][0]["rate_limits"], [])
            self.assertNotIn('"used_percent":0', text)

    def test_config_is_exact_and_uses_only_one_owner(self):
        document = {"version": 1, "owner": "example-ci", "host": {
            "label": "Shared CI host", "ssh_argv": ["ssh", "-T"],
            "destination": "example-ci-host",
            "listener_config_path": "/etc/example-ci/listener.json",
            "lane_name": "example-ci", "workspace_path": "/srv/example-ci/work",
            "codex_home": "/var/lib/example-ci/codex"}}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            path.write_text(json.dumps(document))
            self.assertEqual(COLLECTOR.load_config(path).owner, "example-ci")
            document["owners"] = ["other-owner"]
            path.write_text(json.dumps(document))
            with self.assertRaisesRegex(ValueError, "invalid fields"):
                COLLECTOR.load_config(path)


if __name__ == "__main__":
    unittest.main()
