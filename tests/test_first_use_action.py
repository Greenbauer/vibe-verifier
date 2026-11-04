"""actions/first-use: it writes the first-use walk's prompt from the body's `## First-use task`, says whether there is
a walk to run (`walk`), and removes the body, which holds the criteria, so the walk's session cannot read them in the
workspace (harnesses/first-use/README.md). Driven by running the action's step as a workflow runs it."""
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from helpers import ROOT, clean_env

ACTION = ROOT / "actions" / "first-use" / "action.yml"
BODY = ("## First-use task\n\nGive someone at Acme access to the portal.\n\n"
        "## Acceptance criteria\n\n- An admin can invite\n")


def action_script():
    match = re.search(r"\n      run: \|\n((?:        [^\n]*\n|\n)+)", ACTION.read_text())
    return "".join(line[8:] for line in match.group(1).splitlines(True))


class Action(unittest.TestCase):
    def run_action(self, body):
        work = Path(tempfile.mkdtemp(prefix="vv-first-use-action-"))
        self.addCleanup(shutil.rmtree, work, True)
        (work / "qae-inputs").mkdir()
        (work / "pr-body.md").write_text(body)
        output = work / "output"
        output.write_text("")
        env = clean_env({"GITHUB_ACTION_PATH": str(ACTION.parent), "GITHUB_OUTPUT": str(output),
                         "BODY": str(work / "pr-body.md"), "SITE": "http://localhost:3000",
                         "OUT": "qae-inputs/first-use-prompt.md"})
        result = subprocess.run(["bash", "-c", action_script()], cwd=work, capture_output=True, text=True, env=env)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        outputs = dict(line.split("=", 1) for line in output.read_text().splitlines())
        return work, outputs

    def test_a_task_writes_the_prompt_says_walk_and_takes_the_body_away(self):
        work, outputs = self.run_action(BODY)
        self.assertEqual(outputs, {"walk": "true"})
        prompt = (work / "qae-inputs" / "first-use-prompt.md").read_text()
        self.assertIn("Give someone at Acme access to the portal.", prompt)
        self.assertNotIn("An admin can invite", prompt)
        self.assertFalse((work / "pr-body.md").exists())

    def test_no_task_writes_no_prompt_says_no_walk_and_still_takes_the_body_away(self):
        work, outputs = self.run_action("## Acceptance criteria\n\n- An admin can invite\n")
        self.assertEqual(outputs, {"walk": "false"})
        self.assertFalse((work / "qae-inputs" / "first-use-prompt.md").exists())
        self.assertFalse((work / "pr-body.md").exists())


if __name__ == "__main__":
    unittest.main()
