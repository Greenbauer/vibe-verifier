"""pull-sync's --needs-update statuses (actions/pull-sync/pull_sync_reads.py builds the query that reads
them), through the engine's command line and the action's own step, against test_pull_sync's stand-in gh."""
import json
import unittest
from pathlib import Path

from test_pull_sync import Harness, ago, pull

# A status being renamed (docs/check-names.md) is posted under its old name on some heads and its new one
# on others, or under both while an organization ruleset still requires the old one.
BOTH = "merge-safety\nMerge / Safe to merge"


class NeedsUpdate(Harness, unittest.TestCase):
    def test_a_failed_needs_update_status_alone_does_not_make_a_pull_request_red(self):
        # merge-safety fails exactly when a pull request needs an update. #1's only failure is that
        # status: ready. #2 also failed a check run: red, so it waits until its head is a day old. #3
        # failed the status with other checks still running: left alone like any running one here.
        # #4 passed the status and failed a check run: red.
        pulls = [pull(1, checks="FAILURE", needs_update="FAILURE", committed=ago(hours=2)),
                 pull(2, checks="FAILURE", needs_update="FAILURE", failed_runs=1, committed=ago(hours=2)),
                 pull(3, checks="FAILURE", needs_update="FAILURE", unfinished=2, committed=ago(hours=2)),
                 pull(4, checks="FAILURE", needs_update="SUCCESS", committed=ago(hours=2))]
        result = self.run_engine(pulls, "--act", "--max-in-flight", "9", "--needs-update", "merge-safety")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("a failed merge-safety asks for an update", result.stdout.splitlines()[0])
        self.assertEqual(self.rows(result), {
            "#1 sync": "ready, 3 behind main",
            "#2 skip": "not ready, and its head is newer than 24 hours",
            "#3 skip": "checks running",
            "#4 skip": "not ready, and its head is newer than 24 hours",
        })
        self.assertEqual([write[1] for write in self.writes()], ["repos/octo/demo/pulls/1/update-branch"])

    def test_with_two_names_either_failing_is_the_update_and_any_other_failure_is_still_red(self):
        # #1 failed the second name, the one the head posts after its rename. #2 failed the old one. #3
        # posts both, failed both, and nothing else: still only the update. #4 failed the new name and a
        # check run: red. #5 failed a status neither name is: red.
        old, new = BOTH.splitlines()
        pulls = [pull(1, checks="FAILURE", needs_update={new: "FAILURE"}, committed=ago(hours=2)),
                 pull(2, checks="FAILURE", needs_update={old: "FAILURE"}, committed=ago(hours=2)),
                 pull(3, checks="FAILURE", needs_update={old: "FAILURE", new: "FAILURE"}, committed=ago(hours=2)),
                 pull(4, checks="FAILURE", needs_update={new: "FAILURE"}, failed_runs=1, committed=ago(hours=2)),
                 pull(5, checks="FAILURE", needs_update={"lint-status": "FAILURE"}, committed=ago(hours=2))]
        result = self.run_engine(pulls, "--max-in-flight", "9", "--needs-update", BOTH)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("a failed merge-safety or Merge / Safe to merge asks for an update", result.stdout.splitlines()[0])
        self.assertEqual(self.rows(result), {
            "#1 sync": "ready, 3 behind main",
            "#2 sync": "ready, 3 behind main",
            "#3 sync": "ready, 3 behind main",
            "#4 skip": "not ready, and its head is newer than 24 hours",
            "#5 skip": "not ready, and its head is newer than 24 hours",
        })

    def test_without_the_option_a_failed_needs_update_status_is_a_failure_like_any_other(self):
        for args in ((), ("--needs-update", "another-check"), ("--needs-update", "another-check\nlint-status")):
            with self.subTest(args=args):
                result = self.run_engine([pull(1, checks="FAILURE", needs_update="FAILURE", committed=ago(hours=2))], *args)
                self.assertEqual(self.rows(result), {"#1 skip": "not ready, and its head is newer than 24 hours"})

    def test_where_main_merges_only_up_to_date_heads_a_failed_needs_update_status_is_not_already_red(self):
        result = self.run_engine([
            pull(1, checks="FAILURE", needs_update="FAILURE", unfinished=2, committed=ago(hours=1)),
            pull(2, checks="FAILURE", needs_update="FAILURE", failed_runs=1, unfinished=2, committed=ago(hours=1)),
        ], "--needs-update", "merge-safety", classic=True)
        self.assertEqual(self.rows(result), {
            "#1 sync": "checks running on a head 3 behind main, which merges only an up-to-date head",
            "#2 skip": "checks running, and it is already red",
        })

    def test_a_name_given_twice_is_read_once_so_another_failure_stays_red(self):
        # GitHub counts the failed status once; read twice, it would cancel the check run's failure too.
        failed = [pull(1, checks="FAILURE", needs_update="FAILURE", failed_runs=1, committed=ago(hours=2))]
        result = self.run_engine(failed, "--needs-update", "merge-safety\nmerge-safety")
        self.assertEqual(self.rows(result), {"#1 skip": "not ready, and its head is newer than 24 hours"})

    def test_statuses_are_read_only_when_a_needs_update_status_is_named_one_alias_each(self):
        # GitHub refuses the field to a token without statuses read, even for an empty name.
        for args, aliases in (((), []), (("--needs-update", "merge-safety"), ["c0"]),
                              (("--needs-update", "\n" + BOTH + "\n\n"), ["c0", "c1"])):
            with self.subTest(args=args):
                Path(self.fake, "queries").unlink(missing_ok=True)
                self.run_engine([pull(1)], *args)
                [(query, given)] = [json.loads(line) for line in Path(self.fake, "queries").read_text().splitlines()]
                self.assertEqual([name for name in given if name not in ("owner", "name")], aliases)
                self.assertEqual("status{" in query, bool(aliases))
                for alias in aliases:
                    self.assertIn("%s:context(name:$%s){state}" % (alias, alias), query)
                    self.assertIn(",$%s:String!" % alias, query)

    def test_the_action_passes_needs_update_to_the_engine_one_name_per_line(self):
        failed = [pull(1, checks="FAILURE", needs_update="FAILURE", committed=ago(hours=2))]
        self.assertIn("#1 sync: ready", self.run_action(pulls=failed, NEEDS_UPDATE="merge-safety").stdout)
        self.assertIn("#1 skip: not ready", self.run_action(pulls=failed, NEEDS_UPDATE="").stdout)
        renamed = [pull(1, checks="FAILURE", needs_update={"Merge / Safe to merge": "FAILURE"}, committed=ago(hours=2))]
        self.assertIn("#1 sync: ready", self.run_action(pulls=renamed, NEEDS_UPDATE=BOTH).stdout)
        # One line is one name, spaces and all, as before.
        self.assertIn("#1 sync: ready", self.run_action(pulls=renamed, NEEDS_UPDATE="Merge / Safe to merge").stdout)


if __name__ == "__main__":
    unittest.main()
