"""The qae-browser composite action's step, run under bash as a runner would, with the tool install
and playwright stubbed: it installs chromium with its system packages where the job can install
them (root, or passwordless sudo, as on GitHub's hosted runners) and the browser alone on a runner
whose user cannot sudo, where those packages are the runner's own (harnesses/qae/README.md). Its
script is read from the shipped action.yml, so the test judges what a consumer pins."""
import os
import re
import subprocess
import tempfile
import unittest
from pathlib import Path

from helpers import ROOT, clean_env

ACTION = ROOT / "actions" / "qae-browser" / "action.yml"


def script():
    match = re.search(r"\n      run: \|\n((?:        [^\n]*\n|\n)+)", ACTION.read_text())
    return "".join(line[8:] for line in match.group(1).splitlines(True))


class QaeBrowserAction(unittest.TestCase):
    def run_action(self, sudo_exit):
        temp = Path(tempfile.mkdtemp(prefix="vv-qae-browser-"))
        tool = temp / "tool"
        (tool / "node_modules" / ".bin").mkdir(parents=True)
        (tool / "node_modules" / "@playwright" / "mcp").mkdir(parents=True)
        (tool / "node_modules" / "@playwright" / "mcp" / "cli.js").write_text("")
        calls = temp / "playwright-calls"
        playwright = tool / "node_modules" / ".bin" / "playwright"
        playwright.write_text('#!/bin/sh\nprintf "%%s\\n" "$*" >> "%s"\n' % calls)
        playwright.chmod(0o755)
        # The action resolves bin/vibe-verifier relative to itself; the stub answers the tool dir.
        action_path = temp / "actions" / "qae-browser"
        action_path.mkdir(parents=True)
        (temp / "bin").mkdir()
        (temp / "bin" / "vibe-verifier").write_text("import sys; print(%r)\n" % str(tool))
        # sudo on PATH first: -n true answers with the exit the runner would give.
        path_dir = temp / "path"
        path_dir.mkdir()
        (path_dir / "sudo").write_text("#!/bin/sh\nexit %d\n" % sudo_exit)
        (path_dir / "sudo").chmod(0o755)
        output = temp / "output"
        output.write_text("")
        env = clean_env({"GITHUB_ACTION_PATH": str(action_path), "GITHUB_OUTPUT": str(output),
                         "PATH": str(path_dir) + os.pathsep + os.environ["PATH"]})
        result = subprocess.run(["bash", "-c", script()], cwd=temp, capture_output=True, text=True, env=env)
        return result, calls.read_text() if calls.exists() else "", output.read_text()

    def test_where_sudo_needs_no_password_the_system_packages_are_installed_too(self):
        result, calls, output = self.run_action(sudo_exit=0)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(calls, "install --with-deps chromium\n")
        self.assertRegex(output, r"^mcp=.*/node_modules/@playwright/mcp/cli\.js\n$")

    def test_a_runner_whose_user_cannot_sudo_gets_the_browser_alone(self):
        # A password prompt would stop the job; the packages are the runner's own there.
        result, calls, output = self.run_action(sudo_exit=1)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(calls, "install chromium\n")
        self.assertRegex(output, r"^mcp=.*/node_modules/@playwright/mcp/cli\.js\n$")


if __name__ == "__main__":
    unittest.main()
