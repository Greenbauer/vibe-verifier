"""`bin/vibe-verifier affected-tests`, through its command line and its composite action, on small repositories:
a base commit on main and a pull request's commit on a branch, judged against main."""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

from helpers import ROOT, clean_env, commit, git, make_repo, runner

sys.path.insert(0, str(ROOT / "gates"))
import _affected_graph  # noqa: E402
import _affected_tests  # noqa: E402

ACTION = ROOT / "actions" / "affected-tests" / "action.yml"
REPORTER = ROOT / "actions" / "affected-tests" / "failed-files.mjs"

# cart.test.ts -> cart.ts -> price.ts -> money.ts: money is three imports away from the test.
SHOP = {
    "package.json": json.dumps({"name": "shop", "scripts": {"test": "vitest run"}, "devDependencies": {"vitest": "4.1.11"}}),
    "src/money.ts": "export const cents = (x: number) => Math.round(x * 100);\n",
    "src/price.ts": "import { cents } from './money';\nexport const price = (x: number) => cents(x);\n",
    "src/cart.ts": "import { price } from \"./price.js\";\nexport const total = (xs: number[]) => xs.map(price);\n",
    "src/cart.test.ts": "import { it } from 'vitest';\nimport { total } from './cart';\nit('totals', () => total([1]));\n",
    "src/other.ts": "export const other = 1;\n",
    "src/other.test.ts": "import { it } from 'vitest';\nimport { other } from './other';\nit('is one', () => other);\n",
}


def branch(repo, files, deleted=()):
    git(repo, "checkout", "-q", "-b", "change")
    for path in deleted:
        os.remove(os.path.join(repo, path))
    commit(repo, files, "the pull request")


def affected(repo, *args, env=None):
    result = runner("affected-tests", "--repo", repo, "--base-ref", "main", *args, env=env)
    return result, result.stdout.splitlines()


class Selection(unittest.TestCase):
    def test_a_test_importing_a_changed_file_is_selected_and_one_that_does_not_is_not(self):
        repo = make_repo(self, SHOP)
        branch(repo, {"src/other.ts": "export const other = 2;\n"})
        result, listed = affected(repo)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(listed, ["src/other.test.ts"])
        self.assertIn("selected 1 of 2 test files", result.stderr)

    def test_a_test_reaching_a_changed_file_three_imports_away_is_selected(self):
        # Fails if the walk stops short: cart.test.ts reaches money.ts only through cart.ts and price.ts.
        repo = make_repo(self, SHOP)
        branch(repo, {"src/money.ts": "export const cents = (x: number) => Math.floor(x * 100);\n"})
        result, listed = affected(repo)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(listed, ["src/cart.test.ts"])

    def test_a_tsconfig_paths_alias_reached_through_extends_is_resolved(self):
        repo = make_repo(self, {
            "tsconfig.base.json": '{\n  // shared by every package\n  "compilerOptions": {"baseUrl": ".", "paths": {"@core/*": ["packages/core/src/*"],},},\n}\n',
            "packages/web/tsconfig.json": '{"extends": "../../tsconfig.base.json"}',
            "packages/core/src/money.ts": "export const cents = 1;\n",
            "packages/web/lib/pay.test.ts": "import { cents } from '@core/money';\n",
            "packages/web/lib/plain.test.ts": "import { it } from 'vitest';\n",
        })
        branch(repo, {"packages/core/src/money.ts": "export const cents = 2;\n"})
        result, listed = affected(repo)
        self.assertEqual(listed, ["packages/web/lib/pay.test.ts"], result.stderr)

    def test_a_workspace_package_name_resolves_to_its_entry_and_its_subpaths(self):
        repo = make_repo(self, {
            "packages/core/package.json": json.dumps({"name": "@acme/core", "main": "dist/index.js",
                                                      "exports": {".": {"import": "./src/index.ts"}, "./*": "./src/*.ts"}}),
            "packages/core/src/index.ts": "export * from './money';\n",
            "packages/core/src/money.ts": "export const cents = 1;\n",
            "packages/web/entry.test.ts": "import { cents } from '@acme/core';\n",
            "packages/web/subpath.test.ts": "import { cents } from '@acme/core/money';\n",
            "packages/web/external.test.ts": "import { useState } from 'react';\n",
        })
        branch(repo, {"packages/core/src/money.ts": "export const cents = 2;\n"})
        result, listed = affected(repo)
        self.assertEqual(listed, ["packages/web/entry.test.ts", "packages/web/subpath.test.ts"], result.stderr)

    def test_python_imports_absolute_and_relative_are_followed(self):
        repo = make_repo(self, {
            "src/shop/__init__.py": "",
            "src/shop/money.py": "def cents(x):\n    return round(x * 100)\n",
            "src/shop/price.py": "from .money import cents\n\ndef price(x):\n    return cents(x)\n",
            "src/shop/other.py": "OTHER = 1\n",
            "tests/test_price.py": "from shop.price import (\n    price,  # what is tested\n)\n",
            "tests/test_other.py": "import shop.other as other\n",
        })
        branch(repo, {"src/shop/money.py": "def cents(x):\n    return int(x * 100)\n"})
        result, listed = affected(repo)
        self.assertEqual(listed, ["tests/test_price.py"], result.stderr)

    def test_a_node_test_file_counts_as_a_test_whatever_its_name(self):
        repo = make_repo(self, {
            "scripts/lib.mjs": "export const one = 1;\n",
            "scripts/check.mjs": "import test from 'node:test';\nimport { one } from './lib.mjs';\ntest('one', () => one);\n",
            "scripts/run.mjs": "import { one } from './lib.mjs';\nconsole.log(one);\n",
        })
        branch(repo, {"scripts/lib.mjs": "export const one = 2;\n"})
        result, listed = affected(repo)
        self.assertEqual(listed, ["scripts/check.mjs"], result.stderr)

    def test_a_test_still_importing_a_deleted_file_is_selected(self):
        repo = make_repo(self, SHOP)
        branch(repo, {"src/note.md": "other.ts is gone\n"}, deleted=["src/other.ts"])
        result, listed = affected(repo)
        self.assertEqual(listed, ["src/other.test.ts"], result.stderr)

    def test_a_setup_file_a_runner_config_names_selects_the_tests_under_it(self):
        repo = make_repo(self, dict(SHOP, **{
            "web/vitest.config.ts": "export default { test: { setupFiles: ['./test/setup.ts'] } };\n",
            "web/test/setup.ts": "import { server } from './server';\nserver.listen();\n",
            "web/test/server.ts": "export const server = { listen() {} };\n",
            "web/app.test.ts": "import { it } from 'vitest';\n",
        }))
        branch(repo, {"web/test/server.ts": "export const server = { listen() { return 1; } };\n"})
        result, listed = affected(repo)
        self.assertEqual(listed, ["web/app.test.ts"], result.stderr)


class AlwaysRun(unittest.TestCase):
    def test_tests_that_read_files_or_a_database_always_run(self):
        repo = make_repo(self, dict(SHOP, **{
            "src/fixtures.test.ts": "import { readFileSync } from 'node:fs';\nreadFileSync('fixtures/a.json');\n",
            "src/db.test.ts": "const url = process.env.DATABASE_URL;\n",
            "tests/test_files.py": "def test_reads():\n    open('data.csv')\n",
        }))
        branch(repo, {"fixtures/a.json": "{}\n"})
        result, listed = affected(repo)
        self.assertEqual(listed, ["src/db.test.ts", "src/fixtures.test.ts", "tests/test_files.py"], result.stderr)
        self.assertIn("3 always run", result.stderr)

    def test_a_test_whose_helper_starts_a_process_always_runs(self):
        # The helper runs a script by a path it computes, which no import names.
        repo = make_repo(self, {
            "tests/helpers.py": "import subprocess\n\ndef gate(name):\n    return subprocess.run(['python3', 'gates/' + name + '.py'])\n",
            "tests/test_gate.py": "from helpers import gate\n",
            "tests/test_pure.py": "def test_x():\n    assert True\n",
            "gates/check.py": "print('ok')\n",
        })
        branch(repo, {"gates/check.py": "print('changed')\n"})
        result, listed = affected(repo)
        self.assertEqual(listed, ["tests/test_gate.py"], result.stderr)

    def test_prose_in_a_comment_and_a_mocked_module_do_not_count_as_reading(self):
        repo = make_repo(self, dict(SHOP, **{
            "src/prose.test.ts": "// a type-only import (per the rule), and we open (the drawer)\nvi.mock('node:fs');\n",
            "tests/test_prose.py": '"""Reads the glob of sources; open (the file) is not called."""\n',
        }))
        branch(repo, {"README.md": "x\n"})
        result, listed = affected(repo)
        self.assertEqual(listed, [], result.stderr)

    def test_always_names_more_tests(self):
        repo = make_repo(self, SHOP)
        branch(repo, {"src/money.ts": "export const cents = 0;\n"})
        result, listed = affected(repo, "--always", "src/other.test.ts")
        self.assertEqual(listed, ["src/cart.test.ts", "src/other.test.ts"], result.stderr)


class All(unittest.TestCase):
    def assert_all(self, files, reason, *args):
        repo = make_repo(self, SHOP)
        branch(repo, files)
        result, listed = affected(repo, *args)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(listed, ["ALL"], result.stderr)
        self.assertIn(reason, result.stderr)

    def test_a_dependency_change_in_package_json_selects_all(self):
        manifest = json.loads(SHOP["package.json"])
        manifest["devDependencies"]["vitest"] = "4.1.12"
        self.assert_all({"package.json": json.dumps(manifest)}, "package.json changed what it installs")

    def test_a_scripts_only_change_in_package_json_does_not(self):
        repo = make_repo(self, SHOP)
        manifest = json.loads(SHOP["package.json"])
        manifest["scripts"]["test"] = "vitest run src/cart.test.ts src/other.test.ts"
        branch(repo, {"package.json": json.dumps(manifest, indent=2)})
        result, listed = affected(repo)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(listed, [], result.stderr)
        self.assertIn("selected 0 of 2 test files", result.stderr)

    def test_a_lockfile_change_selects_all(self):
        self.assert_all({"pnpm-lock.yaml": "lockfileVersion: '9.0'\n"}, "pnpm-lock.yaml changed (a lockfile)")

    def test_a_tsconfig_or_test_runner_config_change_selects_all(self):
        self.assert_all({"tsconfig.json": "{}"}, "tsconfig.json changed (a tsconfig)")
        self.assert_all({"vitest.config.ts": "export default {};\n"}, "vitest.config.ts changed (a test runner's config)")
        self.assert_all({"tests/conftest.py": "import pytest\n"}, "conftest.py changed (a test runner's config)")

    def test_a_playwright_config_selects_all_only_when_a_listed_test_is_a_playwright_test(self):
        repo = make_repo(self, dict(SHOP, **{
            "playwright.config.ts": "export default { testDir: 'e2e' };\n",
            "src/warm.test.ts": "vi.mock('@playwright/test', () => ({}));\n",
            "e2e/home.spec.ts": "import { test } from '@playwright/test';\n",
        }))
        branch(repo, {"playwright.config.ts": "export default { testDir: 'e2e', retries: 1 };\n"})
        unit, listed = affected(repo, "--exclude", "e2e/**")
        self.assertEqual(listed, [], unit.stderr)
        every, listed = affected(repo)
        self.assertEqual(listed, ["ALL"], every.stderr)
        self.assertIn("playwright.config.ts changed (a test runner's config)", every.stderr)

    def test_a_path_all_on_names_selects_all(self):
        self.assert_all({"scripts/run-tests.mjs": "x\n"}, "scripts/run-tests.mjs changed (named by --all-on)",
                        "--all-on", "scripts/run-tests.mjs")

    def test_no_base_or_an_unreadable_base_selects_all(self):
        repo = make_repo(self, SHOP)
        branch(repo, {"src/other.ts": "export const other = 2;\n"})
        result = runner("affected-tests", "--repo", repo)
        self.assertEqual((result.returncode, result.stdout), (0, "ALL\n"), result.stderr)
        self.assertIn("no base to compare with", result.stderr)
        result = runner("affected-tests", "--repo", repo, "--base-ref", "release")
        self.assertEqual((result.returncode, result.stdout), (0, "ALL\n"), result.stderr)
        self.assertIn("the base release cannot be read", result.stderr)
        from_ci = runner("affected-tests", "--repo", repo, env={"GITHUB_BASE_REF": "main"})
        self.assertEqual(from_ci.stdout, "src/other.test.ts\n", from_ci.stderr)

    def test_outside_a_git_repository_it_cannot_run(self):
        empty = tempfile.mkdtemp(prefix="vv-")
        self.addCleanup(shutil.rmtree, empty, True)
        result = runner("affected-tests", "--repo", empty, "--base-ref", "main")
        self.assertEqual((result.returncode, result.stdout), (2, ""), result.stderr)
        self.assertIn("affected-tests could not run", result.stderr)


VITEST_JUNIT = """<?xml version="1.0" encoding="UTF-8" ?>
<testsuites name="vitest tests" tests="3" failures="2" errors="0">
    <testsuite name="src/cart.test.ts" tests="1" failures="1">
        <testcase classname="src/cart.test.ts" name="totals"><failure message="no">AssertionError</failure></testcase>
    </testsuite>
    <testsuite name="src/other.test.ts" tests="2" failures="1">
        <testcase classname="src/other.test.ts" name="is one"><failure message="no">AssertionError</failure></testcase>
        <testcase classname="src/other.test.ts" name="passes"></testcase>
    </testsuite>
</testsuites>
"""
PYTEST_JUNIT = """<?xml version="1.0" encoding="utf-8"?><testsuites name="pytest tests"><testsuite name="pytest" tests="2">
<testcase classname="tests.test_price.TestPrice" name="test_rounds"><failure message="assert">x</failure></testcase>
<testcase classname="" name="tests.test_other"><error message="collection failure">ImportError</error></testcase>
</testsuite></testsuites>"""


class Report(unittest.TestCase):
    def results(self, text):
        path = os.path.join(tempfile.mkdtemp(prefix="vv-results-"), "results")
        self.addCleanup(shutil.rmtree, os.path.dirname(path), True)
        with open(path, "w") as handle:
            handle.write(text)
        return path

    def test_a_failure_in_a_test_the_selection_left_out_is_a_miss(self):
        repo = make_repo(self, SHOP)
        branch(repo, {"src/money.ts": "export const cents = 0;\n"})
        result, lines = affected(repo, "--report", self.results(VITEST_JUNIT), "--format", "github")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("affected-tests: report: 2 test files failed in the full run, 1 of them selected; 1 missed", lines)
        self.assertIn("::warning title=affected-tests::miss: src/other.test.ts failed in the full run and was not selected", lines)
        self.assertFalse([line for line in lines if "cart.test.ts" in line])

    def test_pytest_names_and_a_plain_list_of_absolute_paths_find_their_files(self):
        repo = make_repo(self, {
            "shop/price.py": "X = 1\n", "shop/other.py": "Y = 1\n",
            "tests/test_price.py": "from shop.price import X\n", "tests/test_other.py": "from shop.other import Y\n",
        })
        branch(repo, {"shop/price.py": "X = 2\n"})
        result, lines = affected(repo, "--report", self.results(PYTEST_JUNIT))
        self.assertIn("affected-tests: report: 2 test files failed in the full run, 1 of them selected; 1 missed", lines, result.stderr)
        self.assertIn("affected-tests: miss: tests/test_other.py failed in the full run and was not selected", lines)
        listed = self.results(os.path.join(os.path.realpath(repo), "tests", "test_other.py") + "\n")
        result, lines = affected(repo, "--report", listed)
        self.assertIn("affected-tests: miss: tests/test_other.py failed in the full run and was not selected", lines, result.stderr)

    def test_a_failure_that_names_no_known_test_file_is_reported_as_a_possible_miss(self):
        repo = make_repo(self, SHOP)
        branch(repo, {"src/money.ts": "export const cents = 0;\n"})
        junit = '<testsuites><testsuite name="jest tests"><testcase classname="Cart totals" name="adds">' \
                '<failure>x</failure></testcase></testsuite></testsuites>'
        result, lines = affected(repo, "--report", self.results(junit))
        self.assertIn("affected-tests: miss: 'Cart totals' failed in the full run and names no test file this selection counts",
                      lines, result.stderr)

    def test_results_that_hold_no_test_case_or_are_missing_cannot_run(self):
        repo = make_repo(self, SHOP)
        branch(repo, {"src/money.ts": "export const cents = 0;\n"})
        result, _ = affected(repo, "--report", self.results('<testsuites name="vitest tests" tests="0"></testsuites>'))
        self.assertEqual(result.returncode, 2)
        self.assertIn("holds no test case", result.stderr)
        result, _ = affected(repo, "--report", os.path.join(repo, "missing.xml"))
        self.assertEqual(result.returncode, 2)
        self.assertIn("cannot read the results", result.stderr)

    def test_when_the_selection_was_all_nothing_could_be_missed(self):
        repo = make_repo(self, SHOP)
        branch(repo, {"yarn.lock": "x\n"})
        result, lines = affected(repo, "--report", self.results(VITEST_JUNIT))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(lines, ["affected-tests: report: the selection was ALL (yarn.lock changed (a lockfile)), "
                                 "so no failure could be missed"])

    @unittest.skipUnless(shutil.which("node"), "node is not installed")
    def test_the_node_test_reporter_writes_the_failed_files_the_report_reads(self):
        repo = make_repo(self, {
            "lib.mjs": "export const one = 1;\n", "other.mjs": "export const two = 2;\n",
            "lib.test.mjs": "import test from 'node:test';\nimport assert from 'node:assert';\nimport { one } from './lib.mjs';\n"
                            "test('one', () => assert.equal(one, 1));\n",
            "other.test.mjs": "import test from 'node:test';\nimport assert from 'node:assert';\nimport { two } from './other.mjs';\n"
                              "test('two', () => assert.equal(two, 3));\n",
        })
        branch(repo, {"lib.mjs": "export const one = 1; // changed\n"})
        failed = os.path.join(repo, "failed-files.txt")
        run = subprocess.run(["node", "--test", "--test-reporter=" + str(REPORTER), "--test-reporter-destination=" + failed,
                              "lib.test.mjs", "other.test.mjs"], cwd=repo, capture_output=True, text=True)
        self.assertEqual(run.returncode, 1, run.stdout + run.stderr)
        result, lines = affected(repo, "--report", failed)
        self.assertIn("affected-tests: miss: other.test.mjs failed in the full run and was not selected", lines, result.stderr)


def action_script():
    match = re.search(r"\n      run: \|\n((?:        [^\n]*\n|\n)+)", ACTION.read_text())
    return "".join(line[8:] for line in match.group(1).splitlines(True))


class Action(unittest.TestCase):
    def run_action(self, repo, **inputs):
        temp = tempfile.mkdtemp(prefix="vv-runner-temp-")
        self.addCleanup(shutil.rmtree, temp, True)
        output = os.path.join(temp, "output")
        env = {"VV_" + name.upper(): inputs.get(name, "") for name in ("base_ref", "tests", "exclude", "always", "all_on", "report")}
        env.update({"RUNNER_TEMP": temp, "GITHUB_ACTION_PATH": str(ACTION.parent), "GITHUB_OUTPUT": output})
        result = subprocess.run(["bash", "-c", action_script()], cwd=repo, capture_output=True, text=True, env=clean_env(env))
        with open(output) as handle:
            outputs = dict(line.split("=", 1) for line in handle.read().splitlines())
        return result, outputs

    def test_the_action_writes_the_list_and_says_whether_every_test_runs(self):
        repo = make_repo(self, SHOP)
        branch(repo, {"src/money.ts": "export const cents = 0;\n"})
        result, outputs = self.run_action(repo, base_ref="main", exclude="src/other.test.ts\n")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((outputs["all"], outputs["count"]), ("false", "1"))
        with open(outputs["list"]) as handle:
            self.assertEqual(handle.read(), "src/cart.test.ts\n")
        self.assertEqual(outputs["reporter"], str(REPORTER))
        result, outputs = self.run_action(repo)  # not a pull request: no base
        self.assertEqual((result.returncode, outputs["all"], outputs["count"]), (0, "true", ""), result.stderr)
        self.assertEqual(os.path.getsize(outputs["list"]), 0)


class Graph(unittest.TestCase):
    def test_a_js_specifier_finds_its_ts_source_and_an_index_file(self):
        graph = _affected_graph.Graph("/nonexistent", ["a/b.ts", "a/c/index.tsx", "a/d.mts"])
        self.assertEqual(graph.resolve_js("a/x.ts", "./b.js"), {"a/b.ts"})
        self.assertEqual(graph.resolve_js("a/x.ts", "./c"), {"a/c/index.tsx"})
        self.assertEqual(graph.resolve_js("a/x.ts", "./d.mjs"), {"a/d.mts"})
        self.assertEqual(graph.resolve_js("a/x.ts", "../outside"), set())

    def test_the_summary_counts_both_reasons(self):
        selection = _affected_tests.Selection(["a.test.ts", "b.test.ts", "c.test.ts"], by_import=["a.test.ts"],
                                              by_reads=["a.test.ts", "b.test.ts"], changed=4)
        self.assertEqual(selection.selected, ["a.test.ts", "b.test.ts"])
        self.assertIn("selected 2 of 3 test files for 4 changed files: 1 are or import a changed file, 2 always run",
                      selection.summary())


if __name__ == "__main__":
    unittest.main()
