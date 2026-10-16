"""The regression-test-fails-on-base gate on synthetic repositories: its rules with no test runner at all
(so a run that tried to start one could not pass), the fixture's real Vitest 4 (npm ci from
tests/fixtures/changed-code-mutation, as the mutation tests install it), and Python under unittest
(a venv without pytest) and under the fixture's pytest (tests/fixtures/changed-code-mutation-python)."""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from helpers import ROOT, commit, gate, git, make_repo, write
from test_mutation import FIXTURE, install_fixture

GATE = "regression-test-fails-on-base"
PYTEST = ROOT / "tests" / "fixtures" / "changed-code-mutation-python" / "requirements.txt"
IGNORE = {".gitignore": "node_modules\n"}  # the symlink below, and the cache Vitest writes under the package it runs in

PRICE_BUG = "export function discount(total: number, member: boolean): number {\n  return total;\n}\n"
PRICE_FIX = """export function discount(total: number, member: boolean): number {
  return member && total > 100 ? total * 0.9 : total;
}
"""
REPRODUCES = """import { expect, it } from "vitest";
import { discount } from "./price";
it("gives members 10% off over 100", () => { expect(discount(200, true)).toBe(180); });
it("charges non-members full price", () => { expect(discount(200, false)).toBe(200); });
"""
# Passes on the code before the fix too: it pins behaviour the fix did not change.
DOES_NOT_REPRODUCE = """import { expect, it } from "vitest";
import { discount } from "./price";
it("charges non-members full price", () => { expect(discount(200, false)).toBe(200); });
"""
PLAYWRIGHT_SPEC = 'import { test, expect } from "@playwright/test";\ntest("x", async ({ page }) => { await page.goto("/"); });\n'


def findings(result):
    return json.loads(result.stdout)["findings"]


class WhatIsMissing(unittest.TestCase):
    """gates/_regression_runners.py's one judgement made from text: a failure that says something the test needs
    does not exist yet. Each message is the shape a runner wrote on a real history, with the names made up."""

    @classmethod
    def setUpClass(cls):
        sys.path.insert(0, str(ROOT / "gates"))
        import _regression_runners
        cls.missing = staticmethod(_regression_runners.missing)

    @classmethod
    def tearDownClass(cls):
        sys.path.remove(str(ROOT / "gates"))

    def test_a_module_export_file_or_script_that_does_not_exist_yet_is_missing(self):
        for error, what in [
            ("Cannot find module './mask' imported from /w/src/mask.test.ts", "./mask"),
            ("Cannot find package '@/lib/mask' imported from /w/src/mask.test.ts", "@/lib/mask"),
            ("Error: Failed to load url ./mask (resolved id: ./mask) in /w/src/a.ts. Does the file exist?", "./mask"),
            ('Failed to resolve import "./mask" from "src/a.test.ts". Does the file exist?', "./mask"),
            ("TypeError: __vi_import_1__.maskToken is not a function", "maskToken"),
            ("TypeError: (0 , __vite_ssr_import_3__.buildMarkers) is not a function", "buildMarkers"),
            ("TypeError: (0 , __vite_ssr_import_0__.Store) is not a constructor", "Store"),
            ('Error: [vitest] No "parse" export is defined on the "./parser" mock. Did you forget to return it from "vi.mock"?', "parse"),
            ("SyntaxError: The requested module './x.js' does not provide an export named 'mask'", "mask"),
            ("ModuleNotFoundError: No module named 'shop.mask'", "shop.mask"),
            ("ImportError: cannot import name 'mask' from 'shop.price' (/w/shop/price.py)", "mask"),
            ("AttributeError: module 'shop.price' has no attribute 'mask'", "shop.price.mask"),
            ("Error: ENOENT: no such file or directory, open 'migrations/001.sql'", "migrations/001.sql"),
            ("FileNotFoundError: [Errno 2] No such file or directory: 'scripts/new.sh'", "scripts/new.sh"),
            ("/bin/bash: scripts/ci/new-check.sh: No such file or directory", "scripts/ci/new-check.sh"),
        ]:
            self.assertEqual(self.missing(error), what, error)

    def test_an_assertion_or_an_error_the_old_code_raised_is_not(self):
        for error in ("AssertionError: expected 200 to be 180 // Object.is equality",
                      "AssertionError: expected 'bash: x.sh: No such file or directory' to be ''",
                      "TypeError: Cannot read properties of null (reading 'value')",
                      'Error: promise resolved "1" instead of rejecting', ""):
            self.assertIsNone(self.missing(error), error)


def fix(repo, files, title="fix(price): members get their discount", body="", *args, env=None):
    """Commit `files` on a branch, write the title and body, and run the gate against main."""
    git(repo, "checkout", "-q", "-b", "change")
    commit(repo, files, "change")
    inputs = tempfile.mkdtemp(prefix="vv-regression-inputs-")
    write(inputs, {"title.txt": title + "\n", "body.md": body})
    try:
        return gate(GATE, repo, "--base-ref", "main", "--title", os.path.join(inputs, "title.txt"),
                    "--body", os.path.join(inputs, "body.md"), *args, env=env)
    finally:
        shutil.rmtree(inputs, ignore_errors=True)


def tree_is_head(test, repo):
    """The working tree is exactly HEAD again: every swapped file was put back."""
    status = subprocess.run(["git", "-C", repo, "status", "--porcelain", "--untracked-files=all"], capture_output=True, text=True)
    test.assertEqual(status.stdout, "")


class Rules(unittest.TestCase):
    """Decided before any test runs. No node_modules and no interpreter, so a gate that tried to run a test
    would exit 2."""

    NO_RUNNER = ("--python", "/nonexistent/python")

    def repo(self):
        return make_repo(self, {"src/price.ts": PRICE_BUG, "src/util.py": "def f():\n    return 1  # one\n",
                                "README.md": "x\n", "package.json": "{}\n"})

    def test_a_pull_request_that_is_not_a_fix_passes_without_running_a_test(self):
        for title in ("feat(price): member discounts", "fixup: later", "chore: fix the typo"):
            repo = self.repo()
            result = fix(repo, {"src/price.ts": PRICE_FIX, "src/price.test.ts": REPRODUCES}, title, "", *self.NO_RUNNER)
            self.assertEqual(result.returncode, 0, title + result.stdout + result.stderr)
            self.assertIn("not a fix pull request", result.stderr)

    def test_a_fix_that_changes_only_tests_docs_or_comments_passes_without_running_a_test(self):
        repo = self.repo()
        result = fix(repo, {"src/price.test.ts": DOES_NOT_REPRODUCE, "tests/helpers.py": "x = 1\n", "docs/price.md": "y\n",
                            "README.md": "z\n", "src/price.ts": "// why the total is returned as is\n" + PRICE_BUG.replace("{\n", "{  \n"),
                            "src/util.py": "def f():\n    # still one\n    return 1\n"},
                     "fix(test): stop the price test racing", "", *self.NO_RUNNER)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("every change is to tests, documentation or comments: nothing to check", result.stderr)

    def test_a_fix_that_adds_no_test_and_gives_no_reason_fails(self):
        repo = self.repo()
        result = fix(repo, {"src/price.ts": PRICE_FIX}, "fix(price): members get their discount", "", "--format", "json", *self.NO_RUNNER)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        [finding] = findings(result)
        self.assertIn("this one changes none", finding["message"])
        self.assertIn("`regression-test: none -- <reason>`", finding["message"])

    def test_a_test_whose_change_is_only_comments_is_not_a_test(self):
        repo = make_repo(self, {"src/price.ts": PRICE_BUG, "src/price.test.ts": DOES_NOT_REPRODUCE})
        result = fix(repo, {"src/price.ts": PRICE_FIX, "src/price.test.ts": "// covers the discount\n" + DOES_NOT_REPRODUCE},
                     "fix(price): members get their discount", "", *self.NO_RUNNER)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("this one changes none (", result.stdout)
        self.assertIn("a change to comments only does not count", result.stdout)

    def test_the_reason_line_passes_a_fix_and_prints_the_reason(self):
        body = "Fixes the seed.\n\n- regression-test: none -- the fix is a SQL seed no unit test can load\n"
        repo = self.repo()
        result = fix(repo, {"src/price.ts": PRICE_FIX}, "fix(seed): keep the rows", body, "--format", "json", *self.NO_RUNNER)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        [finding] = findings(result)
        self.assertTrue(finding["advisory"])
        self.assertEqual(finding["message"], "no regression test, as the pull request body declares: "
                                             "the fix is a SQL seed no unit test can load")
        # The grammar quoted in a code block, or without a reason, declares nothing.
        for body in ("```\nregression-test: none -- an example\n```\n", "regression-test: none\n"):
            repo = self.repo()
            self.assertEqual(fix(repo, {"src/price.ts": PRICE_FIX}, "fix: x", body, *self.NO_RUNNER).returncode, 1, body)

    def test_a_fix_whose_only_test_is_one_this_gate_cannot_run_cannot_be_judged(self):
        repo = self.repo()
        result = fix(repo, {"src/price.ts": PRICE_FIX, "e2e/price.spec.ts": PLAYWRIGHT_SPEC, "db/price.test.sql": "select 1;\n",
                            "scripts/price.test.sh": "[ \"$(node -p 1)\" = 1 ] || exit 1\n"},
                     "fix(price): members get their discount", "", "--soak", *self.NO_RUNNER)
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn("e2e/price.spec.ts: a Playwright test, which needs a browser and a running site", result.stderr)
        self.assertIn("db/price.test.sql: a .sql test; this gate runs Vitest, node:test, pytest and unittest", result.stderr)
        self.assertIn("scripts/price.test.sh: a .sh test; this gate runs Vitest, node:test, pytest and unittest", result.stderr)

    def test_a_jest_or_bun_test_is_not_judged_and_names_its_runner(self):
        # Every file calls describe and it, as Vitest with `globals: true` also reads them: the package decides. The
        # Python test is one this gate runs, so the verdict is its; a file taken for Vitest would stop the gate instead,
        # since no vitest is installed here.
        calls = 'describe("price", () => {\n  it("gives members 10% off", () => { expect(1).toBe(1); });\n});\n'
        repo = make_repo(self, {"shop/__init__.py": "", "shop/price.py": PY_BUG, "apps/tools/package.json": "{}\n",
                                "apps/api/package.json": '{"devDependencies": {"jest": "29.7.0"}}\n',
                                "apps/web/package.json": '{"scripts": {"test": "jest --ci"}}\n', "apps/web/jest.config.js": "module.exports = {};\n",
                                "apps/shop/package.json": '{"scripts": {"test": "bun test --coverage"}}\n'})
        result = fix(repo, {"shop/price.py": PY_FIX, "tests/test_price.py": PY_UNITTEST, "apps/api/src/price.test.ts": calls,
                            "apps/web/src/price.test.ts": calls, "apps/shop/src/price.test.ts": calls,
                            "apps/quote/price.test.ts": 'import { describe, expect, it } from "bun:test";\n' + calls,
                            "apps/tools/price.test.ts": 'import { describe, expect, it } from "@jest/globals";\n' + calls,
                            "scripts/price.test.js": calls},
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

    def test_a_test_whose_package_runs_vitest_is_a_vitest_test_without_importing_it(self):
        # A workspace package that names no runner runs its tests with the root's Vitest; a Vitest config says so too.
        calls = 'describe("price", () => {\n  it("gives members 10% off", () => { expect(1).toBe(1); });\n});\n'
        for files in ({"package.json": '{"devDependencies": {"vitest": "4.1.11"}}\n', "packages/shop/package.json": '{"name": "shop"}\n'},
                      {"packages/shop/vitest.config.ts": "export default {};\n", "package.json": '{"devDependencies": {"jest": "29.7.0"}}\n'}):
            repo = make_repo(self, dict({"packages/shop/src/price.ts": PRICE_BUG}, **files))
            result = fix(repo, {"packages/shop/src/price.ts": PRICE_FIX, "packages/shop/src/price.test.ts": calls}, "fix: x", "",
                         *self.NO_RUNNER)
            self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
            self.assertIn("no vitest is installed for packages/shop (for packages/shop/src/price.test.ts)", result.stderr)

    def test_a_fix_with_a_vitest_test_and_no_vitest_installed_cannot_run(self):
        repo = self.repo()
        result = fix(repo, {"src/price.ts": PRICE_FIX, "src/price.test.ts": REPRODUCES}, "fix: x", "", *self.NO_RUNNER)
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn("no vitest is installed for the repository root", result.stderr)

    def test_a_changed_file_that_differs_from_head_cannot_run(self):
        repo = self.repo()
        git(repo, "checkout", "-q", "-b", "change")
        commit(repo, {"src/price.ts": PRICE_FIX, "src/price.test.ts": REPRODUCES})
        write(repo, {"src/price.ts": PRICE_FIX + "// not committed\n"})
        inputs = Path(tempfile.mkdtemp(prefix="vv-regression-inputs-"))
        self.addCleanup(shutil.rmtree, inputs, True)
        write(inputs, {"title.txt": "fix: x\n", "body.md": ""})
        result = gate(GATE, repo, "--base-ref", "main", "--title", str(inputs / "title.txt"), "--body", str(inputs / "body.md"))
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn("the working tree differs from HEAD at a path the pull request changed (src/price.ts)", result.stderr)

    def test_a_missing_title_or_body_file_cannot_run(self):
        repo = self.repo()
        result = gate(GATE, repo, "--base-ref", "HEAD", "--title", "/nonexistent/title.txt", "--body", "/nonexistent/body.md", "--soak")
        self.assertEqual(result.returncode, 2)
        self.assertIn("--title cannot be read", result.stderr)


class RealVitest(unittest.TestCase):
    """The fixture's own Vitest 4, read through its JSON reporter."""

    @classmethod
    def setUpClass(cls):
        cls.deps = tempfile.mkdtemp(prefix="vv-regression-deps-")
        for name in ("package.json", "package-lock.json"):
            shutil.copy(FIXTURE / name, cls.deps)
        install_fixture(cls.deps)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.deps, ignore_errors=True)

    def project(self, base):
        repo = make_repo(self, dict(IGNORE, **{"package.json": (FIXTURE / "package.json").read_text()}, **base))
        os.symlink(os.path.join(self.deps, "node_modules"), os.path.join(repo, "node_modules"))
        return repo

    def test_a_test_that_fails_on_the_code_before_the_fix_passes(self):
        repo = self.project({"src/price.ts": PRICE_BUG})
        result = fix(repo, {"src/price.ts": PRICE_FIX, "src/price.test.ts": REPRODUCES})
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("src/price.test.ts: gives members 10% off over 100 fails on the code before the fix: "
                      "AssertionError: expected 200 to be 180", result.stderr)
        self.assertEqual(Path(repo, "src/price.ts").read_text(), PRICE_FIX)
        tree_is_head(self, repo)

    def test_a_test_that_passes_on_the_code_before_the_fix_does_not_reproduce_the_bug(self):
        repo = self.project({"src/price.ts": PRICE_BUG})
        result = fix(repo, {"src/price.ts": PRICE_FIX, "src/price.test.ts": DOES_NOT_REPRODUCE}, "fix(price): x", "", "--format", "json")
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        summary, per_file = findings(result)
        self.assertIn("no new or changed test fails on the code before the fix", summary["message"])
        self.assertEqual((per_file["path"], per_file["message"]),
                         ("src/price.test.ts", "its 1 test(s) all pass on the code before the fix, so it does not reproduce the bug"))
        tree_is_head(self, repo)

    def test_a_test_that_fails_there_only_because_what_it_imports_is_missing_does_not_show_the_bug(self):
        repo = self.project({"src/price.ts": PRICE_BUG})
        mask_test = 'import { expect, it } from "vitest";\nimport { mask } from "./mask";\nit("masks", () => { expect(mask("ab")).toBe("**"); });\n'
        export_test = ('import { expect, it } from "vitest";\nimport { roundUp } from "./price";\n'
                       'it("rounds up", () => { expect(roundUp(1.2)).toBe(2); });\n')
        result = fix(repo, {"src/mask.ts": 'export const mask = (s: string) => "*".repeat(s.length);\n', "src/mask.test.ts": mask_test,
                            "src/price.ts": PRICE_BUG + "export const roundUp = (n: number) => Math.ceil(n);\n",
                            "src/round.test.ts": export_test},
                     "fix(price): x", "", "--format", "json")
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        _, *per_file = findings(result)
        self.assertEqual([(f["path"], f["message"].split(" (")[0]) for f in per_file], [
            ("src/mask.test.ts", "fails on the code before the fix only because `./mask` is missing"),
            ("src/round.test.ts", "fails on the code before the fix only because `roundUp` is missing")])
        self.assertTrue(all(f["message"].endswith("which does not show the bug") for f in per_file))
        tree_is_head(self, repo)

    def test_a_test_that_fails_at_head_is_a_red_suite_and_cannot_run(self):
        repo = self.project({"src/price.ts": PRICE_BUG})
        red = REPRODUCES.replace("toBe(180)", "toBe(170)")
        result = fix(repo, {"src/price.ts": PRICE_FIX, "src/price.test.ts": red}, "fix(price): x", "", "--soak")
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn("1 changed test(s) fail at HEAD, so the pull request's suite is red", result.stderr)
        self.assertIn("src/price.test.ts: gives members 10% off over 100: AssertionError: expected 180 to be 170", result.stderr)
        tree_is_head(self, repo)

    def test_each_test_runs_from_its_own_package_and_a_file_the_fix_deleted_is_back_for_the_run_before_it(self):
        # A monorepo package whose Vitest config holds the alias its test imports through, so run from the
        # root the test would not load. The fix deletes the file the bug lived in; before the fix, the code
        # the test reaches re-exports from it.
        config = 'export default { resolve: { alias: { "@shop": new URL("./src", import.meta.url).pathname } } };\n'
        repo = self.project({"packages/shop/vitest.config.mjs": config, "packages/shop/package.json": '{"type": "module"}\n',
                             "packages/shop/src/legacy.ts": PRICE_BUG, "packages/shop/src/price.ts": 'export { discount } from "./legacy";\n'})
        git(repo, "checkout", "-q", "-b", "change")
        os.remove(os.path.join(repo, "packages/shop/src/legacy.ts"))
        inputs = Path(tempfile.mkdtemp(prefix="vv-regression-inputs-"))
        self.addCleanup(shutil.rmtree, inputs, True)
        write(inputs, {"title.txt": "fix(shop): members get their discount\n", "body.md": ""})
        commit(repo, {"packages/shop/src/price.ts": PRICE_FIX,
                      "packages/shop/src/price.test.ts": REPRODUCES.replace('"./price"', '"@shop/price"')})
        result = gate(GATE, repo, "--base-ref", "main", "--title", str(inputs / "title.txt"), "--body", str(inputs / "body.md"))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("packages/shop/src/price.test.ts: gives members 10% off over 100 fails on the code before the fix: "
                      "AssertionError: expected 200 to be 180", result.stderr)
        self.assertFalse(Path(repo, "packages/shop/src/legacy.ts").exists())
        tree_is_head(self, repo)


PY_BUG = "def discount(total, member):\n    return total\n"
PY_FIX = "def discount(total, member):\n    return total * 0.9 if member and total > 100 else total\n"
PY_UNITTEST = """import unittest

from shop.price import discount


class Discount(unittest.TestCase):
    def test_members_get_ten_percent_off_over_100(self):
        self.assertEqual(discount(200, True), 180)
"""
PY_PYTEST = "from shop.price import discount\n\n\ndef test_members_get_ten_percent_off_over_100():\n    assert discount(200, True) == 180\n"
PY_NON_MEMBER = "from shop.price import discount\n\n\ndef test_non_members_pay_full_price():\n    assert discount(200, False) == 200\n"
PY_MASK = "from shop.mask import mask\n\n\ndef test_masks():\n    assert mask('ab') == '**'\n"


class Python(unittest.TestCase):
    """unittest in a venv without pytest, and the fixture's pytest 9 in one with it."""

    @classmethod
    def setUpClass(cls):
        cls.envs = tempfile.mkdtemp(prefix="vv-regression-py-")
        cls.bare = os.path.join(cls.envs, "bare", "bin", "python")
        cls.pytest = os.path.join(cls.envs, "pytest", "bin", "python")
        subprocess.run([sys.executable, "-m", "venv", "--without-pip", os.path.join(cls.envs, "bare")], check=True, capture_output=True)
        subprocess.run([sys.executable, "-m", "venv", os.path.join(cls.envs, "pytest")], check=True, capture_output=True)
        subprocess.run([cls.pytest, "-m", "pip", "install", "--quiet", "--disable-pip-version-check", "--require-hashes",
                        "--only-binary", ":all:", "-r", str(PYTEST)], check=True, capture_output=True, text=True)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.envs, ignore_errors=True)

    def repo(self):
        return make_repo(self, {"shop/__init__.py": "", "shop/price.py": PY_BUG})

    def test_the_runner_reports_each_outcome_and_the_error_that_stopped_a_file_loading(self):
        # gates/_regression_python.py run as the gate runs it, under pytest and under unittest. A file named
        # with a hyphen is run by path, so unittest loads it under both.
        repo = self.repo()
        write(repo, {"tests/test_price.py": PY_PYTEST + "\n\n" + PY_NON_MEMBER.split("\n\n\n")[1], "tests/test_mask.py": PY_MASK,
                     "scripts/price-test.py": PY_UNITTEST})
        files = [os.path.realpath(os.path.join(repo, path)) for path in ("tests/test_price.py", "tests/test_mask.py", "scripts/price-test.py")]
        out = os.path.join(tempfile.mkdtemp(prefix="vv-regression-report-"), "report.json")
        self.addCleanup(shutil.rmtree, os.path.dirname(out), True)
        source = (ROOT / "gates" / "_regression_python.py").read_text()
        for python, price_tests in ((self.pytest, {"test_members_get_ten_percent_off_over_100": ("failed", "AssertionError: assert 200 == 180"),
                                                   "test_non_members_pay_full_price": ("passed", "")}),
                                    (self.bare, {})):  # unittest finds no TestCase in a pytest-style file
            subprocess.run([python, "-c", source, out, *files], cwd=repo, check=True, capture_output=True)
            with open(out) as handle:
                report = json.load(handle)["files"]
            price, mask, script = (report[path] for path in files)
            self.assertEqual({test_id.split(":")[-1]: (status, error) for test_id, status, error in price["tests"]}, price_tests, python)
            self.assertEqual((mask["load_error"], mask["tests"]), ("ModuleNotFoundError: No module named 'shop.mask'", []), python)
            [(test_id, status, error)] = script["tests"]
            self.assertEqual((test_id.split(".", 1)[1], status, error),
                             ("Discount.test_members_get_ten_percent_off_over_100", "failed", "AssertionError: 200 != 180"), python)

    def test_unittest_a_test_that_fails_on_the_code_before_the_fix_passes(self):
        repo = self.repo()
        result = fix(repo, {"shop/price.py": PY_FIX, "tests/test_price.py": PY_UNITTEST}, "fix: members", "", "--python", self.bare)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("tests/test_price.py: test_price.Discount.test_members_get_ten_percent_off_over_100 fails on the code before "
                      "the fix: AssertionError: 200 != 180", result.stderr)
        self.assertEqual(Path(repo, "shop/price.py").read_text(), PY_FIX)
        tree_is_head(self, repo)

    def test_unittest_a_test_whose_failing_check_is_in_a_subtest_fails_on_the_code_before_the_fix(self):
        # unittest reports a failed subtest under the subtest's own id and then nothing for its test (no
        # addSuccess, no addFailure), so the test that passed at HEAD must still read as failed there.
        cases = PY_UNITTEST.replace("        self.assertEqual(discount(200, True), 180)\n",
                                    "        for total, paid in ((50, 50), (200, 180)):\n"
                                    "            with self.subTest(total=total):\n"
                                    "                self.assertEqual(discount(total, True), paid)\n")
        for python, said in ((self.bare, "tests/test_price.py: test_price.Discount.test_members_get_ten_percent_off_over_100 fails "
                                         "on the code before the fix: AssertionError: 200 != 180"),
                             (self.pytest, "tests/test_price.py::Discount::test_members_get_ten_percent_off_over_100 fails on "
                                           "the code before the fix: AssertionError: 200 != 180")):
            with self.subTest(python=os.path.basename(os.path.dirname(os.path.dirname(python)))):
                repo = self.repo()
                result = fix(repo, {"shop/price.py": PY_FIX, "tests/test_price.py": cases}, "fix: members", "", "--python", python)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertIn(said, result.stderr)
                tree_is_head(self, repo)

    def test_unittest_a_test_run_by_path_that_fails_only_on_a_missing_module_does_not_show_the_bug(self):
        repo = self.repo()
        script = "import unittest\n\nfrom shop.mask import mask\n\n\nclass Mask(unittest.TestCase):\n    def test_masks(self):\n" \
                 "        self.assertEqual(mask('ab'), '**')\n\n\nif __name__ == '__main__':\n    unittest.main()\n"
        result = fix(repo, {"shop/mask.py": "def mask(s):\n    return '*' * len(s)\n", "scripts/mask-test.py": script},
                     "fix: mask", "", "--python", self.pytest, "--format", "json")
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertEqual(findings(result)[1]["message"], "fails on the code before the fix only because `shop.mask` is missing "
                                                         "(ModuleNotFoundError: No module named 'shop.mask'), which does not show the bug")
        tree_is_head(self, repo)

    def test_pytest_a_test_that_fails_on_the_code_before_the_fix_passes(self):
        repo = self.repo()
        result = fix(repo, {"shop/price.py": PY_FIX, "tests/test_price.py": PY_PYTEST}, "fix: members", "", "--python", self.pytest)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("tests/test_price.py::test_members_get_ten_percent_off_over_100 fails on the code before the fix: "
                      "AssertionError: assert 200 == 180", result.stderr)
        tree_is_head(self, repo)

    def test_pytest_a_test_that_passes_there_or_fails_only_on_a_missing_module_fails(self):
        repo = self.repo()
        result = fix(repo, {"shop/price.py": PY_FIX, "shop/mask.py": "def mask(s):\n    return '*' * len(s)\n",
                            "tests/test_price.py": PY_NON_MEMBER, "tests/test_mask.py": PY_MASK},
                     "fix: members", "", "--python", self.pytest, "--format", "json")
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        _, *per_file = findings(result)
        self.assertEqual([(f["path"], f["message"]) for f in per_file], [
            ("tests/test_mask.py", "fails on the code before the fix only because `shop.mask` is missing "
                                   "(ModuleNotFoundError: No module named 'shop.mask'), which does not show the bug"),
            ("tests/test_price.py", "its 1 test(s) all pass on the code before the fix, so it does not reproduce the bug")])
        self.assertTrue(Path(repo, "shop/mask.py").exists())
        tree_is_head(self, repo)


if __name__ == "__main__":
    unittest.main()
