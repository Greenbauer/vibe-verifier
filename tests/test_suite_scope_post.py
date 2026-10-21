"""The suite-scope action's `post` input, through its own step and test_suite_scope.py's stand-in `gh`: a value
with a new line in it is one context per line, so a status named as docs/check-names.md names it, with spaces,
is one context; a value on one line is split on spaces, as it always was."""
import unittest

from test_suite_scope import HEAD, Harness


class SuiteScopePost(Harness):
    def posted(self, value):
        self.serve([])
        result, _ = self.action_step(POST=value, SHA=HEAD, REASON="Not applicable: docs only")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return [write[1]["context"] for write in self.writes()]

    def test_a_block_of_one_line_posts_a_name_with_spaces_as_one_context(self):
        self.assertEqual(self.posted("Preview / E2E tests\n"), ["Preview / E2E tests"])

    def test_a_block_is_one_context_per_line_trimmed_with_blank_lines_skipped(self):
        self.assertEqual(self.posted("  Preview / E2E tests  \n\n\tPreview / E2E tests: running\n"),
                         ["Preview / E2E tests", "Preview / E2E tests: running"])

    def test_a_value_on_one_line_is_split_on_spaces_as_before(self):
        self.assertEqual(self.posted("preview-e2e  browser-regressions"), ["preview-e2e", "browser-regressions"])


if __name__ == "__main__":
    unittest.main()
