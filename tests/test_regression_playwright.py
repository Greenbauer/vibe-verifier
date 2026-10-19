"""regression-test-fails-on-base on Playwright specs (--playwright): the gate on synthetic repositories whose
Playwright config starts a small site on this machine (a node http server that renders src/price.mjs), run by
the catalog's pinned Playwright (tools/qae-browser, the build the QAE harness installs) and its chromium.

The fixture's `@playwright/test` is a stand-in. The published package is three re-exports of `playwright`
(index.js `module.exports = require('playwright/test')`, index.mjs the same as ES exports, and cli.js running
playwright's own program), and the catalog pins `playwright`, not `@playwright/test`. The stand-in holds those
same three files, so the gate finds `@playwright/test/cli.js` as it does in a subscriber, and every line that
runs a test is Playwright's.

`Refusals` needs Playwright's package but no browser: PLAYWRIGHT_BROWSERS_PATH names an empty directory, so a run
that got as far as a browser would fail on a missing one instead. `RealBrowser` needs the pinned chromium; it
skips without one, except where VV_REGRESSION_BROWSER=required (the catalog's qae-browser CI job), where that is
a failure. The gate runs with a minimal environment (PATH, HOME, TMPDIR, LANG and the browsers path), never this
machine's own, whose tokens it would rightly refuse."""
import http.server
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path

from helpers import ROOT, make_repo
from test_local_only import HOSTED, jwt
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
CONFIG = """import { defineConfig } from "@playwright/test";
export default defineConfig({
  testDir: "e2e",
  use: { baseURL: %(base)s },
  webServer: { command: "node server.mjs", url: "http://127.0.0.1:%(port)d", env: { PORT: "%(port)d" } },
});
"""
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
        repo = make_repo(self, dict({".gitignore": "node_modules\nstarted.log\n", "package.json": '{"type": "module"}\n',
                                     "server.mjs": SERVER, "src/price.mjs": PRICE_BUG, "playwright.config.mjs": config},
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


class Refusals(Fixture):
    """Stopped before any of the pull request's code starts: no server, no global setup, no browser."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.browsers = tempfile.mkdtemp(prefix="vv-no-browsers-")

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.browsers, ignore_errors=True)

    def refused(self, repo, said, env=None):
        result = self.fix(repo, {"src/price.mjs": PRICE_FIX, "e2e/price.spec.mjs": REPRODUCES}, "--soak", env=env)
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn(said, result.stderr)
        self.assertFalse(Path(repo, "started.log").exists(), "the site started")
        tree_is_head(self, repo)
        return result

    def test_a_target_that_is_not_this_machine_is_refused_before_anything_starts(self):
        for base in ("https://app.example.com", "https://pr-7.preview.example.com"):
            with self.subTest(base=base):
                result = self.refused(self.project(base=base), "--playwright playwright.config.mjs: its project targets %s, "
                                                               "which is not this machine" % base)
                self.assertIn("never production, and never a preview, which serves the pull request's head", result.stderr)

    def test_a_config_that_starts_no_site_here_is_refused(self):
        repo = self.project({"playwright.config.mjs": "import { defineConfig } from \"@playwright/test\";\n"
                                                      "export default defineConfig({ testDir: \"e2e\", use: { baseURL: "
                                                      "\"http://127.0.0.1:4321\" } });\n"})
        self.refused(repo, "it starts no webServer, so nothing serves the code under test from this working tree")

    def test_a_production_credential_in_what_the_run_inherits_is_refused_before_playwright_starts(self):
        secrets = {"SUPABASE_SERVICE_ROLE_KEY": jwt({"iss": "supabase", "ref": HOSTED, "role": "service_role"}),
                   "DATABASE_URL": "postgresql://postgres:hunter2@db.%s.supabase.co:5432/postgres" % HOSTED,
                   "NEXT_PUBLIC_SUPABASE_URL": "https://%s.supabase.co" % HOSTED, "RESEND_API_KEY": "re_live_value"}
        repo = self.project({".env.production": "NETSUITE_TOKEN_SECRET=ns-live-value\n"})
        result = self.refused(repo, "the environment: SUPABASE_SERVICE_ROLE_KEY holds a token issued by supabase for project "
                                    + HOSTED, env=self.env(secrets))
        for said in ("the environment: DATABASE_URL holds a connection string for db.%s.supabase.co, which is not this machine" % HOSTED,
                     "the environment: NEXT_PUBLIC_SUPABASE_URL holds the address of a hosted Supabase project (%s.supabase.co)" % HOSTED,
                     "the environment: RESEND_API_KEY is named as a credential, and the manifest does not declare it",
                     "/.env.production: NETSUITE_TOKEN_SECRET is named as a credential"):
            self.assertIn(said, result.stderr)
        for value in list(secrets.values()) + ["hunter2", "ns-live-value"]:
            self.assertNotIn(value, result.stdout + result.stderr)

    def test_a_site_whose_local_stack_is_not_running_cannot_be_judged(self):
        # The site needs the job's local database; none is running, so it exits before it serves.
        server = SERVER.replace('appendFileSync("started.log", "started\\n");',
                                'console.error("the local database at 127.0.0.1:54322 is not running"); process.exit(1);')
        repo = self.project({"server.mjs": server})
        result = self.fix(repo, {"src/price.mjs": PRICE_FIX, "e2e/price.spec.mjs": REPRODUCES}, "--soak")
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn("the Playwright run of playwright.config.mjs stopped before its tests could show anything: "
                      "Error: Process from config.webServer was not able to start. Exit code: 1", result.stderr)
        tree_is_head(self, repo)

    def test_a_server_already_answering_at_the_site_s_address_cannot_be_judged(self):
        # It would serve whatever code it was started with, so Playwright must not reuse it.
        repo = self.project()
        squatter = http.server.ThreadingHTTPServer(("127.0.0.1", self.port), Answers)
        threading.Thread(target=squatter.serve_forever, daemon=True).start()
        try:
            result = self.fix(repo, {"src/price.mjs": PRICE_FIX, "e2e/price.spec.mjs": REPRODUCES}, "--soak")
        finally:
            squatter.shutdown()
            squatter.server_close()
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn("the Playwright run of playwright.config.mjs stopped before its tests could show anything: "
                      "Error: http://127.0.0.1:%d is already used" % self.port, result.stderr)
        tree_is_head(self, repo)

    def test_no_playwright_installed_cannot_be_judged(self):
        repo = self.project()
        shutil.rmtree(Path(repo, "node_modules"))
        result = self.fix(repo, {"src/price.mjs": PRICE_FIX, "e2e/price.spec.mjs": REPRODUCES}, "--soak")
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn("no Playwright is installed for playwright.config.mjs", result.stderr)


class Answers(http.server.BaseHTTPRequestHandler):
    """A server already running at the site's address: it answers every request, and logs nothing."""

    def do_GET(self):
        self.send_response(200)
        self.end_headers()

    def log_message(self, *_):
        pass


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
        self.assertIn("e2e/rates.spec.mjs: reads the rates reached for a host that is not this machine, which this gate blocks: "
                      "Error: page.goto: net::ERR_PROXY_CONNECTION_FAILED at https://rates.example.com/today", result.stderr)
        tree_is_head(self, repo)


if __name__ == "__main__":
    unittest.main()
