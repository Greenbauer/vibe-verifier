"""proof-audit's prompt: its placeholders, and every section the body links. The gate refuses an exclusion to a
linked section (that item is the pull request's own), and the grader never sees the body, so each shard is told
every linked section and to grade, never exclude, an item the plan gives to one. Fixtures: tests/test_proof_audit.py."""
import json
import sys
import unittest

from helpers import ROOT, write

sys.path.insert(0, str(ROOT / "gates"))
import _proof_audit  # noqa: E402
import test_proof_audit as base  # as a module, so its test classes are not collected twice


class LinkedSections(unittest.TestCase):
    setUp, run_grader, calls = base.Grader.setUp, base.Grader.run_grader, base.Grader.calls

    def test_every_shard_is_told_the_sections_the_body_links_so_it_grades_an_item_given_to_one(self):
        # Shard 1 holds only C-section requirements; P3, which takes C-30, is linked too and must still be named.
        write(self.work, {"body.md": base.BODY + "Ticket: docs/plan.md#p3-gallery\n"})
        plan = self.work / "plan.json"
        result = self.run_grader("--body", str(self.work / "body.md"), "--plan", str(plan), "--max-ids", "2")
        self.assertEqual(result.returncode, 0, result.stderr)
        written = json.loads(plan.read_text())
        self.assertEqual([[item["id"] for item in cut] for cut in written["shards"]], [["C-15", "C-8"], ["C-30", "P3-1"]])
        self.assertEqual(written["linked"], ["docs/plan.md#c-covers", "docs/plan.md#p3-gallery"])
        result = self.run_grader("--requirements", str(plan), "--shard", "1", "--grades", str(self.work / "grades"))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        (call,) = self.calls()
        prompt = call[-1]
        self.assertNotIn('- docs/plan.md#p3-gallery (heading', prompt)
        self.assertIn("this pull request links, the work it implements:\n\n- docs/plan.md#c-covers\n- docs/plan.md#p3-gallery\n", prompt)
        self.assertIn("is this pull request's own: grade it PROVEN or UNPROVEN, never EXCLUDED", prompt)


class Placeholders(unittest.TestCase):
    def test_a_requirement_naming_a_placeholder_is_written_as_it_is(self):
        item = {"id": "C-1", "plan": "docs/plan.md", "line": 3, "text": "SECTIONS, LINKED and REQUIREMENTS stay [req: C-1]",
                "section": "docs/plan.md#c", "heading": "C"}
        prompt = _proof_audit.render_prompt([item], ["docs/plan.md#c"])
        self.assertIn("- C-1 (docs/plan.md:3): SECTIONS, LINKED and REQUIREMENTS stay [req: C-1]", prompt)
        self.assertIn('- docs/plan.md#c (heading "C")', prompt)
        self.assertNotIn("\nREQUIREMENTS\n", prompt)

    def test_a_plan_with_no_linked_section_says_none(self):
        item = {"id": "PL-1", "plan": "plan:7", "line": 1, "text": "Totals round.", "section": "plan:7", "heading": "Totals"}
        self.assertIn("the work it implements:\n\n- none\n", _proof_audit.render_prompt([item], []))
