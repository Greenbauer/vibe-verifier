"""regression-test-fails-on-base on Playwright specs (--playwright): the gate on synthetic repositories whose
Playwright config starts a small site (a node http server that renders src/price.mjs), run in the gate's sealed
Docker network by a real Playwright: tools/regression-browser pins @playwright/test to a release whose official image
(mcr.microsoft.com/playwright:v<version>-noble) holds its browsers, and each repository gets a copy of it in its
node_modules, as a subscriber's install would put it there.

The refusals made before anything runs, which need no Docker, and those made from the sealed listing are
tests/test_regression_playwright_refusals.py; the seal itself, both ways (each way out the review rounds found is
refused, and the job's stack is reachable), is tests/test_regression_playwright_network.py. `Browser`'s tests need
Docker and pull Playwright's image, so they run only where VV_REGRESSION_SEALED is set: 1 runs them when Docker
answers and skips them when not; `required` (the catalog's regression-browser CI job) makes no Docker a failure.
The gate runs with a minimal environment (PATH, HOME, TMPDIR, LANG and Docker's own settings), never this
machine's own, whose tokens it would rightly refuse."""
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from helpers import ROOT, make_repo
from test_regression_test import findings, tree_is_head

sys.path.insert(0, str(ROOT / "gates"))
from _tools import ensure_node_tool  # noqa: E402  (gates/ is not a package)

PRICE_BUG = "export function discount(total, member) {\n  return total;\n}\n"
PRICE_FIX = "export function discount(total, member) {\n  return member && total > 100 ? total * 0.9 : total;\n}\n"
# The site: what src/price.mjs says a member pays on 200. Each start is recorded, so a test can show none happened.
SERVER = """import { appendFileSync } from "node:fs";
import { createServer } from "node:http";
import { discount } from "./src/price.mjs";
appendFileSync("started.log", "started\\n");
createServer((request, response) => {
  response.setHeader("content-type", "text/html");
  response.end(`<h1 id="total">${discount(200, true)}</h1>`);
}).listen(Number(process.env.PORT), "127.0.0.1");
"""
# The same site, whose server first fetches another host and records how that went: the network refuses it.
SERVER_FETCHES = SERVER.replace('appendFileSync("started.log", "started\\n");', """const outside = await fetch("https://rates.example.com/", { signal: AbortSignal.timeout(5000) })
  .then((response) => "reached " + response.status, () => "blocked");
appendFileSync("started.log", "outside: " + outside + "\\n");""")
# Before the fix the page's total comes from another host's script, which the run blocks; the fix renders it itself.
SERVER_SCRIPT = SERVER.replace("response.end(`<h1 id=\"total\">${discount(200, true)}</h1>`);",
                               "response.end(discount(200, true) === 200 ? '<h1 id=\"total\"></h1><script src=\"https://cdn.example.com/total.js\">'\n"
                               "    + '</script>' : `<h1 id=\"total\">${discount(200, true)}</h1>`);")
CONFIG = """import { defineConfig } from "@playwright/test";
export default defineConfig({
  testDir: "e2e",
  use: { baseURL: %(base)s },
  webServer: { command: "node server.mjs", url: "http://127.0.0.1:%(port)d", env: { PORT: "%(port)d" } },
  globalSetup: "./global-setup.mjs",
});
"""
GLOBAL_SETUP = 'import { appendFileSync } from "node:fs";\nexport default () => appendFileSync("global-setup.log", "ran\\n");\n'
SPEC = 'import { test, expect } from "@playwright/test";\n'
REPRODUCES = SPEC + 'test("members get 10% off over 100", async ({ page }) => {\n  await page.goto("/");\n' \
                    '  await expect(page.locator("#total")).toHaveText("180", { timeout: 3000 });\n});\n'
DOES_NOT_REPRODUCE = SPEC + 'test("the total shows", async ({ page }) => {\n  await page.goto("/");\n' \
                            '  await expect(page.locator("#total")).toBeVisible();\n});\n'
PACKAGES = (("@playwright", "test"), ("playwright",), ("playwright-core",))


def free_port():
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


class Fixture(unittest.TestCase):
    """A repository whose Playwright config starts the site above, with the pinned Playwright copied in."""

    @classmethod
    def setUpClass(cls):
        cls.toolchain = Path(ensure_node_tool("regression-browser")) / "node_modules"

    def project(self, files=None, base=None):
        self.port = free_port()
        config = CONFIG % {"base": json.dumps(base or "http://127.0.0.1:%d" % self.port), "port": self.port}
        repo = make_repo(self, dict({".gitignore": "node_modules\nstarted.log\nglobal-setup.log\n", "package.json": '{"type": "module"}\n',
                                     "global-setup.mjs": GLOBAL_SETUP, "server.mjs": SERVER, "src/price.mjs": PRICE_BUG, "playwright.config.mjs": config},
                                    **(files or {})))
        for parts in PACKAGES:  # copied, not linked: the sealed run mounts the repository and nothing outside it
            shutil.copytree(self.toolchain.joinpath(*parts), Path(repo, "node_modules", *parts), symlinks=True)
        return repo

    def env(self, extra=None):
        return dict({name: value for name, value in os.environ.items()
                     if name in ("PATH", "HOME", "TMPDIR", "LANG") or name.startswith("DOCKER_")}, **(extra or {}))

    def fix(self, repo, files, *args, env=None):
        """Commit `files` on a branch and run the gate against main with the fixture's config and a minimal env."""
        git = ["git", "-C", repo, "-c", "user.name=Test", "-c", "user.email=test@example.com", "-c", "commit.gpgsign=false"]
        subprocess.run(git + ["checkout", "-q", "-b", "change"], check=True)
        for relative, text in files.items():
            Path(repo, relative).parent.mkdir(parents=True, exist_ok=True)
            Path(repo, relative).write_text(text)
        subprocess.run(git + ["add", "-A"], check=True)
        subprocess.run(git + ["commit", "-q", "-m", "change"], check=True)
        inputs = Path(tempfile.mkdtemp(prefix="vv-regression-inputs-"))
        self.addCleanup(shutil.rmtree, inputs, True)
        (inputs / "title.txt").write_text("fix(price): members get their discount\n")
        (inputs / "body.md").write_text("")
        return subprocess.run([sys.executable, str(ROOT / "gates" / "regression_test_fails_on_base.py"), "--repo", repo,
                               "--base-ref", "main", "--title", str(inputs / "title.txt"), "--body", str(inputs / "body.md"),
                               "--playwright", "playwright.config.mjs", *args],
                              capture_output=True, text=True, env=env or self.env())


def has_docker():
    try:
        return subprocess.run(["docker", "version"], capture_output=True, timeout=30).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


class Browser(Fixture):
    """Runs the gate's sealed Docker runs: only where VV_REGRESSION_SEALED is set, and a failure without Docker where
    it says `required`."""

    @classmethod
    def setUpClass(cls):
        wanted = os.environ.get("VV_REGRESSION_SEALED")
        if not wanted:
            raise unittest.SkipTest("the sealed runs need Docker and pull Playwright's image: set VV_REGRESSION_SEALED=1")
        if not has_docker():
            if wanted == "required":
                raise AssertionError("VV_REGRESSION_SEALED=required, and no Docker daemon answers")
            raise unittest.SkipTest("no Docker daemon answers")
        super().setUpClass()


class RealBrowser(Browser):
    """The specs run in Playwright's image against the site the config starts, at the head and before the fix."""

    def test_a_browser_spec_that_fails_on_the_code_before_the_fix_passes(self):
        repo = self.project()
        result = self.fix(repo, {"src/price.mjs": PRICE_FIX, "e2e/price.spec.mjs": REPRODUCES})
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('e2e/price.spec.mjs: members get 10% off over 100 fails on the code before the fix: Error: '
                      'expect(locator).toHaveText(expected) failed Expected: "180" Received: "200"', result.stderr)
        self.assertEqual(Path(repo, "started.log").read_text(), "started\n" * 2)  # the head's site, then the one before the fix
        self.assertEqual(Path(repo, "global-setup.log").read_text(), "ran\n" * 2)
        self.assertEqual(Path(repo, "src/price.mjs").read_text(), PRICE_FIX)
        tree_is_head(self, repo)

    def test_a_browser_spec_that_passes_on_the_code_before_the_fix_does_not_reproduce_the_bug(self):
        repo = self.project()
        result = self.fix(repo, {"src/price.mjs": PRICE_FIX, "e2e/price.spec.mjs": DOES_NOT_REPRODUCE}, "--format", "json")
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        summary, per_file = findings(result)
        self.assertIn("no new or changed test fails on the code before the fix", summary["message"])
        self.assertEqual((per_file["path"], per_file["message"]),
                         ("e2e/price.spec.mjs", "its 1 test(s) all pass on the code before the fix, so it does not reproduce the bug"))
        tree_is_head(self, repo)

    def test_a_browser_spec_that_fails_there_only_because_what_it_imports_is_missing_does_not_show_the_bug(self):
        repo = self.project()
        spec = SPEC + 'import { mask } from "../src/mask.mjs";\ntest("masks", async () => { expect(mask("ab")).toBe("**"); });\n'
        result = self.fix(repo, {"src/mask.mjs": 'export const mask = (s) => "*".repeat(s.length);\n', "e2e/mask.spec.mjs": spec},
                          "--format", "json")
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        _, per_file = findings(result)
        self.assertEqual((per_file["path"], per_file["message"].split(" (")[0]),
                         ("e2e/mask.spec.mjs", "fails on the code before the fix only because `src/mask.mjs` is missing"))
        tree_is_head(self, repo)

    def test_a_spec_that_reaches_for_another_host_is_blocked_and_cannot_be_judged(self):
        repo = self.project()
        spec = SPEC + 'test("reads the rates", async ({ page }) => {\n  await page.goto("https://rates.example.com/today");\n});\n'
        result = self.fix(repo, {"src/price.mjs": PRICE_FIX, "e2e/rates.spec.mjs": spec}, "--soak")
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn("e2e/rates.spec.mjs: reads the rates failed in a run whose sealed network refused rates.example.com, so "
                      "the failure cannot be told from the refusal: Error: page.goto: net::ERR_NAME_NOT_RESOLVED at "
                      "https://rates.example.com/today", result.stderr)
        tree_is_head(self, repo)

    def test_the_request_fixture_reaching_for_another_host_is_blocked_and_cannot_be_judged(self):
        repo = self.project()
        spec = SPEC + 'test("reads the rates", async ({ request }) => {\n' \
                      '  expect((await request.get("https://rates.example.com/today")).ok()).toBe(true);\n});\n'
        result = self.fix(repo, {"src/price.mjs": PRICE_FIX, "e2e/rates.spec.mjs": spec}, "--soak")
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn("e2e/rates.spec.mjs: reads the rates failed in a run whose sealed network refused rates.example.com, so "
                      "the failure cannot be told from the refusal", result.stderr)
        tree_is_head(self, repo)

    def test_the_site_s_own_server_reaching_for_another_host_is_blocked(self):
        # The network refuses the site's server-side fetch in both runs. At the head the spec passes all the same; before
        # the fix it fails in a run whose network refused something: not the bug showing.
        repo = self.project({"server.mjs": SERVER_FETCHES})
        result = self.fix(repo, {"src/price.mjs": PRICE_FIX, "e2e/price.spec.mjs": REPRODUCES}, "--soak")
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertEqual(Path(repo, "started.log").read_text(), "outside: blocked\n" * 2)
        self.assertIn("e2e/price.spec.mjs: members get 10% off over 100 failed in a run whose sealed network refused "
                      "rates.example.com", result.stderr)
        tree_is_head(self, repo)

    def test_a_failure_before_the_fix_in_a_run_that_reached_out_is_not_the_bug_showing(self):
        # Before the fix the page's total comes from another host's script, which the network refuses; the spec fails there
        # for that alone, and no test's own error says so. The name was recorded, so it is exit 2, not a pass.
        repo = self.project({"server.mjs": SERVER_SCRIPT})
        result = self.fix(repo, {"src/price.mjs": PRICE_FIX, "e2e/price.spec.mjs": REPRODUCES}, "--soak")
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn("e2e/price.spec.mjs: members get 10% off over 100 failed in a run whose sealed network refused "
                      "cdn.example.com, so the failure cannot be told from the refusal", result.stderr)
        tree_is_head(self, repo)


if __name__ == "__main__":
    unittest.main()
