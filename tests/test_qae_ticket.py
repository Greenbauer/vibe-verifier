"""The ticket's criteria in the QAE harness: a consumer's workflow writes the acceptance criteria of the
ticket a pull request implements to qae-inputs/ticket.md, and `vibe-verifier criteria`, actions/criteria,
acceptance-verdict and qae-artifacts hold them as TC1, TC2, ... in addition to the pull request's own,
so a pull request cannot drop a ticket requirement from its list. Driven through the command lines, the
shipped action and the template's own steps."""
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from helpers import ROOT, clean_env, commit, gate, make_repo, runner, write
from test_qae_annotations import action_script
from test_qae_artifacts import CLEAN_REQUESTS, session_with

TEMPLATE = ROOT / "harnesses" / "qae" / "explore.yml"
MANIFEST = ROOT / "harnesses" / "qae" / "manifest"
BODY = "## Why\n\nx\n\n## Acceptance criteria\n\n- The cart shows its total\n"
NONE = "## Why\n\nx\n\n## Acceptance criteria\n\n- None: a refactor with no visible change\n"
# The shape of a Dependabot body: prose, HTML and a bullet list, and no Markdown heading.
DEPENDABOT = ("Bumps [sharp](https://github.com/lovell/sharp) from 0.35.4 to 0.35.5.\n<details>\n"
              "<summary>Release notes</summary>\n<blockquote>\n<h2>v0.35.5</h2>\n<ul>\n"
              "<li>Add upper bounds check on length of <code>linear</code> arrays.</li>\n</ul>\n</blockquote>\n"
              "</details>\n\nYou can trigger Dependabot actions by commenting on this PR:\n"
              "- `@dependabot rebase` will rebase this PR\n- `@dependabot recreate` will recreate this PR\n")
TICKET = ("# Show prices in euros\n\n## Context\n\nCustomers in the EU asked.\n\n## Acceptance criteria\n\n"
          "- Prices show in euros\n- The checkout total matches the cart [as: shopper]\n")
VERDICT = ("acceptance-check: AC1 -- PASS -- shown (qae/AC1.md::step 1: opened /cart -> a total)\n"
           "acceptance-check: TC1 -- PASS -- euros (qae/TC1.md::step 1: opened /shop -> prices in euros)\n"
           "acceptance-check: TC2 -- PASS -- matches (qae/TC2.md::step 1: as shopper: checked out -> the cart's total)\n")


def workdir(test):
    path = tempfile.mkdtemp(prefix="vv-ticket-")
    test.addCleanup(shutil.rmtree, path, True)
    return path


def step_script(start, end):
    text = TEMPLATE.read_text()
    step = text[text.index(start):text.index(end)]
    match = re.search(r"^( +)run: \|\n((?:\1 .*\n|\n)+)", step, re.MULTILINE)
    indent = len(match.group(1)) + 2
    return "".join(line[indent:] if line.strip() else "\n" for line in match.group(2).splitlines(True))


class Criteria(unittest.TestCase):
    def run_criteria(self, body, ticket, *changed):
        work = workdir(self)
        write(work, {"pr-body.md": body, "ticket.md": ticket, "changed": "".join(path + "\n" for path in changed)})
        args = ["criteria", os.path.join(work, "pr-body.md"), "--ticket", os.path.join(work, "ticket.md")]
        return runner(*args, *(["--changed-files", os.path.join(work, "changed")] if changed else []))

    def test_the_tickets_criteria_count_with_the_pull_requests(self):
        result = self.run_criteria(BODY, TICKET, "app/cart.tsx")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "criteria: 3\nTC2 roles: shopper\n")

    def test_a_pull_request_cannot_declare_or_leave_its_tickets_criteria_away(self):
        self.assertEqual(self.run_criteria(NONE, TICKET, "app/shop.tsx").stdout, "criteria: 2\nTC2 roles: shopper\n")
        # No criteria of its own and only unrendered paths would need no check, but the ticket lists two.
        self.assertEqual(self.run_criteria("## Why\n\ndocs\n", TICKET, "README.md").stdout.splitlines()[0], "criteria: 2")

    def test_a_dependency_update_is_held_to_the_criteria_its_workflow_supplies_and_to_no_line_of_its_body(self):
        # A consumer's workflow supplies default criteria for a pull request that changes only the package
        # manifest. The body has no `Acceptance criteria` heading, so it lists none: read as a plain list
        # it was one criterion a line (105 on a real one), which no explorer could ever answer.
        result = self.run_criteria(DEPENDABOT, TICKET, "package.json", "package-lock.json")
        self.assertEqual(result.stdout, "criteria: 2\nTC2 roles: shopper\n")
        # Without them it lists none on a path the site serves: nothing is explored, and the gate refuses it.
        self.assertEqual(self.run_criteria(DEPENDABOT, "", "package-lock.json").stdout, "criteria: 0\n")

    def test_an_empty_ticket_links_none_and_a_ticket_that_declares_none_has_none(self):
        self.assertEqual(self.run_criteria(NONE, "").stdout, "none: a refactor with no visible change\n")
        self.assertEqual(self.run_criteria(BODY, "## Acceptance criteria\n\n- None: a chore\n").stdout, "criteria: 1\n")

    def test_a_ticket_whose_criteria_cannot_be_found_still_needs_a_check(self):
        result = self.run_criteria(NONE, "# A ticket\n\n## Context\n\nAcceptance criteria: prices in euros\n")
        self.assertEqual(result.stdout.splitlines()[0], "criteria: 0")
        self.assertIn("ticket: the ticket in", result.stdout)
        self.assertIn("lists no acceptance criteria", result.stdout)

    def test_a_ticket_criterion_the_body_proves_with_a_test_is_not_walked(self):
        proof = "\nticket-proof: TC1 -- euros are a unit-tested format (lib/money.test.ts::formats euros)\n"
        self.assertEqual(self.run_criteria(BODY + proof, TICKET, "app/cart.tsx").stdout,
                         "criteria: 2\nTC1 proven by a test, not walked\nTC2 roles: shopper\n")
        # Every ticket criterion proven and the body declaring None: nothing is left for a browser.
        both = proof + "ticket-proof: TC2 -- (lib/checkout.test.ts::matches the cart total)\n"
        self.assertEqual(self.run_criteria(NONE + both, TICKET, "app/shop.tsx").stdout, "none: a refactor with no visible change\n")
        # A proof line inside an HTML comment (a template's example) proves nothing.
        self.assertEqual(self.run_criteria(BODY + "<!--" + proof + "-->\n", TICKET).stdout.splitlines()[0], "criteria: 3")

    def test_the_action_counts_the_ticket_and_passes_its_text_on(self):
        work = workdir(self)
        write(work, {"pr-body.md": NONE, "ticket.md": TICKET})
        output = Path(work, "github-output")
        output.write_text("")
        result = subprocess.run(["bash", "-e", "-c", action_script("criteria")], cwd=work, capture_output=True, text=True,
                                env=clean_env({"GITHUB_ACTION_PATH": str(ROOT / "actions" / "criteria"), "GITHUB_OUTPUT": str(output),
                                               "VV_PATH": "pr-body.md", "VV_CHANGED": "", "VV_TICKET": "ticket.md",
                                               "VV_MAX_SHARDS": "1", "VV_MANIFEST": "", "VV_ENTRIES": ""}))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        text = output.read_text()
        self.assertTrue(text.startswith("count=2\ndeclared-none=\nticket<<VV_TICKET_"), text)
        delimiter = text.split("ticket<<", 1)[1].split("\n", 1)[0]
        self.assertEqual(len(delimiter), len("VV_TICKET_") + 32)
        self.assertEqual(text.split(delimiter + "\n", 1)[1].rsplit("\n" + delimiter + "\n", 1)[0], TICKET)


class Gates(unittest.TestCase):
    def setUp(self):
        self.repo = make_repo(self, {"README.md": "x\n"})
        self.work = workdir(self)
        self.artifacts = os.path.join(self.work, "qae-artifacts")
        write(self.artifacts, {"qae/AC1.md": "- step 1: opened /cart -> a total\n", "qae/AC1-step-1.png": "png",
                               "qae/TC1.md": "- step 1: opened /shop -> prices in euros\n", "qae/TC1-step-1.png": "png",
                               "qae/TC2.md": "- step 1: as shopper: checked out -> the cart's total\n", "qae/TC2-step-1.png": "png",
                               "session-1/session.md": session_with(CLEAN_REQUESTS)})

    def verdict_gate(self, body=BODY, ticket=TICKET, verdict=VERDICT, *extra):
        write(self.work, {"pr-body.md": body, "ticket.md": ticket, "verdict.md": verdict})
        return gate("acceptance-verdict", self.repo, "--criteria", os.path.join(self.work, "pr-body.md"),
                    "--verdict", os.path.join(self.work, "verdict.md"), "--artifacts", self.artifacts,
                    "--ticket", os.path.join(self.work, "ticket.md"), *extra)

    def artifacts_gate(self, body=BODY, ticket=TICKET, *extra):
        write(self.work, {"pr-body.md": body, "ticket.md": ticket, "site-url": "http://localhost:3000\n"})
        return gate("qae-artifacts", self.repo, "--artifacts", self.artifacts, "--criteria", os.path.join(self.work, "pr-body.md"),
                    "--site-file", os.path.join(self.work, "site-url"), "--ticket", os.path.join(self.work, "ticket.md"), *extra)

    def test_every_criterion_of_the_pull_request_and_its_ticket_passed(self):
        result = self.verdict_gate()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.artifacts_gate().returncode, 0)

    def test_a_dependency_update_passes_on_its_supplied_criteria_alone(self):
        ticket_only = "\n".join(VERDICT.splitlines()[1:]) + "\n"
        result = self.verdict_gate(DEPENDABOT, TICKET, ticket_only)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.artifacts_gate(DEPENDABOT, TICKET).returncode, 0)
        # and still needs every one of them: no ticket criterion is excused with the body's lines
        result = self.verdict_gate(DEPENDABOT, TICKET, ticket_only.splitlines()[0] + "\n")
        self.assertEqual(result.returncode, 1)
        self.assertIn("TC2 has no `acceptance-check: TC2` line", result.stdout)
        self.assertNotIn("AC1", result.stdout)
        # with no supplied criteria it has none at all, which the gate refuses
        result = self.verdict_gate(DEPENDABOT, "", "")
        self.assertEqual(result.returncode, 1)
        self.assertIn("no acceptance criteria found", result.stdout)

    def test_a_ticket_criterion_without_its_pass_is_refused(self):
        result = self.verdict_gate(BODY, TICKET, VERDICT.replace("TC1 -- PASS", "TC1 -- FAIL"))
        self.assertEqual(result.returncode, 1)
        self.assertIn("TC1 is not a PASS", result.stdout)
        result = self.verdict_gate(BODY, TICKET, "\n".join(VERDICT.splitlines()[:2]) + "\n")
        self.assertIn("TC2 has no `acceptance-check: TC2` line", result.stdout)

    def test_a_declared_none_does_not_drop_the_tickets_criteria(self):
        result = self.verdict_gate(NONE, TICKET, "\n".join(VERDICT.splitlines()[1:]) + "\n")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        result = self.verdict_gate(NONE, TICKET, "")
        self.assertEqual(result.returncode, 1)
        self.assertIn("TC1 has no", result.stdout)
        os.remove(os.path.join(self.artifacts, "qae", "TC2-step-1.png"))
        result = self.artifacts_gate(NONE)
        self.assertEqual(result.returncode, 1)
        self.assertIn("qae/TC2.md: step 1 has no screenshot", result.stdout)

    def test_a_ticket_criterion_no_browser_can_check_fails_and_never_passes(self):
        # A plan section's "Tests:" item (a consumer's plan family had one, 2026-10-13): the explorer FAILs it
        # with no step to anchor, and the gate refuses that; a PASS anchored to a step that is not in its log too.
        ticket = "## Acceptance criteria\n\n- Tests: extend cart-total.test.ts with the empty-cart case\n"
        unverifiable = VERDICT.splitlines()[0] + "\nacceptance-check: TC1 -- FAIL -- not checkable in a browser: asks that a test exists\n"
        result = self.verdict_gate(BODY, ticket, unverifiable)
        self.assertEqual(result.returncode, 1)
        self.assertIn("TC1 is not a PASS", result.stdout)
        claimed = VERDICT.splitlines()[0] + "\nacceptance-check: TC1 -- PASS -- tests exist (qae/TC1.md::step 1: read the test file -> it is there)\n"
        self.assertEqual(self.verdict_gate(BODY, ticket, claimed).returncode, 1)
        prompt = " ".join((ROOT / "harnesses" / "qae" / "prompt.md").read_text().split())
        self.assertIn('A criterion that asks for something no browser can show (that a test exists or passes, that an email '
                      'reached an inbox, a log line, a stored value the site never displays) is one you could not check: FAIL '
                      'it, saying "not checkable in a browser" and what it asks for, and never PASS it on the strength of a '
                      'nearby screen.', prompt)

    def test_a_ticket_criterion_proven_by_a_test_needs_its_anchor_at_the_head_and_no_walk(self):
        commit(self.repo, {"lib/money.test.ts": "it('formats euros as the shop shows them', () => {})\n"}, "test")
        os.remove(os.path.join(self.artifacts, "qae", "TC1.md"))
        os.remove(os.path.join(self.artifacts, "qae", "TC1-step-1.png"))
        proof = "\nticket-proof: TC1 -- no browser needed (lib/money.test.ts::formats euros as the shop shows them)\n"
        walked = "\n".join(line for line in VERDICT.splitlines() if not line.startswith("acceptance-check: TC1")) + "\n"
        result = self.verdict_gate(BODY + proof, TICKET, walked)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("TC1 is proven by a test the pull request names, not walked", result.stdout)
        self.assertEqual(self.artifacts_gate(BODY + proof).returncode, 0)
        # The anchor must resolve in the tree: a title the file does not hold, or a path only the artifacts have, fails.
        result = self.verdict_gate(BODY + proof.replace("as the shop shows them", "in every currency"), TICKET, walked)
        self.assertEqual(result.returncode, 1)
        self.assertIn("TC1: no anchor of its `ticket-proof:` line resolves at the head", result.stdout)
        result = self.verdict_gate(BODY + "\nticket-proof: TC1 -- (qae/TC2.md::step 1: as shopper: checked out)\n", TICKET, walked)
        self.assertEqual(result.returncode, 1)
        self.assertIn("qae/TC2.md is not in the tree", result.stdout)
        # Without the proof TC1 is walked again; a proof for a criterion the ticket does not have is a typo.
        self.assertIn("TC1 has no `acceptance-check: TC1` line", self.verdict_gate(BODY, TICKET, walked).stdout)
        result = self.verdict_gate(BODY + proof + "ticket-proof: TC9 -- (lib/money.test.ts::formats euros)\n", TICKET, walked)
        self.assertEqual(result.returncode, 1)
        self.assertIn("`ticket-proof: tc9` names no criterion of the ticket", result.stdout)

    def test_a_ticket_proven_whole_with_a_declared_none_needs_no_verdict(self):
        commit(self.repo, {"lib/money.test.ts": "it('formats euros as the shop shows them', () => {})\n"}, "test")
        both = ("\nticket-proof: TC1 -- (lib/money.test.ts::formats euros as the shop shows them)\n"
                "ticket-proof: TC2 -- (lib/money.test.ts:1)\n")
        write(self.work, {"pr-body.md": NONE + both, "ticket.md": TICKET})
        result = gate("acceptance-verdict", self.repo, "--criteria", os.path.join(self.work, "pr-body.md"),
                      "--verdict", os.path.join(self.work, "no-verdict.md"), "--ticket", os.path.join(self.work, "ticket.md"))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_an_annotation_on_a_ticket_criterion_is_held_to_its_log(self):
        write(self.artifacts, {"qae/TC2.md": "- step 1: checked out -> the cart's total\n"})
        result = self.verdict_gate(BODY, TICKET, VERDICT.replace("as shopper: checked out", "checked out"))
        self.assertEqual(result.returncode, 1)
        self.assertIn("TC2 is a PASS, but no step line in qae/TC2.md starts `as shopper:`", result.stdout)

    def test_a_ticket_whose_criteria_cannot_be_found_is_a_finding(self):
        unreadable = "# A ticket\n\n## Context\n\nAcceptance criteria: prices in euros\n"
        result = self.verdict_gate(NONE, unreadable, "")
        self.assertEqual(result.returncode, 1)
        self.assertIn("lists no acceptance criteria", result.stdout)

    def test_an_empty_ticket_changes_nothing(self):
        self.assertEqual(self.verdict_gate(NONE, "", "").returncode, 0)
        self.assertEqual(self.verdict_gate(BODY, "", VERDICT.splitlines()[0] + "\n").returncode, 0)
        self.assertEqual(self.artifacts_gate(NONE, "").returncode, 0)

    def test_a_tickets_expected_refusal_is_honoured(self):
        ticket = "## Acceptance criteria\n\n- Signed out, the orders API refuses (expected-refusal: 401 /api/orders)\n"
        write(self.artifacts, {"session-1/session.md": session_with(CLEAN_REQUESTS + ["[GET] http://localhost:3000/api/orders => [401] Unauthorized"])})
        self.assertEqual(self.artifacts_gate(BODY, ticket).returncode, 0)
        self.assertEqual(self.artifacts_gate(BODY, "").returncode, 1)

    def test_a_missing_ticket_file_cannot_run(self):
        write(self.work, {"pr-body.md": BODY, "verdict.md": VERDICT})
        for name in ("acceptance-verdict", "qae-artifacts"):
            args = (["--verdict", os.path.join(self.work, "verdict.md")] if name == "acceptance-verdict" else [])
            result = gate(name, self.repo, "--criteria", os.path.join(self.work, "pr-body.md"), "--artifacts", self.artifacts,
                          "--ticket", os.path.join(self.work, "missing.md"), *args, "--soak")
            self.assertEqual(result.returncode, 2, name)


class Template(unittest.TestCase):
    """The criteria job reads the ticket before any model runs and passes it on; the explorer and the
    verify job's gates read that text, never a file the pull request or the explorer could change."""

    def test_one_consumer_step_writes_the_ticket_in_the_criteria_job(self):
        text = TEMPLATE.read_text()
        criteria = text[text.index("jobs:\n  criteria:\n"):text.index("\n  explore:\n")]
        self.assertIn("- name: Write the ticket's criteria   # CONSUMER", criteria)
        self.assertLess(criteria.index("- name: Write the ticket's criteria"), criteria.index("- name: Read the criteria"))
        self.assertIn("      ticket: ${{ steps.criteria.outputs.ticket }}\n", criteria)
        self.assertEqual(text.count("          ticket: qae-inputs/ticket.md\n"), 3)  # both criteria reads and the review
        self.assertEqual(text.count("          TICKET: ${{ needs.criteria.outputs.ticket }}\n"), 2)  # explore and verify inputs
        for line in MANIFEST.read_text().splitlines():
            if line.startswith(("acceptance-verdict ", "qae-artifacts ")):
                self.assertIn("--ticket qae-inputs/ticket.md", line)

    def test_the_default_step_reads_the_linked_plan_section(self):
        for template in (TEMPLATE, TEMPLATE.with_name("explore-codex.yml")):
            text = template.read_text()
            step = text[text.index("- name: Write the ticket's criteria"):text.index("- name: Read the criteria\n        # A PR")]
            self.assertIn("uses: Greenbauer/vibe-verifier/actions/ticket@", step, template.name)
            self.assertIn("body: qae-inputs/pr-body.md\n", step, template.name)
            self.assertIn("out: qae-inputs/ticket.md\n", step, template.name)
            self.assertIn("names: ''   # CONSUMER", step, template.name)


PLAN = ("# Continuation plan\n\n## Families\n\n### A. Shell\n\n- Badge counts notifications\n\n"
        "### B. Notifications (C010-C013; F037/F042 shared)\n\nSources: the legacy modal.\n\n"
        "- Mark All selects every informational row\n  without a write\n"
        "- Read confirms the count\n  - Cancel keeps the selection\n\n"
        "Notes that end the list.\n\n#### Roles\n\n1. Each seat walks it at 375 pixels\n\n"
        "```\n- not an item\n### B. Notifications (C010-C013; F037/F042 shared)\n```\n\n"
        "### C. Dashboard\n\n- Folders collapse\n\n### C. Dashboard\n\n- The second C\n\n### Empty\n\nNo list.\n")
ANCHOR = "b-notifications-c010-c013-f037f042-shared"


class TicketLink(unittest.TestCase):
    """actions/ticket: a `Ticket: <path>#<anchor>` line in the body makes the plan section, read at the base
    commit, the ticket's criteria; no line links none; a link that cannot be read fails, never "no ticket"."""

    def setUp(self):
        self.fixtures = workdir(self)
        write(self.fixtures, {"o/r/docs/plan.md": PLAN})
        bin_dir = workdir(self)
        Path(bin_dir, "gh").write_text("#!/bin/sh\nexec %s %s \"$@\"\n" % (sys.executable, ROOT / "tests" / "fake_gh.py"))
        os.chmod(os.path.join(bin_dir, "gh"), 0o755)
        self.env = clean_env({"PATH": bin_dir + os.pathsep + os.environ["PATH"], "FAKE_GH_ROOT": self.fixtures})
        self.work = workdir(self)

    def run_ticket(self, body, *extra):
        write(self.work, {"body.md": body})
        out = os.path.join(self.work, "ticket.md")
        result = subprocess.run([sys.executable, str(ROOT / "actions" / "ticket" / "ticket.py"), "--body",
                                 os.path.join(self.work, "body.md"), "--repo", "o/r", "--base", "b" * 40, "--out", out, *extra],
                                capture_output=True, text=True, env=self.env)
        return result, (Path(out).read_text() if os.path.exists(out) else None)

    def test_the_linked_section_is_the_ticket_read_at_the_base(self):
        result, text = self.run_ticket(BODY + "\nTicket: docs/plan.md#%s\n" % ANCHOR)
        self.assertEqual(result.returncode, 0, result.stderr)
        criteria = text[text.index("## Acceptance criteria"):]
        self.assertEqual(criteria.splitlines()[2:], [
            "- Mark All selects every informational row without a write",
            "- Read confirms the count Cancel keeps the selection",
            "- Each seat walks it at 375 pixels"])
        self.assertIn("ref=" + "b" * 40, Path(self.fixtures, "calls.log").read_text())
        self.assertIn("ticket: 3 criteria", result.stdout)
        # The criteria job reads them as TC1-TC3 beside the body's AC1.
        write(self.work, {"pr-body.md": BODY})
        read = runner("criteria", os.path.join(self.work, "pr-body.md"), "--ticket", os.path.join(self.work, "ticket.md"))
        self.assertEqual(read.stdout.splitlines()[0], "criteria: 4", read.stdout + read.stderr)

    def test_a_sections_own_acceptance_criteria_are_its_only_criteria(self):
        # A plan family holds sources, data-model and test bullets beside the criteria a browser checks.
        plan = ("# Plan\n\n## Families\n\n### G1. Checkout\n\nSources: legacy.\n\n- Data model: one row per order line\n"
                "- Tests: extend cart-total.test.ts\n\n#### Acceptance criteria\n\n- Pay names the total [as: shopper]\n"
                "- Cancel keeps the cart\n\n##### Roles\n\n- A guest pays once\n\n#### Tests\n\n- not a criterion\n\n"
                "### G2. Receipts\n\n- Receipts show totals\n\n#### Acceptance criteria\n\n- The receipt lists every line\n\n"
                "### G3. Search\n\n- Search finds a product\n")
        write(self.fixtures, {"o/r/docs/plan.md": plan})
        result, text = self.run_ticket("Ticket: docs/plan.md#g1-checkout\nTicket: docs/plan.md#g2-receipts\nTicket: docs/plan.md#g3-search\n")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(text[text.index("## Acceptance criteria"):].splitlines()[2:], [
            "- Pay names the total [as: shopper]", "- Cancel keeps the cart", "- A guest pays once",
            "- The receipt lists every line", "- Search finds a product"])
        # An empty one is a section with no items, never the whole section.
        write(self.fixtures, {"o/r/docs/plan.md": plan.replace("- The receipt lists every line\n", "To be written.\n")})
        result, _ = self.run_ticket("Ticket: docs/plan.md#g2-receipts\n")
        self.assertEqual(result.returncode, 2)
        self.assertIn("lists no items", result.stderr)

    def test_a_repeated_heading_takes_githubs_numbered_anchor(self):
        _, text = self.run_ticket("Ticket: docs/plan.md#c-dashboard-1\n")
        self.assertTrue(text.endswith("## Acceptance criteria\n\n- The second C\n"), text)

    def test_two_links_are_one_ticket(self):
        result, text = self.run_ticket("Ticket: docs/plan.md#a-shell\nTicket: `docs/plan.md#c-dashboard`\n")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(text.endswith("- Badge counts notifications\n- Folders collapse\n"), text)

    def test_no_link_writes_an_empty_ticket(self):
        result, text = self.run_ticket(BODY + "\n```\nTicket: docs/plan.md#a-shell\n```\n")
        self.assertEqual((result.returncode, text), (0, ""), result.stderr)
        self.assertIn("none linked", result.stdout)

    def test_a_body_that_names_a_ticket_without_linking_it_fails(self):
        # A consumer's pull request named its plan only as a Markdown link, and QAE graded the author's list alone.
        names = ("--names", r"TKT-[0-9]+|docs/plan\.md")
        for body in ("Implements TKT-2286.\n", "From the [plan](docs/plan.md), families G1 and G2.\n"):
            result, text = self.run_ticket(BODY + "\n" + body, *names)
            self.assertEqual(result.returncode, 2, body)
            self.assertIn("names `", result.stderr)
            self.assertIn("add a line `Ticket: <path>#<heading anchor>`", result.stderr)
            self.assertIsNone(text, body)
        # A linked section satisfies it; a name inside a code fence is not a name; no pattern asks for nothing.
        result, text = self.run_ticket("Implements TKT-2286.\nTicket: docs/plan.md#a-shell\n", *names)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(text.endswith("- Badge counts notifications\n"), text)
        self.assertEqual(self.run_ticket("```\nTKT-2286\n```\n", *names)[1], "")
        self.assertEqual(self.run_ticket("Implements TKT-2286.\n")[1], "")

    def test_a_link_that_cannot_be_read_fails(self):
        for line, says in (("Ticket: docs/plan.md#no-such-family", "no heading has the anchor #no-such-family"),
                           ("Ticket: docs/gone.md#a-shell", "docs/gone.md cannot be read at the base commit"),
                           ("Ticket: docs/plan.md#empty", "lists no items"),
                           ("Ticket: ../secrets.md#a", "is not a link"),
                           ("Ticket: family B", "is not a link")):
            result, text = self.run_ticket(BODY + "\n" + line + "\n")
            self.assertEqual(result.returncode, 2, line)
            self.assertIn(says, result.stderr, line)
            self.assertIsNone(text, line)

    def test_the_ticket_reaches_the_explorer_and_the_gate(self):
        work = workdir(self)
        bin_dir = workdir(self)
        Path(bin_dir, "gh").write_text('#!/bin/sh\ncase "$1 $2" in\n  pr*) cat "%s" ;;\n  *pulls*) printf "app/shop.tsx\\n" ;;\nesac\n'
                                       % Path(work, "body.md"))
        os.chmod(os.path.join(bin_dir, "gh"), 0o755)
        Path(work, "body.md").write_text(NONE)
        env = clean_env({"PATH": bin_dir + os.pathsep + os.environ["PATH"], "GH_TOKEN": "x", "PR_NUMBER": "7", "REPO": "o/r",
                         "SITE_URL": "http://localhost:3000", "SITE_ORIGINS": "", "REFERENCES": "", "TICKET": TICKET + "\n",
                         "SHARDS": '[""]', "EXPLORE_ATTEMPT": "1", "CARRY": ""})
        explore = workdir(self)
        ran = subprocess.run(["bash", "-e", "-c", step_script("- name: Write the explorer's input", "- name: Select the features")],
                             cwd=explore, capture_output=True, text=True, env=env)
        self.assertEqual(ran.returncode, 0, ran.stdout + ran.stderr)
        self.assertEqual(Path(explore, "qae-inputs", "ticket.md").read_text().strip(), TICKET.strip())
        # What the explore job uploaded: the explorer's logs and the verdict the workflow copied beside them.
        verify = workdir(self)
        evidence = "qae-evidence/qae-artifacts-1/"
        write(verify, {evidence + "qae/TC1.md": "- step 1: opened /shop -> prices in euros\n", evidence + "qae/TC1-step-1.png": "png",
                       evidence + "qae/TC2.md": "- step 1: as shopper: checked out -> the cart's total\n",
                       evidence + "qae/TC2-step-1.png": "png", evidence + "verdicts/1.md": VERDICT,
                       evidence + "session-1/session.md": session_with(CLEAN_REQUESTS)})
        for step in ("- name: Write the declared inputs", "- name: Merge the evidence and read the verdict"):
            ran = subprocess.run(["bash", "-e", "-c", step_script(step, "- name: Run the QA gates")],
                                 cwd=verify, capture_output=True, text=True, env=env)
            self.assertEqual(ran.returncode, 0, ran.stdout + ran.stderr)
        repo = make_repo(self, {"README.md": "x\n"})
        for line in MANIFEST.read_text().splitlines():
            if line.startswith(("acceptance-verdict ", "qae-artifacts ")):
                result = subprocess.run([sys.executable, str(ROOT / "gates" / (line.split()[0].replace("-", "_") + ".py")),
                                         "--repo", repo, *shlex.split(line)[1:]], cwd=verify, capture_output=True, text=True,
                                        env=clean_env())
                self.assertEqual(result.returncode, 0, line + "\n" + result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
