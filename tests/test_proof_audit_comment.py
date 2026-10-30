"""actions/proof-audit's verify step and its pull request comment: a pull request that links no plan gets none, an
existing comment is replaced whatever the table says, and any other table is posted. Run as the runner runs the
step, against a gh that logs its calls (fixtures: tests/test_proof_audit.py, tests/test_proof_audit_unlinked.py)."""
import os
import unittest

from helpers import write

import test_proof_audit as base  # as modules, so their test classes are not collected twice
import test_proof_audit_unlinked as unlinked


FAKE_GH = """#!/bin/bash
echo "$*" >> "$GH_LOG"
case "$*" in *--paginate*) [ -n "$GH_FOUND" ] && echo "$GH_FOUND" ;; esac
exit 0
"""


class Comment(unittest.TestCase):
    repo, outside = unlinked.Unlinked.repo, unlinked.Unlinked.outside

    def calls(self, body, found="", grades=()):
        bin_dir = self.outside()
        write(bin_dir, {"gh": FAKE_GH})
        os.chmod(bin_dir / "gh", 0o755)
        log = bin_dir / "gh.log"
        result, _ = unlinked.Unlinked.verify_step(
            self, self.repo(), body, grades=grades, GH_TOKEN="t", VV_PR="7", VV_REPO="o/r", VV_HEAD="a" * 40,
            VV_RUN="https://github.com/o/r/actions/runs/42", GH_LOG=str(log), GH_FOUND=found,
            PATH="%s:%s" % (bin_dir, os.environ["PATH"]))
        return result, [line.split()[:3] for line in (log.read_text().splitlines() if log.exists() else [])]

    def test_a_pull_request_that_links_no_plan_gets_no_comment(self):
        result, calls = self.calls("Built it.\n")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertFalse([call for call in calls if "-X" in call], calls)

    def test_an_earlier_comment_is_replaced_even_when_nothing_is_audited_now(self):
        result, calls = self.calls("Built it.\n", found="99")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn(["api", "-X", "PATCH"], calls)

    def test_any_other_table_is_posted(self):
        ids = ["C-15", "C-8", "C-30"]
        unproven = base.valid()
        unproven[1] = {"id": "C-8", "status": "UNPROVEN", "cases": [], "exclusion": None, "missing": "no JPEG case"}
        cases = {  # a table, the gate's exit code
            "nothing audited": ("Built it.\n\n- [req: C-8] [proof: tests/covers.test.ts::x]\n", (), 1),
            "could not run": (base.BODY, (), 2),
            "proven": (base.BODY, (base.envelope(1, ids, base.valid()),), 0),
            "unproven": (base.BODY, (base.envelope(1, ids, unproven),), 1)}
        for name, (body, grades, code) in cases.items():
            with self.subTest(name):
                result, calls = self.calls(body, grades=grades)
                self.assertEqual(result.returncode, code, result.stdout + result.stderr)
                self.assertIn(["api", "-X", "POST"], calls)
