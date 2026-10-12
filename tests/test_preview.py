"""The preview recorder and finder, through its command line and its action's step, against a
stand-in `gh` that serves a commit's check runs and statuses from a JSON file and records writes."""
import json
import os
import re
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from helpers import ROOT, clean_env

ENGINE = ROOT / "actions" / "preview" / "preview.py"
ACTION = ROOT / "actions" / "preview" / "action.yml"
REPO, SHA = "acme/app", "a" * 40

# Serves $FAKE/state.json. "runs" is a list of answers, one per read of the check's runs (the last
# repeats): each a list of [started_at, status, conclusion]. "statuses" is the commit's statuses.
# "fail_reads" fails that many reads first; "refuse_writes" fails every write. Writes go to
# $FAKE/writes, one JSON line each.
FAKE_GH = '''#!/usr/bin/env python3
import json, os, sys
root, argv = os.environ["FAKE"], sys.argv[1:]
state = json.load(open(os.path.join(root, "state.json")))
counter = os.path.join(root, "calls")
calls = int(open(counter).read()) if os.path.exists(counter) else 0
open(counter, "w").write(str(calls + 1))
if "-X" in argv:
    if state.get("refuse_writes"):
        sys.stderr.write("HTTP 403: Resource not accessible by integration\\n")
        sys.exit(1)
    with open(os.path.join(root, "writes"), "a") as handle:
        handle.write(json.dumps([argv[argv.index("-X") + 1], argv[3], json.load(sys.stdin)]) + "\\n")
    sys.exit(0)
if calls < state.get("fail_reads", 0):
    sys.stderr.write("HTTP 502: Bad Gateway\\n")
    sys.exit(1)
endpoint = argv[1]
if endpoint.endswith("/check-runs"):
    reads = os.path.join(root, "run-reads")
    n = int(open(reads).read()) if os.path.exists(reads) else 0
    open(reads, "w").write(str(n + 1))
    for run in state["runs"][min(n, len(state["runs"]) - 1)]:
        print("\\t".join(run))
elif endpoint.endswith("/statuses"):
    for status in state.get("statuses", []):
        if status["context"] == "preview":
            print("%s\\t%s" % (status["created_at"], status.get("target_url") or ""))
'''

PASSED = [["2026-10-12T15:00:00Z", "completed", "success"]]


class Preview(unittest.TestCase):
    def setUp(self):
        self.fake = Path(tempfile.mkdtemp())
        bin_dir = self.fake / "bin"
        bin_dir.mkdir()
        gh = bin_dir / "gh"
        gh.write_text(FAKE_GH)
        gh.chmod(gh.stat().st_mode | stat.S_IEXEC)
        self.env = clean_env({"FAKE": str(self.fake), "PATH": "%s:%s" % (bin_dir, os.environ["PATH"])})

    def serve(self, **state):
        (self.fake / "state.json").write_text(json.dumps(state))

    def run_engine(self, *args):
        return subprocess.run([sys.executable, str(ENGINE), *args, "--repo", REPO, "--sha", SHA, "--interval", "0"],
                              capture_output=True, text=True, env=self.env)

    def find(self, *extra):
        return self.run_engine("find", "--check", "sync-preview-branch", *extra)

    def writes(self):
        path = self.fake / "writes"
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def run_reads(self):
        return int((self.fake / "run-reads").read_text())

    def test_record_writes_a_preview_status_on_the_commit(self):
        self.serve(runs=[[]])
        result = self.run_engine("record", "--url", "https://app-git-x.vercel.app/")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.writes(), [["POST", "repos/%s/statuses/%s" % (REPO, SHA), {
            "state": "success", "context": "preview", "target_url": "https://app-git-x.vercel.app",
            "description": "The preview of this commit under test"}]])

    def test_record_refuses_an_address_that_is_not_https_and_writes_nothing(self):
        self.serve(runs=[[]])
        result = self.run_engine("record", "--url", "app.vercel.app")
        self.assertEqual(result.returncode, 1)
        self.assertIn("::error::", result.stdout)
        self.assertEqual(self.writes(), [])

    def test_a_record_refused_three_times_fails_naming_the_permission(self):
        self.serve(runs=[[]], refuse_writes=True)
        result = self.run_engine("record", "--url", "https://app.vercel.app")
        self.assertEqual(result.returncode, 1)
        self.assertIn("statuses: write", result.stdout)
        self.assertEqual(int((self.fake / "calls").read_text()), 3)

    def test_find_prints_the_newest_recorded_address_once_the_check_passes(self):
        self.serve(runs=[PASSED], statuses=[
            {"context": "preview", "created_at": "2026-10-12T15:01:00Z", "target_url": "https://first.vercel.app"},
            {"context": "preview", "created_at": "2026-10-12T16:30:00Z", "target_url": "https://resync.vercel.app"},
            {"context": "ci", "created_at": "2026-10-12T17:00:00Z", "target_url": "https://elsewhere.example"}])
        result = self.find()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(result.stdout, "https://resync.vercel.app\n")

    def test_find_waits_while_the_check_runs(self):
        running = [["2026-10-12T15:00:00Z", "in_progress", ""]]
        self.serve(runs=[running, running, PASSED],
                   statuses=[{"context": "preview", "created_at": "2026-10-12T15:01:00Z", "target_url": "https://p.vercel.app"}])
        result = self.find()
        self.assertEqual(result.stdout, "https://p.vercel.app\n")
        self.assertEqual(self.run_reads(), 3)

    def test_a_failed_check_means_no_preview(self):
        self.serve(runs=[[["2026-10-12T15:00:00Z", "completed", "failure"]]])
        result = self.find()
        self.assertEqual(result.returncode, 1)
        self.assertIn('"sync-preview-branch" ended in failure', result.stdout)

    def test_a_newer_skipped_run_does_not_hide_the_real_one(self):
        self.serve(runs=[[["2026-10-12T15:05:00Z", "completed", "skipped"], PASSED[0]]],
                   statuses=[{"context": "preview", "created_at": "2026-10-12T15:01:00Z", "target_url": "https://p.vercel.app"}])
        self.assertEqual(self.find().stdout, "https://p.vercel.app\n")

    def test_a_failed_run_behind_a_newer_skipped_one_is_still_red(self):
        self.serve(runs=[[["2026-10-12T15:05:00Z", "completed", "skipped"], ["2026-10-12T15:00:00Z", "completed", "cancelled"]]])
        self.assertIn("ended in cancelled", self.find().stdout)

    def test_only_skipped_runs_fail_after_three_minutes_of_polls(self):
        self.serve(runs=[[["2026-10-12T15:05:00Z", "completed", "skipped"]]])
        result = self.find()
        self.assertEqual(result.returncode, 1)
        self.assertIn("only skipped runs", result.stdout)
        self.assertEqual(self.run_reads(), 6)
        self.find("--poll-seconds", "10")
        self.assertEqual(self.run_reads(), 6 + 18)

    def test_a_check_that_never_passes_fails_at_the_end_of_the_wait(self):
        self.serve(runs=[[]])
        result = self.find("--wait-minutes", "1")
        self.assertEqual(result.returncode, 1)
        self.assertIn("did not pass", result.stdout)
        self.assertIn("ran for about 0 of them", result.stdout)
        self.assertEqual(self.run_reads(), 2)

    def test_only_running_time_counts_against_the_running_budget(self):
        queued = [["2026-10-12T15:00:00Z", "queued", ""]]
        running = [["2026-10-12T15:00:00Z", "in_progress", ""]]
        # Queued for ten minutes, then running for two and passing: inside a 3-minute running budget.
        self.serve(runs=[queued] * 20 + [running] * 4 + [PASSED],
                   statuses=[{"context": "preview", "created_at": "2026-10-12T15:01:00Z", "target_url": "https://p.vercel.app"}])
        self.assertEqual(self.find("--running-minutes", "3").stdout, "https://p.vercel.app\n")

    def test_a_check_running_past_its_budget_fails(self):
        self.serve(runs=[[["2026-10-12T15:00:00Z", "in_progress", ""]]])
        result = self.find("--running-minutes", "2")
        self.assertEqual(result.returncode, 1)
        self.assertIn("ran for 2 minutes", result.stdout)
        self.assertEqual(self.run_reads(), 5)

    def test_a_passed_check_with_no_address_fails_at_once_naming_the_recorder(self):
        self.serve(runs=[PASSED], statuses=[{"context": "ci", "created_at": "2026-10-12T15:01:00Z", "target_url": "https://x"}])
        result = self.find()
        self.assertEqual(result.returncode, 1)
        self.assertIn("recorded no `preview` status", result.stdout)
        self.assertIn("will not help", result.stdout)
        self.assertEqual(self.run_reads(), 1)

    def test_five_failed_reads_in_a_row_are_survived(self):
        self.serve(runs=[PASSED], fail_reads=5,
                   statuses=[{"context": "preview", "created_at": "2026-10-12T15:01:00Z", "target_url": "https://p.vercel.app"}])
        self.assertEqual(self.find().stdout, "https://p.vercel.app\n")

    def test_six_failed_reads_in_a_row_exit_2_naming_the_call(self):
        self.serve(runs=[PASSED], fail_reads=6)
        result = self.find()
        self.assertEqual(result.returncode, 2)
        self.assertIn("check-runs failed 6 times in a row", result.stdout)

    def action_step(self, **inputs):
        """Run the action's one step as GitHub would, with these inputs as its env."""
        script = re.search(r"      run: \|\n(.*)", ACTION.read_text(), re.S).group(1)
        script = "\n".join(line[8:] for line in script.splitlines())
        output = self.fake / "output"
        output.write_text("")
        env = dict(self.env, GITHUB_ACTION_PATH=str(ACTION.parent), GITHUB_OUTPUT=str(output),
                   SHA=SHA, REPO=REPO, WAIT_MINUTES="1", RECORD="", CHECK="")
        env.update(inputs)
        return subprocess.run(["bash", "-c", script], capture_output=True, text=True, env=env), output.read_text()

    def test_the_action_outputs_the_address_it_found(self):
        self.serve(runs=[PASSED], statuses=[{"context": "preview", "created_at": "2026-10-12T15:01:00Z", "target_url": "https://p.vercel.app"}])
        result, output = self.action_step(CHECK="sync-preview-branch")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(output, "url=https://p.vercel.app\n")

    def test_the_action_shows_why_a_find_failed(self):
        self.serve(runs=[[["2026-10-12T15:00:00Z", "completed", "failure"]]])
        result, output = self.action_step(CHECK="sync-preview-branch")
        self.assertEqual(result.returncode, 1)
        self.assertIn('::error::"sync-preview-branch" ended in failure', result.stdout)
        self.assertEqual(output, "")

    def test_the_action_records_when_given_an_address(self):
        self.serve(runs=[[]])
        result, _ = self.action_step(RECORD="https://p.vercel.app")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.writes()[0][2]["target_url"], "https://p.vercel.app")


if __name__ == "__main__":
    unittest.main()
