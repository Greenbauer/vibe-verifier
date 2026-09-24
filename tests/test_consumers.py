"""The consumers inventory, driven through the command line against a fake `gh`."""
import json
import os
import re
import shutil
import stat
import tempfile
import unittest
from pathlib import Path

from helpers import ROOT, commit, git, make_repo, runner

TEMPLATE = (ROOT / "consumer" / "vibe-verifier.yml").read_text()
PIN = re.compile(r"(vibe-verifier/actions/gates@)[0-9a-f]{40}[^\n]*")


def stub_pinned_to(sha):
    return PIN.sub(r"\g<1>%s # main 2026-09-17" % sha, TEMPLATE)


class Consumers(unittest.TestCase):
    def setUp(self):
        # A fake `gh` first on PATH, serving files from a fixture tree.
        self.fixtures = tempfile.mkdtemp(prefix="vv-gh-")
        self.addCleanup(shutil.rmtree, self.fixtures, True)
        bin_dir = Path(self.fixtures) / "bin"
        bin_dir.mkdir()
        gh = bin_dir / "gh"
        gh.write_text("#!/bin/sh\nexec %s %s \"$@\"\n" % (os.environ.get("PYTHON", "python3"), ROOT / "tests" / "fake_gh.py"))
        gh.chmod(gh.stat().st_mode | stat.S_IEXEC)
        self.env = {"PATH": "%s:%s" % (bin_dir, os.environ["PATH"]), "FAKE_GH_ROOT": self.fixtures}
        # A stand-in for this repository's history: a release commit, a gate change, then docs only.
        self.source = make_repo(self, {"gates/a.py": "1", "docs/x.md": "1"})
        self.old = self.head()
        commit(self.source, {"gates/a.py": "2"}, "gate change")
        self.release = self.head()
        commit(self.source, {"docs/x.md": "2"}, "docs only")
        self.new = self.head()

    def head(self):
        import subprocess
        return subprocess.run(["git", "-C", self.source, "rev-parse", "HEAD"], capture_output=True, text=True, check=True).stdout.strip()

    def consumer(self, repo, manifest="no-duplicate-package-json-keys\n", stub=None, workflows=None, native=None):
        base = Path(self.fixtures) / repo
        base.mkdir(parents=True, exist_ok=True)
        if native is not None:  # absent means fully enforced; see tests/fake_gh.py
            (base / ".native").write_text(json.dumps(native))
        if manifest is not None:
            (base / ".vibe-verifier").write_text(manifest)
        if stub is not None:
            (base / ".github" / "workflows").mkdir(parents=True, exist_ok=True)
            (base / ".github" / "workflows" / "vibe-verifier.yml").write_text(stub)
        for name, text in (workflows or {}).items():
            (base / ".github" / "workflows" / name).write_text(text)

    def check(self, *args):
        return runner("consumers", "--source", self.source, "--source-ref", "main", *args, env=self.env)

    def test_a_review_harness_copy_is_current_only_when_it_matches_the_template_outside_the_pin(self):
        review = (ROOT / "harnesses" / "review" / "review.yml").read_text()
        pinned = re.sub(r"(vibe-verifier/actions/gates@)[0-9a-f]{40}[^\n]*", r"\g<1>%s # main 2026-09-22" % self.new, review)
        self.consumer("acme/app", stub=stub_pinned_to(self.new), workflows={"review.yml": pinned})
        result = self.check("--repo", "acme/app")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("review.yml current", result.stdout)
        self.consumer("acme/app", workflows={"review.yml": pinned.replace("--max-turns ${{ env.REVIEW_MAX_TURNS }}", "--max-turns 5")})
        result = self.check("--repo", "acme/app")
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("review.yml current drifts from harnesses/review/review.yml outside the pin line", result.stdout)

    def test_current_consumer_passes(self):
        self.consumer("acme/app", stub=stub_pinned_to(self.new))
        result = self.check("--repo", "acme/app")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("pin: current", result.stdout)
        self.assertIn("stub: ok", result.stdout)
        self.assertIn("manifest: ok", result.stdout)

    def test_pin_behind_a_release_commit_is_stale(self):
        self.consumer("acme/app", stub=stub_pinned_to(self.old))
        result = self.check("--repo", "acme/app")
        self.assertEqual(result.returncode, 1)
        self.assertIn("stale (1 release commit behind)", result.stdout)

    def test_pin_behind_docs_only_is_still_current(self):
        self.consumer("acme/app", stub=stub_pinned_to(self.release))
        result = self.check("--repo", "acme/app")
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertIn("pin: current", result.stdout)

    def test_pin_outside_the_history_is_unknown(self):
        self.consumer("acme/app", stub=stub_pinned_to("f" * 40))
        result = self.check("--repo", "acme/app")
        self.assertEqual(result.returncode, 1)
        self.assertIn("unknown (not in main)", result.stdout)

    def test_other_workflow_pins_are_inventoried(self):
        # A harness copy pins gates@ (verify) and criteria@ (explore); both are judged like the stub's.
        harness = lambda sha: ("jobs:\n  explore:\n    steps:\n      - uses: Greenbauer/vibe-verifier/actions/criteria@%s # main x\n"
                               "  verify:\n    steps:\n      - uses: Greenbauer/vibe-verifier/actions/gates@%s # main x\n" % (sha, sha))
        self.consumer("acme/app", stub=stub_pinned_to(self.new), workflows={"qae-explore.yml": harness(self.new), "ci.yml": "jobs: {}\n"})
        result = self.check("--repo", "acme/app")
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertIn("workflows: qae-explore.yml current", result.stdout)
        self.assertNotIn("ci.yml", result.stdout)
        self.consumer("acme/app", stub=stub_pinned_to(self.new), workflows={"qae-explore.yml": harness(self.old)})
        result = self.check("--repo", "acme/app", "--format", "github")
        self.assertEqual(result.returncode, 1)
        self.assertIn("pin: current", result.stdout)
        self.assertIn("workflows: qae-explore.yml stale (1 release commit behind)", result.stdout)
        self.assertIn("::error title=vibe-verifier consumers::acme/app: qae-explore.yml stale", result.stdout)

    def test_stub_that_differs_outside_the_pin_line_is_drift(self):
        self.consumer("acme/app", stub=stub_pinned_to(self.new).replace("timeout-minutes: 5", "timeout-minutes: 50"))
        result = self.check("--repo", "acme/app")
        self.assertEqual(result.returncode, 1)
        self.assertIn("stub: drift", result.stdout)

    def test_unknown_gate_in_manifest(self):
        self.consumer("acme/app", manifest="no-duplicate-package-json-keys\nnot-a-gate\n", stub=stub_pinned_to(self.new))
        result = self.check("--repo", "acme/app")
        self.assertEqual(result.returncode, 1)
        self.assertIn("unknown gate not-a-gate", result.stdout)

    def test_named_repo_that_never_subscribed_needs_action(self):
        self.consumer("acme/app", manifest=None)
        result = self.check("--repo", "acme/app")
        self.assertEqual(result.returncode, 1)
        self.assertIn("not subscribed", result.stdout)

    def test_owner_discovery_skips_repos_that_never_subscribed(self):
        self.consumer("acme/app", stub=stub_pinned_to(self.new))
        self.consumer("acme/brochure", manifest=None)
        result = self.check("--owner", "acme")
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertIn("acme/app", result.stdout)
        self.assertNotIn("acme/brochure", result.stdout)

    def test_the_source_repository_is_a_row_with_its_stub_and_manifest_skipped(self):
        # The source repository dogfoods through its own ci.yml and carries no stub, so only its
        # native state is judged; it is listed so every repository the catalog governs is watched.
        git(self.source, "remote", "add", "origin", "https://github.com/acme/vibe-verifier.git")
        self.consumer("acme/app", stub=stub_pinned_to(self.new))
        self.consumer("acme/vibe-verifier", manifest="branch-name-length --max 60\n")
        discovered = self.check("--owner", "acme")
        self.assertEqual(discovered.returncode, 0, discovered.stdout)
        self.assertIn("acme/vibe-verifier", discovered.stdout)
        self.assertIn("stub: source", discovered.stdout)
        self.assertIn("manifest: source", discovered.stdout)
        # Its native state is judged like any consumer's.
        self.consumer("acme/vibe-verifier", manifest=None, native={"alerts": False})
        off = self.check("--owner", "acme")
        self.assertEqual(off.returncode, 1, off.stdout)
        self.assertIn("alerts OFF", off.stdout)

    def test_native_enforcement_is_reported_and_the_pattern_list_is_not_judged(self):
        self.consumer("acme/app", stub=stub_pinned_to(self.new),
                      native={"patterns_allowed": ["anthropics/claude-code-action@*", "oven-sh/setup-bun@*"]})
        result = self.check("--repo", "acme/app")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("native: sha-pin on, actions selected [anthropics/claude-code-action@* oven-sh/setup-bun@*], alerts on",
                      result.stdout)
        # GitHub holds the only copy of the list, so a widened one is visible and does not flag.
        self.consumer("acme/app", native={"patterns_allowed": ["anthropics/claude-code-action@*", "evil/action@*"]})
        widened = self.check("--repo", "acme/app")
        self.assertEqual(widened.returncode, 0, widened.stdout)
        self.assertIn("evil/action@*", widened.stdout)

    def test_actions_allowed_from_anywhere_needs_action(self):
        self.consumer("acme/app", stub=stub_pinned_to(self.new), native={"allowed_actions": "all"})
        result = self.check("--repo", "acme/app", "--format", "github")
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertIn("native: sha-pin on, actions all, alerts on", result.stdout)
        self.assertIn("::error title=vibe-verifier consumers::acme/app: allowed_actions is all, not selected", result.stdout)

    def test_sha_pinning_off_needs_action(self):
        self.consumer("acme/app", stub=stub_pinned_to(self.new), native={"sha_pinning_required": False})
        result = self.check("--repo", "acme/app", "--format", "github")
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertIn("::error title=vibe-verifier consumers::acme/app: sha_pinning_required is off", result.stdout)

    def test_dependabot_alerts_off_needs_action(self):
        self.consumer("acme/app", stub=stub_pinned_to(self.new), native={"alerts": False})
        result = self.check("--repo", "acme/app", "--format", "github")
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertIn("native: sha-pin on, actions selected", result.stdout)
        self.assertIn("::error title=vibe-verifier consumers::acme/app: Dependabot alerts are off", result.stdout)

    def test_unreadable_repo_is_could_not_run_even_when_others_are_fine(self):
        self.consumer("acme/app", stub=stub_pinned_to(self.new))
        self.consumer("acme/broken", stub=stub_pinned_to(self.new))
        (Path(self.fixtures) / "acme" / "broken" / ".broken").write_text("")
        result = self.check("--repo", "acme/app", "--repo", "acme/broken")
        self.assertEqual(result.returncode, 2)
        self.assertIn("could not read", result.stdout)

    def test_github_format_annotates_each_problem(self):
        self.consumer("acme/app", manifest="not-a-gate\n", stub=stub_pinned_to(self.old))
        result = self.check("--repo", "acme/app", "--format", "github")
        self.assertIn("::error title=vibe-verifier consumers::acme/app: stale", result.stdout)
        self.assertIn("::error title=vibe-verifier consumers::acme/app: unknown gate not-a-gate", result.stdout)

    def test_no_repositories_is_could_not_run(self):
        self.assertEqual(self.check().returncode, 2)


if __name__ == "__main__":
    unittest.main()
