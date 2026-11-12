"""`bin/vibe-verifier affected-tests --report`: which tests a full run failed that the selection left out (a miss),
read from JUnit XML or a list of failed files, and `--fail-on-miss`. Split from test_affected_tests.py for size."""

import os
import shutil
import subprocess
import tempfile
import unittest

from helpers import make_repo
from test_affected_tests import REPORTER, SHOP, affected, branch

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

    def test_fail_on_miss_fails_on_a_miss_and_passes_when_every_failure_was_selected(self):
        repo = make_repo(self, SHOP)
        branch(repo, {"src/money.ts": "export const cents = 0;\n"})
        result, lines = affected(repo, "--report", self.results(VITEST_JUNIT), "--format", "github", "--fail-on-miss")
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn("::error title=affected-tests::miss: src/other.test.ts failed in the full run and was not selected", lines)
        selected_only = VITEST_JUNIT.replace('<failure message="no">AssertionError</failure></testcase>\n        <testcase classname="src/other.test.ts" name="passes">',
                                             '</testcase>\n        <testcase classname="src/other.test.ts" name="passes">')
        self.assertNotIn("src/other.test.ts\" name=\"is one\"><failure", selected_only)
        result, lines = affected(repo, "--report", self.results(selected_only), "--fail-on-miss")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("affected-tests: report: 1 test files failed in the full run, 1 of them selected; 0 missed", lines)

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

    def test_the_node_test_reporter_writes_each_failed_file_once(self):
        run = subprocess.run(["node", "--test", "--test-reporter=tap", str(REPORTER.with_suffix(".test.mjs"))],
                             capture_output=True, text=True)
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
        self.assertIn("# pass 2", run.stdout)


if __name__ == "__main__":
    unittest.main()
