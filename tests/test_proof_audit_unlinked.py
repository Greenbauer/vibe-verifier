"""proof-audit with no `Ticket:` line: nothing is graded, so a body that claims `[req:]` items anyway, or (with
--require-ticket) a pull request that changes a listed path without a `No ticket: <why>` line, is a finding, and
the action's verify step keeps the table when the gate fails. Fixtures and helpers: tests/test_proof_audit.py."""
import re
import subprocess
import unittest

from helpers import ROOT, clean_env, write

import test_proof_audit as base  # as a module, so its test classes are not collected twice


class Unlinked(unittest.TestCase):
    repo, outside, audit, assert_code = (base.ProofAudit.repo, base.ProofAudit.outside, base.ProofAudit.audit,
                                         base.ProofAudit.assert_code)

    def test_requirement_ids_claimed_without_a_ticket_line_fail(self):
        # A body that keeps its proof map but drops the Ticket line would otherwise audit nothing and pass.
        body = "Built the covers.\n\n## Requirements (family C)\n\n- [req: C-8] [proof: tests/covers.test.ts::x]\n- [req: C-15, C-30] same\n"
        result, table = self.audit(self.repo(), body=body)
        self.assert_code(result, 1, "claims 3 requirement ID(s) (C-8, C-15, C-30) but no `Ticket: <plan>#<anchor>` line")
        self.assertIn("## Proof audit: nothing audited: the body claims 3", table)
        result, _ = self.audit(self.repo(), body="Built it.\n\n```\n- [req: C-8] an example\n```\n")
        self.assert_code(result, 0, "no ticket linked")

    def test_a_required_path_changed_with_no_ticket_needs_a_stated_reason(self):
        repo = self.repo()  # its branch changes src/covers.ts
        result, table = self.audit(repo, body="Built it.\n", extra=("--require-ticket", "src/**", "--require-ticket", "app/**"))
        self.assert_code(result, 1, "changes 1 file(s) under --require-ticket (src/covers.ts) and links no plan section")
        self.assertIn("nothing audited", table)
        result, _ = self.audit(repo, body="Built it.\n\nNo ticket: a crash fix no plan section covers.\n",
                               extra=("--require-ticket", "src/**"))
        self.assert_code(result, 0, "no ticket linked")
        # A template's placeholder, a dash, or a reason inside an HTML comment states nothing.
        for body in ("No ticket: <why>\n", "No ticket: -\n", "<!--\nNo ticket: an example\n-->\n"):
            with self.subTest(body=body):
                result, _ = self.audit(repo, body=body, extra=("--require-ticket", "src/**"))
                self.assert_code(result, 1, "links no plan section")
        for extra in (("--require-ticket", "app/**"), ()):
            with self.subTest(extra=extra):
                result, _ = self.audit(repo, body="Built it.\n", extra=extra)
                self.assert_code(result, 0, "no ticket linked")

    def verify_step(self, repo, body, require_ticket=""):
        """The action's verify step, run as GitHub runs `shell: bash` (with -e), against an empty grades directory."""
        text = (ROOT / "actions" / "proof-audit" / "action.yml").read_text()
        step = text[text.index("- name: Hold the grades to the repository"):]
        block = re.search(r"^( +)run: \|\n((?:\1 .*\n|\n)+)", step, re.MULTILINE)
        indent = len(block.group(1)) + 2
        script = "".join(line[indent:] if line.strip() else "\n" for line in block.group(2).splitlines(True))
        work = self.outside()
        write(work, {"body.md": body, "summary.md": ""})
        (work / "grades").mkdir()
        env = dict(clean_env(), GITHUB_ACTION_PATH=str(ROOT / "actions" / "proof-audit"), RUNNER_TEMP=str(work),
                   GITHUB_STEP_SUMMARY=str(work / "summary.md"), VV_BODY=str(work / "body.md"),
                   VV_GRADES=str(work / "grades"), VV_REQUIRE_TICKET=require_ticket, GH_TOKEN="", VV_PR="",
                   VIBE_VERIFIER_BASE_REF="main")
        result = subprocess.run(["bash", "--noprofile", "--norc", "-eo", "pipefail", "-c", script], cwd=repo, env=env,
                                capture_output=True, text=True)
        return result, (work / "summary.md").read_text()

    def test_the_action_passes_each_glob_and_keeps_the_table_when_the_gate_fails(self):
        repo = self.repo()
        result, summary = self.verify_step(repo, "Built it.\n", require_ticket="\n  src/**  \n\napp/**\n")
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("changes 1 file(s) under --require-ticket (src/covers.ts)", result.stdout)
        self.assertIn("## Proof audit: nothing audited: the pull request changes 1 file(s)", summary)
        result, summary = self.verify_step(repo, "Built it.\n")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("## Proof audit: nothing to audit (no ticket linked)", summary)


class UnlinkedLocalRun(unittest.TestCase):
    setUp, run_grader = base.Grader.setUp, base.Grader.run_grader

    def test_the_local_run_passes_require_ticket_to_the_gate(self):
        write(self.work, {"plain.md": "Built the covers.\n"})
        result = self.run_grader("--body", str(self.work / "plain.md"), "--require-ticket", "src/**")
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("changes 1 file(s) under --require-ticket (src/covers.ts)", result.stdout)
        self.assertFalse((self.work / "calls.jsonl").exists())
