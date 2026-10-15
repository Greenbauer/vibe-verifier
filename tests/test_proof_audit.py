"""proof-audit: a model's grades count only when every quote it gives is in the repository, and a usage
limit or any other failed grading run is could-not-run, never a pass. The grader runs a fake Codex CLI
here; tests/fixtures/proof-audit/c8 replays a real grade."""
import copy
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from helpers import ROOT, clean_env, commit, gate, git, make_repo, runner, write

sys.path.insert(0, str(ROOT / "gates"))
import _proof_audit  # noqa: E402
import proof_audit  # noqa: E402

FIXTURE = ROOT / "tests" / "fixtures" / "proof-audit" / "c8"
PLAN = ("# Plan\n\n## C. Covers\n\n"
        "- Created values persist after a reload. [req: C-15]\n"
        "- Cover upload keeps only the first dropped file. [req: C-8]\n"
        "- Gallery video limits belong to the video picker (family P3). [req: C-30]\n\n"
        "### Acceptance criteria\n\n- Create a journey and reload it [req: C-15]\n\n"
        "### Not checked in a browser\n\n[req: C-8] and [req: C-30] are graded by tests.\n\n"
        "## P3. Gallery\n\n- The video picker limits videos. [req: P3-1] [takes: C-30]\n\n"
        "### Acceptance criteria\n\n- Videos are limited [req: P3-1, C-30]\n")
TEST = ('import assert from "node:assert/strict";\n'          # 1
        'import test from "node:test";\n'                     # 2
        '\n'                                                  # 3
        'test("a created journey reads back after a reload", async () => {\n'  # 4
        '  const created = await api.create({ title: "Lake" });\n'           # 5
        '  const reread = await api.read(created.id);\n'                      # 6
        '  assert.equal(reread.title, "Lake");\n'                             # 7
        '});\n'                                                               # 8
        '\n'                                                                  # 9
        'test("a second PNG dropped with the first is not cropped", () => {\n'  # 10
        '  drop(file("first.png"), file("second.png"));\n'                     # 11
        '  assert.equal(cropped(), "first.png");\n'                            # 12
        '});\n')                                                               # 13
BODY = "Ticket: docs/plan.md#c-covers\n\nBuilt the covers.\n"
CASE_15 = {"case": "a created value reads back after a reload", "proof": [
    {"file": "tests/covers.test.ts", "test": "a created journey reads back after a reload", "line": 7,
     "assertion": 'assert.equal(reread.title, "Lake");'}]}
CASE_8 = {"case": "only the first of two acceptable dropped files is cropped", "proof": [
    {"file": "tests/covers.test.ts", "test": "a second PNG dropped with the first is not cropped", "line": 12,
     "assertion": 'assert.equal(cropped(), "first.png");'}]}
EXCLUSION = {"plan": "docs/plan.md", "line": 7, "quote": "Gallery video limits belong to the video picker (family P3).",
             "owner": "docs/plan.md#p3-gallery"}


def grade(rid, status, cases=(), exclusion=None, missing=""):
    return {"id": rid, "status": status, "cases": list(cases), "exclusion": exclusion, "missing": missing}


def valid():
    return copy.deepcopy([grade("C-15", "PROVEN", [CASE_15]), grade("C-8", "PROVEN", [CASE_8]),
                          grade("C-30", "EXCLUDED", exclusion=EXCLUSION)])


def envelope(shard, ids, grades=None, error=None):
    return {"shard": shard, "ids": ids, "error": error,
            "output": None if grades is None else {"requirements": grades}, "seconds": 1.0}


class ProofAudit(unittest.TestCase):
    def repo(self, plan=PLAN, test=TEST):
        repo = make_repo(self, {"docs/plan.md": plan, "tests/covers.test.ts": test})
        git(repo, "checkout", "-q", "-b", "feat/covers")
        commit(repo, {"src/covers.ts": "export {};\n"})
        return repo

    def outside(self):
        work = tempfile.mkdtemp(prefix="vv-proof-")
        self.addCleanup(shutil.rmtree, work, True)
        return Path(work)

    def audit(self, repo, *envelopes, body=BODY, extra=()):
        work = self.outside()
        write(work, {"body.md": body})
        for number, item in enumerate(envelopes, 1):
            write(work, {"grades/proof-audit-%d/%d.json" % (number, item["shard"]): json.dumps(item)})
        result = gate("proof-audit", repo, "--body", str(work / "body.md"), "--grades", str(work / "grades"),
                      "--base-ref", "main", "--table", str(work / "table.md"), *extra)
        table = (work / "table.md").read_text() if (work / "table.md").exists() else ""
        return result, table

    def assert_code(self, result, code, *words):
        self.assertEqual(result.returncode, code, result.stdout + result.stderr)
        for word in words:
            self.assertIn(word, result.stdout + result.stderr)

    def test_a_valid_run_passes_and_lists_every_id(self):
        result, table = self.audit(self.repo(), envelope(1, ["C-15", "C-8", "C-30"], valid()))
        self.assert_code(result, 0)
        self.assertIn("3 of 3 requirements proven or excluded", table)
        self.assertIn("| C-8 | PROVEN | only the first of two acceptable dropped files is cropped: `tests/covers.test.ts:12`", table)
        self.assertIn("| C-30 | EXCLUDED | docs/plan.md#p3-gallery", table)

    def test_shards_are_graded_together(self):
        grades = valid()
        result, _ = self.audit(self.repo(), envelope(1, ["C-15", "C-8"], grades[:2]), envelope(2, ["C-30"], grades[2:]))
        self.assert_code(result, 0)

    def test_a_fabricated_quote_is_unproven(self):
        for line, assertion in ((12, 'assert.equal(cropped(), "second.png");'), (11, 'assert.equal(cropped(), "first.png");'),
                                (99, 'assert.equal(cropped(), "first.png");')):
            with self.subTest(line=line, assertion=assertion):
                case = {"case": CASE_8["case"], "proof": [dict(CASE_8["proof"][0], line=line, assertion=assertion)]}
                grades = valid()
                grades[1] = grade("C-8", "PROVEN", [case])
                result, table = self.audit(self.repo(), envelope(1, ["C-15", "C-8", "C-30"], grades))
                self.assert_code(result, 1, "C-8 UNPROVEN", "docs/plan.md:6")
                self.assertIn("| C-8 | UNPROVEN |", table)
                self.assertRegex(result.stdout, "fabricated quote|has no line 99")

    def test_a_quote_too_short_to_mean_anything_is_unproven(self):
        case = {"case": CASE_8["case"], "proof": [dict(CASE_8["proof"][0], assertion=");")]}
        grades = valid()
        grades[1] = grade("C-8", "PROVEN", [case])
        result, _ = self.audit(self.repo(), envelope(1, ["C-15", "C-8", "C-30"], grades))
        self.assert_code(result, 1, "too short to check")

    def test_a_missing_test_title_or_file_is_unproven(self):
        other = "a created journey reads back after a reload"
        for change, words in (({"test": "a second GIF dropped with the first is not cropped"}, "not 'a second GIF"),
                              ({"test": other}, "is in the test 'a second PNG dropped with the first is not cropped', not"),
                              ({"line": 7, "assertion": 'assert.equal(reread.title, "Lake");'}, "is in the test '%s'" % other),
                              ({"assertion": 'cropped(), "first.png"'}, "as its whole line"),
                              ({"line": 4, "assertion": 'test("a created journey reads back after a reload", async () => {',
                                "test": other}, "no test is declared above"),
                              ({"file": "./tests/missing.test.ts"}, "tests/missing.test.ts is not a file in the repository"),
                              ({"file": "docs/plan.md", "line": 6, "assertion": "- Cover upload keeps only the first dropped "
                                "file. [req: C-8]"}, "docs/plan.md is not a test file")):
            with self.subTest(change=change):
                case = {"case": CASE_8["case"], "proof": [dict(CASE_8["proof"][0], **change)]}
                grades = valid()
                grades[1] = grade("C-8", "PROVEN", [case])
                result, _ = self.audit(self.repo(), envelope(1, ["C-15", "C-8", "C-30"], grades))
                self.assert_code(result, 1, "C-8 UNPROVEN", words)

    def test_an_unproven_id_fails_with_what_is_missing(self):
        grades = valid()
        grades[1] = grade("C-8", "UNPROVEN", [], missing="no test drops two acceptable files")
        result, table = self.audit(self.repo(), envelope(1, ["C-15", "C-8", "C-30"], grades))
        self.assert_code(result, 1, "C-8 UNPROVEN: no test drops two acceptable files",
                         "Requirement: Cover upload keeps only the first dropped file.")
        self.assertIn("2 of 3 requirements proven or excluded", table)

    def test_proven_needs_an_assertion_for_every_case(self):
        grades = valid()
        grades[0] = grade("C-15", "PROVEN", [CASE_15, {"case": "an edited value reads back", "proof": []}])
        result, _ = self.audit(self.repo(), envelope(1, ["C-15", "C-8", "C-30"], grades))
        self.assert_code(result, 1, "C-15 UNPROVEN", "has no assertion")
        grades[0] = grade("C-15", "PROVEN", [])
        result, _ = self.audit(self.repo(), envelope(1, ["C-15", "C-8", "C-30"], grades))
        self.assert_code(result, 1, "C-15 UNPROVEN", "with no case")

    def test_a_missing_or_doubled_grade_is_unproven(self):
        grades = valid()
        result, _ = self.audit(self.repo(), envelope(1, ["C-15", "C-8", "C-30"], grades[1:]))
        self.assert_code(result, 1, "C-15 UNPROVEN: the grader returned no grade")
        result, _ = self.audit(self.repo(), envelope(1, ["C-15", "C-8", "C-30"], grades + grades[:1]))
        self.assert_code(result, 1, "C-15 UNPROVEN: the grader graded it 2 times")

    def test_an_exclusion_needs_an_owner_that_takes_the_item_at_the_merge_base(self):
        cases = ((dict(EXCLUSION, owner="docs/plan.md#c-covers"), "is a section this pull request links"),
                 (dict(EXCLUSION, owner="./docs/plan.md#c-covers"), "is a section this pull request links"),
                 (dict(EXCLUSION, line=6), "a fabricated quote"),
                 (dict(EXCLUSION, owner="docs/plan.md#nowhere"), "the owner cannot be read"),
                 (dict(EXCLUSION, owner="P3"), "is not a path#anchor link"))
        for exclusion, words in cases:
            with self.subTest(words=words):
                grades = valid()
                grades[2] = grade("C-30", "EXCLUDED", exclusion=exclusion)
                result, _ = self.audit(self.repo(), envelope(1, ["C-15", "C-8", "C-30"], grades))
                self.assert_code(result, 1, "C-30 UNPROVEN", words)
        # The owner's [takes:] lands on the branch only: the merge base has none, so it does not count.
        repo = self.repo(plan=PLAN.replace(" [takes: C-30]", ""))
        commit(repo, {"docs/plan.md": PLAN})
        result, _ = self.audit(repo, envelope(1, ["C-15", "C-8", "C-30"], valid()))
        self.assert_code(result, 1, "carries no [takes: C-30] at the merge base")

    def test_a_usage_limit_or_failed_run_could_not_run_even_in_soak(self):
        limit = "You've hit your usage limit. Upgrade to Pro or try again at 3:05 PM."
        result, table = self.audit(self.repo(), envelope(1, ["C-15", "C-8", "C-30"], error=limit), extra=("--soak",))
        self.assert_code(result, 2, "a model usage limit", "try again at 3:05 PM")
        self.assertIn("could not run", table)
        result, _ = self.audit(self.repo(), envelope(1, ["C-15", "C-8", "C-30"], error="the grader ran past --timeout (5 seconds)"))
        self.assert_code(result, 2, "shard 1 could not be graded: the grader ran past --timeout")
        self.assertNotIn("usage limit", result.stdout + result.stderr)

    def test_grades_that_do_not_match_the_body_or_the_schema_could_not_run(self):
        repo = self.repo()
        result, _ = self.audit(repo, envelope(1, ["C-15", "C-8"], valid()[:2]))
        self.assert_code(result, 2, "the grades are for C-15, C-8, the body's tickets define C-15, C-8, C-30")
        result, _ = self.audit(repo, envelope(1, ["C-15", "C-8", "C-30"], valid()), envelope(1, ["C-15"], valid()[:1]))
        self.assert_code(result, 2, "C-15 is in shards 1 and 1")
        broken = valid()
        broken[0]["status"] = "PROBABLY"
        result, _ = self.audit(repo, envelope(1, ["C-15", "C-8", "C-30"], broken))
        self.assert_code(result, 2, "status is 'PROBABLY'")
        broken = valid()
        broken[1]["cases"][0]["proof"][0]["line"] = "12"
        result, _ = self.audit(repo, envelope(1, ["C-15", "C-8", "C-30"], broken))
        self.assert_code(result, 2, "line is not integer")
        result, _ = self.audit(repo)
        self.assert_code(result, 2, "graded nothing")

    def test_no_ticket_is_a_qualified_pass_and_a_bad_link_cannot_run(self):
        result, table = self.audit(self.repo(), body="No ticket here.\n")
        self.assert_code(result, 0, "no ticket linked")
        self.assertIn("nothing to audit", table)
        result, _ = self.audit(self.repo(), body="Ticket: docs/plan.md#nowhere\n", extra=("--soak",))
        self.assert_code(result, 2, "no heading has the anchor #nowhere")


FAKE_CODEX = r'''#!/usr/bin/env python3
import json, os, sys
args = sys.argv[1:]
with open(os.environ["FAKE_LOG"], "a") as log:
    log.write(json.dumps(args) + "\n")
behaviour = os.environ.get("FAKE_BEHAVIOUR", "")
print(json.dumps({"type": "thread.started"}), flush=True)
if behaviour == "limit":
    print(json.dumps({"type": "error", "message": "You've hit your usage limit. Try again at 3:05 PM."}), flush=True)
    print(json.dumps({"type": "turn.failed", "error": {"message": "You've hit your usage limit. Try again at 3:05 PM."}}), flush=True)
    sys.exit(1)
if behaviour == "write":
    open(os.path.join(args[args.index("--cd") + 1], "scratch.txt"), "w").write("x")
prompt = args[-1]
ids = [line.split(" ", 2)[1] for line in prompt.splitlines() if line.startswith("- ") and " (docs/plan.md:" in line]
grades = json.load(open(os.environ["FAKE_GRADES"]))
out = {"requirements": [grades[i] for i in ids]}
open(args[args.index("-o") + 1], "w").write("not json" if behaviour == "garbled" else json.dumps(out))
print(json.dumps({"type": "turn.completed", "usage": {"input_tokens": 1, "cached_input_tokens": 0, "output_tokens": 1, "reasoning_output_tokens": 0}}))
'''


class Grader(unittest.TestCase):
    """bin/vibe-verifier proof-audit with a fake Codex CLI: what the model is given, and what it is not."""

    def setUp(self):
        self.work = Path(tempfile.mkdtemp(prefix="vv-grader-"))
        self.addCleanup(shutil.rmtree, self.work, True)
        self.repo = make_repo(self, {"docs/plan.md": PLAN, "tests/covers.test.ts": TEST})
        git(self.repo, "checkout", "-q", "-b", "feat/covers")
        commit(self.repo, {"src/covers.ts": "export {};\n"})
        write(self.work, {"body.md": BODY + "- [req: C-8] [proof: tests/covers.test.ts::SECRET-PROOF-MAP]\n",
                          "home/auth.json": json.dumps({"auth_mode": "chatgpt", "tokens": {"refresh_token": "r"}}),
                          "codex": FAKE_CODEX, "grades.json": json.dumps({g["id"]: g for g in valid()})})
        os.chmod(self.work / "codex", 0o755)

    def run_grader(self, *args, behaviour="", grades=None):
        if grades is not None:
            write(self.work, {"grades.json": json.dumps({g["id"]: g for g in grades})})
        env = {"FAKE_LOG": str(self.work / "calls.jsonl"), "FAKE_GRADES": str(self.work / "grades.json"),
               "FAKE_BEHAVIOUR": behaviour}
        return runner("proof-audit", "--repo", self.repo, "--base-ref", "main", "--codex", str(self.work / "codex"),
                      "--codex-home", str(self.work / "home"), *args, env=env)

    def calls(self):
        return [json.loads(line) for line in (self.work / "calls.jsonl").read_text().splitlines()]

    def test_the_local_run_gives_the_model_the_requirements_and_never_the_body(self):
        result = self.run_grader("--body", str(self.work / "body.md"), "--grades", str(self.work / "out"),
                                 "--model", "gpt-test", "--reasoning-effort", "high")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("| C-8 | PROVEN |", result.stdout)
        (call,) = self.calls()
        prompt = call[-1]
        self.assertIn("- C-8 (docs/plan.md:6): Cover upload keeps only the first dropped file. [req: C-8]", prompt)
        self.assertIn('- docs/plan.md#c-covers (heading "C. Covers")', prompt)
        self.assertNotIn("SECRET-PROOF-MAP", prompt)
        self.assertNotIn("Built the covers", prompt)
        for setting in (["--sandbox", "read-only"], ["--output-schema", str(ROOT / "harnesses/proof-audit/schema.json")],
                        ["--model", "gpt-test"], ["-c", 'model_reasoning_effort="high"'], ["--cd", os.path.abspath(self.repo)]):
            self.assertIn(setting, [call[i:i + len(setting)] for i in range(len(call))])
        self.assertIn("--ignore-user-config", call)

    def test_the_local_run_exits_as_the_gate_does(self):
        grades = valid()
        grades[1] = grade("C-8", "UNPROVEN", missing="no test drops two acceptable files")
        result = self.run_grader("--body", str(self.work / "body.md"), grades=grades)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("C-8 UNPROVEN: no test drops two acceptable files", result.stdout)

    def test_ci_plans_then_grades_each_shard_into_its_own_file(self):
        plan = self.work / "plan.json"
        result = self.run_grader("--body", str(self.work / "body.md"), "--plan", str(plan), "--max-ids", "2")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.splitlines(), ["requirements: 3", "shards: [1,2]"])
        self.assertEqual([[item["id"] for item in cut] for cut in json.loads(plan.read_text())["shards"]], [["C-15", "C-8"], ["C-30"]])
        for shard in ("1", "2"):
            result = self.run_grader("--requirements", str(plan), "--shard", shard, "--grades", str(self.work / "grades"))
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(json.loads((self.work / "grades" / "2.json").read_text())["ids"], ["C-30"])
        audited = gate("proof-audit", self.repo, "--body", str(self.work / "body.md"), "--grades", str(self.work / "grades"),
                       "--base-ref", "main")
        self.assertEqual(audited.returncode, 0, audited.stdout + audited.stderr)

    def test_grades_written_inside_the_repository_are_not_the_grader_changing_it(self):
        result = self.run_grader("--body", str(self.work / "body.md"), "--grades", os.path.join(self.repo, "grades"),
                                 "--max-ids", "1", "--jobs", "3")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(len(self.calls()), 3)

    def test_a_usage_limit_is_recorded_and_the_gate_cannot_run(self):
        result = self.run_grader("--body", str(self.work / "body.md"), "--grades", str(self.work / "out"), "--format", "github",
                                 behaviour="limit")
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn("::error title=proof-audit::shard 1 (C-15, C-8, C-30): Codex ended with an error: You've hit your usage limit", result.stdout)
        self.assertIn("a model usage limit", result.stdout)
        envelope_ = json.loads((self.work / "out" / "1.json").read_text())
        self.assertIsNone(envelope_["output"])

    def test_a_grader_that_writes_or_answers_garbage_is_not_graded(self):
        for behaviour, words in (("write", "changed the working tree"), ("garbled", "final message is not JSON")):
            with self.subTest(behaviour=behaviour):
                result = self.run_grader("--body", str(self.work / "body.md"), "--grades", str(self.work / behaviour),
                                         behaviour=behaviour)
                self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
                self.assertIn(words, result.stdout)
                (Path(self.repo) / "scratch.txt").unlink(missing_ok=True)

    def test_ci_runs_without_codex_sandbox_which_cannot_start_in_its_containers(self):
        result = self.run_grader("--body", str(self.work / "body.md"), "--grades", str(self.work / "out"),
                                 "--sandbox", "danger-full-access")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn(["--sandbox", "danger-full-access"], [call[i:i + 2] for call in self.calls() for i in range(len(call))])
        action = (ROOT / "actions" / "proof-audit" / "action.yml").read_text()
        self.assertIn('--sandbox "$VV_SANDBOX"', action)
        self.assertIn("    default: danger-full-access\n", action)

    def test_a_runner_without_a_chatgpt_login_cannot_grade(self):
        write(self.work, {"home/auth.json": json.dumps({"auth_mode": "apikey"})})
        result = self.run_grader("--body", str(self.work / "body.md"), "--grades", str(self.work / "out"))
        self.assertEqual(result.returncode, 2)
        self.assertIn("not a ChatGPT-managed login", result.stdout)

    def test_the_prompt_and_schema_have_one_copy(self):
        sentence = "Your final message is the JSON object the output schema describes"
        holders = subprocess.run(["git", "-C", str(ROOT), "grep", "--untracked", "-l", sentence, "--", ":!tests"], capture_output=True, text=True).stdout.split()
        self.assertEqual(holders, ["harnesses/proof-audit/prompt.md"])
        action = (ROOT / "actions" / "proof-audit" / "action.yml").read_text()
        self.assertIn('bin/vibe-verifier" proof-audit --requirements', action)
        self.assertIn('gates/proof_audit.py" --body', action)


class Plan(unittest.TestCase):
    def test_shards_are_contiguous_even_and_never_over_the_cap(self):
        items = [{"id": "C-%d" % number} for number in range(1, 29)]
        cuts = _proof_audit.shards(items, 10)
        self.assertEqual([len(cut) for cut in cuts], [10, 9, 9])
        self.assertEqual([item for cut in cuts for item in cut], items)
        self.assertEqual([len(cut) for cut in _proof_audit.shards(items[:10], 10)], [10])
        self.assertEqual(_proof_audit.shards([], 10), [])

    def test_a_requirement_naming_a_placeholder_is_written_as_it_is(self):
        item = {"id": "C-1", "plan": "docs/plan.md", "line": 3, "text": "SECTIONS and REQUIREMENTS stay [req: C-1]",
                "section": "docs/plan.md#c", "heading": "C"}
        prompt = _proof_audit.render_prompt([item])
        self.assertIn("- C-1 (docs/plan.md:3): SECTIONS and REQUIREMENTS stay [req: C-1]", prompt)
        self.assertIn('- docs/plan.md#c (heading "C")', prompt)
        self.assertNotIn("\nREQUIREMENTS\n", prompt)


class Declarations(unittest.TestCase):
    def test_an_assertion_belongs_to_the_test_it_sits_in_not_a_call_inside_it(self):
        lines = ['test("first", () => {});', "for (const width of [1, 2]) {", "  test(", "    `wraps at ${width}px`,",
                 "    async () => {", "      test.setTimeout(9);", '      await test.step("open", async () => {});',
                 "      expect(a).toBe(1);", "  });", "}", "def test_python_case(self):", "    self.assertTrue(x)"]
        self.assertEqual(proof_audit.declared_title(proof_audit.enclosing_test(lines, 8)), "wraps at ${width}px")
        self.assertEqual(proof_audit.declared_title(proof_audit.enclosing_test(lines, 12)), "test_python_case")
        self.assertIsNone(proof_audit.enclosing_test(lines, 1))

    def test_a_line_that_only_opens_a_call_is_not_an_assertion(self):
        for opener in ("assert.equal(", "await expect(", "assert.deepEqual("):
            self.assertIn("only opens a call", proof_audit.quote_problem([opener], "a.test.ts", 1, opener, whole=True) or "")
        self.assertIsNone(proof_audit.quote_problem(["expect(a).toEqual({"], "a.test.ts", 1, "expect(a).toEqual({", whole=True))


class Replay(unittest.TestCase):
    """A consumer's pull request (2026-10-15) passed every gate with a one-file requirement, "drag-hover and
    one-file enforcement on cover upload", whose cited test dropped a PNG and a GIF: the type filter refuses
    the GIF anyway, so the test passes with or without the one-file rule. tests/fixtures/proof-audit/c8 is
    that case with the same shape and none of its code, the grade the real grader gave it (UNPROVEN), and,
    under proven/, the tree with a second acceptable file and an assertion on the one kept, with the grade
    the real grader gave that (PROVEN). harnesses/proof-audit/README.md, "The one-file replay"."""

    def fixture_repo(self, proven=False):
        files = {path.relative_to(FIXTURE / "repo").as_posix(): path.read_text()
                 for path in (FIXTURE / "repo").rglob("*") if path.is_file()}
        repo = make_repo(self, files)
        git(repo, "checkout", "-q", "-b", "feat/cover-upload")
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

    def test_the_recorded_grade_of_the_gif_case_is_unproven(self):
        result, table = self.replay(self.fixture_repo(), FIXTURE / "grades")
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("C-8 UNPROVEN", result.stdout)
        self.assertIn("GIF", result.stdout)
        self.assertIn("| C-8 | UNPROVEN |", table)

    def test_the_recorded_grade_of_the_proven_tree_passes_and_does_not_pass_on_the_other_tree(self):
        result, table = self.replay(self.fixture_repo(proven=True), FIXTURE / "proven" / "grades")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("| C-8 | PROVEN |", table)
        # The same grade held to the tree it was not given: its one-file quote is not there.
        result, _ = self.replay(self.fixture_repo(), FIXTURE / "proven" / "grades")
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("a fabricated quote", result.stdout)

    @unittest.skipUnless(os.environ.get("VV_PROOF_AUDIT_LIVE"), "set VV_PROOF_AUDIT_LIVE=1 to grade with the real model "
                         "(a ChatGPT-managed Codex login in CODEX_HOME or ~/.codex)")
    def test_the_real_grader_grades_the_gif_case_unproven_and_the_proven_tree_proven(self):
        for proven, code in ((False, 1), (True, 0)):
            with self.subTest(proven=proven):
                work = Path(tempfile.mkdtemp(prefix="vv-replay-"))
                self.addCleanup(shutil.rmtree, work, True)
                result = runner("proof-audit", "--repo", self.fixture_repo(proven), "--base-ref", "main",
                                "--body", str(FIXTURE / "body.md"), "--grades", str(work / "grades"),
                                "--model", os.environ.get("VV_PROOF_AUDIT_MODEL", ""),
                                "--reasoning-effort", os.environ.get("VV_PROOF_AUDIT_EFFORT", ""))
                self.assertEqual(result.returncode, code, result.stdout + result.stderr)
                self.assertIn("| C-8 | %s |" % ("PROVEN" if proven else "UNPROVEN"), result.stdout)


if __name__ == "__main__":
    unittest.main()
