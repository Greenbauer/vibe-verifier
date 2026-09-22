"""The gitleaks gate: the real pinned binary on a fixture with a fake key, and a stand-in binary
for the contract's edges (exit codes, redaction, --soak, an unusable tool)."""
import json
import os
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path

from helpers import ROOT, clean_env, commit, gate, git, make_repo

# Base32 alphabet after the AKIA prefix, which is what gitleaks' aws-access-token rule wants.
FAKE_KEY = "AKIA" + "Q7ZK3MN2PX6T5V4W"  # a fixture key, assembled so the literal never sits in this tree; the gate under test must still catch it in the fixture repo


def commit_leak(repo):
    Path(repo, "secrets.env").write_text("AWS_ACCESS_KEY_ID=%s\n" % FAKE_KEY)
    git(repo, "add", "-A")
    # The operator's local git wrapper scans staged changes for secret shapes and refuses; this
    # fixture's whole point is a fake key, so it says so. CI runs plain git.
    subprocess.run(["git", "-C", repo, "commit", "-q", "-m", "leak"], check=True, capture_output=True,
                   env=clean_env({"ALLOW_SECRETS": "1"}))


class RealGitleaks(unittest.TestCase):
    """Resolves the pinned gitleaks from PATH, the cache or a verified download."""

    def test_a_new_secret_fails_and_is_never_printed(self):
        repo = make_repo(self, {"a.txt": "clean\n"})
        commit_leak(repo)
        result = gate("gitleaks", repo, "--base-ref", "HEAD~1", "--format", "json")
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        report = json.loads(result.stdout)
        self.assertEqual(report["status"], "violations")
        self.assertEqual([(f["path"], f["line"]) for f in report["findings"]], [("secrets.env", 1)])
        self.assertIn("aws-access-token", report["findings"][0]["message"])
        self.assertNotIn(FAKE_KEY, result.stdout + result.stderr)

    def test_clean_commits_pass(self):
        repo = make_repo(self, {"a.txt": "clean\n"})
        commit(repo, {"b.txt": "still clean\n"}, "more")
        result = gate("gitleaks", repo, "--base-ref", "HEAD~1")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_a_secret_already_in_the_base_is_not_this_prs_finding(self):
        repo = make_repo(self, {"a.txt": "clean\n"})
        commit_leak(repo)
        commit(repo, {"b.txt": "clean\n"}, "after")
        self.assertEqual(gate("gitleaks", repo, "--base-ref", "HEAD~1").returncode, 0)

    def test_soak_reports_and_passes(self):
        repo = make_repo(self, {"a.txt": "clean\n"})
        commit_leak(repo)
        result = gate("gitleaks", repo, "--base-ref", "HEAD~1", "--soak")
        self.assertEqual(result.returncode, 0)
        self.assertIn("secrets.env:1", result.stdout)

    def test_an_unresolvable_base_cannot_run(self):
        repo = make_repo(self, {"a.txt": "clean\n"})
        self.assertEqual(gate("gitleaks", repo, "--base-ref", "no-such-branch").returncode, 2)


class StandInGitleaks(unittest.TestCase):
    """A fake gitleaks first on PATH that reports the pinned version, so the resolver takes it."""

    def setUp(self):
        self.bin = tempfile.mkdtemp(prefix="vv-gl-")
        self.addCleanup(lambda: subprocess.run(["rm", "-rf", self.bin]))
        self.cache = tempfile.mkdtemp(prefix="vv-gl-cache-")
        self.addCleanup(lambda: subprocess.run(["rm", "-rf", self.cache]))

    def fake(self, version="8.30.1", exit_code=0, report="[]", stderr=""):
        script = Path(self.bin, "gitleaks")
        script.write_text("#!/bin/sh\nif [ \"$1\" = version ]; then echo %s; exit 0; fi\nprintf '%%s' '%s'\nprintf '%%s' '%s' >&2\nexit %d\n"
                          % (version, report.replace("'", "'\\''"), stderr, exit_code))
        script.chmod(script.stat().st_mode | stat.S_IXUSR)
        return {"PATH": "%s:%s" % (self.bin, os.environ["PATH"]), "VIBE_VERIFIER_TOOLS": self.cache}

    def repo(self):
        repo = make_repo(self, {"a.txt": "1\n"})
        commit(repo, {"a.txt": "2\n"}, "change")
        return repo

    def test_findings_are_read_from_the_report_without_the_secret(self):
        leak = {"RuleID": "generic-api-key", "Description": "Detected a Generic API Key.", "File": "src/config.ts",
                "StartLine": 12, "Commit": "abcdef1234567890", "Secret": "REDACTED", "Match": "REDACTED"}
        result = gate("gitleaks", self.repo(), "--base-ref", "HEAD~1", env=self.fake(exit_code=1, report=json.dumps([leak])))
        self.assertEqual(result.returncode, 1)
        self.assertIn("src/config.ts:12: generic-api-key: Detected a Generic API Key (commit abcdef1234)", result.stdout)

    def test_a_tool_crash_cannot_run(self):
        result = gate("gitleaks", self.repo(), "--base-ref", "HEAD~1", env=self.fake(exit_code=126, stderr="segfault"))
        self.assertEqual(result.returncode, 2)
        self.assertIn("gitleaks exited 126", result.stderr)

    def test_exit_one_with_an_empty_report_cannot_run(self):
        self.assertEqual(gate("gitleaks", self.repo(), "--base-ref", "HEAD~1", env=self.fake(exit_code=1, report="[]")).returncode, 2)

    def test_a_config_is_passed_through(self):
        env = self.fake()
        Path(self.bin, "gitleaks").write_text("#!/bin/sh\nif [ \"$1\" = version ]; then echo 8.30.1; exit 0; fi\necho \"$@\" > %s/args\necho '[]'\n" % self.bin)
        result = gate("gitleaks", self.repo(), "--base-ref", "HEAD~1", "--config", "rules.toml", env=env)
        self.assertEqual(result.returncode, 0, result.stderr)
        args = Path(self.bin, "args").read_text()
        self.assertIn("--config rules.toml", args)
        self.assertIn("--redact", args)
        self.assertIn("--log-opts=HEAD~1..HEAD", args)

    def test_a_wrong_version_on_path_is_not_taken(self):
        # Resolution must fall through to the cache; an empty cache means a download, which this
        # test forbids by pointing the cache at a file, so the gate must report could-not-run.
        env = self.fake(version="7.0.0")
        Path(self.cache, "blocker").write_text("")
        env["VIBE_VERIFIER_TOOLS"] = str(Path(self.cache, "blocker"))
        result = gate("gitleaks", self.repo(), "--base-ref", "HEAD~1", env=env)
        self.assertEqual(result.returncode, 2, result.stdout)


if __name__ == "__main__":
    unittest.main()
