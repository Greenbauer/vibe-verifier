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
# "fail_reads" fails that many reads first, "fail_calls" the reads at those call numbers (from 0);
# "refuse_writes" fails every write. Writes go to $FAKE/writes, one JSON line each, and every call's
# argv to $FAKE/argv. A check-runs read that does not page through every run, or does not filter by
# the check's name, is refused like a broken call, so no test passes on a read that would miss runs.
FAKE_GH = '''#!/usr/bin/env python3
import json, os, sys
root, argv = os.environ["FAKE"], sys.argv[1:]
state = json.load(open(os.path.join(root, "state.json")))
counter = os.path.join(root, "calls")
calls = int(open(counter).read()) if os.path.exists(counter) else 0
open(counter, "w").write(str(calls + 1))
with open(os.path.join(root, "argv"), "a") as handle:
    handle.write(json.dumps(argv) + "\\n")
if "-X" in argv:
    if state.get("refuse_writes"):
        sys.stderr.write("HTTP 403: Resource not accessible by integration\\n")
        sys.exit(1)
    with open(os.path.join(root, "writes"), "a") as handle:
        handle.write(json.dumps([argv[argv.index("-X") + 1], argv[3], json.load(sys.stdin)]) + "\\n")
    sys.exit(0)
if calls < state.get("fail_reads", 0) or calls in state.get("fail_calls", []):
    sys.stderr.write("HTTP 502: Bad Gateway\\n")
    sys.exit(1)
endpoint = argv[1]
if endpoint.endswith("/check-runs"):
    if "--paginate" not in argv or "check_name=sync-preview-branch" not in argv:
        sys.stderr.write("unpaged or unfiltered check-runs read\\n")
        sys.exit(9)
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
ADDRESS = {"context": "preview", "created_at": "2026-10-12T15:01:00Z", "target_url": "https://p.vercel.app"}


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

    def test_a_timed_out_check_means_no_preview(self):
        self.serve(runs=[[["2026-10-12T15:00:00Z", "completed", "timed_out"]]])
        result = self.find()
        self.assertEqual(result.returncode, 1)
        self.assertIn('"sync-preview-branch" ended in timed_out', result.stdout)

    def test_a_newer_success_beats_an_older_failure(self):
        self.serve(runs=[[["2026-10-12T14:00:00Z", "completed", "failure"], PASSED[0]]], statuses=[ADDRESS])
        self.assertEqual(self.find().stdout, "https://p.vercel.app\n")

    def test_the_check_runs_are_read_by_name_on_every_page(self):
        self.serve(runs=[PASSED], statuses=[ADDRESS])
        self.assertEqual(self.find().returncode, 0)
        first = json.loads((self.fake / "argv").read_text().splitlines()[0])
        self.assertEqual(first[:2], ["api", "repos/%s/commits/%s/check-runs" % (REPO, SHA)])
        for flag in ("--paginate", "check_name=sync-preview-branch", "per_page=100"):
            self.assertIn(flag, first)

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

    def test_a_wait_that_ends_with_the_check_queued_or_running_exits_3(self):
        for status in ("queued", "in_progress"):
            with self.subTest(status):
                self.serve(runs=[[["2026-10-12T15:00:00Z", status, "null"]]])
                result = self.find("--wait-minutes", "1")
                self.assertEqual(result.returncode, 3)
                self.assertIn("did not pass", result.stdout)

    def test_a_failed_last_read_keeps_the_answer_before_it(self):
        self.serve(runs=[[["2026-10-12T15:00:00Z", "in_progress", "null"]]], fail_calls=[1])
        result = self.find("--wait-minutes", "1")
        self.assertEqual(result.returncode, 3)
        self.assertIn("(last seen: unread)", result.stdout)

    def test_a_wait_that_ends_with_no_run_to_wait_for_exits_1(self):
        # No run of the check, and a run that ended without passing: neither is a run a longer wait
        # would see pass. (A run past its running budget is exit 1 above, before the wait ends.)
        for runs in ([[]], [[["2026-10-12T15:00:00Z", "completed", "neutral"]]]):
            with self.subTest(runs=runs):
                self.serve(runs=runs)
                self.assertEqual(self.find("--wait-minutes", "1").returncode, 1)

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

    def test_an_answer_starts_the_failed_read_count_again(self):
        # Five failed reads, an answer (still running), five more, then the pass: ten failures in
        # all, never six in a row.
        running = [["2026-10-12T15:00:00Z", "in_progress", ""]]
        self.serve(runs=[running, PASSED], fail_calls=[0, 1, 2, 3, 4, 6, 7, 8, 9, 10], statuses=[ADDRESS])
        result = self.find()
        self.assertEqual(result.stdout, "https://p.vercel.app\n", result.stderr)

    def test_failed_reads_neither_stretch_the_wait_nor_count_as_running(self):
        # A one-minute wait is two polls, failed or not, and a failed poll is not a running one.
        self.serve(runs=[[]], fail_calls=[0])
        result = self.find("--wait-minutes", "1", "--running-minutes", "1")
        self.assertIn("did not pass", result.stdout)
        self.assertIn("ran for about 0 of them", result.stdout)
        self.assertEqual(int((self.fake / "calls").read_text()), 2)

    def test_a_failed_check_is_still_red_after_a_failed_read(self):
        self.serve(runs=[[["2026-10-12T15:00:00Z", "completed", "failure"]]], fail_calls=[0])
        self.assertIn("ended in failure", self.find().stdout)

    def test_a_failed_statuses_read_after_the_pass_is_tried_again(self):
        self.serve(runs=[PASSED], fail_calls=[1], statuses=[ADDRESS])
        result = self.find()
        self.assertEqual(result.stdout, "https://p.vercel.app\n")
        self.assertIn("/statuses failed (1 in a row)", result.stderr)

    def test_a_record_with_no_address_is_passed_over(self):
        self.serve(runs=[PASSED], statuses=[ADDRESS, {"context": "preview", "created_at": "2026-10-12T16:00:00Z", "target_url": None}])
        self.assertEqual(self.find().stdout, "https://p.vercel.app\n")

    def test_the_wait_says_where_the_check_was_last_seen(self):
        self.serve(runs=[[["2026-10-12T15:00:00Z", "queued", ""]]])
        self.assertIn("(last seen: queued", self.find("--wait-minutes", "1").stdout)

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
                   SHA=SHA, REPO=REPO, WAIT_MINUTES="1", RECORD="", CHECK="", HANDOVER="false")
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

    def test_a_handover_passes_with_no_address_when_the_check_is_still_running(self):
        self.serve(runs=[[["2026-10-12T15:00:00Z", "in_progress", "null"]]])
        result, output = self.action_step(CHECK="sync-preview-branch", HANDOVER="true")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('::notice::"sync-preview-branch" did not pass', result.stdout)
        self.assertNotIn("::error::", result.stdout)
        self.assertEqual(output, "url=\n")

    def test_without_a_handover_a_check_still_running_fails(self):
        self.serve(runs=[[["2026-10-12T15:00:00Z", "in_progress", "null"]]])
        result, output = self.action_step(CHECK="sync-preview-branch")
        self.assertEqual(result.returncode, 3)
        self.assertIn('::error::"sync-preview-branch" did not pass', result.stdout)
        self.assertEqual(output, "")

    def test_a_handover_still_fails_a_check_that_failed_or_never_ran(self):
        for runs, said in (([[["2026-10-12T15:00:00Z", "completed", "failure"]]], "ended in failure"), ([[]], "did not pass")):
            with self.subTest(said):
                self.serve(runs=runs)
                result, output = self.action_step(CHECK="sync-preview-branch", HANDOVER="true")
                self.assertEqual(result.returncode, 1)
                self.assertIn("::error::", result.stdout)
                self.assertIn(said, result.stdout)
                self.assertEqual(output, "")

    def test_the_action_records_when_given_an_address(self):
        self.serve(runs=[[]])
        result, _ = self.action_step(RECORD="https://p.vercel.app")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.writes()[0][2]["target_url"], "https://p.vercel.app")


if __name__ == "__main__":
    unittest.main()
