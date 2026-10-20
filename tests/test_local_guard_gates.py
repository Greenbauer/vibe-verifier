"""regression-test-fails-on-base's test processes under gates/_local_guard.py, both ways, for each runner it starts: Vitest
(the fixture's 4.1.11, npm ci from tests/fixtures/coverage-not-lost), node:test (the Node on PATH) and Python (unittest
under the python3 on PATH). Each fixture test reaches a server of its own on 127.0.0.1 by the name localhost;
the guarded ones also try an outside address (192.0.2.1, TEST-NET-1) and start a child process that tries another
(192.0.2.2), and expect both refused. The same fixtures run coverage-not-lost and changed-code-mutation in
tests/test_local_guard_runs.py."""
import json
import shutil
import tempfile
import unittest
from pathlib import Path

from helpers import ROOT, install_fixture, make_repo
from test_regression_test import fix, tree_is_head

FIXTURE = ROOT / "tests" / "fixtures" / "coverage-not-lost"
TITLE = "fix(price): members get their discount"
OUTSIDE = "192.0.2.1:80, 192.0.2.2:80"
# A server on this machine read by localhost, an outside address, and a child that tries another: src/guarded.mjs. When
# loaded with VV_RAN set it appends a line to that file, so a test can tell whether any test process started.
JS_GUARDED = r"""import { createServer, connect } from "node:net";
import { execFileSync } from "node:child_process";
import { appendFileSync } from "node:fs";
if (process.env.VV_RAN) appendFileSync(process.env.VV_RAN, "ran\n");
export const here = () => new Promise((resolve, reject) => {
  const server = createServer((socket) => socket.end("ok")).listen(0, "127.0.0.1", () => {
    let said = "";
    connect(server.address().port, "localhost").on("data", (data) => { said += data; })
      .on("end", () => { server.close(); resolve(said); }).on("error", reject);
  });
});
export const outside = () => fetch("http://192.0.2.1/").then(() => "reached", (error) => error.cause.code);
export const child = () => execFileSync(process.execPath, ["-e",
  "require('net').connect(80, '192.0.2.2').on('error', (error) => console.log(error.code))"]).toString().trim();
"""
PY_GUARDED = r"""import os
import socket
import subprocess
import sys
import threading

if os.environ.get("VV_RAN"):
    with open(os.environ["VV_RAN"], "a") as ran:
        ran.write("ran\n")
CHILD = "import socket\ntry:\n    socket.create_connection(('192.0.2.2', 80), timeout=5)\nexcept ConnectionRefusedError:\n    print('refused')"


def here():
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen(1)
    threading.Thread(target=lambda: server.accept()[0].sendall(b"ok"), daemon=True).start()
    with socket.create_connection(("localhost", server.getsockname()[1]), timeout=5) as client:
        return client.recv(2).decode()


def outside():
    try:
        socket.create_connection(("192.0.2.1", 80), timeout=5)
    except ConnectionRefusedError:
        return "refused"
    return "reached"


def child():
    return subprocess.run([sys.executable, "-c", CHILD], capture_output=True, text=True).stdout.strip()
"""
JS_PRICE_BUG = "export function discount(total, member) {\n  return total;\n}\n"
JS_PRICE_FIX = "export function discount(total, member) {\n  return member && total > 100 ? total * 0.9 : total;\n}\n"
PY_PRICE_BUG = "def discount(total, member):\n    return total\n"
PY_PRICE_FIX = "def discount(total, member):\n    if member and total > 100:\n        return total * 0.9\n    return total\n"
VITEST = """import { expect, it } from "vitest";
import { discount } from "./price.mjs";
import { here, outside, child } from "./guarded.mjs";
it("gives members 10% off over 100", async () => {
  expect(await here()).toBe("ok");
OUTSIDE  expect(discount(200, true)).toBe(180);
});
"""
JS_OUTSIDE = '  expect(await outside()).toBe("ECONNREFUSED");\n  expect(child()).toBe("ECONNREFUSED");\n'
NODE_TEST = """import { test } from "node:test";
import assert from "node:assert/strict";
import { discount } from "./price.mjs";
import { here, outside, child } from "./guarded.mjs";
test("gives members 10% off over 100", async () => {
  assert.equal(await here(), "ok");
OUTSIDE  assert.equal(discount(200, true), 180);
});
"""
NODE_OUTSIDE = '  assert.equal(await outside(), "ECONNREFUSED");\n  assert.equal(child(), "ECONNREFUSED");\n'
UNITTEST = """import unittest

from shop.guarded import child, here, outside
from shop.price import discount


class Price(unittest.TestCase):
    def test_members_get_ten_percent_off_over_100(self):
        self.assertEqual(here(), "ok")
OUTSIDE        self.assertEqual(discount(200, True), 180)
"""
PY_OUTSIDE = '        self.assertEqual(outside(), "refused")\n        self.assertEqual(child(), "refused")\n'
# A credential for somewhere else: in the gate's environment, and in a .env file a framework would load.
IN_ENV = {"RESEND_API_KEY": "re_value_in_env"}
IN_DOTENV = ("apps/web/.env.local", "DATABASE_URL=postgresql://app:pw-in-dotenv@db.example.com:5432/app\n")


def setUpModule():
    global DEPS
    DEPS = tempfile.mkdtemp(prefix="vv-guard-deps-")
    for name in ("package.json", "package-lock.json"):
        shutil.copy(FIXTURE / name, DEPS)
    install_fixture(DEPS)


def tearDownModule():
    shutil.rmtree(DEPS, ignore_errors=True)


def runner_repo(test, kind):
    """(a repository of one runner's fixture before the fix, its test's path, the fix's files: with the test's outside
    calls when asked)."""
    if kind == "python":
        repo = make_repo(test, {".gitignore": ".env.local\n", "shop/__init__.py": "", "shop/price.py": PY_PRICE_BUG,
                                "shop/guarded.py": PY_GUARDED})
        path, text, calls, fixed = "tests/test_price.py", UNITTEST, PY_OUTSIDE, {"shop/price.py": PY_PRICE_FIX}
    else:
        package = {"type": "module", "scripts": {"test": "node --test src/*.test.mjs"}} if kind == "node" else {"type": "module"}
        repo = make_repo(test, {".gitignore": "node_modules\n.env.local\n", "package.json": json.dumps(package) + "\n",
                                "src/price.mjs": JS_PRICE_BUG, "src/guarded.mjs": JS_GUARDED})
        if kind == "vitest":
            Path(repo, "node_modules").symlink_to(Path(DEPS, "node_modules"))
        path, text, calls = "src/price.test.mjs", *((VITEST, JS_OUTSIDE) if kind == "vitest" else (NODE_TEST, NODE_OUTSIDE))
        fixed = {"src/price.mjs": JS_PRICE_FIX}
    return repo, path, lambda outside=False: dict(fixed, **{path: text.replace("OUTSIDE", calls if outside else "")})


def marker(test):
    """A path the fixture's guarded module appends to when a test process loads it (VV_RAN), not there yet."""
    directory = tempfile.mkdtemp(prefix="vv-guard-ran-")
    test.addCleanup(shutil.rmtree, directory, True)
    return str(Path(directory, "ran"))


class RegressionRunners(unittest.TestCase):
    """Each runner regression-test-fails-on-base starts: Vitest, node:test and Python."""

    RUNNERS = ("vitest", "node", "python")

    def test_a_run_that_reaches_only_this_machine_is_judged_as_before(self):
        # A local secret the manifest declares with --local-env runs too.
        for kind in self.RUNNERS:
            with self.subTest(runner=kind):
                repo, test, files = runner_repo(self, kind)
                ran = marker(self)
                result = fix(repo, files(), TITLE, "", "--local-env", "SESSION_SECRET", env={"SESSION_SECRET": "local-only", "VV_RAN": ran})
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertIn("%s: " % test, result.stderr)
                self.assertIn("fails on the code before the fix", result.stderr)
                self.assertNotIn("refused", result.stderr)
                self.assertTrue(Path(ran).exists())  # the marker works: a test process loaded the module
                tree_is_head(self, repo)

    def test_a_failure_in_a_run_that_reached_out_cannot_be_judged(self):
        # At the head the test passes with both connections refused, which is noted; before the fix its check fails in
        # a run that refused them, so that failure cannot be told from the refusals.
        for kind in self.RUNNERS:
            with self.subTest(runner=kind):
                repo, test, files = runner_repo(self, kind)
                result = fix(repo, files(outside=True), TITLE, "", "--soak")
                self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
                self.assertRegex(result.stderr, r"tried to reach %s, which the gate refused; its tests passed all the same" % OUTSIDE)
                self.assertRegex(result.stderr, r"%s: .*members.* failed in .*, which tried to reach %s: the gate "
                                                r"refused it, because a gate's test runs reach only this machine, so the "
                                                r"failure cannot be told from the refusal" % (test, OUTSIDE))
                tree_is_head(self, repo)

    def test_a_credential_for_anywhere_else_stops_each_runner_before_it_starts(self):
        for kind in self.RUNNERS:
            for where in ("the environment", "a .env file"):
                with self.subTest(runner=kind, credential=where):
                    repo, _, files = runner_repo(self, kind)
                    if where == "a .env file":
                        Path(repo, IN_DOTENV[0]).parent.mkdir(parents=True)
                        Path(repo, IN_DOTENV[0]).write_text(IN_DOTENV[1])
                    ran = marker(self)
                    result = fix(repo, files(), TITLE, "", "--soak", env=dict(IN_ENV if where == "the environment" else {}, VV_RAN=ran))
                    self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
                    self.assertFalse(Path(ran).exists())  # no test process started
                    self.assertIn("would run the pull request's code with a credential for somewhere other than this machine "
                                  "in what it inherits, so this gate does not start it", result.stderr)
                    self.assertIn("the environment: RESEND_API_KEY is named as a credential" if where == "the environment" else
                                  "apps/web/.env.local: DATABASE_URL holds a connection string for db.example.com", result.stderr)
                    for value in ("re_value_in_env", "pw-in-dotenv"):
                        self.assertNotIn(value, result.stdout + result.stderr)
                    self.assertNotIn("fails on the code before the fix", result.stderr)
                    tree_is_head(self, repo)

    def test_a_file_or_an_assignment_a_node_test_script_gives_its_process_is_checked_too(self):
        # config/test.env is no .env name: only the script's own --env-file says the test process loads it. A leading
        # NAME=value is in the process's environment.
        remote, local = "postgresql://app:pw-in-script@db.example.com:5432/app", "postgresql://postgres:postgres@127.0.0.1:54322/postgres"
        for command, value, said in (("node --env-file config/test.env --test src/*.test.mjs", remote, "config/test.env: DATABASE_URL"),
                                     ("node --env-file=config/test.env --test src/*.test.mjs", remote, "config/test.env: DATABASE_URL"),
                                     ("node --env-file config/test.env --test src/*.test.mjs", local, None),
                                     ("DATABASE_URL=%s node --test src/*.test.mjs" % remote, local, "the environment: DATABASE_URL")):
            with self.subTest(command=command.split(" node")[0][:30], value=value.split("@")[1]):
                script = {"type": "module", "scripts": {"test": command}}
                repo = make_repo(self, {"package.json": json.dumps(script) + "\n", "config/test.env": "DATABASE_URL=%s\n" % value,
                                        "src/price.mjs": JS_PRICE_BUG, "src/guarded.mjs": JS_GUARDED})
                result = fix(repo, {"src/price.mjs": JS_PRICE_FIX, "src/price.test.mjs": NODE_TEST.replace("OUTSIDE", "")}, TITLE, "")
                self.assertEqual(result.returncode, 2 if said else 0, result.stdout + result.stderr)
                if said:
                    self.assertIn(said + " holds a connection string for db.example.com", result.stderr)
                self.assertNotIn("pw-in-script", result.stdout + result.stderr)
                tree_is_head(self, repo)


if __name__ == "__main__":
    unittest.main()
