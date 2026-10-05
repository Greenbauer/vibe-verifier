"""The repo-rules gate, on the real pinned ast-grep: the ratchet, base control, the rule
requirements, packs, and the runner replaying a pull request offline."""
import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from helpers import ROOT, commit, gate, git, make_repo, runner, write

RULE = """id: no-console-log
language: TypeScript
severity: error
message: Use `log.info` from src/log instead of console.log.
note: |
  The logger tags each line with the request id.
rule:
  pattern: console.log($$$ARGS)
"""
TEST = """id: no-console-log
valid:
  - log.info("ready")
invalid:
  - console.log("ready")
"""
RULES = {".vibe-verifier-rules/no-console-log.yml": RULE, ".vibe-verifier-rules/tests/no-console-log-test.yml": TEST}
LOG = 'console.log("ready")\n'


def rules_repo(test, files):
    return make_repo(test, dict(RULES, **files))


def run(repo, *args):
    result = gate("repo-rules", repo, "--base-ref", "HEAD~1", "--format", "json", *args)
    findings = json.loads(result.stdout)["findings"] if result.returncode in (0, 1) else None
    return result, findings


def rev(repo):
    return subprocess.run(["git", "-C", repo, "rev-parse", "HEAD"], capture_output=True, text=True, check=True).stdout.strip()


def where(findings):
    return [(f["path"], f["line"]) for f in findings]


class Ratchet(unittest.TestCase):
    def test_a_new_violation_fails_and_an_identical_one_already_at_the_base_does_not(self):
        repo = rules_repo(self, {"src/a.ts": LOG + "export const a = 1\n"})
        commit(repo, {"src/a.ts": LOG + "export const a = 2\n", "src/b.ts": "export const b = 1\n" + LOG})
        result, findings = run(repo)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertEqual(where(findings), [("src/b.ts", 2)])
        message = findings[0]["message"]
        for part in ("no-console-log", "Use `log.info` from src/log instead of console.log.", "new in this pull request",
                     "note: The logger tags each line with the request id."):
            self.assertIn(part, message)

    def test_text_output_carries_path_line_rule_message_and_note(self):
        repo = rules_repo(self, {"src/a.ts": "export const a = 1\n"})
        commit(repo, {"src/b.ts": LOG})
        result = gate("repo-rules", repo, "--base-ref", "HEAD~1")
        self.assertEqual(result.returncode, 1)
        self.assertIn("repo-rules: src/b.ts:1: no-console-log: Use `log.info`", result.stdout)
        self.assertIn("\n  note: The logger tags each line with the request id.", result.stdout)

    def test_a_second_copy_in_the_same_file_is_a_finding(self):
        repo = rules_repo(self, {"src/a.ts": LOG})
        commit(repo, {"src/a.ts": LOG + LOG})
        result, findings = run(repo)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertEqual(where(findings), [("src/a.ts", 1), ("src/a.ts", 2)])
        self.assertIn("2 in this file now, 1 at the merge base", findings[0]["message"])

    def test_reindenting_moving_down_or_rewrapping_a_violation_is_not_new(self):
        repo = rules_repo(self, {"src/a.ts": 'console.log(\n  "a",\n  "b")\n', "src/b.ts": 'console.log(\n  "a",\n  "b"\n)\n'})
        commit(repo, {"src/a.ts": 'export const x = 1\nif (x) {\n  console.log(\n    "a",\n    "b")\n}\n',
                      "src/b.ts": 'console.log("a", "b")\n'})
        result, findings = run(repo)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_moving_a_file_is_not_a_finding_but_adding_to_it_is(self):
        body = "export const a = 1\nexport const b = 2\nexport const c = 3\n" + LOG
        repo = rules_repo(self, {"src/a.ts": body})
        Path(repo, "lib").mkdir()
        git(repo, "mv", "src/a.ts", "lib/a.ts")
        commit(repo, {}, "move")
        self.assertEqual(run(repo)[0].returncode, 0)
        commit(repo, {"lib/a.ts": body + 'console.log("again")\n'}, "grow")
        result = gate("repo-rules", repo, "--base-ref", "HEAD~2", "--format", "json")
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertEqual(where(json.loads(result.stdout)["findings"]), [("lib/a.ts", 5)])

    def test_all_reports_every_finding_and_soak_does_not_block(self):
        repo = rules_repo(self, {"src/a.ts": LOG})
        commit(repo, {"README.md": "x\n"})
        self.assertEqual(run(repo)[0].returncode, 0)
        result, findings = run(repo, "--all")
        self.assertEqual(result.returncode, 1)
        self.assertEqual(where(findings), [("src/a.ts", 1)])
        result, findings = run(repo, "--all", "--soak")
        self.assertEqual((result.returncode, where(findings)), (0, [("src/a.ts", 1)]))

    def test_an_unused_suppression_is_not_a_finding_and_a_used_one_suppresses(self):
        repo = rules_repo(self, {"src/a.ts": "export const a = 1\n"})
        commit(repo, {"src/a.ts": "// ast-grep-ignore: some-other-rule\nexport const a = 2\n// ast-grep-ignore: no-console-log\n" + LOG})
        self.assertEqual(run(repo)[0].returncode, 0)


class BaseControl(unittest.TestCase):
    def test_a_rule_the_branch_deletes_still_judges_it(self):
        repo = rules_repo(self, {"src/a.ts": "export const a = 1\n"})
        shutil.rmtree(Path(repo, ".vibe-verifier-rules"))
        commit(repo, {"src/b.ts": LOG})
        result, findings = run(repo)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertEqual(where(findings), [("src/b.ts", 1)])
        self.assertIn(".vibe-verifier-rules/no-console-log.yml is judged as it is at HEAD~1", result.stderr)

    def test_a_rule_the_branch_renames_still_judges_it_wherever_it_went(self):
        for target in (".vibe-verifier-rules/no-console-log.txt", ".vibe-verifier-rules/tests/moved.yml",
                       ".vibe-verifier-rules/renamed.yml", "elsewhere/no-console-log.yml"):
            repo = rules_repo(self, {"src/a.ts": "export const a = 1\n"})
            Path(repo, target).parent.mkdir(parents=True, exist_ok=True)
            git(repo, "mv", ".vibe-verifier-rules/no-console-log.yml", target)
            commit(repo, {"src/b.ts": LOG})
            result, findings = run(repo)
            self.assertEqual(result.returncode, 1, "%s: %s" % (target, result.stdout + result.stderr))
            self.assertEqual(where(findings), [("src/b.ts", 1)])
            self.assertIn(".vibe-verifier-rules/no-console-log.yml is judged as it is at HEAD~1", result.stderr)

    def test_a_rule_the_branch_weakens_still_judges_it_as_the_base_has_it(self):
        repo = rules_repo(self, {"src/a.ts": "export const a = 1\n"})
        commit(repo, {".vibe-verifier-rules/no-console-log.yml": RULE.replace("console.log($$$ARGS)", "console.never($$$ARGS)"),
                      ".vibe-verifier-rules/tests/no-console-log-test.yml": TEST.replace('console.log("ready")', 'console.never("ready")'),
                      "src/b.ts": LOG})
        result, findings = run(repo)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertEqual(where(findings), [("src/b.ts", 1)])

    def test_a_rule_the_branch_adds_applies_at_once_to_what_the_branch_adds(self):
        repo = make_repo(self, {"src/old.ts": LOG, "src/a.ts": LOG})
        added = RULE + "ignores: ['legacy/**']\n"
        commit(repo, {".vibe-verifier-rules/no-console-log.yml": added, ".vibe-verifier-rules/tests/no-console-log-test.yml": TEST,
                      "src/a.ts": LOG + "export const a = 1\n", "src/new.ts": LOG, "legacy/x.ts": LOG})
        result, findings = run(repo)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertEqual(where(findings), [("src/new.ts", 1)])

    def test_a_directory_outside_the_repository_is_read_as_it_is(self):
        outside = tempfile.mkdtemp(prefix="vv-rules-outside-")
        self.addCleanup(shutil.rmtree, outside, True)
        write(outside, {"no-console-log.yml": RULE, "tests/no-console-log-test.yml": TEST})
        repo = make_repo(self, {"src/a.ts": "export const a = 1\n"})
        commit(repo, {"src/b.ts": LOG})
        result, findings = run(repo, "--rules", outside)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertEqual(where(findings), [("src/b.ts", 1)])


class RuleRequirements(unittest.TestCase):
    def cannot_run(self, rules, *args):
        repo = make_repo(self, dict({"src/a.ts": "export const a = 1\n"}, **rules))
        commit(repo, {"src/b.ts": LOG})
        result = gate("repo-rules", repo, "--base-ref", "HEAD~1", *args)
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        return result.stderr

    def test_a_rule_without_a_message_cannot_run(self):
        error = self.cannot_run({".vibe-verifier-rules/r.yml": RULE.replace("message: Use `log.info` from src/log instead of console.log.\n", "message: ''\n"),
                                 ".vibe-verifier-rules/tests/t.yml": TEST})
        self.assertIn("rule no-console-log (.vibe-verifier-rules/r.yml) has no message", error)

    def test_a_rule_without_tests_cannot_run_even_under_soak(self):
        error = self.cannot_run({".vibe-verifier-rules/r.yml": RULE}, "--soak")
        self.assertIn("rule no-console-log (.vibe-verifier-rules/r.yml) has 0 valid and 0 invalid test cases", error)

    def test_a_rule_test_without_a_valid_case_cannot_run(self):
        error = self.cannot_run({".vibe-verifier-rules/r.yml": RULE,
                                 ".vibe-verifier-rules/tests/t.yml": "id: no-console-log\ninvalid:\n  - console.log(1)\n  - console.log(2)\n"})
        self.assertIn("has 0 valid and 2 invalid test cases", error)

    def test_a_failing_rule_test_cannot_run_and_names_the_rule(self):
        error = self.cannot_run({".vibe-verifier-rules/r.yml": RULE,
                                 ".vibe-verifier-rules/tests/t.yml": TEST.replace('console.log("ready")', 'log.warn("ready")')})
        self.assertIn("the rule tests of .vibe-verifier-rules/ do not pass", error)
        self.assertIn("FAIL no-console-log", error)

    def test_a_rule_ast_grep_cannot_parse_cannot_run_and_names_the_file(self):
        error = self.cannot_run(dict(RULES, **{".vibe-verifier-rules/broken.yml": "id: broken\nlanguage: Klingon\nmessage: x\nrule: {pattern: x}\n"}))
        self.assertIn(".vibe-verifier-rules/broken.yml", error)
        self.assertNotIn("vv-rules-", error)

    def test_a_rule_the_reader_cannot_see_into_cannot_run(self):
        flow = "{id: no-alert, language: TypeScript, rule: {pattern: alert($$$A)}}\n"
        error = self.cannot_run({".vibe-verifier-rules/only.yml": flow})
        self.assertIn(".vibe-verifier-rules/only.yml holds a document whose id cannot be read", error)
        error = self.cannot_run(dict(RULES, **{".vibe-verifier-rules/tests/no-console-log-test.yml": '{id: no-console-log, valid: [a()], invalid: ["console.log(1)"]}\n'}))
        self.assertIn(".vibe-verifier-rules/tests/no-console-log-test.yml holds a document whose id cannot be read", error)

    def test_no_rules_an_empty_directory_and_an_unknown_pack_cannot_run(self):
        self.assertIn("no rules to run", self.cannot_run({}))
        self.assertIn("--rules lint/rules holds no rule files", self.cannot_run({}, "--rules", "lint/rules"))
        self.assertIn("no catalog pack 'nope' (packs: example", self.cannot_run({}, "--pack", "nope"))
        self.assertIn("no catalog pack '../rules'", self.cannot_run({}, "--pack", "../rules"))
        self.assertIn(".vibe-verifier-rules/ holds no rules", self.cannot_run({".vibe-verifier-rules/tests/t.yml": TEST}))

    def test_flow_style_quoted_and_multi_document_rule_files_are_read(self):
        second = 'id: "no-alert"\nlanguage: TypeScript\nmessage: "Show a toast instead of alert()."\nrule: {pattern: alert($$$A)}\n'
        repo = make_repo(self, {"lint/rules.yml": RULE + "---\n" + second,
                                "lint/tests/all.yml": "id: 'no-alert'\nvalid: [toast('x')]\ninvalid: [\"alert('x')\"]\n---\n" + TEST})
        commit(repo, {"src/b.ts": "alert('saved')\n"})
        result, findings = run(repo, "--rules", "lint")
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertEqual([(f["path"], f["line"], f["message"].split(":")[0]) for f in findings], [("src/b.ts", 1, "no-alert")])


EFFECT = """id: no-raw-effect
language: Tsx
message: Call `useMountEffect` from src/hooks instead of useEffect.
rule:
  pattern: useEffect($$$ARGS)
"""
EFFECT_TEST = "id: no-raw-effect\nvalid:\n  - useMountEffect(load)\ninvalid:\n  - useEffect(load)\n"
TS_AS_TSX = "# only languageGlobs is read\nruleDirs: [elsewhere]\nlanguageGlobs:\n  tsx: ['*.ts']\n"
EFFECTS = {".vibe-verifier-rules/no-raw-effect.yml": EFFECT, ".vibe-verifier-rules/tests/no-raw-effect-test.yml": EFFECT_TEST}
HOOK = "export function useData() {\n  useEffect(load)\n}\n"


class LanguageGlobs(unittest.TestCase):
    """A tsx rule sees only .tsx files unless its directory's sgconfig.yml maps more extensions to tsx."""

    def replay(self, files, change):
        repo = make_repo(self, dict({"src/a.tsx": "export const a = 1\n", ".vibe-verifier": "repo-rules\n"}, **files))
        base = rev(repo)
        change(repo)
        return runner("run", "--repo", repo, "--manifest", str(Path(repo, ".vibe-verifier")), "--base-ref", base)

    def test_a_tsx_rule_finds_a_ts_violation_through_the_runner_only_when_its_directory_maps_ts(self):
        mapped = self.replay(dict(EFFECTS, **{".vibe-verifier-rules/sgconfig.yml": TS_AS_TSX}), lambda r: commit(r, {"src/hook.ts": HOOK}))
        self.assertEqual(mapped.returncode, 1, mapped.stdout + mapped.stderr)
        self.assertIn("repo-rules: src/hook.ts:2: no-raw-effect: Call `useMountEffect`", mapped.stdout)
        unmapped = self.replay(EFFECTS, lambda r: commit(r, {"src/hook.ts": HOOK}))
        self.assertEqual(unmapped.returncode, 0, unmapped.stdout + unmapped.stderr)  # documented: tsx covers .tsx only

    def test_the_base_mapping_wins_over_a_branch_that_removes_or_adds_one(self):
        def remove(repo):
            Path(repo, ".vibe-verifier-rules/sgconfig.yml").unlink()
            commit(repo, {"src/hook.ts": HOOK})
        removed = self.replay(dict(EFFECTS, **{".vibe-verifier-rules/sgconfig.yml": TS_AS_TSX}), remove)
        self.assertEqual(removed.returncode, 1, removed.stdout + removed.stderr)
        self.assertIn("src/hook.ts:2: no-raw-effect", removed.stdout)
        self.assertIn(".vibe-verifier-rules/sgconfig.yml is judged as it is at", removed.stderr)
        added = self.replay(EFFECTS, lambda r: commit(r, {".vibe-verifier-rules/sgconfig.yml": TS_AS_TSX, "src/hook.ts": HOOK}))
        self.assertEqual(added.returncode, 0, added.stdout + added.stderr)  # it applies once it merges
        self.assertIn(".vibe-verifier-rules/sgconfig.yml is judged as it is at", added.stderr)

    def test_each_directory_keeps_its_own_mapping_so_none_is_picked_silently(self):
        # Under one shared mapping, `*.ts` as tsx would hide every .ts file from lint/'s TypeScript rule.
        repo = make_repo(self, dict(EFFECTS, **{".vibe-verifier-rules/sgconfig.yml": TS_AS_TSX,
                                                "lint/no-console-log.yml": RULE, "lint/tests/no-console-log-test.yml": TEST,
                                                "src/a.ts": "export const a = 1\n"}))
        commit(repo, {"src/x.ts": "useEffect(load)\n" + LOG})
        result, findings = run(repo, "--rules", ".vibe-verifier-rules", "--rules", "lint")
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertEqual([(f["line"], f["message"].split(":")[0]) for f in findings], [(1, "no-raw-effect"), (2, "no-console-log")])

    def test_rule_tests_run_under_the_mapping_and_a_bad_mapping_cannot_run(self):
        repo = make_repo(self, dict(EFFECTS, **{".vibe-verifier-rules/sgconfig.yml": "languageGlobs:\n  klingon: ['*.ts']\n"}))
        commit(repo, {"src/hook.ts": HOOK})
        result = gate("repo-rules", repo, "--base-ref", "HEAD~1")
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn("the rule tests of .vibe-verifier-rules/ do not pass", result.stderr)
        self.assertIn("klingon", result.stderr)

    def test_an_unreadable_sgconfig_cannot_run(self):
        repo = make_repo(self, dict(EFFECTS, **{".vibe-verifier-rules/sgconfig.yml": "{languageGlobs: {tsx: ['*.ts']}}\n"}))
        commit(repo, {"src/hook.ts": HOOK})
        result = gate("repo-rules", repo, "--base-ref", "HEAD~1")
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn(".vibe-verifier-rules/sgconfig.yml cannot be read", result.stderr)


class Directories(unittest.TestCase):
    def test_one_rule_id_in_two_directories_cannot_run(self):
        repo = make_repo(self, dict(RULES, **{"lint/no-console-log.yml": RULE, "lint/tests/t.yml": TEST, "src/a.ts": "x\n"}))
        commit(repo, {"src/b.ts": LOG})
        result = gate("repo-rules", repo, "--base-ref", "HEAD~1", "--rules", ".vibe-verifier-rules", "--rules", "lint")
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn("rule no-console-log is defined in both .vibe-verifier-rules/no-console-log.yml and lint/no-console-log.yml", result.stderr)

    def test_a_test_counts_only_for_a_rule_of_its_own_directory(self):
        repo = make_repo(self, {".vibe-verifier-rules/no-console-log.yml": RULE, "lint/tests/no-console-log-test.yml": TEST,
                                "lint/other.yml": EFFECT, "lint/tests/other.yml": EFFECT_TEST, "src/a.ts": "x\n"})
        commit(repo, {"src/b.ts": LOG})
        result = gate("repo-rules", repo, "--base-ref", "HEAD~1", "--rules", ".vibe-verifier-rules", "--rules", "lint")
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn("rule no-console-log (.vibe-verifier-rules/no-console-log.yml) has 0 valid and 0 invalid", result.stderr)


class Packs(unittest.TestCase):
    def test_every_pack_in_the_catalog_meets_the_rule_requirements(self):
        packs = sorted(p.name for p in (ROOT / "rules").iterdir() if p.is_dir())
        self.assertIn("example", packs)
        repo = make_repo(self, {"README.md": "x\n"})
        for pack in packs:
            result = gate("repo-rules", repo, "--all", "--pack", pack)
            self.assertEqual(result.returncode, 0, "pack %s: %s" % (pack, result.stdout + result.stderr))

    def test_a_catalog_pack_runs_alongside_the_repositorys_rules(self):
        repo = rules_repo(self, {"src/a.ts": "export const a = 1\n"})
        commit(repo, {"src/b.ts": "debugger\n" + LOG})
        result, findings = run(repo, "--pack", "example")
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertEqual([(f["line"], f["message"].split(":")[0]) for f in findings], [(1, "no-debugger"), (2, "no-console-log")])


class Runner(unittest.TestCase):
    def test_a_historical_pull_request_replays_offline_through_the_runner(self):
        repo = rules_repo(self, {"src/a.ts": LOG, ".vibe-verifier": "repo-rules --pack example\n"})
        base = rev(repo)
        commit(repo, {"src/a.ts": LOG + "export const a = 1\n", "src/b.ts": "debugger\n"}, "pr")
        head = rev(repo)
        git(repo, "checkout", "-q", "main")
        git(repo, "reset", "-q", "--hard", base)
        commit(repo, {"src/c.ts": LOG}, "main moves on")
        git(repo, "checkout", "-q", "--detach", head)
        outside = tempfile.mkdtemp(prefix="vv-manifest-")
        self.addCleanup(shutil.rmtree, outside, True)
        manifest = Path(outside, "manifest")
        manifest.write_text("repo-rules --pack example\n")
        for path, base_ref in ((str(manifest), base), (str(Path(repo, ".vibe-verifier")), base), (str(manifest), "main")):
            result = runner("run", "--repo", repo, "--manifest", path, "--base-ref", base_ref)
            self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
            self.assertIn("repo-rules: src/b.ts:1: no-debugger:", result.stdout)
            self.assertNotIn("src/a.ts", result.stdout)  # touched, but its console.log was already at the base
            self.assertNotIn("src/c.ts", result.stdout)  # on main after the merge base, not this pull request's
            self.assertRegex(result.stdout, r"repo-rules\s+FAIL")


if __name__ == "__main__":
    unittest.main()
