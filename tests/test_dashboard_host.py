"""The Linux host helpers mint exactly the read token asked for, publish only telemetry the dashboard
accepts, and ship units with the sandbox the previous deploy tooling used."""

import base64
import importlib.machinery
import importlib.util
import io
import json
import os
import pwd
import stat
import subprocess
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

from dashboard import host
from dashboard.telemetry import read_telemetry
from dashboard.config import load_config
from test_dashboard_collector import quota, remote_host
from test_dashboard_config_telemetry import config_value

ROOT = Path(__file__).resolve().parent.parent
SYSTEMD = ROOT / "dashboard" / "systemd"
USER = pwd.getpwuid(os.getuid()).pw_name
REPOSITORIES = ["octocat/example", "octocat/other"]
loader = importlib.machinery.SourceFileLoader("vibe_dashboard_host_cli", str(ROOT / "bin/vibe-dashboard-host"))
spec = importlib.util.spec_from_loader(loader.name, loader)
cli = importlib.util.module_from_spec(spec)
loader.exec_module(cli)


def b64decode(text):
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def granted(**changes):
    value = {"token": "ghs_" + "a" * 36, "expires_at": "2026-10-05T13:00:00Z",
             "permissions": dict(host.READ_PERMISSIONS),
             "repositories": [{"full_name": name} for name in REPOSITORIES]}
    value.update(changes)
    return value


class Host(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.keys = tempfile.TemporaryDirectory()
        cls.key = Path(cls.keys.name) / "app.pem"
        subprocess.run(["openssl", "genrsa", "-out", str(cls.key), "2048"], check=True, capture_output=True)
        cls.key.chmod(0o600)
        cls.public = Path(cls.keys.name) / "app.pub"
        subprocess.run(["openssl", "rsa", "-in", str(cls.key), "-pubout", "-out", str(cls.public)],
                       check=True, capture_output=True)

    @classmethod
    def tearDownClass(cls):
        cls.keys.cleanup()

    def setUp(self):
        # The trusted owner is root on a host; here it is whoever runs the tests.
        patch = mock.patch.object(host, "ROOT_UID", os.getuid())
        patch.start()
        self.addCleanup(patch.stop)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.published = self.base / "published"
        (self.published / "gh").mkdir(parents=True)
        for directory in (self.published, self.published / "gh"):
            directory.chmod(0o755)  # what the runbook installs, whatever this runner's umask is
        self.hosts = self.published / "gh" / "hosts.yml"
        self.telemetry = self.published / "telemetry.json"
        value = config_value(telemetry=self.telemetry)
        value["repositories"] = REPOSITORIES
        self.config = self.base / "dashboard.json"
        self.config.write_text(json.dumps(value))
        self.requests = []

    def opener(self, response):
        def open_(request, timeout):
            self.requests.append((request, timeout))
            if isinstance(response, Exception):
                raise response
            return io.BytesIO(json.dumps(response).encode())
        return open_

    def refresh_token(self, response, app="Iv23example"):
        host.refresh_token(str(self.config), USER, app, 42, self.key, "example-app[bot]", self.hosts,
                           opener=self.opener(response))

    def test_the_jwt_is_signed_by_openssl_with_the_previous_claims(self):
        token = host.app_jwt("Iv23example", self.key, now=1_000_000)
        header, claims, signature = token.split(".")
        self.assertEqual(json.loads(b64decode(header)), {"alg": "RS256", "typ": "JWT"})
        self.assertEqual(json.loads(b64decode(claims)), {"iat": 999_940, "exp": 1_000_540, "iss": "Iv23example"})
        data, signed = self.base / "data", self.base / "signature"
        data.write_text(header + "." + claims)
        signed.write_bytes(b64decode(signature))
        verified = subprocess.run(["openssl", "dgst", "-sha256", "-verify", str(self.public), "-signature",
                                   str(signed), str(data)], capture_output=True, text=True)
        self.assertEqual(verified.returncode, 0, verified.stdout + verified.stderr)
        numeric = host.app_jwt("123456", self.key, now=1_000_000).split(".")[1]
        self.assertEqual(json.loads(b64decode(numeric))["iss"], 123456)
        with self.assertRaisesRegex(host.HostError, "openssl could not sign"):
            host.app_jwt("Iv23example", self.base / "missing.pem")

    def test_the_token_is_narrowed_to_the_configured_reads_and_written_for_the_dashboard_user(self):
        self.refresh_token(granted())
        request, timeout = self.requests[0]
        self.assertEqual(request.full_url, "https://api.github.com/app/installations/42/access_tokens")
        self.assertEqual(request.get_method(), "POST")
        self.assertEqual(timeout, 15)
        self.assertEqual(json.loads(request.data), {"repositories": ["example", "other"],
                                                    "permissions": dict.fromkeys(
            ["actions", "administration", "checks", "contents", "metadata", "pull_requests", "statuses"], "read")})
        self.assertTrue(request.get_header("Authorization").startswith("Bearer eyJ"))
        self.assertEqual(self.hosts.read_text(), "github.com:\n    oauth_token: ghs_%s\n    user: example-app[bot]\n"
                                                 "    git_protocol: https\n" % ("a" * 36))
        info = self.hosts.stat()
        self.assertEqual((stat.S_IMODE(info.st_mode), info.st_uid), (0o600, os.getuid()))
        self.assertEqual(sorted(path.name for path in self.hosts.parent.iterdir()), ["hosts.yml"])

    def test_a_grant_wider_narrower_or_malformed_fails_closed_and_keeps_the_old_token(self):
        self.hosts.write_text("old\n")
        wider = dict(host.READ_PERMISSIONS, contents="write")
        narrower = {name: level for name, level in host.READ_PERMISSIONS.items() if name != "statuses"}
        cases = {
            "write permission": granted(permissions=wider),
            "missing permission": granted(permissions=narrower),
            "extra repository": granted(repositories=[{"full_name": name} for name in REPOSITORIES + ["octocat/x"]]),
            "missing repository": granted(repositories=[{"full_name": REPOSITORIES[0]}]),
            "repository list absent": {key: value for key, value in granted().items() if key != "repositories"},
            "token that would break the YAML": granted(token="ghs_a\n    user: someone"),
            "token that starts a YAML sequence": granted(token="- ghs_a"),
            "token over 4096 characters": granted(token="ghs_" + "a" * 4093),
            "no token": {key: value for key, value in granted().items() if key != "token"},
        }
        for name, response in cases.items():
            with self.subTest(name), self.assertRaises(host.HostError):
                self.refresh_token(response)
            self.assertEqual(self.hosts.read_text(), "old\n")
        self.refresh_token(granted(repositories=[{"full_name": "OctoCat/Example"}, {"full_name": "octocat/other"}]))
        self.assertIn("oauth_token: ghs_", self.hosts.read_text())

    def test_a_long_token_with_dots_and_dashes_is_written_verbatim(self):
        # GitHub's installation tokens grew to about 390 characters with "." and "-" (2026-10-06).
        token = "ghs_" + "AbC1.dEf-2_" * 35 + "z"
        self.refresh_token(granted(token=token))
        self.assertEqual(self.hosts.read_text(), "github.com:\n    oauth_token: %s\n    user: example-app[bot]\n"
                                                 "    git_protocol: https\n" % token)

    def test_a_refused_exchange_reports_its_status_without_the_body(self):
        refusal = urllib.error.HTTPError(host.ACCESS_TOKENS % 42, 422, "Unprocessable", {},
                                         io.BytesIO(b"SECRET_RESPONSE_BODY"))
        with self.assertRaises(host.HostError) as caught:
            self.refresh_token(refusal)
        self.assertEqual(str(caught.exception), "GitHub refused the token exchange (HTTP 422)")
        self.assertFalse(self.hosts.exists())

    def test_a_key_others_can_read_or_a_bad_identifier_stops_before_any_request(self):
        self.key.chmod(0o640)
        try:
            with self.assertRaisesRegex(host.HostError, "only root can read"):
                self.refresh_token(granted())
        finally:
            self.key.chmod(0o600)
        with self.assertRaisesRegex(host.HostError, "not a GitHub identifier"):
            self.refresh_token(granted(), app="Iv23 example")
        self.assertEqual(self.requests, [])

    def test_the_configuration_must_belong_to_the_dashboard_user(self):
        self.assertEqual(load_config(self.config, uid=os.getuid()).repositories, tuple(REPOSITORIES))
        with mock.patch.object(host, "_account", return_value=(os.getuid() + 1, os.getgid())):
            with self.assertRaisesRegex(ValueError, "owned by the dashboard user"):
                self.refresh_token(granted())
            with self.assertRaisesRegex(ValueError, "owned by the dashboard user"):
                host.refresh_telemetry(str(self.config), USER, self.collector_config(), self.base / "state.json")
        self.assertEqual(self.requests, [])

    def test_publish_refuses_a_directory_others_can_write_and_an_invalid_draft(self):
        self.hosts.write_text("old\n")
        self.hosts.parent.chmod(0o775)
        with self.assertRaisesRegex(host.HostError, "only root can write"):
            host.publish(self.hosts, b"new\n", (os.getuid(), os.getgid()))
        self.hosts.parent.chmod(0o755)
        with self.assertRaisesRegex(host.HostError, "previous file stays"):
            host.publish(self.hosts, b"new\n", (os.getuid(), os.getgid()), validate=lambda draft: False)
        self.assertEqual(self.hosts.read_text(), "old\n")
        self.assertEqual(sorted(path.name for path in self.hosts.parent.iterdir()), ["hosts.yml"])

    def collector_config(self, owner="octocat"):
        path = self.base / "collector.json"
        path.write_text(json.dumps({"version": 1, "owner": owner, "host": {
            "label": "Shared CI host", "listener_config_path": "/etc/example-ci/listener.json",
            "lane_name": "example-ci", "workspace_path": "/srv/example-ci/work",
            "codex_home": "/var/lib/example-ci/codex"}}))
        return str(path)

    def sampler(self):
        """A stand-in for remote_sampler.py as the collector's local mode loads it, and the payloads it got."""
        payloads = []

        def respond(payload):
            payloads.append(payload)
            return {"ok": True, "host": remote_host(), "quota": {"status": "ok", "rate_limits": quota()}}
        return mock.patch.object(host.collector.runpy, "run_path", return_value={"respond": respond}), payloads

    def test_telemetry_publishes_a_readable_projection_without_samples_and_keeps_raw_state_apart(self):
        state = self.base / "state" / "collector.json"
        local_sampler, payloads = self.sampler()
        with local_sampler:
            self.assertTrue(host.refresh_telemetry(str(self.config), USER, self.collector_config(), state))
        self.assertEqual(payloads[0]["lane_name"], "example-ci")
        self.assertEqual(len(json.loads(state.read_text())["samples"]), 1)
        published = json.loads(self.telemetry.read_text())
        self.assertEqual(published["samples"], [])
        self.assertEqual(published["hosts"], json.loads(state.read_text())["hosts"])
        info = self.telemetry.stat()
        self.assertEqual((stat.S_IMODE(info.st_mode), info.st_uid), (0o600, os.getuid()))
        view = read_telemetry(load_config(self.config))
        self.assertTrue(view["available"])
        self.assertTrue(view["capacity"]["available"])

    def test_telemetry_the_dashboard_would_refuse_keeps_the_old_file(self):
        self.telemetry.write_text("previous\n")
        self.telemetry.chmod(0o600)
        local_sampler, _ = self.sampler()
        with local_sampler, self.assertRaisesRegex(host.HostError, "previous file stays"):
            host.refresh_telemetry(str(self.config), USER, self.collector_config("example-ci"),
                                   self.base / "state" / "collector.json")
        self.assertEqual(self.telemetry.read_text(), "previous\n")

    def test_telemetry_needs_a_telemetry_file_in_the_configuration(self):
        self.config.write_text(json.dumps(dict(json.loads(self.config.read_text()), telemetry_file=None)))
        with self.assertRaisesRegex(host.HostError, "names no telemetry_file"):
            host.refresh_telemetry(str(self.config), USER, self.collector_config(), self.base / "state.json")
        self.assertFalse((self.base / "state.json").exists())

    def test_a_failed_sample_still_publishes_its_stale_status(self):
        # The real sampler, run in this process from the package import: the lane's listener is absent here.
        state = self.base / "state" / "collector.json"
        self.assertFalse(host.refresh_telemetry(str(self.config), USER, self.collector_config(), state))
        published = json.loads(self.telemetry.read_text())
        self.assertEqual(published["hosts"][0]["error"], "listener_config_unavailable")
        self.assertTrue(published["hosts"][0]["stale"])
        self.assertFalse(read_telemetry(load_config(self.config))["capacity"]["available"])


class Cli(unittest.TestCase):
    TOKEN = ["token", "--config", "c", "--user", "u", "--app", "1", "--installation", "2",
             "--key", "k", "--login", "l", "--output", "o"]
    TELEMETRY = ["telemetry", "--config", "c", "--user", "u", "--collector", "x", "--state", "/s"]

    def run_cli(self, argv, euid=0):
        """(exit code, stderr) of the command line, as the given effective user."""
        with mock.patch.object(cli.os, "geteuid", return_value=euid), \
                mock.patch.object(cli.sys, "stderr", io.StringIO()) as stderr:
            return cli.main(argv), stderr.getvalue()

    def test_only_root_runs_the_helpers(self):
        with mock.patch.object(cli.host, "refresh_token") as run:
            self.assertEqual(self.run_cli(self.TOKEN, euid=1000), (2, "vibe-dashboard-host: must run as root\n"))
            run.assert_not_called()
            self.assertEqual(self.run_cli(self.TOKEN), (0, ""))
        self.assertEqual(run.call_args.args, ("c", "u", "1", 2, Path("k"), "l", Path("o")))

    def test_exit_codes_tell_a_failed_sample_from_a_refusal(self):
        with mock.patch.object(cli.host, "refresh_telemetry", return_value=True):
            self.assertEqual(self.run_cli(self.TELEMETRY), (0, ""))
        with mock.patch.object(cli.host, "refresh_telemetry", return_value=False):
            self.assertEqual(self.run_cli(self.TELEMETRY)[0], 1)
        with mock.patch.object(cli.host, "refresh_telemetry", side_effect=host.HostError("refused")):
            self.assertEqual(self.run_cli(self.TELEMETRY), (2, "vibe-dashboard-host: refused\n"))


WEB_SANDBOX = """NoNewPrivileges=yes
ProtectSystem=strict
ProtectHome=yes
PrivateTmp=yes
PrivateDevices=yes
ProtectControlGroups=yes
ProtectKernelModules=yes
ProtectKernelTunables=yes
RestrictSUIDSGID=yes
LockPersonality=yes
CapabilityBoundingSet=
AmbientCapabilities=
RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6
MemoryMax=512M
CPUQuota=100%
TasksMax=64
UMask=0077""".splitlines()
ROOT_SANDBOX = """NoNewPrivileges=yes
ProtectSystem=strict
PrivateTmp=yes
PrivateDevices=yes
ProtectControlGroups=yes
ProtectKernelModules=yes
ProtectKernelTunables=yes
RestrictSUIDSGID=yes
LockPersonality=yes""".splitlines()


def unit(name):
    lines = (SYSTEMD / name).read_text().splitlines()
    return lines, {line.split("=", 1)[0]: line.split("=", 1)[1] for line in lines if "=" in line and not line.startswith("#")}


class Units(unittest.TestCase):
    def test_the_web_unit_has_the_full_sandbox_and_a_clean_environment(self):
        lines, values = unit("vibe-dashboard@.service")
        self.assertEqual([line for line in WEB_SANDBOX if line not in lines], [])
        self.assertEqual(values["User"], "vibe-dashboard-%i")
        self.assertNotIn("ReadWritePaths", values)
        command = values["ExecStart"]
        self.assertTrue(command.startswith("/usr/bin/env -i "))
        for setting in ("GH_PROMPT_DISABLED=1", "PYTHONNOUSERSITE=1", "GH_CONFIG_DIR=/var/lib/vibe-dashboard/%i/gh"):
            self.assertIn(" %s " % setting, command)

    def test_root_units_are_sandboxed_and_write_only_their_own_paths(self):
        writable = {"vibe-dashboard-token@.service": "/var/lib/vibe-dashboard/%i/gh",
                    "vibe-dashboard-telemetry@.service": "/var/lib/vibe-dashboard/%i /var/lib/vibe-dashboard-state/%i",
                    "vibe-dashboard-follow.service": "/opt/vibe-verifier"}
        for name, paths in writable.items():
            with self.subTest(name):
                lines, values = unit(name)
                self.assertEqual([line for line in ROOT_SANDBOX if line not in lines], [])
                self.assertIn(values["ProtectHome"], ("yes", "read-only"))
                self.assertEqual(values["ReadWritePaths"], paths)
                self.assertTrue(values["ExecStart"].startswith("/usr/bin/env -i HOME=/root PYTHONNOUSERSITE=1 "))
        self.assertEqual(unit("vibe-dashboard-follow.service")[1]["UMask"], "0022")
        self.assertIn("--output /var/lib/vibe-dashboard/%i/gh/hosts.yml", unit("vibe-dashboard-token@.service")[1]["ExecStart"])
        self.assertIn("--state /var/lib/vibe-dashboard-state/%i/", unit("vibe-dashboard-telemetry@.service")[1]["ExecStart"])

    def test_every_unit_runs_a_script_this_checkout_has(self):
        for path in SYSTEMD.glob("*.service"):
            with self.subTest(path.name):
                script = unit(path.name)[1]["ExecStart"].split(" /opt/vibe-verifier/", 1)[1].split()[0]
                self.assertTrue(os.access(ROOT / script, os.X_OK), script)

    def test_timers_run_at_the_documented_cadence(self):
        self.assertEqual(unit("vibe-dashboard-token@.timer")[1]["OnUnitActiveSec"], "10min")
        self.assertEqual(unit("vibe-dashboard-telemetry@.timer")[1]["OnUnitActiveSec"], "30s")


if __name__ == "__main__":
    unittest.main()
