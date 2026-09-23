"""Wrapper support in consumers and apply-down, driven through the command line against a fake `gh`.

A wrapper is an organization's CI repository: its own workflows pin catalog actions, the
organization's rulesets require its workflows pinned by commit, and a caller may still name one of
its workflows in a `uses:` line. Each of those pins is judged and bumped where it lives."""
import copy
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

REVIEW = (ROOT / "harnesses" / "review" / "review.yml").read_text()
GATE = ".github/workflows/vibe-verifier.yml"
LEGACY = ".github/workflows/claude-review.yml"


def gate_workflow(sha):
    return ("name: Vibe Verifier\non:\n  pull_request:\njobs:\n  vibe-verifier-ok:\n    runs-on: ubuntu-latest\n"
            "    steps:\n      - uses: Greenbauer/vibe-verifier/actions/gates@%s # main 2026-09-23\n" % sha)


def legacy_caller(ref):
    return "jobs:\n  review:\n    uses: acme/ci/.github/workflows/claude-review.yml@%s\n    secrets: {}\n" % ref


def wrapper_review(sha, token="CLAUDE_CODE_OAUTH_TOKEN_ORG"):
    """The review harness as a wrapper carries it: its own header (no concurrency group), its own
    token secret, the catalog's jobs."""
    jobs = REVIEW[REVIEW.index("\njobs:\n"):]
    jobs = jobs.replace("secrets.CLAUDE_CODE_OAUTH_TOKEN }}", "secrets.%s }}" % token)
    jobs = re.sub(r"(vibe-verifier/actions/gates@)0{40}[^\n]*", r"\g<1>%s # main 2026-09-23" % sha, jobs)
    return "name: Review\n# required by an organization ruleset\non:\n  pull_request:\npermissions:\n  contents: read\n" + jobs


class Wrapper(unittest.TestCase):
    def setUp(self):
        self.fixtures = tempfile.mkdtemp(prefix="vv-gh-")
        self.addCleanup(shutil.rmtree, self.fixtures, True)
        bin_dir = Path(self.fixtures) / "bin"
        bin_dir.mkdir()
        gh = bin_dir / "gh"
        gh.write_text("#!/bin/sh\nexec %s %s \"$@\"\n" % (os.environ.get("PYTHON", "python3"), ROOT / "tests" / "fake_gh.py"))
        gh.chmod(gh.stat().st_mode | stat.S_IEXEC)
        self.env = {"PATH": "%s:%s" % (bin_dir, os.environ["PATH"]), "FAKE_GH_ROOT": self.fixtures}
        # The catalog: a release commit, then a gate change.
        self.source = make_repo(self, {"gates/a.py": "1"})
        self.old = self.head(self.source)
        commit(self.source, {"gates/a.py": "2"}, "gate change")
        self.new = self.head(self.source)
        # The wrapper's history: each workflow changes once, then the README alone.
        self.wrapper = make_repo(self, {GATE: "1", LEGACY: "1", "README.md": "1"})
        self.w1 = self.head(self.wrapper)
        commit(self.wrapper, {GATE: "2"}, "gate workflow change")
        self.w2 = self.head(self.wrapper)
        commit(self.wrapper, {LEGACY: "2"}, "legacy workflow change")
        self.w3 = self.head(self.wrapper)
        commit(self.wrapper, {"README.md": "2"}, "docs only")
        self.w4 = self.head(self.wrapper)
        git(self.wrapper, "remote", "add", "origin", "https://github.com/acme/ci.git")
        self.repo("acme/ci", workflows={"vibe-verifier.yml": gate_workflow(self.new)}, repo_id=42)

    def head(self, repo):
        return subprocess.run(["git", "-C", repo, "rev-parse", "HEAD"], capture_output=True, text=True, check=True).stdout.strip()

    def repo(self, name, manifest=None, workflows=None, repo_id=None):
        base = Path(self.fixtures) / name
        (base / ".github" / "workflows").mkdir(parents=True, exist_ok=True)
        if manifest is not None:
            (base / ".vibe-verifier").write_text(manifest)
        elif (base / ".vibe-verifier").exists():
            (base / ".vibe-verifier").unlink()
        for file, text in (workflows or {}).items():
            (base / ".github" / "workflows" / file).write_text(text)
        if repo_id is not None:
            (base / ".id").write_text(str(repo_id))

    def ruleset(self, pins, targets=("app",), enforcement="active"):
        """One organization ruleset requiring wrapper workflows: pins are (path, sha or None for a bare ref)."""
        entries = [dict({"repository_id": 42, "path": path, "ref": "refs/heads/main"}, **({"sha": sha} if sha else {}))
                   for path, sha in pins]
        entries.append({"repository_id": 99, "path": GATE, "ref": "refs/heads/main", "sha": "e" * 40})  # another repository's
        ruleset = {"id": 7, "name": "vibe-verifier-gates", "target": "branch", "enforcement": enforcement,
                   "conditions": {"ref_name": {"include": ["~DEFAULT_BRANCH"], "exclude": []},
                                  "repository_name": {"include": list(targets), "exclude": []}},
                   "rules": [{"type": "deletion"},
                             {"type": "workflows", "parameters": {"do_not_enforce_on_create": True, "workflows": entries}}]}
        (Path(self.fixtures) / "acme" / ".rulesets.json").write_text(json.dumps([ruleset]))
        return ruleset

    def calls(self):
        log = Path(self.fixtures) / "calls.log"
        return [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []

    def check(self, *args):
        return runner("consumers", "--source", self.source, "--source-ref", "main",
                      "--wrapper", self.wrapper, "--wrapper-ref", "main", *args, env=self.env)

    def apply_down(self, *args):
        return runner("apply-down", "--source", self.source, "--source-ref", "main",
                      "--wrapper", self.wrapper, "--wrapper-ref", "main", *args, env=self.env)

    def test_the_wrapper_is_a_consumer_of_the_catalog_with_no_stub_expected(self):
        result = self.check("--repo", "acme/ci")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("stub: wrapper", result.stdout)
        self.assertIn("workflows: vibe-verifier.yml current", result.stdout)
        self.repo("acme/ci", workflows={"vibe-verifier.yml": gate_workflow(self.old)})
        stale = self.check("--repo", "acme/ci")
        self.assertEqual(stale.returncode, 1, stale.stdout)
        self.assertIn("vibe-verifier.yml stale (1 release commit behind)", stale.stdout)

    def test_a_wrapper_review_copy_owns_its_header_and_token_secret_and_nothing_under_jobs(self):
        self.repo("acme/ci", workflows={"review.yml": wrapper_review(self.new)})
        result = self.check("--repo", "acme/ci")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("review.yml current", result.stdout)
        self.assertNotIn("drifts", result.stdout)
        self.repo("acme/ci", workflows={"review.yml": wrapper_review(self.new).replace("--max-turns 50", "--max-turns 5")})
        drift = self.check("--repo", "acme/ci")
        self.assertEqual(drift.returncode, 1, drift.stdout)
        self.assertIn("review.yml current drifts from harnesses/review/review.yml under jobs: outside the pin and token lines",
                      drift.stdout)

    def test_a_ruleset_pin_is_judged_by_commits_to_the_workflow_it_names(self):
        self.repo("acme/app", manifest="gitleaks\n")
        self.ruleset([(GATE, self.w2)])  # since w2 only the legacy workflow and the README changed
        result = self.check("--repo", "acme/ci")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("ruleset acme/vibe-verifier-gates", result.stdout)
        self.assertIn("enforcement: active  targets: [app]  pins: vibe-verifier.yml current", result.stdout)
        self.assertNotIn("e" * 10, result.stdout)  # another repository's entry is not the wrapper's pin
        self.ruleset([(GATE, self.w1)])
        self.assertIn("pins: vibe-verifier.yml stale (1 commit behind)", self.check("--repo", "acme/ci").stdout)
        self.ruleset([(GATE, "f" * 40)])
        self.assertIn("vibe-verifier.yml unknown (not in main)", self.check("--repo", "acme/ci").stdout)
        self.ruleset([(GATE, None)])
        bare = self.check("--repo", "acme/ci", "--format", "github")
        self.assertEqual(bare.returncode, 1, bare.stdout)
        self.assertIn("::error title=vibe-verifier consumers::ruleset acme/vibe-verifier-gates: vibe-verifier.yml unpinned "
                      "(refs/heads/main is not a commit SHA)", bare.stdout)
        self.ruleset([(GATE, self.w2)], enforcement="disabled")
        off = self.check("--repo", "acme/ci", "--format", "github")
        self.assertEqual(off.returncode, 1, off.stdout)
        self.assertIn("ruleset acme/vibe-verifier-gates: enforcement is disabled, not active", off.stdout)

    def test_a_repository_a_ruleset_targets_needs_its_manifest_and_no_stub(self):
        self.repo("acme/app", manifest="gitleaks\n")
        self.ruleset([(GATE, self.w4)])
        result = self.check("--repo", "acme/ci")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertRegex(result.stdout, r"acme/app +pin: - +stub: ruleset +manifest: ok")
        self.repo("acme/app")
        missing = self.check("--repo", "acme/ci")
        self.assertEqual(missing.returncode, 1, missing.stdout)
        self.assertRegex(missing.stdout, r"acme/app +pin: - +stub: ruleset +manifest: missing")

    def test_callers_of_a_wrapper_workflow_are_judged_against_it(self):
        self.repo("acme/legacy", workflows={"claude-review.yml": legacy_caller("v1")})
        self.repo("acme/brochure")
        result = self.check("--owner", "acme")
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("claude-review.yml (acme/ci) unpinned (v1 is not a commit SHA)", result.stdout)
        self.assertNotIn("acme/brochure", result.stdout)
        self.repo("acme/legacy", workflows={"claude-review.yml": legacy_caller(self.w2)})
        self.assertIn("claude-review.yml (acme/ci) stale (1 commit behind)", self.check("--owner", "acme").stdout)
        self.repo("acme/legacy", workflows={"claude-review.yml": legacy_caller(self.w3)})
        current = self.check("--owner", "acme")
        self.assertEqual(current.returncode, 0, current.stdout)
        self.assertIn("claude-review.yml (acme/ci) current", current.stdout)

    def test_apply_down_plans_every_level_and_writes_nothing(self):
        self.repo("acme/ci", workflows={"vibe-verifier.yml": gate_workflow(self.old)})
        self.repo("acme/legacy", workflows={"claude-review.yml": legacy_caller("v1")})
        self.repo("acme/app", manifest="gitleaks\n")
        self.ruleset([(GATE, self.w1)])
        plan = self.apply_down("--owner", "acme")
        self.assertEqual(plan.returncode, 1, plan.stdout + plan.stderr)
        self.assertRegex(plan.stdout, r"acme/ci +%s +%s -> %s" % (re.escape(GATE), self.old[:10], self.new[:10]))
        self.assertRegex(plan.stdout, r"acme/legacy +%s +v1 -> %s" % (re.escape(LEGACY), self.w4[:10]))
        self.assertRegex(plan.stdout, r"ruleset acme/vibe-verifier-gates +%s +%s -> %s" % (re.escape(GATE), self.w1[:10], self.w4[:10]))
        self.assertIn("2 pull request(s) and 1 ruleset update(s)", plan.stdout)
        self.assertEqual([c for c in self.calls() if c["method"] != "GET"], [])

    def test_apply_down_confirm_moves_each_pin_where_it_lives(self):
        self.repo("acme/ci", workflows={"vibe-verifier.yml": gate_workflow(self.old)})
        self.repo("acme/legacy", workflows={"claude-review.yml": legacy_caller("v1")})
        self.repo("acme/app", manifest="gitleaks\n")
        ruleset = self.ruleset([(GATE, self.w1)])
        digest = re.search(r"--confirm (\w+)", self.apply_down("--owner", "acme").stdout).group(1)
        result = self.apply_down("--owner", "acme", "--confirm", digest)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        written = {c["endpoint"]: c.get("content") or c.get("body") for c in self.calls() if c["method"] == "PUT"}
        self.assertEqual(sorted(written), ["orgs/acme/rulesets/7", "repos/acme/ci/contents/" + GATE, "repos/acme/legacy/contents/" + LEGACY])
        self.assertEqual(written["repos/acme/ci/contents/" + GATE],
                         gate_workflow(self.old).replace("@%s # main 2026-09-23" % self.old, "@%s # main %s" % (self.new, self.date(self.source, self.new))))
        self.assertEqual(written["repos/acme/legacy/contents/" + LEGACY],
                         legacy_caller("v1").replace("@v1", "@%s # main %s" % (self.w4, self.date(self.wrapper, self.w4))))
        expected = copy.deepcopy(ruleset["rules"])
        expected[1]["parameters"]["workflows"][0]["sha"] = self.w4  # the wrapper's entry moves; the other repository's does not
        self.assertEqual(written["orgs/acme/rulesets/7"], {"rules": expected})
        self.assertEqual(len([c for c in self.calls() if c["method"] == "PR"]), 2)
        self.assertRegex(result.stdout, r"ruleset acme/vibe-verifier-gates +updated: vibe-verifier.yml")

    def test_a_wrapper_that_is_not_a_checkout_with_an_origin_is_refused(self):
        bare = make_repo(self, {"README.md": "1"})
        result = runner("consumers", "--source", self.source, "--source-ref", "main", "--wrapper", bare,
                        "--wrapper-ref", "main", "--repo", "acme/ci", env=self.env)
        self.assertEqual(result.returncode, 2)
        self.assertIn("not a checkout with an origin remote", result.stderr)

    def date(self, repo, sha):
        return subprocess.run(["git", "-C", repo, "log", "-1", "--date=short", "--format=%cd", sha],
                              capture_output=True, text=True, check=True).stdout.strip()


if __name__ == "__main__":
    unittest.main()
