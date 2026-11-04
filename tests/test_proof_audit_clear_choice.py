"""The proof audit's clear-choice replay: a case worded as a person's judgment of a screen ("the dialog makes clear
which is about to happen") is not proven by a test that tells two same-named controls apart by position.

A consumer's pull request (2026-11-04) graded that requirement PROVEN on a test that picked the dialog's "Send
invite" choice with `.first()` and its "Send invite" submit with `.last()`, and asserted only that the choice was
pressed. Its requester then took the choice for the send. tests/fixtures/proof-audit/d2 is that shape with none of
its code. old-prompt-grades/ keeps the real grader's grade on the prompt before this case: PROVEN, citing those
assertions, and the gate passes it, since its quotes are real; grades/ keeps the real grader's
grade on the prompt that says such a test never proves such a case (UNPROVEN), and proven/ is the dialog with a
radio choice and one submit that names its outcome, which the real grader graded PROVEN. The suite replays both
grades through the gate. harnesses/proof-audit/README.md, "The clear-choice replay"."""
import os
import shutil
import tempfile
import unittest
from pathlib import Path

from helpers import ROOT, commit, gate, git, make_repo, runner

FIXTURE = ROOT / "tests" / "fixtures" / "proof-audit" / "d2"
PROMPT = ROOT / "harnesses" / "proof-audit" / "prompt.md"


class ClearChoiceReplay(unittest.TestCase):
    def fixture_repo(self, proven=False):
        files = {path.relative_to(FIXTURE / "repo").as_posix(): path.read_text()
                 for path in (FIXTURE / "repo").rglob("*") if path.is_file()}
        repo = make_repo(self, files)
        git(repo, "checkout", "-q", "-b", "feat/add-user")
        commit(repo, {"web/src/note.txt": "the pull request's change\n"})
        if proven:
            commit(repo, {path.relative_to(FIXTURE / "proven" / "repo").as_posix(): path.read_text()
                          for path in (FIXTURE / "proven" / "repo").rglob("*") if path.is_file()})
        return repo

    def replay(self, repo, grades):
        work = Path(tempfile.mkdtemp(prefix="vv-replay-"))
        self.addCleanup(shutil.rmtree, work, True)
        result = gate("proof-audit", repo, "--body", str(FIXTURE / "body.md"), "--grades", str(grades),
                      "--base-ref", "main", "--table", str(work / "table.md"))
        return result, (work / "table.md").read_text()

    def test_the_old_prompts_grade_passed_the_gate_because_its_quotes_are_real(self):
        # The gate checks quotes, not judgment: only the prompt can stop this grade.
        result, table = self.replay(self.fixture_repo(), FIXTURE / "old-prompt-grades")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("| D-2 | PROVEN |", table)

    def test_the_recorded_grade_of_same_named_buttons_picked_by_position_is_unproven(self):
        result, table = self.replay(self.fixture_repo(), FIXTURE / "grades")
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("D-2 UNPROVEN", result.stdout)
        self.assertIn("by position", result.stdout)
        self.assertIn("| D-2 | UNPROVEN |", table)

    def test_the_recorded_grade_of_the_radio_choice_passes_and_does_not_pass_on_the_other_tree(self):
        result, table = self.replay(self.fixture_repo(proven=True), FIXTURE / "proven" / "grades")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("| D-2 | PROVEN |", table)
        # The same grade held to the tree it was not given: its radio quotes are not there.
        result, _ = self.replay(self.fixture_repo(), FIXTURE / "proven" / "grades")
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("a fabricated quote", result.stdout)

    def test_the_prompt_says_a_positional_pick_never_proves_a_clear_choice(self):
        prompt = " ".join(PROMPT.read_text().split())
        self.assertIn("never only that a text or a control is there or is pressed. It needs every one of these:", prompt)
        self.assertIn("the control a person presses names the one outcome it has, and no other control in view carries "
                      "the same name; the chosen option shows as chosen; and the explanation follows the choice.", prompt)
        self.assertIn("A test that tells two controls apart by position (`.first()`, `.last()`, `.nth(...)`) because "
                      "they share a name shows a person cannot tell them apart either: it never proves such a case.",
                      prompt)

    @unittest.skipUnless(os.environ.get("VV_PROOF_AUDIT_LIVE"), "set VV_PROOF_AUDIT_LIVE=1 to grade with the real model "
                         "(a ChatGPT-managed Codex login in CODEX_HOME or ~/.codex)")
    def test_the_real_grader_grades_the_positional_pick_unproven_and_the_radio_choice_proven(self):
        for proven, code in ((False, 1), (True, 0)):
            with self.subTest(proven=proven):
                work = Path(tempfile.mkdtemp(prefix="vv-replay-"))
                self.addCleanup(shutil.rmtree, work, True)
                result = runner("proof-audit", "--repo", self.fixture_repo(proven), "--base-ref", "main",
                                "--body", str(FIXTURE / "body.md"), "--grades", str(work / "grades"),
                                "--model", os.environ.get("VV_PROOF_AUDIT_MODEL", ""),
                                "--reasoning-effort", os.environ.get("VV_PROOF_AUDIT_EFFORT", ""))
                self.assertEqual(result.returncode, code, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
