"""The review harness template: its prompt is the catalog's prompt byte for byte, the model runs
only when there is something new to review, the receipt is the workflow's, never the model's."""
import re
import unittest

from helpers import ROOT

TEMPLATE = ROOT / "harnesses" / "review" / "review.yml"
PROMPT = ROOT / "harnesses" / "review" / "prompt.md"


def prompt_block(text):
    match = re.search(r"^( +)prompt: \|\n((?:\1  .*\n|\n)+)", text, re.MULTILINE)
    indent = len(match.group(1)) + 2
    return "".join(line[indent:] if line.strip() else "\n" for line in match.group(2).splitlines(True))


class ReviewHarness(unittest.TestCase):
    def test_the_workflow_prompt_is_the_catalog_prompt(self):
        self.assertEqual(prompt_block(TEMPLATE.read_text()), PROMPT.read_text())

    def test_the_model_is_skipped_when_nothing_changed_and_never_tolerated_on_failure(self):
        text = TEMPLATE.read_text()
        review = text[text.index("- name: Review\n"):text.index("- name: Post the receipt")]
        self.assertIn("if: env.REVIEW_MODE != 'nochange'", review)
        self.assertNotIn("continue-on-error", text)

    def test_the_receipt_is_posted_by_the_workflow_after_the_review(self):
        text = TEMPLATE.read_text()
        self.assertLess(text.index("- name: Review\n"), text.index("- name: Post the receipt"))
        self.assertIn('--body "review-receipt: ${HEAD_SHA} -- ${REVIEW_MODE} -- run ${GITHUB_RUN_ID}"', text)
        self.assertNotIn("sentinel", prompt_block(text).lower().replace("post a\nsentinel", ""))

    def test_the_verify_job_runs_the_catalog_manifest_with_placeholder_pins(self):
        text = TEMPLATE.read_text()
        self.assertIn("manifest: .vibe-verifier-review", text)
        pins = re.findall(r"vibe-verifier/actions/\w+@(\S+)", text)
        self.assertEqual(pins, ["0" * 40])
        manifest = (ROOT / "harnesses" / "review" / "manifest").read_text()
        self.assertIn("review-receipt --receipt review-inputs/receipt.md --head review-inputs/head.txt --threads review-inputs/threads.json", manifest)


if __name__ == "__main__":
    unittest.main()
