"""Every gate the QAE manifest template offers passes a run that walked nothing, on the files the verify job writes for
it. A `- None:` pull request runs no explorer, so the verify job leaves an empty site file, an empty verdict and empty
evidence; a wrapper that still runs the gates must get a pass from each, not "could not run". The test reads the
manifest template, so a gate line added to it is held to this before it ships (ui-audit and copy-audit were not: #294)."""
import os
import re
import subprocess
import sys
import unittest
from pathlib import Path

from helpers import ROOT, clean_env, make_repo
from test_qae_explore import step_script, stub_bin

MANIFEST = ROOT / "harnesses" / "qae" / "manifest"
# A gate line, live or commented out for a lane to switch on: `<gate> --arg ...` or `# <gate> --arg ...`.
GATE_LINE = re.compile(r"^(?:# )?([a-z][a-z-]+) (--.*)$")


def offered_gates(text):
    """The manifest template's gate lines, each with the `# ` of a line a lane uncomments taken off."""
    return [match.group(1) + " " + match.group(2) for match in map(GATE_LINE.match, text.splitlines()) if match]


class NothingWalked(unittest.TestCase):
    def test_the_template_offers_the_gates_this_holds(self):
        names = [line.split()[0] for line in offered_gates(MANIFEST.read_text())]
        # A parser that found fewer would hold fewer gates; a new line needs no edit here.
        self.assertLessEqual({"acceptance-verdict", "qae-artifacts", "ui-audit", "copy-audit", "ticket-coverage"}, set(names))

    def test_every_offered_gate_passes_a_run_that_walked_nothing(self):
        repo = make_repo(self, {"README.md": "docs\n"})
        gh = stub_bin(self, {"gh": 'case "$*" in\n'
                                   '  pr*) printf "## Acceptance criteria\\n\\n- None: a docs-only change\\n" ;;\n'
                                   '  *pulls*) printf "README.md\\n" ;;\n'
                                   'esac\n'})
        # The verify job's own step, as the template has it, with the explore job's outputs a skipped job leaves.
        script = step_script("- name: Write the declared inputs", "- name: Run the QA gates")
        written = subprocess.run(["bash", "-e", "-c", script], cwd=repo, capture_output=True, text=True,
                                 env=clean_env({"PATH": gh + os.pathsep + os.environ["PATH"], "GH_TOKEN": "x",
                                                "PR_NUMBER": "7", "REPO": "o/r", "SITE_URL": "", "SITE_ORIGINS": "",
                                                "REFERENCES": "", "TICKET": "", "CARRY": ""}))
        self.assertEqual(written.returncode, 0, written.stdout + written.stderr)
        self.assertEqual(Path(repo, "qae-inputs", "site-url").read_text(), "\n")
        # This template skips the gates on a declaration; a wrapper whose verify job runs them anyway makes the
        # evidence directory first, and nothing is merged into it.
        Path(repo, "qae-artifacts").mkdir()
        for line in offered_gates(MANIFEST.read_text()):
            with self.subTest(gate=line.split()[0]):
                # The gates action runs in the workspace, where the manifest's paths are relative to.
                manifest = Path(repo, "qae-inputs", "manifest")
                manifest.write_text(line + "\n")
                result = subprocess.run([sys.executable, str(ROOT / "bin" / "vibe-verifier"), "run", "--manifest",
                                         str(manifest)], cwd=repo, capture_output=True, text=True, env=clean_env())
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
