"""The actionlint and zizmor gates: the real pinned binaries on a fixture, and a stand-in for the
edges. Both judge only the workflows a pull request changed."""
import json
import os
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path

from helpers import commit, gate, make_repo

OK = """name: ok
on: push
permissions: {}
jobs:
  build:
    runs-on: ubuntu-latest
    permissions:
      contents: read
    steps:
      - uses: actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1 # v7.0.1
        with:
          persist-credentials: false
      - run: echo ok
"""
BAD = """name: bad
on: push
jobs:
  build:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - run: echo "${{ github.event.pull_request.title }}"
      - uses: actions/setup-node@v4
        with:
          node-verison: 20
"""


def repo_with(test, added):
    repo = make_repo(test, {".github/workflows/ok.yml": OK, "README.md": "x\n"})
    commit(repo, added, "change")
    return repo


class Actionlint(unittest.TestCase):
    def test_a_bad_workflow_the_pr_adds_fails(self):
        result = gate("actionlint", repo_with(self, {".github/workflows/bad.yml": BAD}), "--base-ref", "HEAD~1", "--format", "json")
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        findings = json.loads(result.stdout)["findings"]
        self.assertIn((".github/workflows/bad.yml", 11), [(f["path"], f["line"]) for f in findings])
        self.assertTrue(any("node-verison" in f["message"] and "[action]" in f["message"] for f in findings))

    def test_a_clean_change_passes(self):
        result = gate("actionlint", repo_with(self, {".github/workflows/ok.yml": OK.replace("echo ok", "echo fine")}), "--base-ref", "HEAD~1")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_a_pre_existing_problem_is_not_this_prs_finding_unless_all(self):
        repo = make_repo(self, {".github/workflows/bad.yml": BAD})
        commit(repo, {"README.md": "y\n"}, "docs")
        self.assertEqual(gate("actionlint", repo, "--base-ref", "HEAD~1").returncode, 0)
        self.assertEqual(gate("actionlint", repo, "--all").returncode, 1)

    def test_an_unresolvable_base_cannot_run(self):
        self.assertEqual(gate("actionlint", repo_with(self, {"README.md": "y\n"}), "--base-ref", "no-such-branch").returncode, 2)


class Zizmor(unittest.TestCase):
    def test_a_bad_workflow_the_pr_adds_fails_with_zizmors_idents(self):
        result = gate("zizmor", repo_with(self, {".github/workflows/bad.yml": BAD}), "--base-ref", "HEAD~1", "--format", "json")
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        findings = json.loads(result.stdout)["findings"]
        idents = {f["message"].split(":")[0] for f in findings}
        self.assertTrue({"template-injection", "unpinned-uses"} <= idents, idents)
        injection = next(f for f in findings if f["message"].startswith("template-injection"))
        self.assertEqual((injection["path"], injection["line"]), (".github/workflows/bad.yml", 8))
        self.assertIn("high severity", injection["message"])

    def test_a_clean_change_passes(self):
        result = gate("zizmor", repo_with(self, {".github/workflows/ok.yml": OK.replace("echo ok", "echo fine")}), "--base-ref", "HEAD~1")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_min_severity_is_passed_through(self):
        loose = json.loads(gate("zizmor", repo_with(self, {".github/workflows/bad.yml": BAD}), "--base-ref", "HEAD~1", "--format", "json").stdout)
        strict = json.loads(gate("zizmor", repo_with(self, {".github/workflows/bad.yml": BAD}), "--base-ref", "HEAD~1", "--format", "json",
                                 "--min-severity", "high").stdout)
        self.assertLess(len(strict["findings"]), len(loose["findings"]))
        self.assertTrue(all("high severity" in f["message"] for f in strict["findings"]))

    def test_a_pre_existing_problem_is_not_this_prs_finding_unless_all(self):
        repo = make_repo(self, {".github/workflows/bad.yml": BAD})
        commit(repo, {"README.md": "y\n"}, "docs")
        self.assertEqual(gate("zizmor", repo, "--base-ref", "HEAD~1").returncode, 0)
        self.assertEqual(gate("zizmor", repo, "--all").returncode, 1)


class StandIns(unittest.TestCase):
    """A fake binary first on PATH reporting the pinned version, so the resolver takes it."""

    def fake(self, name, version_flag, version, script):
        bin_dir = tempfile.mkdtemp(prefix="vv-lint-")
        self.addCleanup(lambda: subprocess.run(["rm", "-rf", bin_dir]))
        path = Path(bin_dir, name)
        path.write_text("#!/bin/sh\nif [ \"$1\" = %s ]; then echo '%s'; exit 0; fi\n%s\n" % (version_flag, version, script))
        path.chmod(path.stat().st_mode | stat.S_IXUSR)
        return {"PATH": "%s:%s" % (bin_dir, os.environ["PATH"])}

    def test_actionlint_crash_cannot_run(self):
        env = self.fake("actionlint", "-version", "1.7.12", "echo boom >&2; exit 3")
        result = gate("actionlint", repo_with(self, {".github/workflows/bad.yml": BAD}), "--base-ref", "HEAD~1", env=env)
        self.assertEqual(result.returncode, 2)
        self.assertIn("actionlint exited 3: boom", result.stderr)

    def test_zizmor_error_cannot_run_and_ignored_findings_are_not_findings(self):
        env = self.fake("zizmor", "--version", "zizmor 1.30.1", "echo broken >&2; exit 1")
        self.assertEqual(gate("zizmor", repo_with(self, {".github/workflows/bad.yml": BAD}), "--base-ref", "HEAD~1", env=env).returncode, 2)
        report = json.dumps([{"ident": "x", "ignored": True, "determinations": {}, "locations": []}])
        env = self.fake("zizmor", "--version", "zizmor 1.30.1", "printf '%%s' '%s'; exit 0" % report)
        self.assertEqual(gate("zizmor", repo_with(self, {".github/workflows/bad.yml": BAD}), "--base-ref", "HEAD~1", env=env).returncode, 0)


if __name__ == "__main__":
    unittest.main()
