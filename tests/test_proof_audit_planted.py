"""Proof-audit grades the pull request supplies are never judged (gates/_contract.py, planted).

The grade and verify jobs check out the pull request being judged. A grades directory inside that checkout is one the
branch can commit to: grades that pass, a `<K>.json` directory that makes the grader's own write fail so only the
branch's file is uploaded, or a symbolic link. Catalog PR #239 closed the same hole for the QAE explorers' evidence;
a cold audit of the consumers' follow-ups found the proof-audit recipe still downloading into the checkout."""
import json
import os
import re
import shutil
import tempfile
import unittest
from pathlib import Path

from helpers import ROOT, commit, gate, git, make_repo, write
from test_proof_audit import BODY, PLAN, TEST, envelope, valid

REFUSED = "is not only the run's own, so none of it is judged"
GRADES = json.dumps(envelope(1, ["C-15", "C-8", "C-30"], valid()))


class PlantedGrades(unittest.TestCase):
    def setUp(self):
        self.repo = make_repo(self, {"docs/plan.md": PLAN, "tests/covers.test.ts": TEST})
        git(self.repo, "checkout", "-q", "-b", "feat/covers")
        commit(self.repo, {"src/covers.ts": "export {};\n"})
        self.work = Path(tempfile.mkdtemp(prefix="vv-planted-grades-"))
        self.addCleanup(shutil.rmtree, self.work, True)
        write(self.work, {"body.md": BODY})
        self.grades = Path(self.repo, "proof-audit-grades")

    def audit(self):
        result = gate("proof-audit", self.repo, "--body", str(self.work / "body.md"), "--grades", str(self.grades),
                      "--base-ref", "main", "--table", str(self.work / "table.md"))
        return result, (self.work / "table.md").read_text() if (self.work / "table.md").exists() else ""

    def assert_refused(self, *named):
        result, table = self.audit()
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn(REFUSED, result.stdout)
        self.assertIn("what the grade jobs wrote", result.stdout)
        for text in named:
            self.assertIn(text, result.stdout)
        self.assertIn("nothing judged: the pull request supplies grades of its own", table)
        self.assertNotIn("| C-8 |", table)

    def test_the_run_s_own_grades_in_the_checkout_are_judged(self):
        # A workflow written before the grades moved out of the checkout: what the grader wrote there still counts.
        write(self.grades, {"proof-audit-grades-1/1.json": GRADES})
        result, table = self.audit()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("3 of 3 requirements proven or excluded", table)

    def test_committed_grades_are_never_judged(self):
        # Grades that would pass, committed where the download puts an artifact, and under a `1.json` directory,
        # which makes the grader's own write of 1.json fail so the upload carries only the branch's file.
        for path in ("proof-audit-grades-1/1.json", "1.json/9.json"):
            with self.subTest(path=path):
                self.setUp()
                commit(self.repo, {"proof-audit-grades/" + path: GRADES}, "grades")
                self.assert_refused("the pull request tracks proof-audit-grades/" + path)

    def test_a_link_is_never_grades(self):
        # The grades directory as a committed link to grades the branch keeps elsewhere, and a link inside it the
        # checkout supplies: each points the gate at grades no grade job wrote.
        commit(self.repo, {"elsewhere/proof-audit-grades-1/1.json": GRADES}, "grades")
        os.symlink("elsewhere", self.grades)
        git(self.repo, "add", "proof-audit-grades")
        git(self.repo, "commit", "-q", "-m", "link")
        self.assert_refused("%s is a symbolic link" % self.grades, "the pull request tracks proof-audit-grades")
        self.setUp()
        commit(self.repo, {"elsewhere/1.json": GRADES}, "grades")
        Path(self.grades, "proof-audit-grades-1").mkdir(parents=True)
        os.symlink("../../elsewhere/1.json", Path(self.grades, "proof-audit-grades-1", "1.json"))
        self.assert_refused("proof-audit-grades-1/1.json is a symbolic link")

    def test_the_recipe_keeps_the_grades_outside_the_checkout(self):
        # From the grader's write through the upload and the download to the gate's read, every grades path in the
        # recipe is in the runner's temporary directory.
        text = (ROOT / "harnesses" / "proof-audit" / "README.md").read_text()
        temp = "${{ runner.temp }}/proof-audit-grades"
        for step in ("          grades: %s   # outside the checkout" % temp,
                     "          name: proof-audit-grades-${{ matrix.shard }}\n          path: %s/\n" % temp,
                     "          pattern: proof-audit-grades-*\n          path: %s\n" % temp,
                     "          grades: %s\n          require-ticket:" % temp):
            self.assertIn(step, text)
        recipe = "\n".join(re.findall(r"```yaml\n(.*?)```", text, re.S))
        self.assertEqual([line.strip() for line in recipe.splitlines() if re.search(r"(path|grades): (?!\$\{\{ runner\.temp)", line)
                          and "proof-audit-grades" in line], [])


if __name__ == "__main__":
    unittest.main()
