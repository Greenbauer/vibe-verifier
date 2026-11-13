"""The QAE templates' token scopes. The explorer posts nothing: its verdict reaches the pull request only through the
verify job's QA review (actions/qa-review), so the verify job is the one job that may write to a pull request, in both
lanes. The explore job reads the body and the changed files, and nothing in it writes."""
import unittest

from helpers import ROOT

TEMPLATES = ("explore.yml", "explore-codex.yml")


def job(text, name, following):
    return text[text.index("\n  %s:\n" % name):text.index("\n  %s:\n" % following)]


class Permissions(unittest.TestCase):
    def test_only_the_verify_job_may_write_to_a_pull_request(self):
        for template in TEMPLATES:
            with self.subTest(template=template):
                text = (ROOT / "harnesses" / "qae" / template).read_text()
                self.assertIn("      pull-requests: read   # reads the body and changed files",
                              job(text, "explore", "verify"))
                verify = text[text.index("\n  verify:\n"):]
                self.assertIn("      pull-requests: write   # the QA review comment, and nothing else\n", verify)
                self.assertEqual(text.count("pull-requests: write"), 1)
                self.assertNotIn("issues: write", text)


if __name__ == "__main__":
    unittest.main()
