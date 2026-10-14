"""ticket-coverage: plan IDs cannot disappear between the spec, browser criteria and PR claims."""
import json
import os
import subprocess
import sys
import unittest
from pathlib import Path

from helpers import ROOT, clean_env, commit, gate, git, make_repo, runner, write

PLAN = ("# Plan\n\n## P1\n\n- Itineraries show trips [req: P1-1]\n"
        "- Itineraries paginate with a Load More button [req: P1-2]\n\n"
        "### Acceptance criteria\n\n- Trips appear [req: P1-1]\n- Load More adds trips [req: P1-2]\n\n"
        "## P2\n\n[takes: P1-2]\n\n- Search trips [req: P2-1]\n\n"
        "### Acceptance criteria\n\n- Search returns trips [req: P2-1]\n")
LINK = "Ticket: docs/plan.md#p1\n"
PROOF = "- Trips [req: P1-1] [proof: tests/trips.py]\n- More [req: P1-2] [proof: tests/trips.py::load_more]\n"
DEFER = "- Trips [req: P1-1] [proof: tests/trips.py]\n- More [req: P1-2] [deferred: docs/plan.md#p2]\n"


class TicketCoverage(unittest.TestCase):
    def branch(self, plan=PLAN, base=None, head=None):
        repo = make_repo(self, {"docs/plan.md": plan, "tests/trips.py": "def load_more(): pass\n", **(base or {})})
        git(repo, "checkout", "-q", "-b", "feat/test")
        commit(repo, head or {"change.txt": "changed\n"})
        return repo

    def plans(self, repo, *args):
        return gate("ticket-coverage", repo, "--plans", "docs/*.md", *args)

    def body(self, repo, body=LINK + PROOF, *args, env=None):
        write(repo, {"body.md": body})
        return gate("ticket-coverage", repo, "--body", os.path.join(repo, "body.md"), "--base-ref", "main", *args, env=env)

    def assert_code(self, result, code, *words):
        self.assertEqual(result.returncode, code, result.stdout + result.stderr)
        for word in words:
            self.assertIn(word, result.stdout + result.stderr)

    def test_tagged_requirements_with_own_criteria_pass(self):
        self.assert_code(self.plans(self.branch()), 0)

    def test_untagged_requirement_names_its_file_line_and_text(self):
        plan = PLAN.replace(" [req: P1-2]", "", 1)
        self.assert_code(self.plans(self.branch(plan)), 1, "docs/plan.md:6:", "requirement has no [req: ID]", "Load More button")

    def test_each_id_is_defined_once_across_files_and_sections(self):
        duplicate = PLAN.replace("P1-1", "P2-1")
        self.assert_code(self.plans(self.branch(duplicate)), 1, "P2-1 is defined more than once")
        self.assert_code(self.plans(self.branch(base={"docs/other.md": PLAN})), 1, "P1-1 is defined more than once", "first at")
        self.assert_code(self.plans(self.branch(PLAN.replace("[req: P1-1]", "[req: P1-1, P1-1]", 1))), 1,
                         "P1-1 is defined more than once")

    def test_a_missing_criterion_is_a_finding_even_if_another_section_cites_it(self):
        plan = PLAN.replace("- Load More adds trips [req: P1-2]\n", "").replace("Search returns trips [req: P2-1]",
                                                                               "Search returns trips [req: P2-1, P1-2]")
        self.assert_code(self.plans(self.branch(plan)), 1, "P1-2 has no criterion and no not-checked entry")

    def test_not_checked_can_cite_an_id_in_prose(self):
        plan = PLAN.replace("- Load More adds trips [req: P1-2]", "### NOT CHECKED IN A BROWSER: jobs\n\nVerified in tests [req: P1-2]")
        self.assert_code(self.plans(self.branch(plan)), 0)

    def test_unknown_criterion_not_checked_and_owner_ids_fail(self):
        for replacement in ("- Load More adds trips [req: P1-2, B-12]",
                            "- Load More adds trips [req: P1-2]\n\n### Not checked in a browser\n\n[req: B-12]",
                            "- Load More adds trips [req: P1-2]\n\n[takes: B-12]"):
            with self.subTest(replacement=replacement):
                plan = PLAN.replace("- Load More adds trips [req: P1-2]", replacement)
                self.assert_code(self.plans(self.branch(plan)), 1, "B-12 is defined nowhere")

    def test_comma_ids_repeated_tags_and_wrapped_nested_items_share_the_ticket_parser(self):
        plan = ("## Scope\n\n1. Compound requirement [req: B-12, G2-3a]\n"
                "   with a continuation\n   - and a nested clause [req: P1-4]\n\n"
                "### Acceptance criteria\n\n- All clauses [req: B-12] [req: G2-3a, P1-4]\n")
        self.assert_code(self.plans(self.branch(plan)), 0)
        # _ticket.py owns list/fence/anchor parsing for the action and the gate.
        self.assertTrue((ROOT / "gates" / "_ticket.py").is_file())
        self.assertIn("from _ticket import", (ROOT / "actions/ticket/ticket.py").read_text())
        self.assertIn("from _ticket import", (ROOT / "gates/ticket_coverage.py").read_text())

    def test_invalid_id_grammar_does_not_define_a_requirement(self):
        for invalid in ("1P-2", "P_1-2", "P1-2A", "P1-2aa", "P1-x"):
            self.assert_code(self.plans(self.branch(PLAN.replace("[req: P1-1]", "[req: %s]" % invalid, 1))), 1,
                             "requirement has no [req: ID]")

    def test_only_direct_criteria_make_a_ticket_section(self):
        plan = ("# Parent\n\n- Not a ticket requirement\n\n## Child\n\n- Child [req: B-12]\n\n"
                "#### Acceptance criteria\n\n- Child works [req: B-12]\n")
        self.assert_code(self.plans(self.branch(plan)), 0)

    def test_a_grandchild_not_checked_does_not_close_coverage(self):
        plan = PLAN.replace("- Load More adds trips [req: P1-2]", "### Implementation\n\n#### Not checked in a browser\n\n[req: P1-2]")
        self.assert_code(self.plans(self.branch(plan)), 1, "P1-2 has no criterion and no not-checked entry")

    def test_requirements_stop_at_first_subheading_and_prose_is_not_a_criterion_item(self):
        plan = PLAN.replace("- Load More adds trips [req: P1-2]", "\nParagraph [req: P1-2]").replace(
            "## P2", "### Notes\n\n- No requirement tag needed here\n\n## P2")
        self.assert_code(self.plans(self.branch(plan)), 1, "P1-2 has no criterion and no not-checked entry")

    def test_fenced_examples_define_and_cite_nothing(self):
        plan = PLAN.replace("- Load More adds trips [req: P1-2]", "```md\n- Load More adds trips [req: P1-2]\n```")
        plan += "\n~~~\n## Example\n- no ID\n### Acceptance criteria\n- [req: B-12]\n[takes: G2-3a]\n~~~\n"
        result = self.plans(self.branch(plan))
        self.assert_code(result, 1, "P1-2 has no criterion")
        self.assertNotIn("B-12", result.stdout)
        self.assertNotIn("G2-3a", result.stdout)

    def test_plans_are_read_at_head_not_from_uncommitted_edits(self):
        repo = self.branch(PLAN.replace("- Load More adds trips [req: P1-2]\n", ""))
        write(repo, {"docs/plan.md": PLAN})
        self.assert_code(self.plans(repo), 1, "P1-2 has no criterion")
        commit(repo, {"docs/plan.md": PLAN})
        self.assert_code(self.plans(repo), 0)

    def test_fnmatch_repeatable_globs_select_tracked_head_paths_only(self):
        extra = PLAN.replace("P1-", "B-").replace("P2-", "G2-")
        repo = self.branch(base={"plans/deep/other.md": extra, "ignored.md": "## Broken\n- no tag\n### Acceptance criteria\n- x\n"})
        write(repo, {"docs/untracked.md": "## Broken\n- no tag\n### Acceptance criteria\n- x\n"})
        self.assert_code(self.plans(repo, "--plans", "plans/*.md"), 0)
        commit(repo, {"plans/deep/other.md": PLAN})
        self.assert_code(self.plans(repo, "--plans", "plans/*.md"), 1, "defined more than once")

    def test_proof_path_and_literal_text_at_head_pass(self):
        repo = self.branch(head={"new.py": "new proof\n"})
        self.assert_code(self.body(repo, LINK + PROOF.replace("tests/trips.py::load_more", "new.py::new proof")), 0)

    def test_proof_missing_path_or_text_fails_with_requirement_and_remedy(self):
        for proof, reason in (("missing.py", "does not exist at HEAD"), ("tests/trips.py::absent", "proof text"),
                              ("tests/trips.py::", "proof text"), ("../trips.py", "repository path")):
            body = LINK + PROOF.replace("tests/trips.py::load_more", proof)
            self.assert_code(self.body(self.branch(), body), 1, "P1-2", "Load More button", reason, "claim with [req: P1-2]")

    def test_directory_proof_exists_but_text_proof_needs_a_file(self):
        repo = self.branch()
        self.assert_code(self.body(repo, LINK + PROOF.replace("tests/trips.py::load_more", "tests")), 0)
        self.assert_code(self.body(repo, LINK + PROOF.replace("tests/trips.py::load_more", "tests::load_more")), 1,
                         "proof text needs a file")

    def test_one_valid_alternative_proof_covers_the_requirement(self):
        body = LINK + PROOF.replace("tests/trips.py::load_more", "missing.py")
        body += "- Another claim [req: P1-2] [proof: tests/trips.py::load_more]\n"
        self.assert_code(self.body(self.branch(), body), 0)

    def test_working_tree_proof_cannot_claim_missing_head_file_or_text(self):
        repo = self.branch()
        write(repo, {"uncommitted.py": "load_more", "tests/trips.py": "uncommitted_text"})
        self.assert_code(self.body(repo, LINK + PROOF.replace("tests/trips.py::load_more", "uncommitted.py")), 1, "does not exist at HEAD")
        self.assert_code(self.body(repo, LINK + PROOF.replace("::load_more", "::uncommitted_text")), 1, "proof text")

    def test_deferral_passes_only_with_owner_at_merge_base(self):
        self.assert_code(self.body(self.branch(), LINK + DEFER), 0)
        missing = PLAN.replace("[takes: P1-2]", "")
        self.assert_code(self.body(self.branch(missing), LINK + DEFER), 1, "has no [takes: P1-2] at the merge base")
        self.assert_code(self.body(self.branch(missing, head={"docs/plan.md": PLAN}), LINK + DEFER), 1,
                         "has no [takes: P1-2] at the merge base")

    def test_owner_can_accept_comma_ids_anywhere_in_a_non_ticket_section(self):
        plan = PLAN.replace("[takes: P1-2]", "").replace("## P2", "## Owner\n\n### Notes\n\nTaking [takes: B-12, P1-2]\n\n## P2")
        self.assert_code(self.body(self.branch(plan), LINK + DEFER.replace("#p2", "#owner")), 0)

    def test_owner_and_not_checked_tags_can_be_in_subsection_headings(self):
        plan = PLAN.replace("[takes: P1-2]", "### Ownership [takes: P1-2]")
        self.assert_code(self.body(self.branch(plan), LINK + DEFER), 0)
        plan = PLAN.replace("- Load More adds trips [req: P1-2]",
                            "### Not checked in a browser [req: P1-2]\n\n#### Proof [req: P1-1]")
        self.assert_code(self.plans(self.branch(plan)), 0)

    def test_base_branch_updates_do_not_replace_the_merge_base(self):
        missing = PLAN.replace("[takes: P1-2]", "")
        repo = self.branch(missing)
        git(repo, "checkout", "-q", "main")
        commit(repo, {"docs/plan.md": PLAN})
        git(repo, "checkout", "-q", "feat/test")
        self.assert_code(self.body(repo, LINK + DEFER), 1, "has no [takes: P1-2] at the merge base")

    def test_head_cannot_remove_requirements_it_owes(self):
        repo = self.branch(head={"docs/plan.md": PLAN.replace(" [req: P1-2]", "")})
        self.assert_code(self.body(repo, LINK + PROOF.splitlines()[0] + "\n"), 1, "P1-2", "Load More button")

    def test_missing_id_and_claims_outside_list_items_fail(self):
        for body in (LINK + PROOF.splitlines()[0] + "\n", LINK + PROOF.replace("- More", "\nMore"),
                     LINK + PROOF.replace("[proof: tests/trips.py::load_more]", "")):
            self.assert_code(self.body(self.branch(), body), 1, "P1-2", "no proof or accepted deferral")

    def test_a_claims_tags_must_be_in_the_same_item(self):
        body = LINK + "- Trips [req: P1-1] [proof: tests/trips.py]\n- More [req: P1-2]\n- [proof: tests/trips.py]\n"
        self.assert_code(self.body(self.branch(), body), 1, "P1-2", "no proof or accepted deferral")

    def test_unknown_body_ids_fail_in_items_or_prose(self):
        for extra in ("- Unknown [req: B-12] [proof: tests/trips.py]\n", "\nUnknown [req: B-12]\n"):
            self.assert_code(self.body(self.branch(), LINK + PROOF + extra), 1, "B-12 in the body is not defined")

    def test_multiple_ticket_links_and_comma_claims_pass(self):
        body = LINK + "ticket: `docs/plan.md#p2`\n- All [req: P1-1, P1-2, P2-1] [proof: tests/trips.py::load_more]\n"
        self.assert_code(self.body(self.branch(), body), 0)

    def test_repeated_heading_uses_github_numbered_anchor(self):
        plan = PLAN.replace("## P2", "## P1")
        body = "Ticket: docs/plan.md#p1-1\n- Search [req: P2-1] [proof: tests/trips.py]\n"
        self.assert_code(self.body(self.branch(plan), body), 0)

    def test_no_ticket_is_a_qualified_pass_and_fenced_links_do_not_count(self):
        for body in ("No ticket\n", "```\nTicket: bad\n```\n", "~~~\n" + LINK + "~~~\n"):
            repo = self.branch()
            qualifier = os.path.join(repo, "qualifier")
            result = self.body(repo, body, env={"VIBE_VERIFIER_QUALIFIER": qualifier})
            self.assert_code(result, 0, "no ticket linked")
            self.assertEqual(Path(qualifier).read_text(), "no ticket linked")

    def test_untagged_ticket_is_a_qualified_pass(self):
        repo = self.branch(PLAN.replace("[req: P1-1]", "").replace("[req: P1-2]", ""))
        qualifier = os.path.join(repo, "qualifier")
        self.assert_code(self.body(repo, LINK, env={"VIBE_VERIFIER_QUALIFIER": qualifier}), 0,
                         "ticket section has no requirement IDs; nothing traced")
        self.assertIn("ticket section has no requirement IDs", Path(qualifier).read_text())

    def test_bad_ticket_links_files_anchors_and_history_cannot_run_even_in_soak(self):
        for link in ("docs/plan.md#missing", "docs/gone.md#p1", "../plan.md#p1", "not a link"):
            self.assert_code(self.body(self.branch(), "Ticket: " + link + "\n", "--soak"), 2)
        repo = self.branch(head={"docs/new.md": PLAN})
        self.assert_code(self.body(repo, "Ticket: docs/new.md#p1\n", "--soak"), 2)
        self.assert_code(self.body(repo, LINK + PROOF, "--base-ref", "missing", "--soak"), 2)
        self.assert_code(gate("ticket-coverage", repo, "--body", os.path.join(repo, "missing-body"), "--soak"), 2)

    def test_bad_deferred_links_files_and_anchors_cannot_run_in_soak(self):
        for target in ("docs/plan.md#missing", "docs/gone.md#p2", "../plan.md#p2", "not a link"):
            self.assert_code(self.body(self.branch(), LINK + DEFER.replace("docs/plan.md#p2", target), "--soak"), 2)

    def test_both_checks_run_and_omitted_checks_skip(self):
        repo = self.branch(PLAN.replace("- Load More adds trips [req: P1-2]\n", ""))
        result = self.body(repo, LINK + DEFER.replace("docs/plan.md#p2", "docs/plan.md#p1"), "--plans", "docs/*.md")
        self.assert_code(result, 1, "P1-2 has no criterion", "has no [takes: P1-2]")
        self.assert_code(gate("ticket-coverage", repo), 0)

    def test_formats_and_soak_follow_contract(self):
        repo = self.branch(PLAN.replace("- Load More adds trips [req: P1-2]\n", ""))
        result = self.plans(repo, "--format", "json")
        self.assert_code(result, 1)
        data = json.loads(result.stdout)
        self.assertEqual(data["status"], "violations")
        self.assertEqual(data["findings"][0]["path"], "docs/plan.md")
        self.assertEqual(data["findings"][0]["line"], 6)
        self.assert_code(self.plans(repo, "--format", "github", "--soak"), 0, "::warning file=docs/plan.md,line=6", "P1-2")
        self.assert_code(self.body(repo, LINK + PROOF, "--format", "json"), 0)

    def test_an_inline_not_checked_paragraph_covers_an_id(self):
        plan = PLAN.replace("- Load More adds trips [req: P1-2]\n", "").replace(
            "[req: P1-2]\n\n### Acceptance", "[req: P1-2]\n\nNot checked in a browser: paging is a unit test [req: P1-2]\n\n### Acceptance", 1)
        self.assert_code(self.plans(self.branch(plan)), 0)
        self.assertNotIn("Not checked in a browser", self.plans(self.branch(plan)).stdout)

    def test_keyed_ledger_rows_must_name_a_defined_requirement(self):
        ledger = "\n## Ledger\n\n| ID | Owner | Note |\n| --- | --- | --- |\n| C039 | P1 | pending and rejected [req: P1-1] |\n"
        repo = self.branch(PLAN + ledger)
        self.assert_code(self.plans(repo, "--keyed-column", "owner"), 0)
        # The C039 shape: the ledger assigns a row to a family and no requirement bullet carries it.
        untagged = PLAN + ledger.replace(" [req: P1-1]", "")
        self.assert_code(self.plans(self.branch(untagged), "--keyed-column", "Owner"), 1, "table row has no [req: ID] tag", "C039")
        dangling = PLAN + ledger.replace("P1-1", "P1-9")
        self.assert_code(self.plans(self.branch(dangling), "--keyed-column", "Owner"), 1, "P1-9 is defined nowhere")
        # Without the flag, or in a table without the column, rows are not judged.
        self.assert_code(self.plans(self.branch(untagged)), 0)
        self.assert_code(self.plans(self.branch(untagged), "--keyed-column", "Family"), 0)

    def test_load_more_incident_is_caught_in_the_plan_and_the_pr(self):
        plan = PLAN.replace("- Itineraries paginate with a Load More button", "- Itineraries show details and have a Load More button")
        plan = plan.replace("- Load More adds trips [req: P1-2]\n", "").replace("[takes: P1-2]", "")
        repo = self.branch(plan)
        self.assert_code(self.plans(repo), 1, "P1-2 has no criterion and no not-checked entry")
        self.assert_code(self.body(repo, LINK + DEFER), 1, "P1-2", "Load More button", "has no [takes: P1-2]")

    def test_plain_manifest_plan_subscription_runs(self):
        repo = self.branch(base={".vibe-verifier": "ticket-coverage --plans 'docs/*.md'\n"})
        self.assert_code(runner("run", "--repo", repo, "--manifest", os.path.join(repo, ".vibe-verifier"), "--base-ref", "main"), 0, "ticket-coverage", "PASS")

    def test_criteria_runs_subscribed_coverage_before_browser_skip(self):
        repo = self.branch(base={".vibe-verifier-qae": "ticket-coverage --body body.md\n"})
        write(repo, {"body.md": LINK + "## Acceptance criteria\n\n- None: no browser check\n", "changed": "README.md\n"})
        command = [sys.executable, str(ROOT / "bin/vibe-verifier"), "criteria", "body.md", "--manifest", ".vibe-verifier-qae",
                   "--changed-files", "changed", "--base-ref", "main"]
        result = subprocess.run(command, cwd=repo, capture_output=True, text=True, env=clean_env())
        self.assert_code(result, 1, "P1-2", "claim with")
        self.assertNotIn("none:", result.stdout)
        write(repo, {"body.md": LINK + DEFER + "\n## Acceptance criteria\n\n- None: no browser check\n"})
        result = subprocess.run(command, cwd=repo, capture_output=True, text=True, env=clean_env())
        self.assert_code(result, 0)
        self.assertTrue(result.stdout.startswith("none:"), result.stdout)

    def test_qae_templates_subscribe_before_exploring_with_full_history(self):
        self.assertIn("ticket-coverage --body qae-inputs/pr-body.md", (ROOT / "harnesses/qae/manifest").read_text())
        for name in ("explore.yml", "explore-codex.yml"):
            text = (ROOT / "harnesses/qae" / name).read_text()
            criteria = text.split("\n  criteria:", 1)[1].split("\n  explore:", 1)[0]
            self.assertIn("fetch-depth: 0", criteria)
            self.assertIn("persist-credentials: false", criteria)
            self.assertIn("manifest: qae-inputs/manifest", criteria)
            verify = text.split("\n  verify:", 1)[1]
            read = verify.split("- name: Read the criteria", 1)[1].split("- uses: actions/download-artifact", 1)[0]
            self.assertIn("manifest: .vibe-verifier-qae", read)


if __name__ == "__main__":
    unittest.main()
