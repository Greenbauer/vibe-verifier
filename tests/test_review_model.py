"""The review model is written once, in actions/review-model, and the review workflow reads it at its
catalog pin: no review workflow types a model, so a new model reaches every subscribed copy when its
pin moves (harnesses/review/README.md, "The model follows the pin")."""
import re
import subprocess
import tempfile
import unittest
from pathlib import Path

from helpers import ROOT, clean_env

ACTION = ROOT / "actions" / "review-model" / "action.yml"
TEMPLATE = ROOT / "harnesses" / "review" / "review.yml"
OWN = ROOT / ".github" / "workflows" / "review.yml"
PINNED = "uses: Greenbauer/vibe-verifier/actions/review-model@" + "0" * 40 + " # CONSUMER: pin the commit you subscribe to"
LOCAL = "uses: ./actions/review-model # zizmor: ignore[self-repository]"


def model_step(path):
    """The workflow's step that names the model, up to the review step it comes before."""
    text = path.read_text()
    return text[text.index("      - name: Name the review model\n"):text.index("      - name: Review\n")]


class ReviewModel(unittest.TestCase):
    def test_the_action_outputs_the_one_model_it_names(self):
        text = ACTION.read_text()
        named = re.findall(r"^ +REVIEW_MODEL: (\S+)$", text, re.MULTILINE)
        self.assertEqual(len(named), 1, named)
        self.assertRegex(named[0], r"^claude-[a-z0-9]+(-[a-z0-9]+)*$")
        script = re.search(r"^      run: \|\n((?:        .*\n)+)", text, re.MULTILINE).group(1)
        script = "".join(line[8:] for line in script.splitlines(True))
        with tempfile.TemporaryDirectory(prefix="vv-model-") as temp:
            output = Path(temp) / "github-output"
            output.write_text("")
            result = subprocess.run(["bash", "-e", "-c", script], capture_output=True, text=True,
                                    env=clean_env({"REVIEW_MODEL": named[0], "GITHUB_OUTPUT": str(output)}))
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertEqual(output.read_text(), "name=%s\n" % named[0])
        self.assertEqual(result.stdout, "Code review model: %s\n" % named[0])
        self.assertIn("value: ${{ steps.model.outputs.name }}", text)

    def test_both_accounts_review_on_the_model_the_action_names_and_no_workflow_types_one(self):
        for path in (TEMPLATE, OWN):
            text = path.read_text()
            self.assertEqual(re.findall(r"^ +--model (.*)$", text, re.MULTILINE),
                             ["${{ steps.model.outputs.name }}"] * 2, path)
            self.assertEqual(text.count("--model"), 2, path)

    def test_the_model_is_named_before_the_review_and_only_when_a_review_runs(self):
        for path in (TEMPLATE, OWN):
            step = model_step(path)
            self.assertIn("        id: model\n", step, path)
            self.assertIn("        if: env.REVIEW_MODE != 'nochange'\n", step, path)

    def test_a_copy_pins_the_action_and_the_source_repository_runs_its_own(self):
        self.assertIn("        %s\n" % PINNED, model_step(TEMPLATE))
        self.assertIn("        %s\n" % LOCAL, model_step(OWN))
        self.assertEqual(model_step(TEMPLATE).replace(PINNED, "uses: MODEL_ACTION"),
                         model_step(OWN).replace(LOCAL, "uses: MODEL_ACTION"))


if __name__ == "__main__":
    unittest.main()
