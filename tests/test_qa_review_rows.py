"""bin/cli/qa_review_rows.py: the QA review's walk tables, rendered through `vibe-verifier qa-review` against the fake
`gh` (tests/qa_review_fixture.py). The everyday rows are in tests/test_qa_review.py; these are the verdict lines that
are not one clear PASS or FAIL with a sentence and an anchor."""
import unittest

from qa_review_fixture import VERDICT, GhFixture


class Rows(GhFixture):
    def test_a_line_that_is_not_one_clear_verdict_says_no_verdict(self):
        # Two lines for one criterion, or a line with neither PASS nor FAIL first: the row says so, never a guess.
        self.verdict = self.file("verdict.md", VERDICT + "acceptance-check: AC2 -- PASS -- opened (qae/AC2.md::step 2: x)\n")
        self.assertIn("| ⚠️ | **AC2** · The menu opens | No verdict: the explorer wrote more than one line for it. |\n",
                      self.posted(gates="failure"))
        self.verdict = self.file("verdict.md", VERDICT.splitlines()[0] + "\nacceptance-check: AC2 -- the menu opened "
                                               "(qae/AC2.md::step 1: clicked Menu -> a list)\n")
        self.assertIn("| ⚠️ | **AC2** · The menu opens | No verdict: the explorer's line names neither PASS nor FAIL. It says: "
                      "the menu opened |\n", self.posted(gates="failure"))

    def test_a_pass_with_no_sentence_or_a_loose_anchor_still_reads(self):
        # A PASS with only an anchor, a full stop after the anchor, and an anchor with no step part.
        self.verdict = self.file("verdict.md", "acceptance-check: AC1 -- PASS -- (qae/AC1.md::step 1: opened /)\n"
                                               "acceptance-check: AC2 -- FAIL -- stayed shut (qae/AC2.md).\n")
        body = self.posted(gates="failure")
        self.assertIn("| ✅ | **AC1** · The home page loads | The explorer gave no sentence. |\n", body)
        self.assertIn("| ❌ | **AC2** · The menu opens | stayed shut |\n", body)
        self.assertIn("- **AC2**: `qae/AC2.md`\n", body)
        # A sentence with no anchor keeps its own ending.
        self.verdict = self.file("verdict.md", "acceptance-check: AC1 -- PASS -- Saw the page...\n")
        self.assertIn("| ✅ | **AC1** · The home page loads | Saw the page... |\n", self.posted(gates="failure"))


if __name__ == "__main__":
    unittest.main()
