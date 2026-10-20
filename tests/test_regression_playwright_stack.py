"""regression-test-fails-on-base --stack-network: the job's stack in the sealed network, on the fixture of
tests/test_regression_playwright.py and the outside listener of tests/test_regression_playwright_network.py.

The stack (a stand-in container on a network of its own, its port published on this machine) is moved into the seal at
the first browser run: the site reaches it at its usual loopback port, it cannot reach the listener itself, and the
gate's own node:test and Python runs of the same fix, on this machine, still reach it through the relay that publishes
its port again. Afterwards it is back on its own network, published and able to reach out, also after a run that
cannot be judged, and nothing of the seal is left.

Skips without Docker, as tests/test_regression_playwright.py does (VV_REGRESSION_SEALED)."""
import json
import re
import secrets
import subprocess
import sys
import time
import unittest
import urllib.request

from test_regression_playwright import DOES_NOT_REPRODUCE, PRICE_FIX, REPRODUCES, SERVER, SPEC, free_port
from test_regression_playwright_network import Outside, docker
from test_regression_test import tree_is_head

# A stand-in for the job's stack: answers on 8080 whether it could reach the listener itself, through this machine.
STACK = r"""const net = require("net");
require("http").createServer((request, response) => net.connect({ host: process.env.TARGET_ADDRESS, port: Number(process.env.TARGET_PORT), timeout: 3000 })
  .on("connect", function () { this.destroy(); response.end("stack-ok reached"); })
  .on("timeout", function () { this.destroy(); response.end("stack-ok timeout"); })
  .on("error", (error) => response.end("stack-ok " + error.code))).listen(8080, "0.0.0.0");
"""
# The site renders what the stack answered, read at its loopback port as a site in CI would read it.
SERVER_STACK = SERVER.replace("response.end(`<h1 id=\"total\">${discount(200, true)}</h1>`);",
                              "fetch(process.env.STACK_URL).then((answer) => answer.text(), (error) => 'unreachable').then((stack) =>\n"
                              "    response.end(`<p id=\"stack\">${stack}</p><h1 id=\"total\">${discount(200, true)}</h1>`));")


def seal_left():
    """The gate's own containers and networks (vv-regression-*) on this Docker: none once a gate has finished."""
    return (docker("ps", "-a", "--filter", "name=vv-regression-", "--format", "{{.Names}}"),
            docker("network", "ls", "--filter", "name=vv-regression-", "--format", "{{.Name}}"))


class Stack(Outside):
    """The stack moved into the seal and back."""

    def stack(self):
        """A stand-in for the job's stack on a network of its own, its port published on this machine: (network, port)."""
        stack, port = "vv-stack-" + secrets.token_hex(3), free_port()
        docker("network", "create", stack)
        self.addCleanup(subprocess.run, ["docker", "network", "rm", stack], capture_output=True)
        self.addCleanup(subprocess.run, ["docker", "rm", "-f", stack], capture_output=True)
        # The stack reaches this machine's published port of the listener as host.docker.internal, Docker's name for it.
        docker("run", "-d", "--init", "--name", stack, "--network", stack, "--user", self.user, "--add-host", "host.docker.internal:host-gateway",
               "-e", "TARGET_ADDRESS=host.docker.internal", "-e", "TARGET_PORT=%d" % self.published, "-p", "127.0.0.1:%d:8080" % port,
               self.image, "node", "-e", STACK)
        return stack, port

    def put_back(self, stack, port, before):
        """The stack is on its own network alone again, published on this machine, able to reach out; the seal left nothing."""
        self.assertEqual(list(json.loads(docker("inspect", stack))[0]["NetworkSettings"]["Networks"]), [stack])
        answer = None
        for _ in range(20):
            try:
                answer = urllib.request.urlopen("http://127.0.0.1:%d/" % port, timeout=5).read().decode()
                break
            except OSError:
                time.sleep(0.5)
        self.assertEqual(answer, "stack-ok reached")
        self.assertEqual(seal_left(), before)

    def test_the_job_s_stack_is_reachable_from_the_site_cannot_reach_out_and_is_put_back(self):
        stack, port = self.stack()
        before, left = self.heard(), seal_left()
        repo = self.project({"server.mjs": SERVER_STACK})
        spec = SPEC + 'test("members get 10% off over 100", async ({ page }) => {\n  await page.goto("/");\n' \
                      '  await expect(page.locator("#stack")).toHaveText("stack-ok ENETUNREACH");\n' \
                      '  await expect(page.locator("#total")).toHaveText("180", { timeout: 3000 });\n});\n'
        result = self.fix(repo, {"src/price.mjs": PRICE_FIX, "e2e/price.spec.mjs": spec}, "--stack-network", stack,
                          env=self.env({"STACK_URL": "http://127.0.0.1:%d/" % port}))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('fails on the code before the fix: Error: expect(locator).toHaveText(expected) failed Expected: "180" '
                      'Received: "200"', result.stderr)
        self.assertEqual(self.heard(), before)
        # What each part cost, from the gate's own lines, for the CI log (docs/features/regression-test.md quotes it).
        sys.stderr.write("".join("\n  " + line for line in result.stderr.splitlines() if re.search(r": \d+\.\ds$", line)) + "\n")
        self.put_back(stack, port, left)
        tree_is_head(self, repo)

    def test_the_gate_s_other_tests_reach_the_stack_while_it_is_sealed(self):
        # A node:test file of the same fix runs on this machine. Before the fix it runs after the browser run at the head
        # moved the stack into the seal; the relay publishes its port again, so it reads the stack and fails on the
        # discount, as the bug shows, and not on a stack it cannot reach. The spec does not reproduce, so it decides.
        stack, port = self.stack()
        left = seal_left()
        repo = self.project({"package.json": json.dumps({"type": "module", "scripts": {"test": "node --test tests/*.test.mjs"}}) + "\n"})
        reads = 'import { test } from "node:test";\nimport assert from "node:assert";\nimport { discount } from "../src/price.mjs";\n' \
                'test("members get 10% off, by the stack", async () => {\n' \
                '  assert.match(await (await fetch(process.env.STACK_URL)).text(), /^stack-ok/);\n' \
                '  assert.strictEqual(discount(200, true), 180);\n});\n'
        result = self.fix(repo, {"src/price.mjs": PRICE_FIX, "e2e/price.spec.mjs": DOES_NOT_REPRODUCE, "tests/stack.test.mjs": reads},
                          "--stack-network", stack, env=self.env({"STACK_URL": "http://127.0.0.1:%d/" % port}))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("published again through a relay", result.stderr)
        self.assertRegex(result.stderr, r"tests/stack\.test\.mjs: members get 10% off, by the stack fails on the code before the "
                                        r"fix: .*200 !== 180")
        self.put_back(stack, port, left)
        tree_is_head(self, repo)

    def test_a_python_test_of_the_same_fix_reaches_the_sealed_stack_too(self):
        # The relay is for every runner on this machine, so a Python test reads the stack before the fix as well, and fails
        # on the rate the fix changed, not on a stack it cannot reach.
        stack, port = self.stack()
        left = seal_left()
        repo = self.project({"src/rate.json": '{"member": 1.0}\n'})
        reads = ("import json\nimport os\nimport unittest\nimport urllib.request\n\n\nclass Stack(unittest.TestCase):\n"
                 "    def test_members_get_10_off_by_the_stack(self):\n"
                 "        self.assertTrue(urllib.request.urlopen(os.environ['STACK_URL'], timeout=5).read().decode().startswith('stack-ok'))\n"
                 "        with open(os.path.join(os.path.dirname(__file__), '..', 'src', 'rate.json')) as handle:\n"
                 "            self.assertEqual(json.load(handle)['member'], 0.9)\n")
        result = self.fix(repo, {"src/price.mjs": PRICE_FIX, "src/rate.json": '{"member": 0.9}\n',
                                 "e2e/price.spec.mjs": DOES_NOT_REPRODUCE, "tests/test_stack.py": reads},
                          "--stack-network", stack, env=self.env({"STACK_URL": "http://127.0.0.1:%d/" % port}))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertRegex(result.stderr, r"tests/test_stack\.py: .*fails on the code before the fix: .*1\.0 != 0\.9")
        self.put_back(stack, port, left)
        tree_is_head(self, repo)

    def test_a_run_that_cannot_be_judged_still_puts_the_stack_back(self):
        stack, port = self.stack()
        left = seal_left()
        server = SERVER.replace('appendFileSync("started.log", "started\\n");', 'process.exit(1);')
        repo = self.project({"server.mjs": server})
        result = self.fix(repo, {"src/price.mjs": PRICE_FIX, "e2e/price.spec.mjs": REPRODUCES}, "--stack-network", stack, "--soak",
                          env=self.env({"STACK_URL": "http://127.0.0.1:%d/" % port}))
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn("Process from config.webServer was not able to start", result.stderr)
        self.put_back(stack, port, left)
        tree_is_head(self, repo)


if __name__ == "__main__":
    unittest.main()
