"""regression-test-fails-on-base on a node:test file that takes Playwright's types for its helpers
(`import type { Page } from "@playwright/test"`): judged by node:test, while a Playwright spec, which loads Playwright's
`test` or `expect`, still goes to --playwright. And gates/_js_imports.py's reading of which imports load a module when
the file runs. Synthetic repositories under the Node on PATH."""
import subprocess
import sys
import unittest
from types import SimpleNamespace

from helpers import ROOT, make_repo
from test_regression_node import HEADER, PRICE_BUG, PRICE_FIX, REPRODUCES, package
from test_regression_test import fix, tree_is_head

sys.path.insert(0, str(ROOT / "gates"))
import _js_imports  # noqa: E402  (gates/ is not a package)
import _regression_frameworks  # noqa: E402

SCRIPTS = {"scripts": {"test:e2e:helpers": "node --test 'e2e/**/*.test.*'"}}
# A helper's types, as a plain JS file names them, and a test that reads source text naming the module.
JS_TYPES = REPRODUCES.replace('import { discount } from "./price.mjs";',
                              'import { discount } from "../src/price.mjs";\n'
                              '/** @param {import("@playwright/test").Page} page */\nexport const ready = (page) => page;\n'
                              'const spec = \'import { test } from "@playwright/test";\';')
TS_TYPES = 'import type { Page, Route } from "@playwright/test";\n' + JS_TYPES.replace(
    '/** @param {import("@playwright/test").Page} page */\nexport const ready = (page) => page;',
    "export const ready = (page: Page, route?: Route): Page => page;")
SPEC = 'import { expect, test } from "@playwright/test";\n' \
       'test("gives members 10% off over 100", async ({ page }) => { await page.goto("/"); expect(1).toBe(1); });\n'


def strips_types():
    """Whether the Node on PATH runs a .ts file with --experimental-strip-types (Node 22.6 and later)."""
    return subprocess.run(["node", "--experimental-strip-types", "-e", "1"], capture_output=True).returncode == 0


class NodeTestWithPlaywrightTypes(unittest.TestCase):

    def project(self, scripts=SCRIPTS):
        return make_repo(self, {"package.json": package(scripts), "src/price.mjs": PRICE_BUG})

    def test_a_node_test_file_that_only_names_playwright_types_is_judged_by_node_test(self):
        repo = self.project()
        result = fix(repo, {"src/price.mjs": PRICE_FIX, "e2e/price.test.mjs": JS_TYPES})
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("e2e/price.test.mjs: gives members 10% off over 100 fails on the code before the fix", result.stderr)
        tree_is_head(self, repo)

    @unittest.skipUnless(strips_types(), "the Node on PATH cannot run TypeScript (Node 22.6 and later can)")
    def test_a_typescript_node_test_file_with_a_type_only_playwright_import_is_judged_by_node_test(self):
        repo = self.project({"scripts": {"test:e2e:helpers": "node --experimental-strip-types --test 'e2e/**/*.test.ts'"}})
        result = fix(repo, {"src/price.mjs": PRICE_FIX, "e2e/price.test.ts": TS_TYPES})
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("e2e/price.test.ts: gives members 10% off over 100 fails on the code before the fix", result.stderr)
        tree_is_head(self, repo)

    def test_a_playwright_spec_the_node_script_also_selects_still_needs_playwright(self):
        # It loads Playwright's test, so it is a spec, though `node --test 'e2e/**/*.test.*'` selects it too.
        repo = self.project()
        result = fix(repo, {"src/price.mjs": PRICE_FIX, "e2e/price.test.ts": SPEC}, "fix: x", "", "--soak")
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn("e2e/price.test.ts: not judged: a Playwright test, which needs a browser and a running site", result.stderr)

    def test_which_files_are_node_tests_and_which_are_playwright_specs(self):
        files = {"e2e/types.test.ts": TS_TYPES, "e2e/spec.test.ts": SPEC,
                 # node:test that drives a browser with Playwright's library, and a spec taking test from a fixtures module.
                 "e2e/library.test.ts": HEADER + 'import { chromium } from "@playwright/test";\n',
                 "e2e/fixtures.test.ts": 'import type { Page } from "@playwright/test";\nimport { test } from "./fixtures";\n',
                 "tools/types.test.ts": TS_TYPES}
        repo = make_repo(self, dict({"package.json": package(SCRIPTS)}, **files))
        args = SimpleNamespace(repo=repo, node_test=[], tests=[], playwright=[])
        kinds = {path: _regression_frameworks.kind(args, path, ({}, {}))[0] for path in files}
        self.assertEqual(kinds, {"e2e/types.test.ts": "node", "e2e/spec.test.ts": None, "e2e/library.test.ts": None,
                                 "e2e/fixtures.test.ts": None, "tools/types.test.ts": None})
        self.assertIn("a Playwright test", _regression_frameworks.kind(args, "tools/types.test.ts", ({}, {}))[1])
        # Declared with --node-test, the file outside the script's glob is a node:test file too.
        args.node_test = ["tools/**"]
        self.assertEqual(_regression_frameworks.kind(args, "tools/types.test.ts", ({}, {}))[0], "node")


class RuntimeImports(unittest.TestCase):
    """Which imports load a module when the file runs, in the shapes test files write them."""

    def loads(self, text):
        return _js_imports.imports_at_runtime(text, "@playwright/test")

    def test_an_import_that_binds_a_value_loads_the_module(self):
        for text in ('import { test, expect } from "@playwright/test";',
                     "import { type Page, expect } from '@playwright/test'",
                     'import {\n  Page,\n  expect as check,\n} from "@playwright/test";',
                     'import test from "@playwright/test";', 'import test, { type Page } from "@playwright/test";',
                     'import * as pw from "@playwright/test";', 'import "@playwright/test";', 'import {} from "@playwright/test";',
                     'export * from "@playwright/test";', 'export { expect } from "@playwright/test";',
                     'const { test } = require("@playwright/test");', 'const pw = await import("@playwright/test");',
                     'const t = 2 / 1; import { test } from "@playwright/test";'):
            self.assertTrue(self.loads(text), text)

    def test_an_import_that_names_only_types_or_a_mention_outside_code_does_not(self):
        for text in ('import type { Page } from "@playwright/test";', 'import type { Page, Route } from "@playwright/test"',
                     'import { type Page, type Route } from "@playwright/test";', 'import { Page, Locator } from "@playwright/test";',
                     'import type * as pw from "@playwright/test";', 'export type { Page } from "@playwright/test";',
                     'let page: import("@playwright/test").Page;', 'type T = typeof import("@playwright/test");',
                     '/** @param {import("@playwright/test").Page} page */', '// import { test } from "@playwright/test";',
                     '/* import { test } from "@playwright/test"; */', 'const deps = pkg.dependencies?.["@playwright/test"];',
                     "const source = 'import { test } from \"@playwright/test\";';",
                     'const source = `import { test } from "@playwright/test"; ${name}`;',
                     'const found = /from ["\']@playwright\\/test["\']/.test(text);',
                     'import type { TestCase } from "@playwright/test/reporter";', 'import { test } from "./fixtures";'):
            self.assertFalse(self.loads(text), text)
