"""actions/features: the composite action the explore job runs, which runs `vibe-verifier features` and hands
the job its count, driven through the action's own script against a temp repository (tests/test_features.py)."""
import os
import re
import subprocess
import unittest
from pathlib import Path

from helpers import ROOT, clean_env, make_repo, write
from test_features import BASE, QAE


class Action(unittest.TestCase):
    """actions/features runs the command and hands the explore job its count."""

    def run_action(self, repo, changed, entries=""):
        text = (ROOT / "actions" / "features" / "action.yml").read_text()
        script = re.search(r"^      run: \|\n((?:        .*\n|\n)+)", text, re.MULTILINE).group(1)
        script = "".join(line[8:] if line.strip() else "\n" for line in script.splitlines(True))
        output = os.path.join(repo, ".git", "github-output")
        Path(output).write_text("")
        env = clean_env({"GITHUB_ACTION_PATH": str(ROOT / "actions" / "features"), "GITHUB_OUTPUT": output,
                         "RUNNER_TEMP": os.path.join(repo, ".git"), "VV_MANIFEST": ".vibe-verifier-qae",
                         "VV_ENTRIES": entries, "VV_CHANGED": changed, "VV_OUT": "qae-inputs/features"})
        result = subprocess.run(["bash", "-e", "-c", script], cwd=repo, capture_output=True, text=True, env=env)
        return result, Path(output).read_text()

    def test_the_count_and_the_copies(self):
        repo = make_repo(self, BASE)
        write(repo, {"changed": "app/auth/login.ts\n"})
        result, output = self.run_action(repo, "changed")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(output, "count=1\n")
        self.assertTrue(os.path.isfile(os.path.join(repo, "qae-inputs", "features", "auth.md")))
        self.assertIn("- auth: app/auth/login.ts\n", Path(repo, "qae-inputs", "features.md").read_text())

    def test_off_is_a_count_of_zero_and_a_wrapper_passes_its_list(self):
        repo = make_repo(self, dict(BASE, **{".vibe-verifier-qae": "acceptance-verdict --criteria a --verdict b\n"}))
        write(repo, {"changed": "app/auth/login.ts\n"})
        self.assertEqual(self.run_action(repo, "changed")[1], "count=0\n")
        result, output = self.run_action(repo, "changed", entries=QAE)
        self.assertEqual((result.returncode, output), (0, "count=1\n"), result.stdout + result.stderr)

    def test_a_command_that_cannot_run_fails_the_step(self):
        repo = make_repo(self, BASE)
        result, output = self.run_action(repo, "missing-file")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(output, "")


if __name__ == "__main__":
    unittest.main()
