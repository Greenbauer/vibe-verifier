"""regression-test-fails-on-base on Playwright specs (--playwright): the gate on synthetic repositories whose
Playwright config starts a small site on this machine (a node http server that renders src/price.mjs), run by
the catalog's pinned Playwright (tools/qae-browser, the build the QAE harness installs) and its chromium.

The fixture's `@playwright/test` is a stand-in. The published package is three re-exports of `playwright`
(index.js `module.exports = require('playwright/test')`, index.mjs the same as ES exports, and cli.js running
playwright's own program), and the catalog pins `playwright`, not `@playwright/test`. The stand-in holds those
same three files, so the gate finds `@playwright/test/cli.js` as it does in a subscriber, and every line that
runs a test is Playwright's.

The refusals, which need no browser, are tests/test_regression_playwright_refusals.py. `RealBrowser` needs the
pinned chromium; it
skips without one, except where VV_REGRESSION_BROWSER=required (the catalog's qae-browser CI job), where that is
a failure. The gate runs with a minimal environment (PATH, HOME, TMPDIR, LANG and the browsers path), never this
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
# The same site, whose server first fetches another host and records how that went: the Node side of a run is proxied too.
SERVER_FETCHES = SERVER.replace('appendFileSync("started.log", "started\\n");', """const outside = await fetch("https://rates.example.com/", { signal: AbortSignal.timeout(5000) })
  .then((response) => "reached " + response.status, () => "blocked");
appendFileSync("started.log", "outside: " + outside + "\\n");""")
# The same site, whose server first opens two connections no proxy setting reaches: http with its own Agent, and a raw
# socket to a database. Every Node process of a run is guarded, so both are refused and recorded.
SERVER_CONNECTS = SERVER.replace('appendFileSync("started.log", "started\\n");', """import http from "node:http";
import net from "node:net";
const agent = await new Promise((done) => http.get("http://rates.example.com/", { agent: new http.Agent() }, () => done("reached"))
  .on("error", () => done("blocked")));
const raw = await new Promise((done) => net.connect(5432, "db.example.com", () => done("reached")).on("error", () => done("blocked")));
appendFileSync("started.log", "agent: " + agent + ", raw: " + raw + "\\n");""")
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
STAND_IN = {  # @playwright/test as published: three re-exports of playwright
    "package.json": json.dumps({"name": "@playwright/test", "main": "index.js", "bin": {"playwright": "cli.js"},
                                "exports": {".": {"import": "./index.mjs", "require": "./index.js", "default": "./index.js"}}}),
    "index.js": "module.exports = require('playwright/test');\n",
    "index.mjs": "export * from 'playwright/test';\nexport { default } from 'playwright/test';\n",
    "cli.js": "#!/usr/bin/env node\nconst { program } = require('playwright/lib/program');\nprogram.parse(process.argv);\n",
}


def free_port():
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


class Fixture(unittest.TestCase):
    """A repository whose Playwright config starts the site above, with the pinned Playwright linked in."""

    browsers = None  # None: Playwright's own default; a path: PLAYWRIGHT_BROWSERS_PATH

    @classmethod
    def setUpClass(cls):
        cls.toolchain = Path(ensure_node_tool("qae-browser")) / "node_modules"

    def project(self, files=None, base=None):
        self.port = free_port()
        config = CONFIG % {"base": json.dumps(base or "http://127.0.0.1:%d" % self.port), "port": self.port}
        repo = make_repo(self, dict({".gitignore": "node_modules\nstarted.log\nglobal-setup.log\n", "package.json": '{"type": "module"}\n',
                                     "global-setup.mjs": GLOBAL_SETUP, "server.mjs": SERVER, "src/price.mjs": PRICE_BUG, "playwright.config.mjs": config},
                                    **(files or {})))
        stand_in = Path(repo, "node_modules", "@playwright", "test")
        stand_in.mkdir(parents=True)
        for name, text in STAND_IN.items():
            (stand_in / name).write_text(text)
        for name in ("playwright", "playwright-core"):
            os.symlink(self.toolchain / name, Path(repo, "node_modules", name))
        return repo

    def env(self, extra=None):
        env = {name: os.environ[name] for name in ("PATH", "HOME", "TMPDIR", "LANG") if name in os.environ}
        browsers = self.browsers or os.environ.get("PLAYWRIGHT_BROWSERS_PATH")
        return dict(env, **({"PLAYWRIGHT_BROWSERS_PATH": browsers} if browsers else {}), **(extra or {}))

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


def has_browser():
    """Whether the pinned Playwright's chromium is installed where this environment's Playwright looks."""
    toolchain = Path(ensure_node_tool("qae-browser")) / "node_modules" / "playwright"
    found = subprocess.run(["node", "-e", "process.stdout.write(require(%s).chromium.executablePath())" % json.dumps(str(toolchain))],
                           capture_output=True, text=True)
    return found.returncode == 0 and os.path.exists(found.stdout)


class RealBrowser(Fixture):
    """The specs run in the pinned chromium against the site the config starts, at the head and before the fix."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        if not has_browser():
            if os.environ.get("VV_REGRESSION_BROWSER") == "required":
                raise AssertionError("VV_REGRESSION_BROWSER=required, and the pinned Playwright's chromium is not installed")
            raise unittest.SkipTest("the pinned Playwright's chromium is not installed (actions/qae-browser installs it)")

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
        self.assertIn("e2e/rates.spec.mjs: reads the rates failed in a run that reached for rates.example.com:443, which this gate "
                      "blocks, so the failure cannot be told from the block: Error: page.goto: net::ERR_TUNNEL_CONNECTION_FAILED at "
                      "https://rates.example.com/today", result.stderr)
        tree_is_head(self, repo)

    def test_the_request_fixture_reaching_for_another_host_is_blocked_and_cannot_be_judged(self):
        repo = self.project()
        spec = SPEC + 'test("reads the rates", async ({ request }) => {\n' \
                      '  expect((await request.get("https://rates.example.com/today")).ok()).toBe(true);\n});\n'
        result = self.fix(repo, {"src/price.mjs": PRICE_FIX, "e2e/rates.spec.mjs": spec}, "--soak")
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertRegex(result.stderr, r"e2e/rates\.spec\.mjs: reads the rates failed in a run that reached for rates\.example\.com"
                                        r"(:443)?, which this gate blocks, so the failure cannot be told from the block")
        tree_is_head(self, repo)

    def test_the_site_s_own_server_reaching_for_another_host_is_blocked(self):
        # Node's fetch in the site's server goes through the same proxy (HTTPS_PROXY with NODE_USE_ENV_PROXY), in both runs.
        # At the head the spec passes all the same; before the fix it fails in a run that reached out: not the bug showing.
        repo = self.project({"server.mjs": SERVER_FETCHES})
        result = self.fix(repo, {"src/price.mjs": PRICE_FIX, "e2e/price.spec.mjs": REPRODUCES}, "--soak")
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertEqual(Path(repo, "started.log").read_text(), "outside: blocked\n" * 2)
        self.assertIn("e2e/price.spec.mjs: members get 10% off over 100 failed in a run that reached for rates.example.com:443",
                      result.stderr)
        tree_is_head(self, repo)

    def test_a_failure_before_the_fix_in_a_run_that_reached_out_is_not_the_bug_showing(self):
        # Before the fix the page's total comes from another host's script, which the run blocks; the spec fails there for
        # that alone, and no test's own error says so. The proxy recorded the request, so it is exit 2, not a pass.
        repo = self.project({"server.mjs": SERVER_SCRIPT})
        result = self.fix(repo, {"src/price.mjs": PRICE_FIX, "e2e/price.spec.mjs": REPRODUCES}, "--soak")
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn("e2e/price.spec.mjs: members get 10% off over 100 failed in a run that reached for cdn.example.com:443, "
                      "which this gate blocks, so the failure cannot be told from the block", result.stderr)
        tree_is_head(self, repo)


    def test_a_link_local_address_goes_to_the_proxy_too(self):
        # Chromium goes direct to 169.254/16 (where a cloud metadata service answers) unless told otherwise.
        repo = self.project()
        spec = SPEC + 'test("reads the metadata", async ({ page }) => {\n  await page.goto("http://169.254.1.1:8081/", { timeout: 3000 });\n' \
                      '  await expect(page.locator("body")).toHaveText("credentials", { timeout: 1000 });\n});\n'
        result = self.fix(repo, {"src/price.mjs": PRICE_FIX, "e2e/metadata.spec.mjs": spec}, "--soak")
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn("e2e/metadata.spec.mjs: reads the metadata failed in a run that reached for 169.254.1.1:8081", result.stderr)
        tree_is_head(self, repo)


    def test_a_node_connection_of_any_kind_to_another_host_is_refused_and_recorded(self):
        repo = self.project({"server.mjs": SERVER_CONNECTS})
        result = self.fix(repo, {"src/price.mjs": PRICE_FIX, "e2e/price.spec.mjs": REPRODUCES}, "--soak")
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertEqual(Path(repo, "started.log").read_text(), "agent: blocked, raw: blocked\n" * 2)
        self.assertIn("failed in a run that reached for db.example.com:5432, rates.example.com:80, which this gate blocks",
                      result.stderr)
        tree_is_head(self, repo)


    def test_a_webserver_env_that_sets_node_options_keeps_the_guard(self):
        repo = self.project({"server.mjs": SERVER_CONNECTS})
        config = Path(repo, "playwright.config.mjs")
        config.write_text(config.read_text().replace('env: { PORT:', 'env: { NODE_OPTIONS: "--max-old-space-size=512", PORT:'))
        subprocess.run(["git", "-C", repo, "-c", "user.name=Test", "-c", "user.email=test@example.com", "-c", "commit.gpgsign=false",
                        "commit", "-qam", "options"], check=True)
        result = self.fix(repo, {"src/price.mjs": PRICE_FIX, "e2e/price.spec.mjs": REPRODUCES}, "--soak")
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertEqual(Path(repo, "started.log").read_text(), "agent: blocked, raw: blocked\n" * 2)
        self.assertIn("failed in a run that reached for db.example.com:5432, rates.example.com:80", result.stderr)
        tree_is_head(self, repo)

    def test_a_site_on_ipv6_loopback_is_this_machine(self):
        repo = self.project({"server.mjs": SERVER.replace('.listen(Number(process.env.PORT), "127.0.0.1")', '.listen(Number(process.env.PORT), "::1")')},
                            base="http://[::1]:%d")
        config = Path(repo, "playwright.config.mjs")
        config.write_text(config.read_text().replace('url: "http://127.0.0.1:', 'url: "http://[::1]:').replace("[::1]:%d", "[::1]:" + str(self.port)))
        subprocess.run(["git", "-C", repo, "-c", "user.name=Test", "-c", "user.email=test@example.com", "-c", "commit.gpgsign=false",
                        "commit", "-qam", "ipv6"], check=True)
        result = self.fix(repo, {"src/price.mjs": PRICE_FIX, "e2e/price.spec.mjs": REPRODUCES})
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("e2e/price.spec.mjs: members get 10% off over 100 fails on the code before the fix", result.stderr)
        tree_is_head(self, repo)


if __name__ == "__main__":
    unittest.main()
