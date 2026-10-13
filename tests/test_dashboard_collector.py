import importlib.util
import json
from pathlib import Path
import stat
import sys
import tempfile
import unittest
from unittest import mock

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
def local_config():
    return COLLECTOR.CollectorConfig(
        owner="example-ci", host_label="Shared CI host", ssh_argv=(), destination="",
        listener_config_path="/etc/example-ci/listener.json", lane_name="example-ci",
        workspace_path="/srv/example-ci/work", codex_home="/var/lib/example-ci/codex")
class LocalModeTests(unittest.TestCase):
    def test_a_host_without_ssh_fields_is_local_and_half_an_ssh_target_is_refused(self):
        document = {"version": 1, "owner": "example-ci", "host": {
            "label": "Shared CI host", "listener_config_path": "/etc/example-ci/listener.json",
            "lane_name": "example-ci", "workspace_path": "/srv/example-ci/work",
            "codex_home": "/var/lib/example-ci/codex"}}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            path.write_text(json.dumps(document))
            self.assertEqual(COLLECTOR.load_config(path), local_config())
            document["host"]["destination"] = "example-ci-host"
            path.write_text(json.dumps(document))
            with self.assertRaisesRegex(ValueError, "invalid fields"):
                COLLECTOR.load_config(path)
            for ssh, destination, error in ((["ssh", "-T"], "example-ci-host", None),
                                            (["sh", "-c"], "example-ci-host", "must invoke ssh"),
                                            (["ssh"], "-oProxyCommand=x", "destination is invalid")):
                document["host"].update(ssh_argv=ssh, destination=destination)
                path.write_text(json.dumps(document))
                with self.subTest(error=error):
                    if error is None:
                        self.assertEqual(COLLECTOR.load_config(path), config())
                        continue
                    with self.assertRaisesRegex(ValueError, error):
                        COLLECTOR.load_config(path)
    def test_local_mode_samples_in_process_without_ssh(self):
        payloads = []
        sampler = {"respond": lambda payload: payloads.append(payload) or {
            "ok": True, "host": remote_host(), "quota": {"status": "ok", "rate_limits": quota()}}}
        with tempfile.TemporaryDirectory() as directory, \
                mock.patch.object(COLLECTOR.runpy, "run_path", return_value=sampler) as load, \
                mock.patch.object(COLLECTOR.subprocess, "Popen") as process:
            output = Path(directory) / "latest.json"
            self.assertTrue(COLLECTOR.refresh(local_config(), output, now="2026-09-30T12:00:00Z"))
            saved = json.loads(output.read_text())
        process.assert_not_called()
        load.assert_called_once_with(str(ROOT / "dashboard" / "remote_sampler.py"))
        self.assertEqual(payloads, [{"owner": "example-ci", "listener_config_path": "/etc/example-ci/listener.json",
                                     "lane_name": "example-ci", "workspace_path": "/srv/example-ci/work",
                                     "collect_quota": True, "codex_home": "/var/lib/example-ci/codex"}])
        self.assertTrue(saved["hosts"][0]["available"])
        self.assertEqual(saved["accounts"][0]["rate_limits"], quota())
    def test_local_failures_keep_the_remote_error_contract(self):
        for answer, code in (({"ok": False, "error": "listener_identity_mismatch"}, "listener_identity_mismatch"),
                             ({"ok": False, "error": "cpu_unavailable"}, "remote_failed")):
            with self.subTest(code=code), mock.patch.object(COLLECTOR.runpy, "run_path",
                                                            return_value={"respond": lambda _payload: answer}):
                with self.assertRaises(COLLECTOR.CollectorError) as caught:
                    COLLECTOR.collect_local(local_config(), False)
                self.assertEqual(str(caught.exception), code)
        # The real sampler, run in this process: the configured listener does not exist here.
        with self.assertRaisesRegex(COLLECTOR.CollectorError, "listener_config_unavailable"):
            COLLECTOR.collect_local(local_config(), False)
    def test_the_sampler_answers_errors_as_codes_never_raw_text(self):
        self.assertEqual(REMOTE.respond([]), {"ok": False, "error": "invalid_arguments"})
        with mock.patch.object(REMOTE, "collect", side_effect=REMOTE.SampleError("foreign_job_target")):
            self.assertEqual(REMOTE.respond({}), {"ok": False, "error": "foreign_job_target"})
        with mock.patch.object(REMOTE, "collect", side_effect=KeyError("SECRET_MARKER")):
            self.assertEqual(REMOTE.respond({}), {"ok": False, "error": "collection_failed"})
    def test_an_ssh_host_still_goes_over_ssh(self):
        with tempfile.TemporaryDirectory() as directory, \
                mock.patch.object(COLLECTOR, "collect_remote", return_value={"host": remote_host()}) as remote, \
                mock.patch.object(COLLECTOR.runpy, "run_path") as local:
            COLLECTOR.refresh(config(), Path(directory) / "latest.json", now="2026-09-30T12:00:00Z")
        remote.assert_called_once_with(config(), True)
        local.assert_not_called()
if __name__ == "__main__":
    unittest.main()
