"""apply-down --auto holds a review copy whose anthropics/claude-code-action is older than the review template's
(gates/_pin_review.py), driven through the command line against the fake `gh`, as test_pin_bump.py does."""
import unittest
from pathlib import Path

from helpers import commit
from test_apply_down import stub_pinned_to
from test_pin_bump import PinBumpHarness


class PinReview(PinBumpHarness):
    def review_copy(self, action_sha):
        return ("jobs:\n  review:\n    runs-on: x\n    steps:\n"
                "      - id: model\n        uses: Greenbauer/vibe-verifier/actions/review-model@%s # main x\n"
                "      - uses: anthropics/claude-code-action@%s # vX\n" % (self.old, action_sha))

    def test_a_review_copy_pinning_an_older_review_action_than_the_template_holds_the_bump(self):
        # The model moves with the pin; the action that runs it moves only when the copy is copied again.
        commit(self.source, {"actions/review-model/action.yml": "name: x\nruns:\n  using: composite\n",
                             "harnesses/review/review.yml": self.review_copy("b" * 40)}, "review model needs a newer action")
        self.consumer("acme/app", stub_pinned_to(self.head()))
        review = Path(self.fixtures) / "acme/app/.github/workflows/review.yml"
        review.write_text(self.review_copy("a" * 40))
        result = self.bump("--act", "--now")
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("held: .github/workflows/review.yml pins anthropics/claude-code-action at aaaaaaaaaa, but %s's "
                      "harnesses/review/review.yml pins bbbbbbbbbb: copy it again first" % self.head()[:10], result.stdout)
        self.assertEqual(self.writes(), [])
        review.write_text(self.review_copy("b" * 40))
        current = self.bump("--act", "--now")
        self.assertEqual(current.returncode, 0, current.stdout + current.stderr)
        self.assertEqual(len(self.calls("PR")), 1)


if __name__ == "__main__":
    unittest.main()
