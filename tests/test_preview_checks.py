"""The preview action's own step, and a check named several ways (one name per line, as a check being
renamed is: docs/check-names.md), against test_preview's stand-in gh."""
import unittest

from test_preview import ADDRESS, DATABASE, PASSED, Harness

BOTH = "sync-preview-branch\nDatabase"
RUNNING = [["2026-10-12T15:00:00Z", "in_progress", "null"]]
FAILED = [["2026-10-12T15:00:00Z", "completed", "failure"]]


class Checks(Harness, unittest.TestCase):
    def find_both(self, **runs_of):
        """find with both names, each answered with its own runs (none when not given)."""
        self.serve(runs=[[]], checks=BOTH.splitlines(), runs_of=runs_of, statuses=[ADDRESS])
        return self.run_engine("find", "--check", BOTH, "--wait-minutes", "1")

    def test_a_pass_under_either_name_finds_the_address(self):
        for runs_of in ({"sync-preview-branch": PASSED}, {"Database": PASSED}):
            with self.subTest(runs_of=list(runs_of)):
                result = self.find_both(**runs_of)
                self.assertEqual((result.returncode, result.stdout), (0, "https://p.vercel.app\n"), result.stderr)

    def test_the_runs_of_every_name_are_one_checks_and_the_newest_decides(self):
        newer_pass = [["2026-10-12T16:00:00Z", "completed", "success"]]
        result = self.find_both(**{"sync-preview-branch": FAILED, "Database": newer_pass})
        self.assertEqual(result.returncode, 0, result.stdout)
        newer_failure = [["2026-10-12T16:00:00Z", "completed", "failure"]]
        result = self.find_both(**{"sync-preview-branch": PASSED, "Database": newer_failure})
        self.assertEqual(result.returncode, 1)
        self.assertIn('::error::"sync-preview-branch" or "Database" ended in failure', result.stdout)

    def test_every_message_names_each_name(self):
        result = self.find_both(Database=RUNNING)
        self.assertEqual(result.returncode, 3)
        self.assertIn('::error::"sync-preview-branch" or "Database" did not pass', result.stdout)
        result = self.find_both()
        self.assertEqual(result.returncode, 1)
        self.assertIn('"sync-preview-branch" or "Database" did not pass', result.stdout)

    def test_each_name_is_read_on_every_poll_and_a_failed_read_of_either_is_no_answer(self):
        self.serve(runs=[[]], checks=BOTH.splitlines(), runs_of={"Database": PASSED}, statuses=[ADDRESS], fail_calls=[0])
        result = self.run_engine("find", "--check", BOTH)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        # Poll 1: the first name's read failed, so no verdict; poll 2 reads both and passes.
        self.assertEqual(self.run_reads(), 3)

    def test_the_action_passes_every_name_and_a_single_line_is_one_name(self):
        self.serve(runs=[[]], checks=["sync-preview-branch", "Preview / Database"],
                   runs_of={"Preview / Database": PASSED}, statuses=[ADDRESS])
        result, output = self.action_step(CHECK="sync-preview-branch\nPreview / Database\n")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(output, "url=https://p.vercel.app\ndatabase=\n")
        result, output = self.action_step(CHECK="Preview / Database")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(output, "url=https://p.vercel.app\ndatabase=\n")

    def test_find_reads_the_statuses_under_their_new_names_too(self):
        # docs/check-names.md renames them; a record step that posts the new names must still be found.
        self.serve(runs=[PASSED], statuses=[
            {"context": "preview", "created_at": "2026-10-12T15:01:00Z", "target_url": "https://old.vercel.app"},
            {"context": "Preview / Address", "created_at": "2026-10-12T16:00:00Z", "target_url": "https://new.vercel.app"},
            {"context": "Preview / Database address", "created_at": "2026-10-12T16:00:00Z",
             "target_url": "https://%s.supabase.co" % ("b" * 20)},
            {"context": "view", "created_at": "2026-10-12T17:00:00Z", "target_url": "https://substring.example"}])
        result = self.run_engine("find", "--check", "sync-preview-branch")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(result.stdout, "https://new.vercel.app\n%s\n" % ("b" * 20))

    def test_the_action_refuses_a_check_of_blank_lines_at_once(self):
        result, _ = self.action_step(CHECK="\n  \n")
        self.assertEqual(result.returncode, 1)
        self.assertIn("::error::to find a preview, name the check that records it (check:)", result.stdout)

    def test_the_action_outputs_the_address_it_found(self):
        self.serve(runs=[PASSED], statuses=[{"context": "preview", "created_at": "2026-10-12T15:01:00Z", "target_url": "https://p.vercel.app"}])
        result, output = self.action_step(CHECK="sync-preview-branch")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(output, "url=https://p.vercel.app\ndatabase=\n")

    def test_the_action_outputs_the_database_recorded_with_the_address(self):
        self.serve(runs=[PASSED], statuses=[ADDRESS, {"context": "preview-database", "created_at": "2026-10-12T15:00:30Z",
                                                      "target_url": "https://%s.supabase.co" % DATABASE}])
        result, output = self.action_step(CHECK="sync-preview-branch")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(output, "url=https://p.vercel.app\ndatabase=%s\n" % DATABASE)
        self.assertIn("reading database %s" % DATABASE, result.stdout)

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
        self.assertEqual(output, "url=\ndatabase=\n")

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

    def test_the_action_records_the_database_when_given_one(self):
        self.serve(runs=[[]])
        result, _ = self.action_step(RECORD="https://p.vercel.app", DATABASE=DATABASE)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual([w[2]["context"] for w in self.writes()], ["preview-database", "preview"])

if __name__ == "__main__":
    unittest.main()
