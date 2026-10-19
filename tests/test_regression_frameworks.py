"""regression-test-fails-on-base on a JS or TS test file that only calls describe, it or test: Vitest with `globals: true`,
Jest and bun test all read those calls, so gates/_regression_frameworks.py reads the runner from the file's imports and
its package. Synthetic repositories with no node_modules, so a file taken for Vitest stops the gate (no vitest is
installed), which is how a Jest or bun repository's every fix used to end."""
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

from helpers import ROOT, make_repo
from test_regression_test import PRICE_BUG, PRICE_FIX, PY_BUG, PY_FIX, PY_UNITTEST, findings, fix

NO_RUNNER = ("--python", "/nonexistent/python")
CALLS = 'describe("price", () => {\n  it("gives members 10% off", () => { expect(1).toBe(1); });\n});\n'
# A Jest test (a jest.config.js beside it) and a bun test (it imports bun:test).
JEST_AND_BUN = {"apps/api/src/price.test.ts": CALLS, "apps/quote/price.test.ts": 'import { expect, it } from "bun:test";\n' + CALLS}


class Frameworks(unittest.TestCase):

    def test_a_jest_or_bun_test_is_not_judged_and_names_its_runner(self):
        # The Python test is one this gate runs, so the verdict is its; a file taken for Vitest would stop the gate instead.
        repo = make_repo(self, {"shop/__init__.py": "", "shop/price.py": PY_BUG, "apps/tools/package.json": "{}\n",
                                "apps/api/package.json": '{"devDependencies": {"jest": "29.7.0"}}\n',
                                "apps/web/package.json": '{"scripts": {"test": "jest --ci"}}\n', "apps/web/jest.config.js": "module.exports = {};\n",
                                "apps/shop/package.json": '{"scripts": {"test": "bun test --coverage"}}\n'})
        result = fix(repo, {"shop/price.py": PY_FIX, "tests/test_price.py": PY_UNITTEST, "apps/api/src/price.test.ts": CALLS,
                            "apps/web/src/price.test.ts": CALLS, "apps/shop/src/price.test.ts": CALLS,
                            "apps/quote/price.test.ts": 'import { describe, expect, it } from "bun:test";\n' + CALLS,
                            "apps/tools/price.test.ts": 'import { describe, expect, it } from "@jest/globals";\n' + CALLS,
                            "scripts/price.test.js": CALLS},
                     "fix(price): members get their discount", "", "--python", sys.executable)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("tests/test_price.py: test_price.Discount.test_members_get_ten_percent_off_over_100 fails on the code before "
                      "the fix", result.stderr)
        for said in ("apps/api/src/price.test.ts: not judged: a Jest test (apps/api/package.json), which this gate does not run",
                     "apps/web/src/price.test.ts: not judged: a Jest test (apps/web/jest.config.js), which this gate does not run",
                     "apps/shop/src/price.test.ts: not judged: a bun test (apps/shop/package.json), which this gate does not run",
                     "apps/quote/price.test.ts: not judged: a bun test (it imports bun:test), which this gate does not run",
                     "apps/tools/price.test.ts: not judged: a Jest test (it imports @jest/globals), which this gate does not run",
                     "scripts/price.test.js: not judged: it calls describe, it or test without importing a framework, and no "
                     "Vitest config or package.json that names vitest holds it"):
            self.assertIn(said, result.stderr)

    def test_a_fix_whose_only_tests_are_jest_or_bun_tests_passes_qualified_and_is_never_exit_2(self):
        # The repository's own runner, which this gate does not run yet: not the job's fault, so not exit 2, and a soak
        # in a Jest or bun repository stays green. Any other test the gate cannot run beside them keeps exit 2.
        qualifier = Path(tempfile.mkdtemp(prefix="vv-qualifier-")) / "note"
        self.addCleanup(shutil.rmtree, qualifier.parent, True)
        for soak in ((), ("--soak",)):
            qualifier.unlink(missing_ok=True)
            repo = make_repo(self, {"src/price.ts": PRICE_BUG, "apps/api/jest.config.js": "module.exports = {};\n"})
            result = fix(repo, dict({"src/price.ts": PRICE_FIX}, **JEST_AND_BUN), "fix(price): x", "", "--format", "json", *soak,
                         *NO_RUNNER, env={"VIBE_VERIFIER_QUALIFIER": str(qualifier)})
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertEqual(qualifier.read_text(), "not judged: the changed tests run under Jest and bun test, which this gate "
                                                    "does not run yet")
            self.assertEqual([(f["path"], f["advisory"], f["message"].split(", which")[0]) for f in findings(result)], [
                ("apps/api/src/price.test.ts", True, "not judged: a Jest test (apps/api/jest.config.js)"),
                ("apps/quote/price.test.ts", True, "not judged: a bun test (it imports bun:test)")])
        repo = make_repo(self, {"src/price.ts": PRICE_BUG, "apps/api/jest.config.js": "module.exports = {};\n"})
        result = fix(repo, dict({"src/price.ts": PRICE_FIX, "db/price.test.sql": "select 1;\n"}, **JEST_AND_BUN), "fix(price): x",
                     "", "--soak", *NO_RUNNER)
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn("no changed test is one this gate can run", result.stderr)

    def test_a_test_whose_package_runs_vitest_is_a_vitest_test_without_importing_it(self):
        # A workspace package that names no runner runs its tests with the root's Vitest; a Vitest config says so too.
        for files in ({"package.json": '{"devDependencies": {"vitest": "4.1.11"}}\n', "packages/shop/package.json": '{"name": "shop"}\n'},
                      {"packages/shop/vitest.config.ts": "export default {};\n", "package.json": '{"devDependencies": {"jest": "29.7.0"}}\n'}):
            repo = make_repo(self, dict({"packages/shop/src/price.ts": PRICE_BUG}, **files))
            result = fix(repo, {"packages/shop/src/price.ts": PRICE_FIX, "packages/shop/src/price.test.ts": CALLS}, "fix: x", "",
                         *NO_RUNNER)
            self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
            self.assertIn("no vitest is installed for packages/shop (for packages/shop/src/price.test.ts)", result.stderr)

    def test_each_way_a_package_names_its_runner(self):
        # Imported here, not at the top, so the black-box tests above still load on the code before this module existed.
        sys.path.insert(0, str(ROOT / "gates"))
        self.addCleanup(sys.path.remove, str(ROOT / "gates"))
        import _regression_frameworks
        directory = Path(tempfile.mkdtemp(prefix="vv-package-"))
        self.addCleanup(shutil.rmtree, directory, True)
        for package, runner in (({"devDependencies": {"vitest": "4.1.11", "jest": "29.7.0"}}, "vitest"),
                                ({"dependencies": {"jest": "29.7.0"}}, "jest"), ({"jest": {"testEnvironment": "node"}}, "jest"),
                                ({"scripts": {"test": "NODE_ENV=test bun test src"}}, "bun"),
                                ({"scripts": {"test": "react-scripts test"}}, None), ({"name": "shop"}, None), ([], None)):
            (directory / "package.json").write_text(json.dumps(package))
            self.assertEqual(_regression_frameworks.declared_runner(str(directory / "package.json")), runner, package)
        (directory / "package.json").write_text("{ not json")
        self.assertIsNone(_regression_frameworks.declared_runner(str(directory / "package.json")))


if __name__ == "__main__":
    unittest.main()
