"""The cognitive-complexity ratchet, on the real pinned toolchain (npm ci from the lockfile into
the tool cache), and its file-length sibling."""
import json
import os
import shutil
import subprocess
import sys
import unittest

from helpers import ROOT, clean_env, commit, gate, git, make_repo

TANGLED = """export function tangled(a: number, b: number, c: number[]): number {
  let total = 0;
  for (const x of c) {
    if (x > a) {
      if (x > b) { total += x; } else if (x === b) { total -= 1; } else {
        for (let i = 0; i < x; i++) {
          if (i % 2 === 0 && i % 3 === 0) { total += i; } else if (i % 5 === 0) { total -= i; }
        }
      }
    } else {
      while (total > 100) { total = total / 2; if (total < 10) { break; } }
    }
  }
  return total;
}
"""
SIMPLE = "export const simple = (n: number) => n + 1;\n"


def worse(source):
    """The same function with one more nested branch in its loop, so its cost rises and its line stays."""
    return source.replace("  for (const x of c) {\n", "  for (const x of c) {\n    if (x < 0) { if (a > b) { total -= 2; } }\n", 1)


def fn(name, extra=0):
    """TANGLED under another name, made worse `extra` times: costs 27, 32, 37, 42."""
    source = TANGLED.replace("function tangled", "function " + name)
    for _ in range(extra):
        source = worse(source)
    return source


def branch_with(test, base_files, head_files):
    repo = make_repo(test, base_files)
    commit(repo, head_files, "change")
    return repo


class CognitiveComplexity(unittest.TestCase):
    def test_a_new_file_with_an_over_limit_function_fails_at_its_line(self):
        repo = branch_with(self, {"README.md": "x\n"}, {"src/a.ts": TANGLED + SIMPLE})
        result = gate("cognitive-complexity", repo, "--base-ref", "HEAD~1", "--format", "json")
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        findings = json.loads(result.stdout)["findings"]
        self.assertEqual([(f["path"], f["line"]) for f in findings], [("src/a.ts", 1)])
        self.assertIn("Cognitive Complexity from 27 to the 15 allowed", findings[0]["message"])
        self.assertIn("0 at the base, 1 now", findings[0]["message"])

    def test_an_over_limit_function_already_at_the_base_does_not_block_a_touch(self):
        repo = branch_with(self, {"src/a.ts": TANGLED}, {"src/a.ts": TANGLED + SIMPLE})
        result = gate("cognitive-complexity", repo, "--base-ref", "HEAD~1")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_adding_a_second_over_limit_function_to_such_a_file_fails(self):
        second = TANGLED.replace("function tangled", "function tangled2")
        repo = branch_with(self, {"src/a.ts": TANGLED}, {"src/a.ts": TANGLED + second})
        result = gate("cognitive-complexity", repo, "--base-ref", "HEAD~1")
        self.assertEqual(result.returncode, 1)
        self.assertIn("1 at the base, 2 now", result.stdout)

    def test_an_over_limit_function_that_gets_worse_fails_though_the_count_is_the_same(self):
        repo = branch_with(self, {"src/a.ts": fn("f1", 0)}, {"src/a.ts": fn("f1", 1)})
        result = gate("cognitive-complexity", repo, "--base-ref", "HEAD~1", "--format", "json")
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        findings = json.loads(result.stdout)["findings"]
        self.assertEqual([(f["path"], f["line"]) for f in findings], [("src/a.ts", 1)])
        self.assertIn("got worse: highest first, [27] at the base, [32] now", findings[0]["message"])

    def test_an_over_limit_function_that_improves_but_stays_over_passes(self):
        # A guard against over-blocking: it passes on the count rule alone, and must keep passing.
        repo = branch_with(self, {"src/a.ts": fn("f1", 1)}, {"src/a.ts": fn("f1", 0)})
        result = gate("cognitive-complexity", repo, "--base-ref", "HEAD~1")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_a_lower_ranked_function_that_gets_worse_fails(self):
        # [37, 27] becomes [37, 32]: the highest cost is unchanged, the second rank rose.
        repo = branch_with(self, {"src/a.ts": fn("f1", 2) + fn("f2", 0)}, {"src/a.ts": fn("f1", 2) + fn("f2", 1)})
        result = gate("cognitive-complexity", repo, "--base-ref", "HEAD~1", "--format", "json")
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        findings = json.loads(result.stdout)["findings"]
        # Every over-limit function in the file is listed: the tools do not say which one changed.
        self.assertEqual([f["line"] for f in findings], [1, len(fn("f1", 2).splitlines()) + 1])
        self.assertIn("[37, 27] at the base, [37, 32] now", findings[0]["message"])

    def test_a_higher_top_cost_fails_even_when_the_files_total_falls(self):
        # [32, 32, 32] becomes [37]: two functions fixed, one worse than any was. An improvement elsewhere does not buy that.
        base = fn("f1", 1) + fn("f2", 1) + fn("f3", 1)
        head = fn("f1", 2) + SIMPLE
        repo = branch_with(self, {"src/a.ts": base}, {"src/a.ts": head})
        result = gate("cognitive-complexity", repo, "--base-ref", "HEAD~1")
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("[32, 32, 32] at the base, [37] now", result.stdout)

    def test_costs_are_compared_sorted_so_file_order_never_matters(self):
        # The cheaper function comes first in the file; touching the file, then swapping the two, both pass.
        base = fn("f1", 0) + fn("f2", 2)
        repo = branch_with(self, {"src/a.ts": base}, {"src/a.ts": base + SIMPLE})
        self.assertEqual(gate("cognitive-complexity", repo, "--base-ref", "HEAD~1").returncode, 0)
        commit(repo, {"src/a.ts": fn("f2", 2) + fn("f1", 0) + SIMPLE}, "swap")
        result = gate("cognitive-complexity", repo, "--base-ref", "HEAD~1")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_rank_not_name_is_compared_so_a_big_improvement_can_cover_a_smaller_worsening(self):
        # [37, 27] becomes [32, 27]: f1 fell from 37 to 27 while f2 rose from 27 to 32. The file's costs are no
        # worse at any rank, and the gate cannot tell the functions apart, so this passes. Documented as a limit.
        repo = branch_with(self, {"src/a.ts": fn("f1", 2) + fn("f2", 0)}, {"src/a.ts": fn("f1", 0) + fn("f2", 1)})
        result = gate("cognitive-complexity", repo, "--base-ref", "HEAD~1")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_the_base_is_the_merge_base_not_the_base_branchs_tip(self):
        # The branch only adds a simple function. Meanwhile main improves f1. Measured against main's tip the
        # branch would look as if it had made f1 worse; measured against where it started, it changed nothing.
        repo = make_repo(self, {"src/a.ts": fn("f1", 2) + fn("f2", 0)})
        git(repo, "checkout", "-q", "-b", "feat/x")
        commit(repo, {"src/a.ts": fn("f1", 2) + fn("f2", 0) + SIMPLE}, "touch")
        git(repo, "checkout", "-q", "main")
        commit(repo, {"src/a.ts": fn("f1", 0) + fn("f2", 0)}, "improve f1 on main")
        git(repo, "checkout", "-q", "feat/x")
        result = gate("cognitive-complexity", repo, "--base-ref", "main")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        # Whether the file existed is asked of the merge base too: main deleting it does not make it new here.
        git(repo, "checkout", "-q", "main")
        git(repo, "rm", "-q", "src/a.ts")
        git(repo, "commit", "-q", "-m", "delete it on main")
        git(repo, "checkout", "-q", "feat/x")
        result = gate("cognitive-complexity", repo, "--base-ref", "main")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_a_cost_the_report_does_not_carry_is_cannot_run_never_zero(self):
        sys.path.insert(0, str(ROOT / "gates"))
        self.addCleanup(sys.path.remove, str(ROOT / "gates"))
        import cognitive_complexity
        from _contract import CannotRun
        self.assertEqual(cognitive_complexity.cost("Refactor this function to reduce its Cognitive Complexity from 27 to the 15 allowed."), 27)
        with self.assertRaises(CannotRun):
            cognitive_complexity.cost("Function is too complex.")

    def test_an_inline_eslint_disable_does_not_hide_a_function(self):
        # The gate measures with its own config; a comment in the file is the repository's suppression, not the gate's.
        for directive in ("// eslint-disable-next-line sonarjs/cognitive-complexity\n", "/* eslint-disable */\n",
                          "/* eslint sonarjs/cognitive-complexity: off */\n"):
            with self.subTest(directive=directive.strip()):
                repo = branch_with(self, {"README.md": "x\n"}, {"src/a.ts": directive + fn("f1", 0)})
                result = gate("cognitive-complexity", repo, "--base-ref", "HEAD~1")
                self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
                self.assertIn("0 at the base, 1 now", result.stdout)
        # A comment eslint 9 rejects outright used to stop the gate (exit 2) on any file carrying it; now it is not read.
        repo = branch_with(self, {"README.md": "x\n"}, {"src/a.ts": "/* eslint-env node */\n" + fn("f1", 0)})
        result = gate("cognitive-complexity", repo, "--base-ref", "HEAD~1")
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        # And a function that gets worse under one is still a function that got worse.
        hidden = "// eslint-disable-next-line sonarjs/cognitive-complexity\n"
        repo = branch_with(self, {"src/a.ts": hidden + fn("f1", 0)}, {"src/a.ts": hidden + fn("f1", 1)})
        result = gate("cognitive-complexity", repo, "--base-ref", "HEAD~1")
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("[27] at the base, [32] now", result.stdout)

    def test_untouched_files_and_tests_are_not_measured_unless_all(self):
        repo = branch_with(self, {"src/a.ts": TANGLED, "src/a.test.ts": TANGLED}, {"README.md": "y\n"})
        self.assertEqual(gate("cognitive-complexity", repo, "--base-ref", "HEAD~1").returncode, 0)
        result = gate("cognitive-complexity", repo, "--all", "--format", "json")
        self.assertEqual(result.returncode, 1)
        self.assertEqual([f["path"] for f in json.loads(result.stdout)["findings"]], ["src/a.ts"])

    def test_max_is_the_limit(self):
        repo = branch_with(self, {"README.md": "x\n"}, {"src/a.ts": TANGLED})
        self.assertEqual(gate("cognitive-complexity", repo, "--base-ref", "HEAD~1", "--max", "30").returncode, 0)

    def test_a_file_that_does_not_parse_cannot_run(self):
        repo = branch_with(self, {"README.md": "x\n"}, {"src/a.ts": "export function (\n"})
        result = gate("cognitive-complexity", repo, "--base-ref", "HEAD~1")
        self.assertEqual(result.returncode, 2)
        self.assertIn("could not parse src/a.ts", result.stderr)

    def test_without_node_it_cannot_run(self):
        repo = branch_with(self, {"README.md": "x\n"}, {"src/a.ts": TANGLED})
        result = gate("cognitive-complexity", repo, "--base-ref", "HEAD~1",
                      env={"PATH": "/usr/bin:/bin", "VIBE_VERIFIER_TOOLS": os.path.join(repo, "empty-cache")})
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn("needs node and npm", result.stderr)


PY_TANGLED = """def tangled(a, b, c):
    total = 0
    for x in c:
        if x > a:
            if x > b:
                total += x
            elif x == b:
                total -= 1
            else:
                for i in range(x):
                    if i % 2 == 0 and i % 3 == 0:
                        total += i
                    elif i % 5 == 0:
                        total -= i
        else:
            while total > 100:
                total = total / 2
                if total < 10:
                    break
    return total
"""
PY_SIMPLE = "def simple(n):\n    return n + 1\n"


class PythonCognitiveComplexity(unittest.TestCase):
    """Python files through the pinned complexipy (pip install --require-hashes into the tool cache)."""

    def test_a_new_python_file_with_an_over_limit_function_fails_at_its_line(self):
        repo = branch_with(self, {"README.md": "x\n"}, {"pkg/a.py": PY_SIMPLE + "\n\n" + PY_TANGLED})
        result = gate("cognitive-complexity", repo, "--base-ref", "HEAD~1", "--format", "json")
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        findings = json.loads(result.stdout)["findings"]
        self.assertEqual([(f["path"], f["line"]) for f in findings], [("pkg/a.py", 5)])
        self.assertIn("Refactor tangled to reduce its Cognitive Complexity from", findings[0]["message"])
        self.assertIn("0 at the base, 1 now", findings[0]["message"])

    def test_the_ratchet_and_python_tests_work_as_for_js(self):
        second = PY_TANGLED.replace("def tangled", "def tangled2")
        repo = branch_with(self, {"pkg/a.py": PY_TANGLED, "tests/test_a.py": PY_TANGLED}, {"pkg/a.py": PY_TANGLED + "\n\n" + PY_SIMPLE})
        self.assertEqual(gate("cognitive-complexity", repo, "--base-ref", "HEAD~1").returncode, 0)
        commit(repo, {"pkg/a.py": PY_TANGLED + "\n\n" + second}, "second")
        result = gate("cognitive-complexity", repo, "--base-ref", "HEAD~1")
        self.assertEqual(result.returncode, 1)
        self.assertIn("1 at the base, 2 now", result.stdout)
        self.assertNotIn("test_a.py", gate("cognitive-complexity", repo, "--all").stdout)

    def test_a_python_function_that_gets_worse_fails_though_the_count_is_the_same(self):
        deeper = PY_TANGLED.replace("    for x in c:\n", "    for x in c:\n        if x < 0:\n            if a > b:\n                total -= 2\n", 1)
        self.assertNotEqual(deeper, PY_TANGLED)
        repo = branch_with(self, {"pkg/a.py": PY_TANGLED}, {"pkg/a.py": deeper})
        result = gate("cognitive-complexity", repo, "--base-ref", "HEAD~1")
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("got worse", result.stdout)
        self.assertEqual(gate("cognitive-complexity", branch_with(self, {"pkg/a.py": deeper}, {"pkg/a.py": PY_TANGLED}),
                              "--base-ref", "HEAD~1").returncode, 0)

    def test_a_method_is_named_by_its_class(self):
        method = "class Rates:\n" + "".join("    " + line + "\n" if line else "\n" for line in PY_TANGLED.replace("(a, b, c)", "(self, a, b, c)").splitlines())
        repo = branch_with(self, {"README.md": "x\n"}, {"pkg/rates.py": method})
        result = gate("cognitive-complexity", repo, "--base-ref", "HEAD~1")
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("Refactor Rates.tangled", result.stdout)

    def test_the_repositorys_own_ignore_comments_do_not_hide_a_function(self):
        for marker in ("  # noqa: complexipy", "  # complexipy: ignore", "  # NOQA: COMPLEXIPY"):
            source = PY_TANGLED.replace("def tangled(a, b, c):", "def tangled(a, b, c):" + marker)
            repo = branch_with(self, {"README.md": "x\n"}, {"pkg/a.py": source})
            self.assertEqual(gate("cognitive-complexity", repo, "--base-ref", "HEAD~1").returncode, 1, marker)

    def test_a_python_file_that_does_not_parse_cannot_run(self):
        repo = branch_with(self, {"README.md": "x\n"}, {"pkg/a.py": "def broken(:\n"})
        result = gate("cognitive-complexity", repo, "--base-ref", "HEAD~1")
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn("could not parse pkg/a.py", result.stderr)

    def only(self, repo, *tools):
        """A PATH directory holding just these tools, so nothing else on this machine is reachable."""
        directory = os.path.join(repo, "only-bin")
        os.makedirs(directory)
        for tool in tools:
            # /usr/bin/git, not PATH's: a developer machine may put a git wrapper script first.
            found = "/usr/bin/git" if tool == "git" and os.path.exists("/usr/bin/git") else shutil.which(tool)
            os.symlink(found, os.path.join(directory, tool))
        return directory

    def run_with(self, repo, path):
        return subprocess.run([sys.executable, str(ROOT / "gates" / "cognitive_complexity.py"), "--repo", repo, "--base-ref", "HEAD~1"],
                              capture_output=True, text=True,
                              env=clean_env({"PATH": path, "VIBE_VERIFIER_TOOLS": os.path.join(repo, "fresh-cache")}))

    def test_python_files_never_need_node(self):
        repo = branch_with(self, {"README.md": "x\n"}, {"pkg/a.py": PY_TANGLED})
        result = self.run_with(repo, self.only(repo, "git", "python3"))
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("Refactor tangled", result.stdout)

    def test_without_python3_it_cannot_run(self):
        repo = branch_with(self, {"README.md": "x\n"}, {"pkg/a.py": PY_TANGLED})
        result = self.run_with(repo, self.only(repo, "git"))
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn("needs python3 on PATH", result.stderr)


class PythonMeasureScript(unittest.TestCase):
    """tools/cognitive-complexity-python/measure.py, run by the pinned venv the way the gate runs it."""

    def measure(self, files, limit=15):
        sys.path.insert(0, str(ROOT / "gates"))
        self.addCleanup(sys.path.remove, str(ROOT / "gates"))
        from _tools import ensure_python_tool
        root = make_repo(self, files)
        return subprocess.run([ensure_python_tool("cognitive-complexity-python"),
                               str(ROOT / "tools" / "cognitive-complexity-python" / "measure.py"), root, str(limit), *files],
                              capture_output=True, text=True)

    def test_it_prints_only_over_limit_functions_at_their_first_line(self):
        result = self.measure({"a.py": PY_SIMPLE + "\n\n" + PY_TANGLED, "b.py": PY_SIMPLE})
        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads(result.stdout)
        self.assertEqual(list(report), ["a.py"])
        self.assertEqual([line for line, _ in report["a.py"]], [5])

    def test_neutralizing_the_ignore_comments_keeps_every_line_number(self):
        marked = "# complexipy: ignore\n" + PY_TANGLED.replace("def tangled(a, b, c):", "def tangled(a, b, c):  # NoQA: Complexipy")
        result = self.measure({"a.py": marked})
        self.assertEqual([line for line, _ in json.loads(result.stdout)["a.py"]], [2])

    def test_a_file_that_does_not_parse_exits_3_naming_it(self):
        result = self.measure({"ok.py": PY_SIMPLE, "bad.py": "def broken(:\n"})
        self.assertEqual(result.returncode, 3)
        self.assertIn("could not parse bad.py", result.stderr)


class ConsumerSuppressions(unittest.TestCase):
    """ESLint reads eslint-suppressions.json (bulk suppressions) from the working directory. The consumer's
    entries are for its own config: against the gate's one rule they all looked unused, and eslint exits 2."""

    # One entry per file for a rule the gate never runs, and one for the gate's own rule on the tangled file:
    # a consumer's count is not the ratchet's, so it must not hide the finding either.
    SUPPRESSIONS = json.dumps({
        "src/bad.ts": {"no-console": {"count": 1}, "sonarjs/cognitive-complexity": {"count": 1}},
        "src/good.ts": {"no-console": {"count": 1}},
    })

    def test_the_gate_runs_and_judges_as_if_the_file_were_absent(self):
        repo = branch_with(self, {"eslint-suppressions.json": self.SUPPRESSIONS}, {"src/good.ts": SIMPLE})
        result = gate("cognitive-complexity", repo, "--base-ref", "HEAD~1")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        commit(repo, {"src/bad.ts": TANGLED}, "tangled")
        for scope in (("--base-ref", "HEAD~1"), ("--all",)):
            result = gate("cognitive-complexity", repo, *scope, "--format", "json")
            self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
            self.assertEqual([(f["path"], f["line"]) for f in json.loads(result.stdout)["findings"]], [("src/bad.ts", 1)])


class MaxFileLines(unittest.TestCase):
    def long(self, n):
        return "".join("export const v%d = %d;\n" % (i, i) for i in range(n))

    def test_a_new_file_over_the_limit_fails(self):
        repo = branch_with(self, {"README.md": "x\n"}, {"src/big.ts": self.long(501)})
        result = gate("max-file-lines", repo, "--base-ref", "HEAD~1")
        self.assertEqual(result.returncode, 1)
        self.assertIn("src/big.ts: 501 lines, over the 500 allowed (new file)", result.stdout)

    def test_a_long_file_that_shrank_or_was_untouched_passes_and_one_that_grew_fails(self):
        repo = branch_with(self, {"src/big.ts": self.long(600), "src/other.ts": self.long(700)}, {"src/big.ts": self.long(590)})
        self.assertEqual(gate("max-file-lines", repo, "--base-ref", "HEAD~1").returncode, 0)
        commit(repo, {"src/big.ts": self.long(601)}, "grow")
        result = gate("max-file-lines", repo, "--base-ref", "HEAD~2")
        self.assertEqual(result.returncode, 1)
        self.assertIn("601 lines, over the 500 allowed (600 at the base)", result.stdout)
        self.assertNotIn("other.ts", result.stdout)

    def test_the_base_is_the_merge_base_not_the_base_branchs_tip(self):
        # The branch changes one line of a 600-line file. Meanwhile main shrinks it to 550. Against main's tip the
        # branch would look as if it had grown the file by 50 lines; against where it started, it grew nothing.
        repo = make_repo(self, {"src/big.ts": self.long(600)})
        git(repo, "checkout", "-q", "-b", "feat/x")
        commit(repo, {"src/big.ts": self.long(599) + "export const last = 0;\n"}, "touch")
        git(repo, "checkout", "-q", "main")
        commit(repo, {"src/big.ts": self.long(550)}, "shrink on main")
        git(repo, "checkout", "-q", "feat/x")
        result = gate("max-file-lines", repo, "--base-ref", "main")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        # Whether the file existed is asked of the merge base too: main deleting it does not make it new here.
        git(repo, "checkout", "-q", "main")
        git(repo, "rm", "-q", "src/big.ts")
        git(repo, "commit", "-q", "-m", "delete it on main")
        git(repo, "checkout", "-q", "feat/x")
        result = gate("max-file-lines", repo, "--base-ref", "main")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        # And real growth on the branch is still measured from where it started.
        commit(repo, {"src/big.ts": self.long(610)}, "grow")
        result = gate("max-file-lines", repo, "--base-ref", "main")
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("610 lines, over the 500 allowed (600 at the base)", result.stdout)

    def test_python_files_count_by_default(self):
        repo = branch_with(self, {"README.md": "x\n"}, {"pkg/big.py": "x = 1\n" * 501})
        result = gate("max-file-lines", repo, "--base-ref", "HEAD~1")
        self.assertEqual(result.returncode, 1)
        self.assertIn("pkg/big.py: 501 lines, over the 500 allowed (new file)", result.stdout)

    def test_all_and_max(self):
        repo = branch_with(self, {"src/big.ts": self.long(120)}, {"README.md": "y\n"})
        self.assertEqual(gate("max-file-lines", repo, "--base-ref", "HEAD~1", "--max", "100").returncode, 0)
        result = gate("max-file-lines", repo, "--all", "--max", "100")
        self.assertEqual(result.returncode, 1)
        self.assertIn("src/big.ts: 120 lines", result.stdout)


class Renames(unittest.TestCase):
    """A moved file is compared with itself at its old path, never counted as new."""

    def test_moving_a_tangled_file_is_not_a_finding_but_growing_it_is(self):
        # Enough unchanged lines that the grown file is still, to git, the same file moved (-M is 50%).
        padding = "".join("export const c%d = %d;\n" % (i, i) for i in range(40))
        repo = make_repo(self, {"src/old.ts": TANGLED + padding})
        git(repo, "mv", "src/old.ts", "src/new.ts")
        git(repo, "commit", "-q", "-m", "move")
        self.assertEqual(gate("cognitive-complexity", repo, "--base-ref", "HEAD~1").returncode, 0)
        commit(repo, {"src/new.ts": TANGLED + padding + TANGLED.replace("function tangled", "function tangled2")}, "grow")
        result = gate("cognitive-complexity", repo, "--base-ref", "HEAD~2")
        self.assertEqual(result.returncode, 1)
        self.assertIn("1 at the base, 2 now", result.stdout)

    def test_a_non_ascii_path_is_still_judged(self):
        # git quotes such a path unless asked for -z; a quoted path matched nothing and was skipped.
        repo = make_repo(self, {"README.md": "x\n"})
        commit(repo, {"src/caf\u00e9.ts": "x\n" * 600}, "add")
        result = gate("max-file-lines", repo, "--base-ref", "HEAD~1", "--format", "json")
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertEqual([f["path"] for f in json.loads(result.stdout)["findings"]], ["src/caf\u00e9.ts"])

    def test_moving_a_long_file_is_not_a_finding_but_growing_it_is(self):
        long = "x\n" * 600
        repo = make_repo(self, {"src/old.ts": long})
        git(repo, "mv", "src/old.ts", "src/new.ts")
        git(repo, "commit", "-q", "-m", "move")
        self.assertEqual(gate("max-file-lines", repo, "--base-ref", "HEAD~1").returncode, 0)
        commit(repo, {"src/new.ts": long + "y\n"}, "grow")
        result = gate("max-file-lines", repo, "--base-ref", "HEAD~2")
        self.assertEqual(result.returncode, 1)
        self.assertIn("600 at the base", result.stdout)


if __name__ == "__main__":
    unittest.main()
