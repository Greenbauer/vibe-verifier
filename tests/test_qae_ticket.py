"""The ticket's criteria in the QAE harness: a consumer's workflow writes the acceptance criteria of the
ticket a pull request implements to qae-inputs/ticket.md, and `vibe-verifier criteria`, actions/criteria,
acceptance-verdict and qae-artifacts hold them as TC1, TC2, ... in addition to the pull request's own,
so a pull request cannot drop a ticket requirement from its list. Driven through the command lines, the
shipped action and the template's own steps."""
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from helpers import ROOT, clean_env, gate, make_repo, runner, write
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

    def test_the_action_counts_the_ticket_and_passes_its_text_on(self):
        work = workdir(self)
        write(work, {"pr-body.md": NONE, "ticket.md": TICKET})
        output = Path(work, "github-output")
        output.write_text("")
        result = subprocess.run(["bash", "-e", "-c", action_script("criteria")], cwd=work, capture_output=True, text=True,
                                env=clean_env({"GITHUB_ACTION_PATH": str(ROOT / "actions" / "criteria"), "GITHUB_OUTPUT": str(output),
                                               "VV_PATH": "pr-body.md", "VV_CHANGED": "", "VV_TICKET": "ticket.md"}))
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

    def test_the_default_step_links_no_ticket(self):
        work = workdir(self)
        os.mkdir(os.path.join(work, "qae-inputs"))
        text = TEMPLATE.read_text()
        step = text[text.index("- name: Write the ticket's criteria"):text.index("- name: Read the criteria\n        # A PR")]
        command = re.search(r"run: '([^']*)'", step).group(1)
        self.assertEqual(subprocess.run(["bash", "-e", "-c", command], cwd=work).returncode, 0)
        self.assertEqual(Path(work, "qae-inputs", "ticket.md").read_text(), "")

    def test_the_ticket_reaches_the_explorer_and_the_gate(self):
        work = workdir(self)
        bin_dir = workdir(self)
        Path(bin_dir, "gh").write_text('#!/bin/sh\ncase "$1 $2" in\n  pr*) cat "%s" ;;\n  *pulls*) printf "app/shop.tsx\\n" ;;\n'
                                       '  api*) cat "%s" ;;\nesac\n' % (Path(work, "body.md"), Path(work, "comments.json")))
        os.chmod(os.path.join(bin_dir, "gh"), 0o755)
        Path(work, "body.md").write_text(NONE)
        Path(work, "comments.json").write_text(json.dumps([{"user": {"login": "github-actions[bot]"}, "body": VERDICT}]))
        env = clean_env({"PATH": bin_dir + os.pathsep + os.environ["PATH"], "GH_TOKEN": "x", "PR_NUMBER": "7", "REPO": "o/r",
                         "SITE_URL": "http://localhost:3000", "SITE_ORIGINS": "", "REFERENCES": "", "TICKET": TICKET + "\n"})
        explore = workdir(self)
        ran = subprocess.run(["bash", "-e", "-c", step_script("- name: Write the explorer's input", "- name: Select the features")],
                             cwd=explore, capture_output=True, text=True, env=env)
        self.assertEqual(ran.returncode, 0, ran.stdout + ran.stderr)
        self.assertEqual(Path(explore, "qae-inputs", "ticket.md").read_text().strip(), TICKET.strip())
        verify = workdir(self)
        ran = subprocess.run(["bash", "-e", "-c", step_script("- name: Write the declared inputs", "- name: Run the QA gates")],
                             cwd=verify, capture_output=True, text=True, env=env)
        self.assertEqual(ran.returncode, 0, ran.stdout + ran.stderr)
        write(verify, {"qae-artifacts/qae/TC1.md": "- step 1: opened /shop -> prices in euros\n", "qae-artifacts/qae/TC1-step-1.png": "png",
                       "qae-artifacts/qae/TC2.md": "- step 1: as shopper: checked out -> the cart's total\n",
                       "qae-artifacts/qae/TC2-step-1.png": "png",
                       "qae-artifacts/session-1/session.md": session_with(CLEAN_REQUESTS)})
        repo = make_repo(self, {"README.md": "x\n"})
        for line in MANIFEST.read_text().splitlines():
            if line.startswith(("acceptance-verdict ", "qae-artifacts ")):
                result = subprocess.run([sys.executable, str(ROOT / "gates" / (line.split()[0].replace("-", "_") + ".py")),
                                         "--repo", repo, *shlex.split(line)[1:]], cwd=verify, capture_output=True, text=True,
                                        env=clean_env())
                self.assertEqual(result.returncode, 0, line + "\n" + result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
