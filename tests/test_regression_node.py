"""regression-test-fails-on-base on node:test files: the gate on synthetic repositories under the Node on
PATH (CI's, and locally 20, 22 and 24), and gates/_regression_node.py's reading of a package.json
script's command and of Node's TAP report in the shapes Node 20 writes (CI runs one Node)."""
import json
import os
import sys
import unittest
from unittest import mock

from helpers import ROOT, make_repo
from test_regression_test import findings, fix, tree_is_head

sys.path.insert(0, str(ROOT / "gates"))
import _regression_node  # noqa: E402  (gates/ is not a package)

PRICE_BUG = "export function discount(total, member) {\n  return total;\n}\n"
PRICE_FIX = "export function discount(total, member) {\n  return member && total > 100 ? total * 0.9 : total;\n}\n"
HEADER = 'import { test } from "node:test";\nimport assert from "node:assert/strict";\n'
REPRODUCES = HEADER + 'import { discount } from "./price.mjs";\n' \
    'test("gives members 10% off over 100", () => { assert.equal(discount(200, true), 180); });\n' \
    'test("charges non-members full price", () => { assert.equal(discount(200, false), 200); });\n'
DOES_NOT_REPRODUCE = HEADER + 'import { discount } from "./price.mjs";\n' \
    'test("charges non-members full price", () => { assert.equal(discount(200, false), 200); });\n'
SCRIPTS = {"scripts": {"lint": "eslint .", "test:unit": "node --test src/*.test.mjs && echo done"}}


def package(scripts=SCRIPTS):
    return json.dumps(dict(scripts, type="module")) + "\n"


class RealNode(unittest.TestCase):
    """The gate running node:test files the way the repository's package.json or manifest says."""

    def project(self, files=None, scripts=SCRIPTS):
        return make_repo(self, dict({"package.json": package(scripts), "src/price.mjs": PRICE_BUG}, **(files or {})))

    def test_a_node_test_that_fails_on_the_code_before_the_fix_passes(self):
        repo = self.project()
        result = fix(repo, {"src/price.mjs": PRICE_FIX, "src/price.test.mjs": REPRODUCES})
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("src/price.test.mjs: gives members 10% off over 100 fails on the code before the fix: "
                      "AssertionError: Expected values to be strictly equal: 200 !== 180", result.stderr)
        tree_is_head(self, repo)

    def test_a_node_test_that_passes_on_the_code_before_the_fix_does_not_reproduce_the_bug(self):
        repo = self.project()
        result = fix(repo, {"src/price.mjs": PRICE_FIX, "src/price.test.mjs": DOES_NOT_REPRODUCE}, "fix(price): x", "",
                     "--format", "json")
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        summary, per_file = findings(result)
        self.assertIn("no new or changed test fails on the code before the fix", summary["message"])
        self.assertEqual((per_file["path"], per_file["message"]),
                         ("src/price.test.mjs", "its 1 test(s) all pass on the code before the fix, so it does not reproduce the bug"))
        tree_is_head(self, repo)

    def test_a_node_test_that_fails_there_only_because_what_it_needs_is_missing_does_not_show_the_bug(self):
        # A module the test imports, an export it imports, and a module a `before` hook loads, each new in the fix:
        # the first two stop the file loading (reported in TAP only as the file's stderr), the third cancels the test.
        repo = self.project()
        hook = 'import { before, describe, it } from "node:test";\nimport assert from "node:assert/strict";\n' \
               'let mask;\ndescribe("mask", () => {\n  before(async () => { ({ mask } = await import("./mask.mjs")); });\n' \
               '  it("masks", () => { assert.equal(mask("ab"), "**"); });\n});\n'
        result = fix(repo, {"src/mask.mjs": 'export const mask = (s) => "*".repeat(s.length);\n',
                            "src/mask.test.mjs": HEADER + 'import { mask } from "./mask.mjs";\ntest("masks", () => { assert.equal(mask("ab"), "**"); });\n',
                            "src/price.mjs": PRICE_BUG + "export const roundUp = (n) => Math.ceil(n);\n",
                            "src/round.test.mjs": HEADER + 'import { roundUp } from "./price.mjs";\ntest("rounds up", () => { assert.equal(roundUp(1.2), 2); });\n',
                            "src/hook.test.mjs": hook},
                     "fix(price): x", "", "--format", "json")
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        _, *per_file = findings(result)
        self.assertEqual([(f["path"], f["message"].split(" (")[0]) for f in per_file], [
            ("src/hook.test.mjs", "fails on the code before the fix only because `src/mask.mjs` is missing"),
            ("src/mask.test.mjs", "fails on the code before the fix only because `src/mask.mjs` is missing"),
            ("src/round.test.mjs", "fails on the code before the fix only because `roundUp` is missing")])
        tree_is_head(self, repo)

    def test_a_file_whose_top_level_assertion_fails_on_the_code_before_the_fix_shows_the_bug(self):
        # The test reads the workflow and asserts its step exists before any test runs: before the fix the step is not
        # there, so the file stops on that assertion, which is the bug showing, not a file that cannot load.
        test = HEADER + 'import { readFileSync } from "node:fs";\nconst workflow = readFileSync("ci/workflow.yml", "utf8");\n' \
                        'assert.match(workflow, /name: Retry the seed/, "the retry step must exist");\n' \
                        'test("the retry step retries three times", () => { assert.match(workflow, /attempts: 3/); });\n'
        repo = self.project({"ci/workflow.yml": "steps:\n  - name: Seed\n"})
        result = fix(repo, {"ci/workflow.yml": "steps:\n  - name: Seed\n  - name: Retry the seed\n    attempts: 3\n",
                            "src/workflow.test.mjs": test})
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("src/workflow.test.mjs: its top-level code fails on the code before the fix: AssertionError [ERR_ASSERTION]: "
                      "the retry step must exist", result.stderr)
        tree_is_head(self, repo)

    def test_a_node_test_that_fails_at_head_is_a_red_suite_and_cannot_run(self):
        repo = self.project()
        result = fix(repo, {"src/price.mjs": PRICE_FIX, "src/price.test.mjs": REPRODUCES.replace("180)", "170)")},
                     "fix(price): x", "", "--soak")
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn("1 changed test(s) fail at HEAD, so the pull request's suite is red", result.stderr)
        self.assertIn("src/price.test.mjs: gives members 10% off over 100: AssertionError: Expected values to be strictly "
                      "equal: 180 !== 170", result.stderr)
        tree_is_head(self, repo)

    def test_a_package_script_runs_its_files_from_its_package_with_its_own_options(self):
        # The command names no file, so Node's default names select price-test.mjs, which is not named like a test
        # anywhere else; the test passes only with the script's --require and only run from the package.
        setup = "globalThis.currency = 'EUR';\n"
        test = HEADER + 'import { discount } from "./price.mjs";\ntest("members in EUR get 10% off", () => {\n' \
                        '  assert.equal(globalThis.currency, "EUR");\n  assert.equal(discount(200, true), 180);\n});\n'
        repo = make_repo(self, {"package.json": package({"scripts": {}}), "packages/shop/setup.cjs": setup,
                                "packages/shop/package.json": package({"scripts": {"test": "TZ=UTC node --require ./setup.cjs --test"}}),
                                "packages/shop/src/price.mjs": PRICE_BUG})
        result = fix(repo, {"packages/shop/src/price.mjs": PRICE_FIX, "packages/shop/src/price-test.mjs": test})
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("packages/shop/src/price-test.mjs: members in EUR get 10% off fails on the code before the fix: "
                      "AssertionError: Expected values to be strictly equal: 200 !== 180", result.stderr)
        tree_is_head(self, repo)

    def test_a_node_test_in_a_folder_named_like_a_glob_runs_as_that_file(self):
        # Node 21 and later read a --test path as a glob: app/items/[id]/price.test.mjs given as it is matched no file
        # and ran no test, and app/+(sale)/price.test.mjs given as it is ran app/sale/price.test.mjs in its place.
        for folder in ("app/items/[id]", "app/+(sale)", "app/@(modal)"):
            with self.subTest(folder):
                repo = make_repo(self, {"package.json": package({"scripts": {"test": "node --test 'app/**/*.test.mjs'"}}),
                                        folder + "/price.mjs": PRICE_BUG, "app/sale/price.mjs": PRICE_BUG,
                                        "app/sale/price.test.mjs": DOES_NOT_REPRODUCE, "app/modal/price.mjs": PRICE_BUG,
                                        "app/modal/price.test.mjs": DOES_NOT_REPRODUCE})
                result = fix(repo, {folder + "/price.mjs": PRICE_FIX, folder + "/price.test.mjs": REPRODUCES})
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertIn(folder + "/price.test.mjs: gives members 10% off over 100 fails on the code before the fix",
                              result.stderr)
                tree_is_head(self, repo)

    def test_a_node_test_the_repository_does_not_run_with_node_test_is_not_judged_unless_declared(self):
        unrun = make_repo(self, {"package.json": package({"scripts": {"test": "vitest run"}}), "scripts/price.mjs": PRICE_BUG})
        result = fix(unrun, {"scripts/price.mjs": PRICE_FIX, "scripts/price.test.mjs": REPRODUCES}, "fix: x", "", "--soak")
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn("scripts/price.test.mjs: a node:test file that no package.json script runs with `node --test` and "
                      "no --node-test declares", result.stderr)
        # Declared, it runs under node --test, though it names "vitest" (as a test of a mutation runner's command line would).
        declared = make_repo(self, {"package.json": package({"scripts": {"test": "vitest run"}}), "scripts/price.mjs": PRICE_BUG})
        result = fix(declared, {"scripts/price.mjs": PRICE_FIX, "scripts/price.test.mjs": REPRODUCES + 'const runner = "vitest";\n'},
                     "fix: x", "", "--node-test", "scripts/**/*.test.mjs")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("scripts/price.test.mjs: gives members 10% off over 100 fails on the code before the fix", result.stderr)


NODE_20_LOAD_ERROR = """TAP version 13
# node:internal/modules/esm/resolve:283
#     throw new ERR_MODULE_NOT_FOUND(
#           ^
# Error [ERR_MODULE_NOT_FOUND]: Cannot find module '/w/src/mask.mjs' imported from /w/src/mask.test.mjs
#     at \\#cachedDefaultResolve (node:internal/modules/esm/loader:657:25) {
#   code: 'ERR_MODULE_NOT_FOUND',
# }
# Node.js v20.20.2
# Subtest: /w/src/mask.test.mjs
not ok 1 - /w/src/mask.test.mjs
  ---
  duration_ms: 31.9
  location: '/w/src/mask.test.mjs:1:1'
  failureType: 'testCodeFailure'
  exitCode: 1
  signal: ~
  error: 'test failed'
  code: 'ERR_TEST_FAILURE'
  ...
1..1
# tests 1
# fail 1
"""
NODE_20_SUITE = """TAP version 13
# Subtest: discount
    # Subtest: gives members 10\\# off
    not ok 1 - gives members 10\\# off
      ---
      duration_ms: 0.8
      location: '/w/src/price.test.mjs:5:3'
      failureType: 'testCodeFailure'
      error: |-
        Expected values to be strictly equal:

        200 !== 180

      code: 'ERR_ASSERTION'
      name: 'AssertionError'
      expected: 180
      actual: 200
      operator: 'strictEqual'
      ...
    # Subtest: rounds
    ok 2 - rounds # SKIP not yet
      ---
      duration_ms: 0.1
      ...
    # Subtest: reads a field
    not ok 3 - reads a field
      ---
      duration_ms: 0.2
      failureType: 'testCodeFailure'
      error: "Cannot read properties of null (reading 'value')"
      code: 'ERR_TEST_FAILURE'
      name: 'TypeError'
      ...
    1..3
not ok 1 - discount
  ---
  duration_ms: 1.5
  location: '/w/src/price.test.mjs:4:1'
  failureType: 'subtestsFailed'
  error: '2 subtests failed'
  code: 'ERR_TEST_FAILURE'
  ...
# Subtest: charges full price
ok 2 - charges full price
  ---
  duration_ms: 0.3
  ...
1..2
"""


class TapAndScripts(unittest.TestCase):
    """What gates/_regression_node.py reads from a script's command and from a TAP report."""

    def test_a_file_that_did_not_load_is_its_own_entry_and_the_reason_is_its_stderr(self):
        run = _regression_node.tap_run(NODE_20_LOAD_ERROR, {"src/mask.test.mjs", "/w/src/mask.test.mjs"})
        self.assertEqual(run, _regression_node.Run("Error [ERR_MODULE_NOT_FOUND]: Cannot find module '/w/src/mask.mjs'", {}))
        passed_script = "TAP version 13\n# Subtest: check.test.mjs\nok 1 - check.test.mjs\n  ---\n  duration_ms: 20\n  ...\n1..1\n"
        self.assertEqual(_regression_node.tap_run(passed_script, {"check.test.mjs"}), _regression_node.Run(None, {}))

    def test_each_test_is_its_suite_path_with_its_outcome_and_error(self):
        run = _regression_node.tap_run(NODE_20_SUITE, {"src/price.test.mjs"})
        self.assertEqual(run.tests, {
            "discount > gives members 10# off": ("failed", "AssertionError: Expected values to be strictly equal: 200 !== 180"),
            "discount > rounds": ("skipped", ""),
            "discount > reads a field": ("failed", "TypeError: Cannot read properties of null (reading 'value')"),
            "charges full price": ("passed", "")})

    def test_a_scripts_node_test_command_keeps_its_options_and_drops_the_ones_the_gate_sets(self):
        [words, _] = _regression_node.commands("NODE_ENV=test node --require ts-node/register --test --test-reporter=spec "
                                               "--test-reporter-destination stdout --watch 'tests/**/*.test.ts' && echo ok")
        self.assertEqual(_regression_node.node_test(words),
                         ({"NODE_ENV": "test"}, ["--require", "ts-node/register"], ["tests/**/*.test.ts"]))
        for script in ("node scripts/run.mjs --test", "vitest run", "node --test \"$RUNNER_TEMP/x.test.mjs\""):
            self.assertEqual([_regression_node.node_test(words) for words in _regression_node.commands(script)], [None], script)

    def test_a_path_is_given_to_node_test_as_a_glob_that_selects_only_that_file(self):
        literal = _regression_node.literal
        with mock.patch.object(_regression_node, "globs", return_value=True):  # Node 21 and later
            self.assertEqual(literal("app/[id]/page.test.ts"), "app/[[]id[]]/page.test.ts")
            self.assertEqual(literal("app/[...slug]/a*b?.test.ts"), "app/[[]...slug[]]/a[*]b[?].test.ts")
            for extglob, given in (("+", "+"), ("@", "@"), ("!", "!"), ("*", "[*]"), ("?", "[?]")):  # ( as a class ends each
                self.assertEqual(literal("app/%s(shop)/a.test.ts" % extglob), "app/%s[(]shop)/a.test.ts" % given)
            self.assertEqual(literal("app/(group)/a.test.ts"), "app/[(]group)/a.test.ts")
            self.assertEqual(literal("app/{a,b}/a.test.ts"), "app/?a,b}/a.test.ts")  # no class stops brace expansion
        with mock.patch.object(_regression_node, "globs", return_value=False):  # Node 20 takes a path as it is
            self.assertEqual(literal("app/[id]/+(a)/page.test.ts"), "app/[id]/+(a)/page.test.ts")

    def test_which_files_a_commands_paths_select(self):
        selects = _regression_node.selects
        self.assertTrue(selects(["./src/a.test.mjs"], "src/a.test.mjs", "/nonexistent"))
        self.assertTrue(selects(["tests/**/*.test.{js,mjs}"], "tests/unit/a.test.mjs", "/nonexistent"))
        self.assertFalse(selects(["tests/**/*.test.js"], "tests/unit/a.test.mjs", "/nonexistent"))
        self.assertTrue(selects([], "lib/test/helpers.mjs", "/nonexistent"))  # Node's default names
        self.assertFalse(selects([], "lib/helpers.mjs", "/nonexistent"))
        self.assertTrue(selects(["tests"], "tests/a.test.mjs", str(ROOT)))  # a directory, as Node 20 searches one
        self.assertFalse(selects(["tests"], "tests/fixtures.mjs", str(ROOT)))


if __name__ == "__main__":
    unittest.main()
