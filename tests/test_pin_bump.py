"""apply-down --auto (gates/_pin_bump.py, actions/pin-bump): the scheduled bump, driven through the command line
against the fake `gh` that records writes."""
import json
import os
import shutil
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path

from helpers import ROOT, commit, make_repo, runner
from test_apply_down import stub_pinned_to
import _pin_bump  # noqa: E402  (gates/ is on the path through helpers)

LANE = "example-ci"


class PinBump(unittest.TestCase):
    def setUp(self):
        self.fixtures = tempfile.mkdtemp(prefix="vv-gh-")
        self.addCleanup(shutil.rmtree, self.fixtures, True)
        bin_dir = Path(self.fixtures) / "bin"
        bin_dir.mkdir()
        gh = bin_dir / "gh"
        gh.write_text("#!/bin/sh\nexec %s %s \"$@\"\n" % (os.environ.get("PYTHON", "python3"), ROOT / "tests" / "fake_gh.py"))
        gh.chmod(gh.stat().st_mode | stat.S_IEXEC)
        self.env = {"PATH": "%s:%s" % (bin_dir, os.environ["PATH"]), "FAKE_GH_ROOT": self.fixtures}
        self.source = make_repo(self, {"gates/a.py": "1", "tools/lint/package-lock.json": "1"})
        self.old = self.head()
        commit(self.source, {"gates/a.py": "2"}, "gate change")
        self.new = self.head()
        self.consumer("acme/app", stub_pinned_to(self.old))

    def head(self):
        return subprocess.run(["git", "-C", self.source, "rev-parse", "HEAD"], capture_output=True, text=True, check=True).stdout.strip()

    def consumer(self, repo, stub):
        base = Path(self.fixtures) / repo
        (base / ".github" / "workflows").mkdir(parents=True, exist_ok=True)
        (base / ".github" / "workflows" / "vibe-verifier.yml").write_text(stub)
        (base / ".vibe-verifier").write_text("no-duplicate-package-json-keys\n")

    def serve(self, repo, name, value):
        (Path(self.fixtures) / repo / name).write_text(json.dumps(value))

    def calls(self, method=None):
        log = Path(self.fixtures) / "calls.log"
        calls = [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []
        return [c for c in calls if method is None or c["method"] == method]

    def writes(self):
        return [c for c in self.calls() if c["method"] not in ("GET",)]

    def bump(self, *args):
        return runner("apply-down", "--source", self.source, "--source-ref", "main", "--repo", "acme/app",
                      "--auto", *args, env=self.env)

    def busy_lane(self):
        self.serve("acme/app", ".runs.json", {"queued": [{"id": 5, "event": "pull_request"}]})
        self.serve("acme/app", ".jobs.json", [{"status": "queued", "labels": [LANE]}])

    def test_without_act_it_prints_the_decision_and_writes_nothing(self):
        result = self.bump("--now")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("%s -> %s" % (self.old[:10], self.new[:10]), result.stdout)
        self.assertIn("bump now: asked to bump now. Dry run: nothing written", result.stdout)
        self.assertEqual(self.writes(), [])

    def test_with_act_and_now_it_opens_the_bump_apply_down_would(self):
        result = self.bump("--act", "--now")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("https://github.com/acme/app/pull/1", result.stdout)
        self.assertEqual(len(self.calls("PR")), 1)
        written = self.calls("PUT")
        self.assertEqual([c["endpoint"] for c in written], ["repos/acme/app/contents/.github/workflows/vibe-verifier.yml"])
        self.assertIn("gates@%s # main " % self.new, written[0]["content"])

    def test_a_busy_lane_waits_and_a_quiet_one_bumps(self):
        self.busy_lane()
        waiting = self.bump("--act", "--lane", LANE)
        self.assertEqual(waiting.returncode, 0, waiting.stdout + waiting.stderr)
        self.assertIn("waiting: the lane is busy: 0 run(s) in the last hour, 1 job(s) waiting for %s" % LANE, waiting.stdout)
        self.assertEqual(self.writes(), [])
        # A job on another lane, and the run this bump is part of, are not this lane's load.
        self.serve("acme/app", ".jobs.json", [{"status": "queued", "labels": ["another-lane"]}])
        quiet = self.bump("--act", "--lane", LANE)
        self.assertIn("bump now: the lane is quiet", quiet.stdout)
        self.assertEqual(len(self.calls("PR")), 1)

    def test_its_own_run_does_not_make_the_lane_busy(self):
        self.busy_lane()
        env = dict(self.env, GITHUB_REPOSITORY="acme/app", GITHUB_RUN_ID="5")
        result = runner("apply-down", "--source", self.source, "--source-ref", "main", "--repo", "acme/app",
                        "--auto", "--lane", LANE, env=env)
        self.assertIn("bump now: the lane is quiet", result.stdout)

    def test_many_recent_runs_are_busy_but_scheduled_ones_do_not_count(self):
        self.serve("acme/app", ".runs.json", {"recent": [{"id": n, "event": "pull_request"} for n in range(7)]})
        self.assertIn("waiting: the lane is busy: 7 runs started in the last hour", self.bump("--lane", LANE).stdout)
        self.serve("acme/app", ".runs.json", {"recent": [{"id": n, "event": "schedule"} for n in range(7)]})
        self.assertIn("bump now: the lane is quiet", self.bump("--lane", LANE).stdout)

    def test_an_overdue_bump_does_not_wait_for_the_lane(self):
        self.busy_lane()
        result = self.bump("--act", "--lane", LANE, "--overdue-hours", "0")
        self.assertIn("bump now: the oldest release commit waiting is", result.stdout)
        self.assertEqual(len(self.calls("PR")), 1)

    def test_a_bump_that_moves_a_tool_version_is_held_and_the_run_says_so(self):
        commit(self.source, {"tools/lint/package-lock.json": "2"}, "tool version")
        result = self.bump("--act", "--now")
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("acme/app", result.stdout)
        self.assertIn("held: moves tools/lint/package-lock.json, so sweep that gate with --all", result.stdout)
        self.assertEqual(self.writes(), [])
        # By hand, the same plan is only a note: a person runs it after the sweep.
        manual = runner("apply-down", "--source", self.source, "--source-ref", "main", "--repo", "acme/app", env=self.env)
        self.assertEqual(manual.returncode, 1)
        self.assertIn("note: moves tools/lint/package-lock.json", manual.stdout)
        self.assertIn("--confirm", manual.stdout)

    def test_a_pin_already_past_the_tool_move_is_not_held(self):
        commit(self.source, {"tools/lint/package-lock.json": "2"}, "tool version")
        tooled = self.head()
        commit(self.source, {"gates/a.py": "3"}, "gate change after it")
        self.consumer("acme/app", stub_pinned_to(tooled))
        result = self.bump("--act", "--now")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertNotIn("held", result.stdout)
        self.assertEqual(len(self.calls("PR")), 1)

    def test_an_older_open_bump_is_closed_as_superseded(self):
        def pull(number, ref, owner="acme"):
            return {"number": number, "html_url": "https://github.com/acme/app/pull/%d" % number,
                    "head": {"ref": ref, "repo": {"full_name": "%s/app" % owner}}}
        older = pull(3, "ci/vibe-verifier-pin-" + self.old[:10])
        fork = pull(4, "ci/vibe-verifier-pin-" + self.old[:10], owner="someone")
        other = pull(5, "feature/x")
        unknown = pull(6, "ci/vibe-verifier-pin-0123456789")  # not an ancestor of the new target: never closed
        # Past the first page of 100, where an older bump still has to be found.
        crowd = [pull(100 + n, "feature/%d" % n) for n in range(100)]
        self.serve("acme/app", ".open-prs.json", crowd + [older, fork, other, unknown])
        result = self.bump("--act", "--now")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        closed = self.calls("CLOSE")
        self.assertEqual([c["number"] for c in closed], ["3"])
        self.assertIn("Superseded by https://github.com/acme/app/pull/1", closed[0]["comment"])
        self.assertIn("closed https://github.com/acme/app/pull/3, superseded", result.stdout)

    def test_only_the_catalog_level_of_a_bump_is_held_for_a_tool_move(self):
        # A caller's pins of a wrapper's workflows move no catalog tool, whatever commits they cross.
        commit(self.source, {"tools/lint/package-lock.json": "2"}, "tool version")
        target = self.head()
        planned = [("acme/app", None, target, [("a.yml", [self.old], "")]),
                   ("acme/app", "acme/ci", "w" * 40, [("b.yml", [self.old], "")])]
        self.assertEqual(_pin_bump.tool_moves(self.source, planned, target), {"acme/app": ["tools/lint/package-lock.json"]})
        self.assertEqual(_pin_bump.tool_moves(self.source, planned[1:], target), {})

    def test_a_pin_the_catalog_history_does_not_have_is_held(self):
        # Its tool moves cannot be read, so it is not bumped blind.
        self.consumer("acme/app", stub_pinned_to("f" * 40))
        result = self.bump("--act", "--now")
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("held: moves (ffffffffff is not in the catalog's history)", result.stdout)
        self.assertEqual(self.writes(), [])

    def test_a_pin_that_is_not_a_sha_never_reaches_git_as_an_option(self):
        planted = Path(self.fixtures) / "planted"
        self.consumer("acme/app", stub_pinned_to(self.old).replace("@" + self.old, "@--output=%s" % planted))
        result = self.bump("--act", "--now")
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("held: moves (--output=/", result.stdout)
        self.assertFalse(planted.exists())
        self.assertFalse(_pin_bump.older_than(self.source, self.new)("--output=x"))

    def test_the_action_is_a_dry_run_unless_act_is_true_and_never_merges(self):
        action = (ROOT / "actions" / "pin-bump" / "action.yml").read_text()
        self.assertIn('[ "$ACT" = true ] && args+=(--act)', action)
        self.assertIn("apply-down --auto", action)
        self.assertNotIn("--confirm", action)
        self.assertIn('overdue-hours:\n    description: Bump once the oldest release commit waiting is this old, quiet or not.\n'
                      '    default: "6"', action)
        for text in (action, (ROOT / "gates" / "_pin_bump.py").read_text()):
            self.assertNotIn('"merge"', text)
            self.assertNotIn("pr merge", text)
        self.bump("--act", "--now")
        self.assertEqual({c["method"] for c in self.writes()}, {"POST", "PUT", "PR"})


if __name__ == "__main__":
    unittest.main()
