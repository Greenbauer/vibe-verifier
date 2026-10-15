"""The explore templates' write-scope step judges each path as written. It reads `git status -z`: plain
porcelain quotes a path holding a space or a non-ASCII byte, and a consumer's explorer failed its job on an
artifact inside qae-artifacts/ whose name ended in its tag ("<record> qae-<run>-e2.pdf", 2026-10-15)."""
import unittest

from helpers import git, write
import test_qae_explore


class QuotedPaths(unittest.TestCase):
    setUp = test_qae_explore.WriteScope.setUp
    run_step = test_qae_explore.WriteScope.run_step

    def test_an_artifact_whose_name_git_would_quote_is_in_scope(self):
        # A consumer's explorer saved "qae-artifacts/<record> qae-<run>-e2.pdf": plain porcelain quotes a
        # path with a space, so the scope check read it as outside qae-artifacts/ and failed the job.
        write(self.repo, {"qae-artifacts/profile qae-38600000000.1-e2.pdf": "pdf", "qae-artifacts/caf\u00e9.png": "",
                          'qae-artifacts/the "draft" quote.png': ""})
        result = self.run_step()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("write scope held", result.stdout)

    def test_a_quoted_name_outside_the_scope_still_fails_and_is_named(self):
        write(self.repo, {"notes from the explorer.txt": "x"})
        result = self.run_step()
        self.assertEqual(result.returncode, 1)
        self.assertIn("?? notes from the explorer.txt", result.stdout)

    def test_a_rename_out_of_the_scope_names_both_paths(self):
        git(self.repo, "mv", "app.js", "moved.js")
        result = self.run_step()
        self.assertEqual(result.returncode, 1)
        self.assertIn("moved.js <- app.js", result.stdout)
