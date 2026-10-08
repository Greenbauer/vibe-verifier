"""Viewport widths in the QAE harness: `qae-artifacts --widths` requires each criterion's step
screenshots to include one of each declared width, read from the PNG header, and actions/qae-inputs
writes the same widths, from the manifest's qae-artifacts line as the base has it, to qae-inputs/widths
for the explorer. Driven through the gate's command line, the action script and the shipped template."""
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from helpers import ROOT, clean_env, commit, gate, git, make_repo, png, write
from test_qae_annotations import action_script
from test_qae_artifacts import CLEAN_REQUESTS, session_with

MANIFEST = ROOT / "harnesses" / "qae" / "manifest"
QAE = ("acceptance-verdict --criteria qae-inputs/pr-body.md --verdict qae-inputs/verdict.md --artifacts qae-artifacts\n"
       "qae-artifacts --artifacts qae-artifacts --criteria qae-inputs/pr-body.md --site-file qae-inputs/site-url --widths 1280,375\n")
BODY = "## Acceptance criteria\n\n- The home page shows its navigation\n- The portfolio page lists its sections\n"


class Gate(unittest.TestCase):
    def setUp(self):
        self.repo = make_repo(self, {"README.md": "x\n"})
        self.root = tempfile.mkdtemp(prefix="vv-qae-")
        self.addCleanup(shutil.rmtree, self.root, True)
        write(self.root, {
            "qae/AC1.md": "- step 1: opened / at 1280 -> the nav\n- step 2: resized to 375 -> the menu button\n",
            "qae/AC2.md": "- step 1: opened /contact at 1280 -> the form\n- step 2: resized to 375 -> the form, one column\n",
            "console-1.log": "", "session-1/session.md": session_with(CLEAN_REQUESTS),
        })
        for name, width in (("AC1-step-1", 1280), ("AC1-step-2", 375), ("AC2-step-1", 1280), ("AC2-step-2", 375)):
            Path(self.root, "qae", name + ".png").write_bytes(png(width))

    def run_gate(self, *args):
        return gate("qae-artifacts", self.repo, "--artifacts", self.root, *args)

    def test_every_criterion_at_every_width_passes(self):
        result = self.run_gate("--widths", "1280,375")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_a_criterion_missing_a_width_is_refused(self):
        Path(self.root, "qae", "AC2-step-2.png").write_bytes(png(1280))
        result = self.run_gate("--widths", "1280,375")
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("qae/AC2.md: no step screenshot 375 pixels wide (its widths: 1280)", result.stdout)
        self.assertNotIn("qae/AC1.md", result.stdout)

    def test_without_widths_the_widths_are_not_judged(self):
        Path(self.root, "qae", "AC2-step-2.png").write_bytes(png(1280))
        self.assertEqual(self.run_gate().returncode, 0)

    def test_a_screenshot_that_is_not_a_png_is_refused(self):
        Path(self.root, "qae", "AC1-step-2.png").write_bytes(b"\xff\xd8\xff\xe0 a jpeg")
        result = self.run_gate("--widths", "1280,375")
        self.assertEqual(result.returncode, 1)
        self.assertIn("qae/AC1.md: step 2's screenshot is not a PNG", result.stdout)
        self.assertIn("qae/AC1.md: no step screenshot 375 pixels wide (its widths: 1280)", result.stdout)

    def test_feature_re_walk_logs_are_not_held_to_widths(self):
        write(self.root, {"qae/features/nav.md": "- step 1: opened / -> the nav\n"})
        Path(self.root, "qae", "features", "nav-step-1.png").write_bytes(png(1280))
        self.assertEqual(self.run_gate("--widths", "1280,375").returncode, 0)

    def test_malformed_widths_cannot_run(self):
        for value in ("1280,mobile", "0", ""):
            self.assertEqual(self.run_gate("--widths", value, "--soak").returncode, 2, value)


class QaeInputs(unittest.TestCase):
    """The explorer is told the widths the gate will require, from the same line, judged from the base."""

    def run_action(self, repo, manifest=".vibe-verifier-qae", entries=""):
        write(repo, {"qae-inputs/pr-body.md": BODY})
        output = Path(repo, ".git", "github-output")
        output.write_text("")
        result = subprocess.run(["bash", "-e", "-c", action_script("qae-inputs")], cwd=repo, capture_output=True, text=True,
                                env=clean_env({"GITHUB_ACTION_PATH": str(ROOT / "actions" / "qae-inputs"), "GITHUB_OUTPUT": str(output),
                                               "RUNNER_TEMP": os.path.join(repo, ".git"), "VV_MANIFEST": manifest,
                                               "VV_ENTRIES": entries, "VV_REFERENCES": "qae-inputs/references",
                                               "VV_EVIDENCE": "qae-artifacts", "VV_SHARD": "1", "VV_SHARDS": "1"}))
        return result, output.read_text()

    def widths(self, repo):
        path = Path(repo, "qae-inputs", "widths")
        return path.read_text() if path.exists() else None

    def test_the_manifest_widths_reach_the_explorer(self):
        repo = make_repo(self, {".vibe-verifier-qae": QAE})
        result, output = self.run_action(repo)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.widths(repo), "1280\n375\n")
        self.assertIn("widths: 1280, 375", result.stdout)
        # Two criteria at two widths are four walks: the turn cap grows with them (bin/vibe-verifier TURNS_BASE).
        self.assertEqual(output, "references={}\nmax-turns=160\nshare=\n")

    def test_no_widths_option_writes_no_file(self):
        repo = make_repo(self, {".vibe-verifier-qae": QAE.replace(" --widths 1280,375", "")})
        result, _ = self.run_action(repo)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIsNone(self.widths(repo))
        self.assertIn("widths: none", result.stdout)

    def test_a_branch_dropping_the_widths_is_still_told_the_bases(self):
        repo = make_repo(self, {".vibe-verifier-qae": QAE})
        git(repo, "checkout", "-q", "-b", "feat")
        commit(repo, {".vibe-verifier-qae": QAE.replace(" --widths 1280,375", "")})
        result, _ = self.run_action(repo)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.widths(repo), "1280\n375\n")

    def test_a_wrapper_passes_its_gate_list(self):
        repo = make_repo(self, {"README.md": "x\n"})
        result, _ = self.run_action(repo, entries=QAE.replace("1280,375", "1440"))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.widths(repo), "1440\n")

    def test_a_malformed_widths_option_fails_the_step(self):
        repo = make_repo(self, {".vibe-verifier-qae": QAE.replace("1280,375", "desktop")})
        result, output = self.run_action(repo)
        self.assertEqual(result.returncode, 2)
        self.assertIn("malformed --widths", result.stderr)
        self.assertEqual(output, "")


class Template(unittest.TestCase):
    def test_the_manifest_names_the_opt_in_and_the_prompt_tells_the_explorer(self):
        self.assertIn("--widths", MANIFEST.read_text())
        prompt = (ROOT / "harnesses" / "qae" / "prompt.md").read_text()
        for phrase in ("If qae-inputs/widths exists", "check every criterion's end state at\n   each of them",
                       "every theme as well", "reload the page",
                       "only\n   when that screen has a form or another input, try one invalid input in it",
                       "Never navigate to a URL that neither the criterion names nor the site links to",
                       "unless the criterion's own text declares it\n   (expected-refusal: <status> <path>), and writing that in "
                       "a step line declares nothing"):
            self.assertIn(phrase, prompt)

    def test_the_explore_step_passes_no_manifest_input_so_the_default_is_the_qae_manifest(self):
        action = (ROOT / "actions" / "qae-inputs" / "action.yml").read_text()
        self.assertIn("  manifest:\n", action)
        self.assertIn("    default: .vibe-verifier-qae\n", action)


if __name__ == "__main__":
    unittest.main()
