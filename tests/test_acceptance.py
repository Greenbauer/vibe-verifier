"""The acceptance-verdict gate, driven through its command line against a temp tree."""
import os
import tempfile
import unittest

from helpers import gate, make_repo, write

SPEC = "import { test } from 'vitest'\ntest('rejects an expired invite', () => {})\ntest('accepts a fresh invite', () => {})\n"
LIB = "".join("line %d\n" % n for n in range(1, 41))
BODY = ("## Summary\n\nAdds invites.\n\n## Acceptance criteria\n\n"
        "- An expired invite is rejected\n- A fresh invite is accepted\n\n## Notes\n\n- not a criterion\n")


class AcceptanceVerdict(unittest.TestCase):
    def setUp(self):
        self.repo = make_repo(self, {"e2e/signup.spec.ts": SPEC, "src/lib/invites.ts": LIB})

    def run_with(self, criteria, verdict, *args):
        inputs = tempfile.mkdtemp(prefix="vv-acc-")
        self.addCleanup(lambda: None)
        write(inputs, {"criteria.md": criteria, "verdict.md": verdict})
        return gate("acceptance-verdict", self.repo, "--criteria", os.path.join(inputs, "criteria.md"),
                    "--verdict", os.path.join(inputs, "verdict.md"), *args)

    def test_one_anchored_pass_per_criterion_passes(self):
        verdict = ("acceptance-check: AC1 -- PASS -- covered by e2e/signup.spec.ts::rejects an expired invite\n"
                   "- [x] acceptance-check: AC2 -- PASS -- see src/lib/invites.ts:12-20 and the spec\n")
        result = self.run_with(BODY, verdict)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_only_the_criteria_section_counts(self):
        # "not a criterion" under Notes must not become AC3.
        verdict = ("acceptance-check: AC1 -- PASS -- e2e/signup.spec.ts::rejects an expired invite\n"
                   "acceptance-check: AC2 -- PASS -- e2e/signup.spec.ts::accepts a fresh invite\n")
        self.assertEqual(self.run_with(BODY, verdict).returncode, 0)

    def test_plain_list_criteria(self):
        verdict = "acceptance-check: AC1 -- PASS -- src/lib/invites.ts:3\n"
        self.assertEqual(self.run_with("# one per line\nexpired invites are rejected\n", verdict).returncode, 0)

    def test_missing_line_fails(self):
        result = self.run_with(BODY, "acceptance-check: AC1 -- PASS -- e2e/signup.spec.ts::rejects an expired invite\n")
        self.assertEqual(result.returncode, 1)
        self.assertIn("AC2 has no", result.stdout)

    def test_duplicate_lines_fail(self):
        verdict = ("acceptance-check: AC1 -- PASS -- e2e/signup.spec.ts::rejects an expired invite\n"
                   "acceptance-check: AC1 -- PASS -- e2e/signup.spec.ts::rejects an expired invite\n"
                   "acceptance-check: AC2 -- PASS -- e2e/signup.spec.ts::accepts a fresh invite\n")
        result = self.run_with(BODY, verdict)
        self.assertEqual(result.returncode, 1)
        self.assertIn("AC1 has 2 check lines", result.stdout)

    def test_a_fail_line_is_not_a_pass(self):
        verdict = ("acceptance-check: AC1 -- FAIL -- e2e/signup.spec.ts::rejects an expired invite\n"
                   "acceptance-check: AC2 -- PASS -- e2e/signup.spec.ts::accepts a fresh invite\n")
        result = self.run_with(BODY, verdict)
        self.assertEqual(result.returncode, 1)
        self.assertIn("AC1 is not a PASS", result.stdout)

    def test_unchecked_box_reads_as_missing(self):
        verdict = ("- [ ] acceptance-check: AC1 -- PASS -- e2e/signup.spec.ts::rejects an expired invite\n"
                   "acceptance-check: AC2 -- PASS -- e2e/signup.spec.ts::accepts a fresh invite\n")
        result = self.run_with(BODY, verdict)
        self.assertEqual(result.returncode, 1)
        self.assertIn("AC1 has no", result.stdout)

    def test_bare_file_name_is_not_an_anchor(self):
        verdict = ("acceptance-check: AC1 -- PASS -- e2e/signup.spec.ts passed both cases\n"
                   "acceptance-check: AC2 -- PASS -- e2e/signup.spec.ts::accepts a fresh invite\n")
        result = self.run_with(BODY, verdict)
        self.assertEqual(result.returncode, 1)
        self.assertIn("AC1 has no anchor", result.stdout)

    def test_anchor_must_resolve_at_the_head(self):
        verdict = ("acceptance-check: AC1 -- PASS -- e2e/signup.spec.ts::rejects a forged invite\n"
                   "acceptance-check: AC2 -- PASS -- src/lib/invites.ts:39-45\n")
        result = self.run_with(BODY, verdict)
        self.assertEqual(result.returncode, 1)
        self.assertIn('the title "rejects a forged invite" is not in e2e/signup.spec.ts', result.stdout)
        self.assertIn("src/lib/invites.ts has 40 lines, so src/lib/invites.ts:39-45 is outside it", result.stdout)

    def test_a_path_outside_the_tree_does_not_resolve(self):
        verdict = ("acceptance-check: AC1 -- PASS -- e2e/other.spec.ts::rejects an expired invite\n"
                   "acceptance-check: AC2 -- PASS -- e2e/signup.spec.ts::accepts a fresh invite\n")
        result = self.run_with(BODY, verdict)
        self.assertEqual(result.returncode, 1)
        self.assertIn("e2e/other.spec.ts is not in the tree", result.stdout)

    def test_one_resolving_anchor_among_several_is_enough(self):
        verdict = ("acceptance-check: AC1 -- PASS -- e2e/old.spec.ts::rejects an expired invite, now e2e/signup.spec.ts::rejects an expired invite\n"
                   "acceptance-check: AC2 -- PASS -- e2e/signup.spec.ts::accepts a fresh invite\n")
        self.assertEqual(self.run_with(BODY, verdict).returncode, 0)

    def test_artifacts_are_a_second_root_for_anchors(self):
        # Browser evidence is never in the tree: an explorer writes a step log per criterion and
        # anchors to it. Tracked paths still win, and a path outside the directory never resolves.
        artifacts = tempfile.mkdtemp(prefix="vv-art-")
        write(artifacts, {"qae/AC1.md": "# AC1\n- step 1: opened /invite?token=expired\n- step 2: saw the 'This invite has expired' message\n"})
        verdict = ("acceptance-check: AC1 -- PASS -- qae/AC1.md::saw the 'This invite has expired' message\n"
                   "acceptance-check: AC2 -- PASS -- e2e/signup.spec.ts::accepts a fresh invite\n")
        self.assertEqual(self.run_with(BODY, verdict, "--artifacts", artifacts).returncode, 0)
        without = self.run_with(BODY, verdict)
        self.assertEqual(without.returncode, 1)
        self.assertIn("qae/AC1.md is not in the tree", without.stdout)
        escaped = "acceptance-check: AC1 -- PASS -- ../../etc/passwd:1\nacceptance-check: AC2 -- PASS -- e2e/signup.spec.ts::accepts a fresh invite\n"
        self.assertEqual(self.run_with(BODY, escaped, "--artifacts", artifacts).returncode, 1)

    def test_no_criteria_is_a_finding_not_a_pass(self):
        result = self.run_with("## Summary\n\nno criteria here\n", "")
        self.assertEqual(result.returncode, 1)
        self.assertIn("no acceptance criteria found", result.stdout)

    def test_a_declared_none_passes_and_a_bare_none_does_not(self):
        # Silence is indistinguishable from forgetting; a declaration is a claim a reviewer can
        # weigh against the diff, so it passes and a bare "None" does not.
        declared = "## Acceptance criteria\n\n- None: this changes one pinned commit SHA, no rendered surface\n"
        result = self.run_with(declared, "")
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertIn("nothing to verify, declared: this changes one pinned commit SHA", result.stdout)
        self.assertEqual(self.run_with("## Acceptance criteria\n\n- None\n", "").returncode, 1)

    def test_none_beside_a_real_criterion_is_not_a_declaration(self):
        both = "## Acceptance criteria\n\n- None: nothing to see\n- The privacy page shows the heading\n"
        result = self.run_with(both, "")
        self.assertEqual(result.returncode, 1)
        self.assertIn("AC1 has no", result.stdout)

    def test_missing_input_file_cannot_run_even_under_soak(self):
        result = gate("acceptance-verdict", self.repo, "--criteria", "/nonexistent/criteria.md",
                      "--verdict", "/nonexistent/verdict.md", "--soak")
        self.assertEqual(result.returncode, 2)

    def test_soak_reports_but_passes(self):
        result = self.run_with(BODY, "", "--soak")
        self.assertEqual(result.returncode, 0)
        self.assertIn("AC1 has no", result.stdout)


if __name__ == "__main__":
    unittest.main()
