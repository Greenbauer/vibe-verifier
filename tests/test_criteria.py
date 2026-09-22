"""`vibe-verifier criteria`: the one-line answer a harness keys its skip on."""
import os
import tempfile
import unittest

from helpers import runner

BODY = "## Why\n\nx\n\n## Acceptance criteria\n\n%s\n"


class Criteria(unittest.TestCase):
    def doc(self, text):
        handle, path = tempfile.mkstemp(prefix="vv-body-", suffix=".md")
        os.write(handle, text.encode()); os.close(handle)
        self.addCleanup(os.remove, path)
        return path

    def test_declared_none(self):
        result = runner("criteria", self.doc(BODY % "- None: a CI step; no rendered surface."))
        self.assertEqual((result.returncode, result.stdout), (0, "none: a CI step; no rendered surface.\n"))

    def test_counts_criteria(self):
        result = runner("criteria", self.doc(BODY % "- AC one\n- AC two\n- AC three"))
        self.assertEqual((result.returncode, result.stdout), (0, "criteria: 3\n"))

    def test_a_bare_none_or_an_absent_section_is_zero_not_declared(self):
        self.assertEqual(runner("criteria", self.doc(BODY % "- None")).stdout, "criteria: 1\n")
        self.assertEqual(runner("criteria", self.doc("## Why\n\nno section\n")).stdout, "criteria: 0\n")

    def test_unreadable_file_cannot_run(self):
        result = runner("criteria", "/nonexistent/pr-body.md")
        self.assertEqual(result.returncode, 2)
        self.assertIn("could not read", result.stderr)


if __name__ == "__main__":
    unittest.main()
