"""The proof-audit gate on a plan kept in a plan store: a requirement a `Split off:` line gives away is SPLIT with its
owner and never graded, EXCLUDED is no way out for a stored plan's requirement, a store the gate cannot read is
could-not-run, and the action's verify step reports only under `soak` but still fails a gate that could not run.
Fixtures: tests/test_proof_audit_plan_store.py and tests/test_proof_audit.py."""
import json
import unittest

from helpers import gate, write

import test_proof_audit as base  # as modules, so their test classes are not collected twice
import test_proof_audit_plan_store as stored
import test_proof_audit_unlinked as unlinked

TITLE = "Euro prices [plan:%s]" % stored.PLAN_ID
PLAN_IDS = [rid for rid, _, _ in stored.CUT]


def everything(*but):
    """One envelope grading every requirement but `but`, each proven by a quote that is in the test file."""
    grades = base.valid() + [stored.proven(rid) for rid in PLAN_IDS if rid not in but]
    return base.envelope(1, [grade["id"] for grade in grades], grades)


class SplitGate(unittest.TestCase):
    setUp = stored.PlanStoreAudit.setUp
    outside, verify_step = base.ProofAudit.outside, unlinked.Unlinked.verify_step

    def judge(self, *envelopes, body=base.BODY, token="secret", extra=()):
        work = self.outside()
        write(work, {"body.md": body})
        for item in envelopes:
            write(work, {"grades/proof-audit-%d/%d.json" % (item["shard"], item["shard"]): json.dumps(item)})
        result = gate("proof-audit", self.repo, "--body", str(work / "body.md"), "--grades", str(work / "grades"),
                      "--base-ref", "main", "--table", str(work / "table.md"), "--title", TITLE,
                      "--plans-endpoint", self.endpoint, *extra, env={"VV_PLANS_TOKEN": token})
        return result, (work / "table.md").read_text() if (work / "table.md").exists() else ""

    def test_a_split_off_requirement_is_listed_with_its_owner_and_never_graded(self):
        body = base.BODY + "\nSplit off: PL-5 -> #123\nSplit off: AM3-1 -> [plan:%s]\n" % stored.OTHER
        result, table = self.judge(everything("PL-5", "AM3-1"), body=body)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("## Proof audit: 12 of 14 requirements proven or excluded, 2 split off", table)
        self.assertIn("| PL-5 | SPLIT | split off to #123 |", table)
        self.assertIn("| AM3-1 | SPLIT | split off to [plan:%s] |" % stored.OTHER, table)
        self.assertIn("| C-30 | EXCLUDED | docs/plan.md#p3-gallery", table)  # a Ticket section's exclusion still counts
        # A grade for a split-off requirement is a grade for an ID the plan step never sent: the gate cannot run.
        result, _ = self.judge(everything(), body=body)
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn("the grades are for", result.stdout + result.stderr)

    def test_a_plan_split_off_whole_grades_nothing_and_cannot_pass(self):
        self.plans[stored.PLAN_ID] = {"planMarkdown": "- Prices show in euros.\n", "amendments": []}
        result, _ = self.judge(body="Built it.\n\nSplit off: PL-1 -> #7\n", extra=("--soak",))
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn("every requirement (1) is split off, so nothing is graded", result.stdout + result.stderr)
        # So does a split to this pull request itself, the gate's --pr.
        self.plans[stored.PLAN_ID] = {"planMarkdown": "- Prices show in euros.\n- Totals too.\n", "amendments": []}
        result, _ = self.judge(base.envelope(1, ["PL-2"], [stored.proven("PL-2")]), body="Split off: PL-1 -> #7\n",
                               extra=("--pr", "7"))
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn("names #7, which is this pull request", result.stdout + result.stderr)

    def test_an_excluded_grade_on_a_stored_requirement_is_unproven(self):
        envelope = everything()
        envelope["output"]["requirements"][3] = base.grade("PL-1", "EXCLUDED", exclusion=base.EXCLUSION)
        self.assertEqual(envelope["ids"][3], "PL-1")
        result, table = self.judge(envelope, extra=("--format", "github"))
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("| PL-1 | UNPROVEN | graded EXCLUDED, but a stored plan gives no requirement to another owner", table)
        # No repository file to annotate, and the plan:<id> source is never read as one.
        self.assertIn("::error title=proof-audit::PL-1 UNPROVEN: graded EXCLUDED", result.stdout)
        self.assertNotIn("is not a file in the repository", result.stdout)
        self.assertIn("| C-30 | EXCLUDED |", table)

    def test_a_store_the_gate_cannot_read_cannot_run(self):
        for token, words in (("", "no token for the plan store"), ("wrong", "the plan store answered 404")):
            with self.subTest(words=words):
                result, _ = self.judge(everything(), token=token, extra=("--soak",))
                self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
                self.assertIn(words, result.stdout + result.stderr)

    def test_the_verify_step_reports_only_under_soak_but_fails_a_gate_that_cannot_run(self):
        store = {"VV_TITLE": TITLE, "VV_PLANS_ENDPOINT": self.endpoint, "VV_PLANS_TOKEN": "secret"}
        result, summary = self.verify_step(self.repo, base.BODY, grades=[everything()], **store)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("## Proof audit: 14 of 14 requirements proven or excluded", summary)
        unproven = everything()
        unproven["output"]["requirements"][4] = base.grade("PL-2", "UNPROVEN", missing="no test shows a euro price")
        for soak, code in (("false", 1), ("true", 0)):
            with self.subTest(soak=soak):
                result, summary = self.verify_step(self.repo, base.BODY, grades=[unproven], VV_SOAK=soak, **store)
                self.assertEqual(result.returncode, code, result.stdout + result.stderr)
                self.assertIn("| PL-2 | UNPROVEN | no test shows a euro price |", summary)
        self.assertIn("reported only (--soak)", result.stdout)
        # Nothing graded is could-not-run, which soak never masks.
        result, summary = self.verify_step(self.repo, base.BODY, VV_SOAK="true", **store)
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn("## Proof audit: could not run: no grades", summary)
