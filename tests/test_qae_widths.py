"""Viewport widths in the QAE harness: `qae-artifacts --widths` requires each criterion's step
screenshots to include one of each declared width, read from the PNG header, and actions/qae-inputs
writes the same widths, from the manifest's qae-artifacts line as the base has it, to qae-inputs/widths
for the harness's captures; they add no walks to the turn cap or the shard plan. Driven through the gate's
command line, the action script and the shipped template."""
import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from helpers import ROOT, clean_env, commit, gate, git, make_repo, png, runner, write
from test_qae_annotations import action_script
from test_qae_artifacts import CLEAN_REQUESTS, session_with

from _widths import width_list  # noqa: E402  (gates/, which helpers puts on the path)

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

    def harness_captured(self, shot, width, file=None, png_width=None):
        """The browser's widths hook (actions/qae-browser/widths.js) captured `shot` at `width`."""
        file = file or "widths/%s-%d.png" % (shot, width)
        digest = hashlib.sha256(Path(self.root, "qae", shot + ".png").read_bytes()).hexdigest()
        target = Path(self.root, file)
        if target.resolve().is_relative_to(Path(self.root).resolve()):
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(png(png_width or width))
        with open(Path(self.root, "widths.jsonl"), "a") as record:
            record.write(json.dumps({"shot": digest, "width": width, "file": file, "disturbed": False}) + "\n")

    def test_the_harness_captures_of_a_step_count_for_its_criterion(self):
        # The explorer walks at 1280 alone; the hook captured step 1 of each criterion at 375.
        for name in ("AC1-step-2", "AC2-step-2"):
            Path(self.root, "qae", name + ".png").write_bytes(png(1280, 2))
        self.harness_captured("AC1-step-1", 375)
        self.harness_captured("AC2-step-2", 375)
        result = self.run_gate("--widths", "1280,375")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_a_capture_is_counted_only_as_wide_as_it_is_and_only_inside_the_evidence(self):
        Path(self.root, "qae", "AC2-step-2.png").write_bytes(png(1280, 2))
        self.harness_captured("AC2-step-1", 375, png_width=1280)
        self.harness_captured("AC2-step-2", 375, file="../elsewhere-375.png")
        Path(self.root).parent.joinpath("elsewhere-375.png").write_bytes(png(375))
        self.addCleanup(Path(self.root).parent.joinpath("elsewhere-375.png").unlink)
        result = self.run_gate("--widths", "1280,375")
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("qae/AC2.md: no step screenshot 375 pixels wide (its widths: 1280)", result.stdout)

    def test_a_split_runs_captures_count_from_each_explorers_folder(self):
        # One explorer of several: the workflow moves its record and captures under shard-<n>/ and keeps the
        # criterion's own step screenshots at the root.
        for name in ("AC1-step-2", "AC2-step-2"):
            Path(self.root, "qae", name + ".png").write_bytes(png(1280, 2))
        self.harness_captured("AC1-step-1", 375)
        self.harness_captured("AC2-step-2", 375)
        shard = Path(self.root, "shard-2")
        shard.mkdir()
        Path(self.root, "widths.jsonl").rename(shard / "widths.jsonl")
        Path(self.root, "widths").rename(shard / "widths")
        result = self.run_gate("--widths", "1280,375")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_a_capture_of_another_screenshot_does_not_count(self):
        Path(self.root, "qae", "AC2-step-2.png").write_bytes(png(1280, 2))
        Path(self.root, "qae", "other.png").write_bytes(png(1280, 3))
        self.harness_captured("other", 375)
        result = self.run_gate("--widths", "1280,375")
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("qae/AC2.md: no step screenshot 375 pixels wide", result.stdout)

    def test_feature_re_walk_logs_are_not_held_to_widths(self):
        write(self.root, {"qae/features/nav.md": "- step 1: opened / -> the nav\n"})
        Path(self.root, "qae", "features", "nav-step-1.png").write_bytes(png(1280))
        self.assertEqual(self.run_gate("--widths", "1280,375").returncode, 0)

    def test_malformed_widths_cannot_run(self):
        for value in ("1280,mobile", "0", ""):
            self.assertEqual(self.run_gate("--widths", value, "--soak").returncode, 2, value)


class QaeInputs(unittest.TestCase):
    """The explorer is told the widths the gate will require, from the same line, judged from the base."""

    def run_action(self, repo, manifest=".vibe-verifier-qae", entries="", body=BODY):
        write(repo, {"qae-inputs/pr-body.md": body})
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
        # Two criteria are two walks at any width: the harness captures the widths (bin/cli/qae_shards.py TURNS_BASE).
        self.assertEqual(output, "references={}\nmax-turns=100\nshare=\nshare-ids=[\"AC1\", \"AC2\"]\n")

    def test_the_widths_change_neither_the_walks_nor_the_turn_cap(self):
        # The harness captures each screenshot at the other widths (actions/qae-browser/widths.js), so four
        # criteria are four walks, one explorer and a cap of 160 with --widths 1280,375 as without them.
        body = BODY + "- The contact page shows its form\n- The footer links to the privacy page\n"
        found = []
        for manifest in (QAE, QAE.replace(" --widths 1280,375", "")):
            repo = make_repo(self, {".vibe-verifier-qae": manifest})
            result, output = self.run_action(repo, body=body)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            planned = runner("criteria", os.path.join(repo, "qae-inputs", "pr-body.md"), "--max-shards", "3", "--repo", repo,
                             "--manifest", os.path.join(repo, ".vibe-verifier-qae"))
            self.assertEqual(planned.returncode, 0, planned.stdout + planned.stderr)
            found.append((planned.stdout, re.search(r"^max turns: .*$", result.stdout, re.MULTILINE).group(0), output))
        self.assertEqual(found[0], found[1])
        self.assertIn("shards: [1]\nwalks: 4\n", found[0][0])
        self.assertEqual(found[0][1], "max turns: 160 (4 walks)")

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

    def test_a_line_the_branch_adds_above_the_bases_does_not_change_the_widths(self):
        repo = make_repo(self, {".vibe-verifier-qae": QAE})
        git(repo, "checkout", "-q", "-b", "feat")
        commit(repo, {".vibe-verifier-qae": "qae-artifacts --widths 1440\n" + QAE})
        result, _ = self.run_action(repo)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.widths(repo), "1280\n375\n")

    def test_a_wrapper_passes_its_gate_list(self):
        repo = make_repo(self, {"README.md": "x\n"})
        result, _ = self.run_action(repo, entries=QAE.replace("1280,375", "1440"))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.widths(repo), "1440\n")

    def environmental(self, repo):
        path = Path(repo, "qae-inputs", "environmental")
        return path.read_text() if path.exists() else None

    def test_the_manifest_allowances_reach_the_explorer(self):
        # A consumer's analytics script 404s on the runner; the gate allows it, so the explorer is told.
        allowed = QAE.replace(" --widths", " --allow-console '/_vercel/insights/script\\.js' "
                              "--allow-request '/_vercel/insights/script\\.js' --allow-request '^https://fonts\\.' --widths")
        repo = make_repo(self, {".vibe-verifier-qae": allowed})
        result, _ = self.run_action(repo)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.environmental(repo), "console: /_vercel/insights/script\\.js\n"
                         "request: /_vercel/insights/script\\.js\nrequest: ^https://fonts\\.\n")
        self.assertIn("environmental: 3", result.stdout)
        self.assertEqual(self.widths(repo), "1280\n375\n")

    def test_no_allowance_writes_no_file(self):
        repo = make_repo(self, {".vibe-verifier-qae": QAE})
        result, _ = self.run_action(repo)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIsNone(self.environmental(repo))
        self.assertIn("environmental: 0", result.stdout)

    def test_a_branch_adding_an_allowance_is_told_only_the_bases(self):
        # The gate judges the line from the base, so a pull request cannot excuse its own 404 to the explorer.
        repo = make_repo(self, {".vibe-verifier-qae": QAE})
        git(repo, "checkout", "-q", "-b", "feat")
        commit(repo, {".vibe-verifier-qae": QAE.replace(" --widths", " --allow-request '/api/broken' --widths")})
        result, _ = self.run_action(repo)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIsNone(self.environmental(repo))

    def test_a_malformed_widths_option_fails_the_step(self):
        repo = make_repo(self, {".vibe-verifier-qae": QAE.replace("1280,375", "desktop")})
        result, output = self.run_action(repo)
        self.assertEqual(result.returncode, 2)
        self.assertIn("malformed --widths", result.stderr)
        self.assertEqual(output, "")


class Captures(unittest.TestCase):
    """The browser's width captures (actions/qae-browser/widths.js) and the gate module that counts them (gates/_widths.py)."""

    def test_the_hooks_node_tests_pass(self):
        result = subprocess.run(["node", "--test", "--test-reporter=tap", str(ROOT / "actions" / "qae-browser" / "widths.test.js")],
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertRegex(result.stdout, r"(?m)^# pass [1-9]")

    def test_the_declared_widths_are_whole_pixels_once_each(self):
        self.assertEqual(width_list("1280,375,1280"), [1280, 375])
        for value in ("1280,mobile", "0", ""):
            with self.assertRaises(argparse.ArgumentTypeError):
                width_list(value)


class Template(unittest.TestCase):
    def test_the_prompt_asks_for_no_routine_record(self):
        # The harness saves the network and console records (actions/qae-browser/console-pages.js): the explorer is
        # told not to, and only a retried step's records are still saved by filename.
        prompt = (ROOT / "harnesses" / "qae" / "prompt.md").read_text()
        self.assertIn("do not call browser_network_requests or\n   browser_console_messages to save them", prompt)
        self.assertNotIn("On every page you visit, read the browser", prompt)
        self.assertIn("filename qae-artifacts/qae/ACn-step-k-network.txt", prompt)

    def test_the_manifest_names_the_opt_in_and_the_prompt_tells_the_explorer(self):
        self.assertIn("--widths", MANIFEST.read_text())
        prompt = (ROOT / "harnesses" / "qae" / "prompt.md").read_text()
        for phrase in ("If qae-inputs/widths exists", "do not resize it for them",
                       "the harness captures the\n   same screen at the other widths itself",
                       "every theme as well", "reload the page",
                       "only\n   when that screen has a form or another input, try one invalid input in it",
                       "Never navigate to a URL that neither the criterion names nor the site links to"):
            self.assertIn(phrase, prompt)
        # Who may declare an expected refusal, and what the manifest declares environmental, are rendered from the
        # gate's own data: tests/test_qae_prompt_rules.py.

    def test_the_explore_step_passes_no_manifest_input_so_the_default_is_the_qae_manifest(self):
        action = (ROOT / "actions" / "qae-inputs" / "action.yml").read_text()
        self.assertIn("  manifest:\n", action)
        self.assertIn("    default: .vibe-verifier-qae\n", action)


if __name__ == "__main__":
    unittest.main()
