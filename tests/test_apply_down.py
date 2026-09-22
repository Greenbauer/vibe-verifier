"""The apply-down pin bump, driven through the command line against a fake `gh` that records writes."""
import json
import os
import re
import shutil
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path

from helpers import ROOT, commit, git, make_repo, runner

TEMPLATE = (ROOT / "consumer" / "vibe-verifier.yml").read_text()
PIN = re.compile(r"(vibe-verifier/actions/gates@)[0-9a-f]{40}[^\n]*")


def stub_pinned_to(sha):
    return PIN.sub(r"\g<1>%s # main 2026-09-17" % sha, TEMPLATE)


class ApplyDown(unittest.TestCase):
    def setUp(self):
        self.fixtures = tempfile.mkdtemp(prefix="vv-gh-")
        self.addCleanup(shutil.rmtree, self.fixtures, True)
        bin_dir = Path(self.fixtures) / "bin"
        bin_dir.mkdir()
        gh = bin_dir / "gh"
        gh.write_text("#!/bin/sh\nexec %s %s \"$@\"\n" % (os.environ.get("PYTHON", "python3"), ROOT / "tests" / "fake_gh.py"))
        gh.chmod(gh.stat().st_mode | stat.S_IEXEC)
        self.env = {"PATH": "%s:%s" % (bin_dir, os.environ["PATH"]), "FAKE_GH_ROOT": self.fixtures}
        self.source = make_repo(self, {"gates/a.py": "1"})
        self.old = self.head()
        commit(self.source, {"gates/a.py": "2"}, "gate change")
        self.new = self.head()

    def head(self):
        return subprocess.run(["git", "-C", self.source, "rev-parse", "HEAD"], capture_output=True, text=True, check=True).stdout.strip()

    def consumer(self, repo, stub, workflows=None):
        base = Path(self.fixtures) / repo
        (base / ".github" / "workflows").mkdir(parents=True, exist_ok=True)
        if stub is not None:
            (base / ".github" / "workflows" / "vibe-verifier.yml").write_text(stub)
        for name, text in (workflows or {}).items():
            (base / ".github" / "workflows" / name).write_text(text)
        (base / ".vibe-verifier").write_text("no-duplicate-package-json-keys\n")

    def harness(self, sha, other=None):
        return ("jobs:\n  explore:\n    steps:\n      - uses: Greenbauer/vibe-verifier/actions/criteria@%s # main x\n"
                "  verify:\n    steps:\n      - uses: Greenbauer/vibe-verifier/actions/gates@%s # main x\n" % (sha, other or sha))

    def calls(self):
        log = Path(self.fixtures) / "calls.log"
        return [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []

    def apply_down(self, *args):
        return runner("apply-down", "--source", self.source, "--source-ref", "main", *args, env=self.env)

    def test_plan_names_the_bump_and_writes_nothing(self):
        self.consumer("acme/app", stub_pinned_to(self.old))
        result = self.apply_down("--repo", "acme/app")
        self.assertEqual(result.returncode, 1)
        self.assertIn("%s -> %s" % (self.old[:10], self.new[:10]), result.stdout)
        self.assertIn("--confirm", result.stdout)
        self.assertEqual([c for c in self.calls() if c["method"] != "GET"], [])

    def test_confirm_opens_one_pr_changing_only_the_pin_line(self):
        self.consumer("acme/app", stub_pinned_to(self.old))
        digest = re.search(r"--confirm (\w+)", self.apply_down("--repo", "acme/app").stdout).group(1)
        result = self.apply_down("--repo", "acme/app", "--confirm", digest)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("https://github.com/acme/app/pull/", result.stdout)
        written = [c for c in self.calls() if c["method"] == "PUT"]
        self.assertEqual(len(written), 1)
        old_lines = stub_pinned_to(self.old).splitlines()
        new_lines = written[0]["content"].splitlines()
        differing = [n for n, (a, b) in enumerate(zip(old_lines, new_lines)) if a != b]
        self.assertEqual(len(old_lines), len(new_lines))
        self.assertEqual(len(differing), 1)
        self.assertIn("gates@%s" % self.new, new_lines[differing[0]])

    def test_the_bump_dates_the_pin_it_writes(self):
        # The comment beside the pin says when the pin was taken; leaving the old date there makes
        # the line lie about the commit next to it.
        self.consumer("acme/app", stub_pinned_to(self.old))
        digest = re.search(r"--confirm (\w+)", self.apply_down("--repo", "acme/app").stdout).group(1)
        self.apply_down("--repo", "acme/app", "--confirm", digest)
        written = [c for c in self.calls() if c["method"] == "PUT"][0]["content"]
        date = subprocess.run(["git", "-C", self.source, "log", "-1", "--date=short", "--format=%cd", self.new],
                              capture_output=True, text=True, check=True).stdout.strip()
        self.assertIn("gates@%s # main %s" % (self.new, date), written)

    def test_workflow_pins_move_in_the_same_pr_as_the_stub(self):
        # A harness copy carries two pins; a current stub with a stale copy still needs the bump, and
        # a stale stub with a stale copy is one PR with one commit per file.
        self.consumer("acme/app", stub_pinned_to(self.new), workflows={"qae-explore.yml": self.harness(self.old)})
        plan = self.apply_down("--repo", "acme/app")
        self.assertEqual(plan.returncode, 1)
        self.assertIn(".github/workflows/qae-explore.yml", plan.stdout)
        self.assertNotIn("vibe-verifier.yml", plan.stdout)
        digest = re.search(r"--confirm (\w+)", plan.stdout).group(1)
        self.assertEqual(self.apply_down("--repo", "acme/app", "--confirm", digest).returncode, 0)
        written = [c for c in self.calls() if c["method"] == "PUT"]
        self.assertEqual([c["endpoint"] for c in written], ["repos/acme/app/contents/.github/workflows/qae-explore.yml"])
        self.assertEqual(written[0]["content"].count("@%s # main " % self.new), 2)
        self.assertNotIn(self.old, written[0]["content"])
        self.assertEqual(len([c for c in self.calls() if c["method"] == "PR"]), 1)

    def test_a_stale_stub_and_a_stale_workflow_bump_together(self):
        self.consumer("acme/app", stub_pinned_to(self.old), workflows={"qae-explore.yml": self.harness(self.old, self.new)})
        digest = re.search(r"--confirm (\w+)", self.apply_down("--repo", "acme/app").stdout).group(1)
        self.assertEqual(self.apply_down("--repo", "acme/app", "--confirm", digest).returncode, 0)
        written = {c["endpoint"].split("/contents/")[1]: c["content"] for c in self.calls() if c["method"] == "PUT"}
        self.assertEqual(sorted(written), [".github/workflows/qae-explore.yml", ".github/workflows/vibe-verifier.yml"])
        # the current gates@ pin in the copy is left alone; only criteria@ moved
        self.assertEqual(written[".github/workflows/qae-explore.yml"].count("gates@%s # main x" % self.new), 1)
        self.assertIn("criteria@%s # main " % self.new, written[".github/workflows/qae-explore.yml"])
        self.assertEqual(len([c for c in self.calls() if c["method"] == "PR"]), 1)

    def test_a_stale_digest_is_refused(self):
        self.consumer("acme/app", stub_pinned_to(self.old))
        result = self.apply_down("--repo", "acme/app", "--confirm", "0" * 12)
        self.assertEqual(result.returncode, 2)
        self.assertIn("plan digest is", result.stderr)
        self.assertEqual([c for c in self.calls() if c["method"] != "GET"], [])

    def test_a_drifted_stub_is_never_bumped(self):
        self.consumer("acme/app", stub_pinned_to(self.old).replace("timeout-minutes: 5", "timeout-minutes: 50"))
        result = self.apply_down("--repo", "acme/app")
        self.assertIn("skipped: stub drift", result.stdout)
        self.assertIn("nothing to bump", result.stdout)
        self.assertEqual(result.returncode, 0)

    def test_a_current_pin_is_left_alone(self):
        self.consumer("acme/app", stub_pinned_to(self.new))
        result = self.apply_down("--repo", "acme/app")
        self.assertIn("already pinned", result.stdout)
        self.assertEqual(result.returncode, 0)

    def test_an_open_bump_is_not_opened_twice(self):
        self.consumer("acme/app", stub_pinned_to(self.old))
        digest = re.search(r"--confirm (\w+)", self.apply_down("--repo", "acme/app").stdout).group(1)
        self.apply_down("--repo", "acme/app", "--confirm", digest)
        (Path(self.fixtures) / "acme/app/.open-pr").write_text("https://github.com/acme/app/pull/7")
        before = len([c for c in self.calls() if c["method"] == "PUT"])
        result = self.apply_down("--repo", "acme/app", "--confirm", digest)
        self.assertEqual(result.returncode, 0)
        self.assertIn("pull/7", result.stdout)
        self.assertEqual(len([c for c in self.calls() if c["method"] == "PUT"]), before)

    def test_discovery_skips_repositories_that_never_subscribed(self):
        self.consumer("acme/app", stub_pinned_to(self.old))
        (Path(self.fixtures) / "acme/brochure").mkdir(parents=True, exist_ok=True)
        git(self.source, "remote", "add", "origin", "https://github.com/acme/vibe-verifier.git")
        result = self.apply_down("--owner", "acme")
        self.assertIn("acme/app", result.stdout)
        self.assertNotIn("acme/brochure", result.stdout)

    def test_an_unreadable_repository_is_could_not_run(self):
        self.consumer("acme/app", stub_pinned_to(self.old))
        (Path(self.fixtures) / "acme/app/.broken").write_text("")
        result = self.apply_down("--repo", "acme/app")
        self.assertEqual(result.returncode, 2)
        self.assertIn("could not read", result.stdout)


if __name__ == "__main__":
    unittest.main()
