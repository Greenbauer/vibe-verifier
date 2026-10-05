"""The changed-code-mutation gate: the real pinned StrykerJS against a fixture project's own Vitest
(npm ci from tests/fixtures/changed-code-mutation), and a stand-in Stryker for the contract's edges."""
import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from helpers import ROOT, commit, gate, git, make_repo

FIXTURE = ROOT / "tests" / "fixtures" / "changed-code-mutation"
TOOL = ROOT / "tools" / "changed-code-mutation"
IGNORE = {".gitignore": "/node_modules\n"}  # root only: a nested node_modules must reach the gate's exclusion

PRICE_BEFORE = "export function discount(total: number, isMember: boolean): number {\n  return total;\n}\n"
PRICE_AFTER = """export function discount(total: number, isMember: boolean): number {
  if (isMember && total > 100) {
    return total * 0.9;
  }
  return total;
}
"""
# Copies the logic instead of importing it: passes whatever src/price.ts does.
COPYING_TEST = """import { expect, it } from "vitest";
function discount(total: number, isMember: boolean): number {
  if (isMember && total > 100) return total * 0.9;
  return total;
}
it("gives members 10% off over 100", () => { expect(discount(200, true)).toBe(180); });
"""
# Stryker rewrites a project's tsconfig.json for its sandbox with the typescript it finds beside itself; a
# project with one failed to run until the toolchain pinned typescript.
TSCONFIG = json.dumps({"compilerOptions": {"strict": True, "module": "esnext", "moduleResolution": "bundler"}})
IMPORTING_TEST = """import { expect, it } from "vitest";
import { discount } from "./price";
it("gives members 10% off over 100", () => { expect(discount(200, true)).toBe(180); });
it("charges non-members full price", () => { expect(discount(200, false)).toBe(200); });
it("charges members full price at exactly 100", () => { expect(discount(100, true)).toBe(100); });
"""


def findings(result):
    return json.loads(result.stdout)["findings"]


class RealStryker(unittest.TestCase):
    """The pinned toolchain (npm ci into the tool cache) running the fixture's own Vitest 4."""

    @classmethod
    def setUpClass(cls):
        cls.deps = tempfile.mkdtemp(prefix="vv-mutation-deps-")
        for name in ("package.json", "package-lock.json"):
            shutil.copy(FIXTURE / name, cls.deps)
        subprocess.run(["npm", "ci", "--ignore-scripts", "--no-audit", "--no-fund"], cwd=cls.deps, check=True,
                       capture_output=True, text=True)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.deps, ignore_errors=True)

    def project(self, base, head, at=""):
        """A repository whose project (at `at`) resolves the fixture's node_modules from its root."""
        repo = make_repo(self, dict(IGNORE, **{at + "package.json": (FIXTURE / "package.json").read_text()}, **base))
        os.symlink(os.path.join(self.deps, "node_modules"), os.path.join(repo, "node_modules"))
        commit(repo, head)
        return repo

    def test_a_test_that_copies_the_logic_leaves_the_changed_lines_unguarded(self):
        repo = self.project({"src/price.ts": PRICE_BEFORE, "src/price.test.ts": COPYING_TEST, "tsconfig.json": TSCONFIG},
                            {"src/price.ts": PRICE_AFTER})
        result = gate("changed-code-mutation", repo, "--base-ref", "HEAD~1", "--format", "json")
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        summary, *mutants = findings(result)
        self.assertIsNone(summary["path"])
        self.assertRegex(summary["message"], r"is 0% \(0 of \d+ mutants killed\), below --break 60")
        self.assertTrue(mutants)
        # Only the changed lines 2-4 were mutated: line 1 holds the function body's mutant when a whole file is.
        self.assertEqual({(m["path"], 2 <= m["line"] <= 4) for m in mutants}, {("src/price.ts", True)})
        self.assertTrue(any("`isMember && total > 100` -> `false` (ConditionalExpression)" in m["message"] for m in mutants),
                        [m["message"] for m in mutants])
        self.assertEqual([p for p in os.listdir(repo) if p.startswith(".vibe-verifier-mutation-")], [])

    def test_a_test_that_imports_the_real_function_passes_in_a_project_below_the_root(self):
        repo = self.project({"web/src/price.ts": PRICE_BEFORE, "web/src/price.test.ts": IMPORTING_TEST, "tsconfig.base.json": TSCONFIG,
                             "web/tsconfig.json": json.dumps({"extends": "../tsconfig.base.json", "include": ["src"]})},
                            {"web/src/price.ts": PRICE_AFTER, "lib/other.ts": "export const other = 1 + 1;\n"}, at="web/")
        result = gate("changed-code-mutation", repo, "--base-ref", "HEAD~1", "--project", "web")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertRegex(result.stderr, r"(\d+) of \1 mutant\(s\) on the changed lines killed: score 100%")
        self.assertIn("1 changed source file(s) outside --project web are not judged: lib/other.ts", result.stderr)


FAKE_STRYKER = r"""
const fs = require("fs");
const config = JSON.parse(fs.readFileSync(process.argv[3], "utf8"));
fs.writeFileSync(process.env.VV_CAPTURE, JSON.stringify({config, cwd: process.cwd(), sandbox: fs.existsSync(config.tempDirName)}));
const mode = process.env.VV_MODE || "report";
if (mode === "hang") {
  setInterval(() => {}, 1000);
} else if (mode === "fail") {
  console.log("There were failed tests in the initial test run.");
  process.exit(1);
} else {
  const files = new Set(config.mutate.map((pattern) => pattern.replace(/:\d+-\d+$/, "")));
  console.log(`INFO ProjectReader Found ${mode === "short" ? files.size - 1 : files.size} of 40 file(s) to be mutated.`);
  fs.writeFileSync(config.jsonReporter.fileName, process.env.VV_REPORT || '{"files": {}}');
}
"""


def mutant(status, line, start, end, replacement, mutator="ConditionalExpression"):
    return {"id": str(line) + status, "mutatorName": mutator, "replacement": replacement, "status": status,
            "location": {"start": {"line": line, "column": start}, "end": {"line": line, "column": end}}}


class StandInStryker(unittest.TestCase):
    """A stand-in for the pinned Stryker in a tool cache of its own, and a stand-in for the project's vitest."""

    def setUp(self):
        self.cache = tempfile.mkdtemp(prefix="vv-mutation-cache-")
        self.addCleanup(shutil.rmtree, self.cache, True)
        digest = hashlib.sha256((TOOL / "package-lock.json").read_bytes()).hexdigest()[:12]
        modules = Path(self.cache, "changed-code-mutation-" + digest, "node_modules")
        (modules / "@stryker-mutator" / "core" / "bin").mkdir(parents=True)
        (modules / ".package-lock.json").write_text("{}")
        (modules / "@stryker-mutator" / "core" / "bin" / "stryker.js").write_text(FAKE_STRYKER)
        self.capture = os.path.join(self.cache, "capture.json")

    def repo(self, base, head, vitest="4.1.11"):
        repo = make_repo(self, dict(IGNORE, **base))
        if vitest:
            Path(repo, "node_modules", "vitest").mkdir(parents=True)
            Path(repo, "node_modules", "vitest", "package.json").write_text(json.dumps({"version": vitest}))
        commit(repo, head)
        return repo

    def run_gate(self, repo, *args, mode="report", report=None, base="HEAD~1"):
        env = {"VIBE_VERIFIER_TOOLS": self.cache, "VV_CAPTURE": self.capture, "VV_MODE": mode}
        if report is not None:
            env["VV_REPORT"] = json.dumps(report)
        return gate("changed-code-mutation", repo, "--base-ref", base, *args, env=env)

    def captured(self):
        with open(self.capture) as handle:
            return json.load(handle)

    def test_only_the_lines_added_or_modified_at_head_are_mutated(self):
        lines = ["const a%d = %d;\n" % (i, i) for i in range(1, 11)]
        padding = "".join("export const keep%d = %d;\n" % (i, i) for i in range(20))
        head = list(lines)
        head[1] = "const a2 = 22;\n"                               # modified: line 2
        head[4:6] = ["const a5 = 55;\n", "const a6 = 66;\n"]       # modified: lines 5-6
        del head[7]                                                # deleted (old line 8): nothing to mutate
        head.insert(8, "const added = 1;\n")                      # added: line 9 at HEAD
        repo = self.repo({"src/a.ts": "".join(lines), "src/old.ts": padding, "src/gone.ts": "export const g = 1;\nexport const h = 2;\n"},
                         {"src/a.ts": "".join(head), "src/new.ts": "export const n = 1;\nexport const m = 2;\n",
                          "src/gone.ts": "export const g = 1;\n"})
        git(repo, "mv", "src/old.ts", "src/moved.ts")
        commit(repo, {"src/moved.ts": padding.replace("keep3 = 3", "keep3 = 33")}, "move and change one line")
        result = self.run_gate(repo, base="HEAD~2")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.captured()["config"]["mutate"],
                         ["src/a.ts:2-2", "src/a.ts:5-6", "src/a.ts:9-9", "src/moved.ts:4-4", "src/new.ts:1-2"])

    def test_tests_types_config_stories_node_modules_and_excludes_are_not_mutated(self):
        repo = self.repo({"README.md": "x\n"},
                         {"src/a.ts": "export const a = 1;\n", "src/a.test.ts": "x\n", "src/b.spec.tsx": "x\n",
                          "src/__tests__/c.ts": "x\n", "src/types.d.ts": "x\n", "vite.config.ts": "x\n",
                          "src/d.stories.tsx": "x\n", "vendor/node_modules/e.js": "x\n", "src/gen/f.ts": "x\n",
                          "docs/g.md": "x\n"})
        result = self.run_gate(repo, "--exclude", "src/gen/**")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.captured()["config"]["mutate"], ["src/a.ts:1-1"])
        self.assertEqual(self.captured()["cwd"], os.path.realpath(repo))

    def test_no_changed_source_passes_without_a_test_runner(self):
        repo = self.repo({"src/a.ts": "export const a = 1;\n"}, {"src/a.test.ts": "x\n", "README.md": "y\n"}, vitest=None)
        result = self.run_gate(repo)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertFalse(os.path.exists(self.capture))

    def test_without_the_projects_vitest_it_cannot_run_even_under_soak(self):
        repo = self.repo({"README.md": "x\n"}, {"src/a.ts": "export const a = 1;\n"}, vitest=None)
        result = self.run_gate(repo, "--soak")
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn("no vitest is installed for .: install the project's dependencies (npm ci)", result.stderr)

    def test_a_vitest_the_pinned_stryker_cannot_kill_mutants_under_cannot_run(self):
        repo = self.repo({"README.md": "x\n"}, {"src/a.ts": "export const a = 1;\n"}, vitest="5.0.3")
        result = self.run_gate(repo)
        self.assertEqual(result.returncode, 2)
        self.assertIn("vitest 5.0.3 is not 2.x to 4.x", result.stderr)

    def test_more_files_than_the_cap_cannot_run_and_nothing_is_mutated(self):
        repo = self.repo({"README.md": "x\n"}, {"src/%s.ts" % n: "export const %s = 1;\n" % n for n in "abc"})
        result = self.run_gate(repo, "--max-files", "2", "--soak")
        self.assertEqual(result.returncode, 2)
        self.assertIn("3 changed source files, over --max-files 2; no verdict on any of them", result.stderr)
        self.assertFalse(os.path.exists(self.capture))
        self.assertEqual(self.run_gate(repo, "--max-files", "3").returncode, 0)

    def test_a_run_past_the_timeout_is_killed_cannot_run_and_leaves_no_sandbox(self):
        repo = self.repo({"README.md": "x\n"}, {"src/a.ts": "export const a = 1;\n"})
        result = self.run_gate(repo, "--timeout", "2", mode="hang")
        self.assertEqual(result.returncode, 2)
        self.assertIn("did not finish within --timeout 2s", result.stderr)
        self.assertTrue(self.captured()["sandbox"])
        self.assertEqual([p for p in os.listdir(repo) if p.startswith(".vibe-verifier-mutation-")], [])

    def test_a_failed_stryker_run_cannot_run_and_shows_why(self):
        repo = self.repo({"README.md": "x\n"}, {"src/a.ts": "export const a = 1;\n"})
        result = self.run_gate(repo, mode="fail")
        self.assertEqual(result.returncode, 2)
        self.assertIn("There were failed tests in the initial test run.", result.stderr)
        self.assertIn("StrykerJS exited 1", result.stderr)

    def test_a_requested_file_stryker_did_not_match_cannot_run(self):
        repo = self.repo({"README.md": "x\n"}, {"src/a.ts": "export const a = 1;\n", "src/b.ts": "export const b = 1;\n"})
        result = self.run_gate(repo, mode="short")
        self.assertEqual(result.returncode, 2)
        self.assertIn("StrykerJS matched 1 of the 2 changed file(s) it was asked to mutate", result.stderr)

    SOURCE = "export const f = (a: number) => a > 1 ? a : 0;\n"

    def scored(self, statuses):
        """A report for src/a.ts with one mutant per status, all on line 1."""
        return {"files": {"src/a.ts": {"source": self.SOURCE, "mutants": [
            mutant(status, 1, 33, 38, "true" if i % 2 else "false") for i, status in enumerate(statuses)]}}}

    def test_the_score_leaves_out_timeouts_and_compile_errors_and_break_decides(self):
        repo = self.repo({"README.md": "x\n"}, {"src/a.ts": self.SOURCE})
        report = self.scored(["Killed", "Killed", "Killed", "Survived", "NoCoverage", "Timeout", "CompileError", "RuntimeError", "Ignored"])
        passed = self.run_gate(repo, "--format", "json", report=report)  # 3 of 5: 60%, the default --break
        self.assertEqual(passed.returncode, 0, passed.stdout + passed.stderr)
        self.assertIn("3 of 5 mutant(s) on the changed lines killed: score 60%, --break 60", passed.stderr)
        for left_out in ("timed out", "did not compile", "crashed the test runner", "disabled by a comment"):
            self.assertIn("src/a.ts:1: mutant %s, left out of the score" % left_out, passed.stderr)
        failed = self.run_gate(repo, "--format", "json", "--break", "61", report=report)
        self.assertEqual(failed.returncode, 1, failed.stdout + failed.stderr)
        summary, *rest = findings(failed)
        self.assertIn("is 60% (3 of 5 mutants killed), below --break 61", summary["message"])
        self.assertEqual([(f["path"], f["line"], f["message"]) for f in rest], [
            ("src/a.ts", 1, "mutant survived: every test still passed: `a > 1` -> `true` (ConditionalExpression)"),
            ("src/a.ts", 1, "mutant not covered: no test runs this code: `a > 1` -> `false` (ConditionalExpression)")])
        self.assertEqual(self.run_gate(repo, "--break", "61", "--soak", report=report).returncode, 0)

    def test_no_scored_mutant_on_the_changed_lines_passes(self):
        repo = self.repo({"README.md": "x\n"}, {"src/a.ts": self.SOURCE})
        self.assertEqual(self.run_gate(repo, report={"files": {}}).returncode, 0)
        result = self.run_gate(repo, report=self.scored(["Timeout"]))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("no mutant on the changed lines to score (1 left out)", result.stderr)

    def test_an_unfinished_mutant_cannot_run(self):
        repo = self.repo({"README.md": "x\n"}, {"src/a.ts": self.SOURCE})
        result = self.run_gate(repo, report=self.scored(["Killed", "Pending"]))
        self.assertEqual(result.returncode, 2)
        self.assertIn("with status 'Pending': the run did not finish", result.stderr)

    def test_a_project_outside_the_repository_cannot_run(self):
        repo = self.repo({"README.md": "x\n"}, {"src/a.ts": self.SOURCE})
        self.assertEqual(self.run_gate(repo, "--project", "../elsewhere").returncode, 2)
        self.assertEqual(self.run_gate(repo, "--project", "missing").returncode, 2)


if __name__ == "__main__":
    unittest.main()
