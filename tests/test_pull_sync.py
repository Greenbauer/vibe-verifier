"""The pull-sync engine, through its command line, against a stand-in `gh` that serves one
repository's pull requests from a JSON file and records every write."""
import json
import os
import re
import stat
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from helpers import ROOT, clean_env

ENGINE = ROOT / "actions" / "pull-sync" / "pull_sync.py"
ACTION = ROOT / "actions" / "pull-sync" / "action.yml"

# Answers the two GraphQL reads from $FAKE/state.json, counts the reads of the pull requests in
# $FAKE/reads, and appends every other call to $FAKE/writes. "unknown_reads": n answers the first n
# of those reads with every pull request's `mergeable` UNKNOWN, as GitHub does right after a merge.
FAKE_GH = '''#!/usr/bin/env python3
import json, os, sys
root, argv = os.environ["FAKE"], sys.argv[1:]
state = json.load(open(os.path.join(root, "state.json")))
given = dict(a.split("=", 1) for a in argv if a.startswith(("owner=", "name=")))
if argv[:2] == ["api", "graphql"]:
    # A repository named under "repos" answers with its own state; any other, with the top level.
    state = state.get("repos", {}).get("%s/%s" % (given["owner"], given["name"]), state)
    if state.get("broken") or os.environ.get("FAKE_BROKEN"):
        sys.stderr.write("HTTP 502: Bad Gateway\\n")
        sys.exit(1)
    query = next(a for a in argv if a.startswith("query="))
    if "defaultBranchRef" in query:
        print(json.dumps({"data": {"repository": {"defaultBranchRef": {"name": "main"}}}}))
    else:
        with open(os.path.join(root, "reads"), "a") as handle:
            handle.write("read\\n")
        pulls = state["pulls"]
        if len(open(os.path.join(root, "reads")).readlines()) <= state.get("unknown_reads", 0):
            pulls = [dict(pull, mergeable="UNKNOWN") for pull in pulls]
        print(json.dumps({"data": {"repository": {
            "pullRequests": {"pageInfo": {"hasNextPage": state.get("more", False)}, "nodes": pulls}}}}))
    sys.exit(0)
method, endpoint = argv[argv.index("-X") + 1], next(a for a in argv[1:] if a.startswith("repos/"))
fields = [argv[i + 1] for i, a in enumerate(argv) if a == "-f"]
with open(os.path.join(root, "writes"), "a") as handle:
    handle.write(json.dumps([method, endpoint, fields]) + "\\n")
if endpoint in state.get("refuse", []):
    sys.stderr.write("HTTP 422: expected head sha didn't match current head ref.\\n")
    sys.exit(1)
'''

def ago(**delta):
    """A GitHub timestamp this long before the call. Read at the call, not at import: a full suite
    imports this module minutes before it runs it, and "29 minutes ago" had become 32."""
    return (datetime.now(timezone.utc) - timedelta(**delta)).strftime("%Y-%m-%dT%H:%M:%SZ")


def rollup(state, unfinished):
    """A head's checks: the rollup state, and how many check runs and commit statuses are in each state."""
    runs = {"IN_PROGRESS": unfinished if state != "EXPECTED" else 0, "SUCCESS": 3, "FAILURE": int(state == "FAILURE")}
    statuses = {"EXPECTED": unfinished if state == "EXPECTED" else 0, "SUCCESS": 1}
    return {"state": state, "contexts": {
        "checkRunCountsByState": [{"state": name, "count": count} for name, count in runs.items()],
        "statusContextCountsByState": [{"state": name, "count": count} for name, count in statuses.items()]}}


def pull(number, behind=3, checks="SUCCESS", draft=False, mergeable="MERGEABLE", labels=(), base="main",
         fork=False, updated=None, committed=None, head_ref=True, unfinished=None):
    """A pull request as the engine's query returns it: by default green, quiet for two days, 3 behind.
    `unfinished` is how many of its checks have not finished: one when the rollup says so, else none."""
    if unfinished is None:
        unfinished = int(checks in ("PENDING", "EXPECTED"))
    return {
        "number": number, "isDraft": draft, "isCrossRepository": fork, "mergeable": mergeable,
        "updatedAt": updated or ago(days=2), "baseRefName": base, "headRefOid": "%040x" % number,
        "labels": {"nodes": [{"name": name} for name in labels]},
        "headRef": {"compare": {"aheadBy": behind}} if head_ref else None,
        "commits": {"nodes": [{"commit": {"committedDate": committed or ago(days=2),
                                          "statusCheckRollup": rollup(checks, unfinished) if checks else None}}]},
    }


class PullSync(unittest.TestCase):
    def setUp(self):
        self.fake = tempfile.mkdtemp(prefix="vv-pull-sync-")
        self.addCleanup(lambda: subprocess.run(["rm", "-rf", self.fake]))
        stub = Path(self.fake, "bin", "gh")
        stub.parent.mkdir()
        stub.write_text(FAKE_GH)
        stub.chmod(stub.stat().st_mode | stat.S_IXUSR)

    def run_engine(self, pulls, *args, sync=("octo/demo",), **state):
        Path(self.fake, "state.json").write_text(json.dumps(dict(state, pulls=pulls)))
        env = clean_env({"FAKE": self.fake, "PATH": "%s%s%s" % (Path(self.fake, "bin"), os.pathsep, os.environ["PATH"])})
        named = [arg for repo in sync for arg in ("--repo", repo)]
        return subprocess.run([sys.executable, str(ENGINE), *named, "--settle-seconds", "0", *args],
                              capture_output=True, text=True, env=env)

    def reads(self):
        return len(Path(self.fake, "reads").read_text().splitlines())

    def writes(self):
        path = Path(self.fake, "writes")
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def rows(self, result):
        return dict(line.split(": ", 1) for line in result.stdout.splitlines()[1:])

    def test_a_dry_run_prints_the_plan_and_writes_nothing(self):
        result = self.run_engine([pull(7)])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("dry run, nothing written", result.stdout.splitlines()[0])
        self.assertEqual(self.rows(result), {"#7 sync": "ready, 3 behind main"})
        self.assertEqual(self.writes(), [])

    def test_acting_updates_a_ready_pull_request_at_the_head_it_judged(self):
        result = self.run_engine([pull(7)], "--act")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.writes(), [["PUT", "repos/octo/demo/pulls/7/update-branch",
                                          ["expected_head_sha=" + "%040x" % 7]]])

    def test_pull_requests_out_of_scope_are_left_alone(self):
        result = self.run_engine([
            pull(1, labels=["no-auto-sync"], mergeable="CONFLICTING"),
            pull(2, fork=True),
            pull(3, base="release"),
            pull(4, head_ref=False),
            pull(5, behind=0),
            pull(6, mergeable="UNKNOWN"),
            dict(pull(7), headRef={"compare": None}),
        ], "--act", "--max-in-flight", "9")
        self.assertEqual(self.rows(result), {
            "#1 skip": "label no-auto-sync",
            "#2 skip": "from a fork",
            "#3 skip": "into release, not main",
            "#4 skip": "its head branch is gone",
            "#5 skip": "up to date",
            "#6 skip": "GitHub has not said whether it merges cleanly",
            "#7 skip": "GitHub could not compare it with main",
        })
        self.assertEqual(self.writes(), [])

    def test_work_in_flight_is_not_interrupted(self):
        result = self.run_engine([
            pull(1, checks="PENDING"),
            pull(2, checks="EXPECTED"),
            pull(3, updated=ago(minutes=25)),
            pull(4, updated=ago(minutes=35)),
            # One check failed and two are still running: the rollup already says FAILURE.
            pull(5, checks="FAILURE", unfinished=2, committed=ago(days=3)),
        ], "--act", "--max-in-flight", "9")
        self.assertEqual(self.rows(result), {
            "#1 skip": "checks running",
            "#2 skip": "checks running",
            "#3 skip": "changed in the last 30 minutes",
            "#4 sync": "ready, 3 behind main",
            "#5 skip": "checks running",
        })
        self.assertIn("3 holding a slot", result.stdout.splitlines()[0])
        self.assertEqual([write[1] for write in self.writes()], ["repos/octo/demo/pulls/4/update-branch"])

    def test_a_head_too_new_for_its_checks_to_have_registered_holds_a_slot(self):
        # What the previous run's update looks like to a run that follows it within seconds.
        result = self.run_engine([pull(1, behind=0, checks=None, committed=ago(minutes=1)), pull(2), pull(3)], "--act")
        self.assertIn("1 holding a slot, 1 free slot(s)", result.stdout.splitlines()[0])
        self.assertEqual(self.rows(result)["#3 wait"], "ready, 3 behind main; no free slot")
        self.assertEqual([write[1] for write in self.writes()], ["repos/octo/demo/pulls/2/update-branch"])

    def test_the_run_reads_again_while_github_has_not_said_what_merges(self):
        result = self.run_engine([pull(1), pull(2, mergeable="CONFLICTING")], "--act", unknown_reads=2)
        self.assertEqual(self.rows(result), {"#1 sync": "ready, 3 behind main",
                                             "#2 conflict": "conflicts with main; adds label sync-conflict"})
        self.assertEqual(self.reads(), 3)

    def test_what_is_still_unknown_after_four_reads_is_left_alone(self):
        result = self.run_engine([pull(1), pull(2, base="release")], "--act", unknown_reads=99)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.rows(result)["#1 skip"], "GitHub has not said whether it merges cleanly")
        self.assertEqual(self.reads(), 4)
        self.assertEqual(self.writes(), [])

    def test_an_unknown_pull_request_out_of_scope_costs_no_second_read(self):
        self.run_engine([pull(1, mergeable="UNKNOWN", base="release"), pull(2)])
        self.assertEqual(self.reads(), 1)

    def test_a_pull_request_that_is_not_ready_is_updated_once_its_head_is_a_day_old(self):
        result = self.run_engine([
            pull(1, checks="FAILURE", committed=ago(hours=23)),
            pull(2, checks="FAILURE", committed=ago(hours=25)),
            pull(3, draft=True, committed=ago(hours=25)),
            pull(4, checks=None, committed=ago(hours=25)),
            pull(5, draft=True, committed=ago(hours=2)),
        ], "--max-in-flight", "9")
        self.assertEqual(self.rows(result), {
            "#1 skip": "not ready, and its head is newer than 24 hours",
            "#2 sync": "stale, 3 behind main",
            "#3 sync": "stale, 3 behind main",
            "#4 sync": "stale, 3 behind main",
            "#5 skip": "not ready, and its head is newer than 24 hours",
        })

    def test_slots_go_to_ready_pull_requests_first_and_count_checks_already_running(self):
        pulls = [
            pull(1, checks="FAILURE"),          # stale: listed first, served last
            pull(2, checks="PENDING"),          # holds one of the three slots
            pull(3),
            pull(4),
            pull(5),
            pull(6, base="release", checks="PENDING"),   # out of scope, and still holds a slot
        ]
        result = self.run_engine(pulls, "--act", "--max-in-flight", "3")
        self.assertIn("2 holding a slot, 1 free slot(s)", result.stdout.splitlines()[0])
        rows = self.rows(result)
        self.assertEqual(rows["#3 sync"], "ready, 3 behind main")
        self.assertEqual(rows["#4 wait"], "ready, 3 behind main; no free slot")
        self.assertEqual(rows["#5 wait"], "ready, 3 behind main; no free slot")
        self.assertEqual(rows["#1 wait"], "stale, 3 behind main; no free slot")
        self.assertEqual([write[1] for write in self.writes()], ["repos/octo/demo/pulls/3/update-branch"])

    def test_no_free_slot_means_no_update(self):
        result = self.run_engine([pull(1, checks="PENDING"), pull(2, checks="PENDING"), pull(3)], "--act")
        self.assertEqual(self.rows(result)["#3 wait"], "ready, 3 behind main; no free slot")
        self.assertEqual(self.writes(), [])

    def test_a_conflict_is_labelled_once_and_never_updated(self):
        result = self.run_engine([
            pull(1, mergeable="CONFLICTING"),
            pull(2, mergeable="CONFLICTING", labels=["sync-conflict"]),
        ], "--act")
        self.assertEqual(self.rows(result), {
            "#1 conflict": "conflicts with main; adds label sync-conflict",
            "#2 conflict": "conflicts with main",
        })
        self.assertEqual(self.writes(), [["POST", "repos/octo/demo/issues/1/labels", ["labels[]=sync-conflict"]]])

    def test_the_conflict_label_comes_off_once_the_pull_request_merges_cleanly(self):
        result = self.run_engine([
            pull(1, behind=0, labels=["sync-conflict"]),
            pull(2, labels=["sync-conflict"]),
            pull(3, mergeable="UNKNOWN", labels=["sync-conflict"]),
        ], "--act")
        self.assertEqual(self.rows(result), {
            "#1 skip": "up to date; removes label sync-conflict",
            "#2 sync": "ready, 3 behind main; removes label sync-conflict",
            "#3 skip": "GitHub has not said whether it merges cleanly",
        })
        self.assertEqual(sorted(write[:2] for write in self.writes()), [
            ["DELETE", "repos/octo/demo/issues/1/labels/sync-conflict"],
            ["DELETE", "repos/octo/demo/issues/2/labels/sync-conflict"],
            ["PUT", "repos/octo/demo/pulls/2/update-branch"],
        ])

    def test_a_refused_update_fails_the_run_and_the_rest_still_happen(self):
        result = self.run_engine([pull(1), pull(2)], "--act", refuse=["repos/octo/demo/pulls/1/update-branch"])
        self.assertEqual(result.returncode, 1)
        self.assertIn("octo/demo: #1 update failed: HTTP 422", result.stderr)
        self.assertEqual([write[1] for write in self.writes()],
                         ["repos/octo/demo/pulls/1/update-branch", "repos/octo/demo/pulls/2/update-branch"])

    def test_a_repository_that_cannot_be_read_has_no_plan(self):
        for state in ({"broken": True}, {"more": True}):
            with self.subTest(state=state):
                result = self.run_engine([pull(1)], "--act", **state)
                self.assertEqual(result.returncode, 2)
                self.assertIn("cannot read octo/demo", result.stderr)
                self.assertEqual(result.stdout, "")
                self.assertEqual(self.writes(), [])

    def test_each_repository_is_planned_whatever_happened_to_the_others(self):
        result = self.run_engine([pull(1)], "--act", sync=("octo/gone", "octo/demo", "octo/held"), repos={
            "octo/gone": {"broken": True},
            "octo/held": {"pulls": [pull(2), pull(3, checks="PENDING"), pull(4, checks="PENDING")]}})
        self.assertEqual(result.returncode, 2)
        self.assertIn("cannot read octo/gone", result.stderr)
        self.assertEqual(result.stdout.splitlines(), [
            "octo/demo: 1 open, 0 holding a slot, 2 free slot(s) (acting)",
            "#1 sync: ready, 3 behind main",
            "octo/held: 3 open, 2 holding a slot, 0 free slot(s) (acting)",
            "#2 wait: ready, 3 behind main; no free slot",
            "#3 skip: checks running",
            "#4 skip: checks running",
        ])
        self.assertEqual([write[1] for write in self.writes()], ["repos/octo/demo/pulls/1/update-branch"])

    def test_a_failed_write_in_one_repository_is_the_exit_code_of_the_run(self):
        result = self.run_engine([pull(1)], "--act", sync=("octo/demo", "octo/other"),
                                 refuse=["repos/octo/demo/pulls/1/update-branch"])
        self.assertEqual(result.returncode, 1)
        self.assertIn("octo/demo: #1 update failed", result.stderr)
        self.assertEqual([write[1] for write in self.writes()],
                         ["repos/octo/demo/pulls/1/update-branch", "repos/octo/other/pulls/1/update-branch"])

    def run_action(self, **inputs):
        """The action's own step, read from the shipped action.yml and run under bash as a runner would."""
        match = re.search(r"\n      run: \|\n((?:        [^\n]*\n|\n)+)", ACTION.read_text())
        script = "".join(line[8:] for line in match.group(1).splitlines(True))
        Path(self.fake, "state.json").write_text(json.dumps({"pulls": [pull(1)]}))
        env = clean_env(dict({"FAKE": self.fake, "GITHUB_ACTION_PATH": str(ACTION.parent),
                              "GITHUB_STEP_SUMMARY": str(Path(self.fake, "summary")),
                              "PATH": "%s%s%s" % (Path(self.fake, "bin"), os.pathsep, os.environ["PATH"]),
                              "REPOS": "octo/demo", "ACT": "false", "MAX_IN_FLIGHT": "2", "QUIET_MINUTES": "30",
                              "STALE_HOURS": "24"}, **inputs))
        return subprocess.run(["bash", "-c", script], cwd=self.fake, capture_output=True, text=True, env=env)

    def test_the_action_is_a_dry_run_unless_act_is_true(self):
        for act in ("false", "", "yes"):
            with self.subTest(act=act):
                result = self.run_action(ACT=act)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn("dry run, nothing written", result.stdout)
                self.assertEqual(self.writes(), [])
        self.assertIn("(acting)", self.run_action(ACT="true").stdout)
        self.assertEqual([write[1] for write in self.writes()], ["repos/octo/demo/pulls/1/update-branch"])

    def test_the_action_takes_a_list_of_repositories_and_never_expands_it_as_file_names(self):
        Path(self.fake, "stray-file").write_text("")
        result = self.run_action(REPOS="octo/demo\n  octo/other *")
        self.assertEqual(result.returncode, 0, result.stderr)
        heads = [line.split(":")[0] for line in result.stdout.splitlines() if " open, " in line]
        self.assertEqual(heads, ["octo/demo", "octo/other", "*"], "the * reached the engine as written")
        self.assertEqual(Path(self.fake, "summary").read_text(), result.stdout)

    def test_the_actions_exit_code_is_the_engines_through_the_pipe_to_the_summary(self):
        result = self.run_action(FAKE_BROKEN="1")
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn("cannot read octo/demo", result.stderr)

if __name__ == "__main__":
    unittest.main()
