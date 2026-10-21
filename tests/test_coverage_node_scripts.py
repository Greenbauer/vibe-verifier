"""coverage-not-lost on node:test files when a pull request changes which files a package.json script runs: a file either
side runs with `node --test` is test code, so it never counts, even when only the head's script selects it. The fixtures
are tests/test_coverage_node.py's."""
import json
import unittest

from test_coverage_node import HEADER, PLAIN_TEST, PRICE, findings, project, run_gate

# A helper the tests import. Its name is one of Node's default test names (test-*), so a bare `node --test` runs it too.
HELPER = 'export function cents(value) {\n  return Math.round(value * 100);\n}\n'
HELPER_TEST = HEADER + 'import { cents } from "../src/test-money.mjs";\ntest("counts cents", () => { assert.equal(cents(1.5), 150); });\n'


def package(command):
    return json.dumps({"type": "module", "scripts": {"test": command}}) + "\n"


class ScriptChanges(unittest.TestCase):
    def test_a_helper_a_widened_script_runs_as_a_test_is_not_lost(self):
        # At the merge base the helper is source the tests ran; at the head the script is a bare `node --test`, which
        # also runs src/test-money.mjs as a test file. Its lines did not stop running: they became test code.
        repo = project(self, {"src/price.mjs": PRICE, "src/test-money.mjs": HELPER, "test/plain.test.mjs": PLAIN_TEST,
                              "test/money.test.mjs": HELPER_TEST},
                       {"package.json": package("node --test")}, package=package("node --test test/*.test.mjs"))
        result = run_gate(repo)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(findings(result), [])
        self.assertRegex(result.stderr, r"the run at the merge base [0-9a-f]{12}: 8 line\(s\) ran \(8 in src/\)")
        self.assertRegex(result.stderr, r"the run at the head: 5 line\(s\) ran \(5 in src/\)")


if __name__ == "__main__":
    unittest.main()
