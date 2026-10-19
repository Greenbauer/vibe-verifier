"""regression-test-fails-on-base --playwright stopping before any of the pull request's code starts a server or a
browser, and the wrapper loading each shape of config: the fixture of tests/test_regression_playwright.py with no
browser at all (PLAYWRIGHT_BROWSERS_PATH names an empty directory, so a run that got as far as one would fail on a
missing browser instead). Needs only the pinned Playwright's package, so it runs in the catalog's test job."""
import http.server
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from helpers import ROOT
from test_local_only import HOSTED, jwt
from test_regression_node import REPRODUCES as NODE_REPRODUCES
from test_regression_playwright import PRICE_FIX, REPRODUCES, SERVER, Fixture
from test_regression_test import tree_is_head

sys.path.insert(0, str(ROOT / "gates"))
import _regression_playwright  # noqa: E402  (gates/ is not a package)


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
        self.assertFalse(Path(repo, "global-setup.log").exists(), "the global setup ran")
        tree_is_head(self, repo)
        return result

    def test_a_target_that_is_not_this_machine_is_refused_before_anything_starts(self):
        for base in ("https://app.example.com", "https://pr-7.preview.example.com"):
            with self.subTest(base=base):
                result = self.refused(self.project(base=base), "--playwright playwright.config.mjs: its project targets %s, "
                                                               "which is not this machine" % base)
                self.assertIn("never production, and never a preview, which serves the pull request's head", result.stderr)

    def test_a_webserver_or_a_setup_project_pointed_elsewhere_is_refused(self):
        # The webServer's own url and env, and a project the spec's project depends on, are checked like the spec's own.
        head = 'import { defineConfig } from "@playwright/test";\nconst site = "http://127.0.0.1:%d";\n'
        server = 'webServer: { command: "node server.mjs", url: %s, env: { PORT: "%d"%s } }'
        for name, body, said in (
                ("url", 'export default defineConfig({ testDir: "e2e", use: { baseURL: site }, %s });\n'
                 % (server % ('"https://app.example.com"', 0, "")), "webServer 1 waits for https://app.example.com, which is not this machine"),
                ("env", 'export default defineConfig({ testDir: "e2e", use: { baseURL: site }, %s });\n'
                 % (server % ("site", 0, ", SUPABASE_SERVICE_ROLE_KEY: %s.join('.')"  # in parts, as a secret scanner would stop
                              % json.dumps(jwt({"iss": "supabase", "ref": HOSTED}).split(".")))),  # a commit holding the token
                 "webServer 1's env: SUPABASE_SERVICE_ROLE_KEY holds a token issued by supabase for project " + HOSTED),
                ("setup", 'export default defineConfig({ testDir: "e2e", %s, projects: [\n'
                 '  { name: "setup", testMatch: /setup\\.mjs/, use: { baseURL: "https://app.example.com" } },\n'
                 '  { name: "e2e", dependencies: ["setup"], use: { baseURL: site } }] });\n' % (server % ("site", 0, "")),
                 "project setup targets https://app.example.com, which is not this machine")):
            with self.subTest(config=name):
                repo = self.project()
                port = int(Path(repo, "playwright.config.mjs").read_text().split("127.0.0.1:")[1].split('"')[0])
                config = (head % port + body).replace('PORT: "0"', 'PORT: "%d"' % port)
                Path(repo, "playwright.config.mjs").write_text(config)
                Path(repo, "e2e").mkdir(exist_ok=True)
                Path(repo, "e2e", "setup.mjs").write_text('import { test } from "@playwright/test";\ntest("signs in", async () => {});\n')
                subprocess.run(["git", "-C", repo, "-c", "user.name=Test", "-c", "user.email=test@example.com", "add", "-A"], check=True)
                subprocess.run(["git", "-C", repo, "-c", "user.name=Test", "-c", "user.email=test@example.com", "-c",
                                "commit.gpgsign=false", "commit", "-qm", name], check=True)
                self.refused(repo, said)

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

    def test_a_fix_with_no_spec_never_starts_playwright(self):
        # A node:test file a package.json script runs is no spec: it is not listed, so a credential in the step, or a
        # broken Playwright, says nothing about it.
        repo = self.project({"package.json": json.dumps({"type": "module", "scripts": {"test": "node --test src/*.test.mjs"}}) + "\n"})
        os.remove(Path(repo, "node_modules", "playwright-core"))  # a Playwright that cannot start
        result = self.fix(repo, {"src/price.mjs": PRICE_FIX, "src/price.test.mjs": NODE_REPRODUCES},
                          env=self.env({"RESEND_API_KEY": "re_live_value"}))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("src/price.test.mjs: gives members 10% off over 100 fails on the code before the fix", result.stderr)


    def test_a_fix_whose_only_tests_use_jest_globals_is_not_held_up_by_the_browser_path(self):
        # describe/it with no import might be a spec (one that imports its test from a fixtures module) until a
        # config collects it; when listing cannot run here (a credential in the step), it is classified as before:
        # a Jest test, not judged, and never exit 2.
        calls = 'describe("price", () => {\n  it("gives members 10% off", () => { expect(1).toBe(1); });\n});\n'
        repo = self.project({"apps/api/jest.config.js": "module.exports = {};\n"})
        result = self.fix(repo, {"src/price.mjs": PRICE_FIX, "apps/api/src/price.test.ts": calls}, "--soak",
                          env=self.env({"RESEND_API_KEY": "re_live_value"}))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("no changed test imports @playwright/test, and listing the others under --playwright could not run, so "
                      "none is taken for a spec", result.stderr)
        self.assertIn("apps/api/src/price.test.ts: not judged: a Jest test (apps/api/jest.config.js)", result.stderr)
        self.assertNotIn("re_live_value", result.stdout + result.stderr)


    def test_a_node_that_ignores_the_proxy_is_refused(self):
        # A Node whose fetch and http do not follow HTTP_PROXY would let the site's server reach other hosts unseen.
        repo, bin_dir = self.project(), Path(tempfile.mkdtemp(prefix="vv-node-"))
        self.addCleanup(shutil.rmtree, bin_dir, True)
        (bin_dir / "node").write_text("#!/bin/sh\nunset NODE_USE_ENV_PROXY HTTP_PROXY HTTPS_PROXY http_proxy https_proxy\n"
                                      "exec %s \"$@\"\n" % shutil.which("node"))
        (bin_dir / "node").chmod(0o755)
        env = self.env()
        result = self.refused(repo, "the Node on PATH does not send fetch and http through HTTP_PROXY with NODE_USE_ENV_PROXY=1",
                              env=dict(env, PATH="%s%s%s" % (bin_dir, os.pathsep, env["PATH"])))
        self.assertIn("this gate does not start it", result.stderr)

    def test_a_fix_whose_only_tests_use_vitest_globals_is_judged_as_before(self):
        # A Vitest file with globals might be a spec, so it is listed; with a credential in the step that listing cannot
        # run, and it is classified as it was before --playwright: here, a Vitest test with no Vitest installed.
        calls = 'describe("price", () => {\n  it("gives members 10% off", () => { expect(1).toBe(1); });\n});\n'
        repo = self.project({"package.json": json.dumps({"type": "module", "devDependencies": {"vitest": "4.1.11"}}) + "\n"})
        result = self.fix(repo, {"src/price.mjs": PRICE_FIX, "src/price.test.mjs": calls}, "--soak",
                          env=self.env({"RESEND_API_KEY": "re_live_value"}))
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn("none is taken for a spec", result.stderr)
        self.assertIn("no vitest is installed for the repository root (for src/price.test.mjs)", result.stderr)
        self.assertNotIn("re_live_value", result.stdout + result.stderr)


class Answers(http.server.BaseHTTPRequestHandler):
    """A server already running at the site's address: it answers every request, and logs nothing."""

    def do_GET(self):
        self.send_response(200)
        self.end_headers()

    def log_message(self, *_):
        pass


class ConfigShapes(Fixture):
    """The wrapper imports the repository's own config whatever its shape, in a CommonJS or an ES module package."""

    def test_each_config_shape_loads_through_the_wrapper(self):
        ts = 'import { defineConfig } from "@playwright/test";\nconst port: number = 4999;\n' \
             'export default defineConfig({ testDir: "e2e", use: { baseURL: `http://127.0.0.1:${port}` } });\n'
        cjs = 'const { defineConfig } = require("@playwright/test");\n' \
              'module.exports = defineConfig({ testDir: "e2e", use: { baseURL: "http://127.0.0.1:4999" } });\n'
        esm = ts.replace(": number", "")
        for kind, extension, text in (("commonjs", "ts", ts), ("module", "ts", ts), ("commonjs", "mts", ts),
                                      ("commonjs", "js", cjs), ("module", "cjs", cjs), ("module", "mjs", esm)):
            with self.subTest(package=kind, config=extension):
                repo = self.project({"package.json": json.dumps({"type": kind}) + "\n", "playwright.config." + extension: text,
                                     "e2e/a.spec.ts": 'import { test } from "@playwright/test";\ntest("t", async () => {});\n'})
                with mock.patch.dict(os.environ, self.env(), clear=True):  # the gate refuses this machine's own tokens
                    tests, errors, projects, _ = _regression_playwright.listed(repo, "playwright.config." + extension,
                                                                              ["e2e/a.spec.ts"], set(), time.monotonic() + 120, 120)
                self.assertEqual((tests, errors), ({"e2e/a.spec.ts": {""}}, {}))
                self.assertEqual(projects[""]["baseURL"], "http://127.0.0.1:4999")
                self.assertEqual([name for name in os.listdir(repo) if name.startswith(".vv-")], [])


if __name__ == "__main__":
    unittest.main()
