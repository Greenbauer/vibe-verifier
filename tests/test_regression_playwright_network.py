"""regression-test-fails-on-base --playwright keeping a run inside its sealed Docker network, both ways, on the fixture
of tests/test_regression_playwright.py.

An outside listener (a container on an ordinary Docker network, its TCP and UDP ports also published on this
machine) records every packet that reaches it. A container that is not sealed reaches it directly and through this
machine's published port, so the listener does hear a way out (the control). Then each way out the review rounds
found is tried from a sealed run, and the listener hears none of them: a raw Node socket from the site's server, a
different node first on PATH that starts the real one with an empty environment, a page's WebRTC, a cloud metadata address, a spec's own
proxy, and this machine itself. Each was counted as refused, so a changed spec that failed in that run is exit 2.
And the job's stack, moved into the seal, is reachable from the site at its usual loopback port and from the gate's
own tests on this machine (through the relay), cannot reach the listener itself, and is back on its own network,
reachable from this machine, afterwards, also after a run that cannot be judged; nothing of the seal is left.

Skips without Docker, as tests/test_regression_playwright.py does (VV_REGRESSION_SEALED)."""
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.request
from pathlib import Path

from test_regression_playwright import DOES_NOT_REPRODUCE, PRICE_FIX, REPRODUCES, SERVER, SPEC, Browser, free_port
from test_regression_test import tree_is_head

LISTENER = r"""const fs = require("fs"), net = require("net"), dgram = require("dgram");
const log = (line) => fs.appendFileSync("/record/heard.log", line + "\n");
net.createServer((socket) => { log("tcp " + socket.remoteAddress); socket.end("answered\n"); }).listen(8443, "0.0.0.0");
dgram.createSocket("udp4").on("message", (message, from) => log("udp " + from.address)).bind(3478, "0.0.0.0", () => log("ready"));
"""
TRY = r"""const net = require("net"), dgram = require("dgram");
const [address, gateway, port] = process.argv.slice(1);
const tcp = (host, to) => new Promise((done) => net.connect({ host, port: Number(to), timeout: 3000 })
  .on("data", function (answer) { this.destroy(); done(String(answer).trim()); })
  .on("timeout", function () { this.destroy(); done("timeout"); }).on("error", (error) => done(error.code)));
(async () => {
  const udp = dgram.createSocket("udp4");
  const sent = await new Promise((done) => udp.send("x", 3478, address, (error) => { udp.close(); done(error ? error.code : "sent"); }));
  console.log(JSON.stringify({ direct: await tcp(address, 8443), machine: await tcp(gateway, port), udp: sent }));
})();
"""
# The site's server first opens a TCP connection to TARGET_ADDRESS:TARGET_PORT and records how that went.
SERVER_CONNECTS = SERVER.replace('appendFileSync("started.log", "started\\n");', """import net from "node:net";
const tried = await new Promise((done) => net.connect({ host: process.env.TARGET_ADDRESS, port: Number(process.env.TARGET_PORT), timeout: 3000 })
  .on("connect", function () { this.destroy(); done("reached"); }).on("timeout", function () { this.destroy(); done("timeout"); })
  .on("error", (error) => done(error.code)));
appendFileSync("started.log", "outside: " + tried + "\\n");""")
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
REFUSED = "failed in a run whose sealed network refused 1 attempt(s) to reach an address off the network"


def docker(*args):
    return subprocess.run(["docker", *args], capture_output=True, text=True, check=True).stdout.strip()


def seal_left():
    """The gate's own containers and networks (vv-regression-*) on this Docker: none once a gate has finished."""
    return (docker("ps", "-a", "--filter", "name=vv-regression-", "--format", "{{.Names}}"),
            docker("network", "ls", "--filter", "name=vv-regression-", "--format", "{{.Name}}"))


def address_of(container, network):
    return json.loads(docker("inspect", container))[0]["NetworkSettings"]["Networks"][network]["IPAddress"]


class Network(Browser):
    """The listener, the ways out, and the stack, in the image the fixture's Playwright runs in."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        version = json.loads((cls.toolchain / "playwright-core" / "package.json").read_text())["version"]
        cls.image, cls.outside = "mcr.microsoft.com/playwright:v%s-noble" % version, "vv-outside-" + secrets.token_hex(3)
        cls.record, cls.user = tempfile.mkdtemp(prefix="vv-outside-"), "%d:%d" % (os.getuid(), os.getgid())
        cls.addClassCleanup(shutil.rmtree, cls.record, True)
        docker("network", "create", cls.outside)
        cls.addClassCleanup(subprocess.run, ["docker", "network", "rm", cls.outside], capture_output=True)
        cls.published = free_port()
        cls.addClassCleanup(subprocess.run, ["docker", "rm", "-f", cls.outside], capture_output=True)
        docker("run", "-d", "--name", cls.outside, "--network", cls.outside, "--user", cls.user, "-p", "%d:8443" % cls.published,
               "-v", "%s:/record" % cls.record, cls.image, "node", "-e", LISTENER)
        cls.address = address_of(cls.outside, cls.outside)
        cls.gateway = json.loads(docker("network", "inspect", cls.outside))[0]["IPAM"]["Config"][0]["Gateway"]
        for _ in range(60):
            if os.path.exists(os.path.join(cls.record, "heard.log")):
                break
            time.sleep(0.5)

    def heard(self):
        time.sleep(0.5)
        with open(os.path.join(self.record, "heard.log"), encoding="utf-8") as handle:
            return [line.strip() for line in handle if line.strip() != "ready"]

    def target(self, address, port, extra=None):
        return self.env(dict({"TARGET_ADDRESS": address, "TARGET_PORT": str(port)}, **(extra or {})))

    def test_the_listener_hears_a_container_that_is_not_sealed(self):
        # The control: without the seal, a container reaches the listener directly, over UDP, and through this machine.
        before = self.heard()
        said = json.loads(docker("run", "--rm", "--network", self.outside, self.image, "node", "-e", TRY, self.address,
                                 self.gateway, str(self.published)))
        self.assertEqual(said, {"direct": "answered", "machine": "answered", "udp": "sent"})  # the listener's own answer
        heard = [line.split()[0] for line in self.heard()[len(before):]]
        self.assertGreaterEqual(heard.count("tcp"), 2, heard)  # directly, and through this machine's published port
        self.assertGreaterEqual(heard.count("udp"), 1, heard)

    def test_a_raw_node_socket_from_the_site_is_refused_and_counted(self):
        before = self.heard()
        repo = self.project({"server.mjs": SERVER_CONNECTS})
        result = self.fix(repo, {"src/price.mjs": PRICE_FIX, "e2e/price.spec.mjs": REPRODUCES}, env=self.target(self.address, 8443))
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertEqual(Path(repo, "started.log").read_text(), "outside: ENETUNREACH\n" * 2)
        self.assertIn("e2e/price.spec.mjs: members get 10% off over 100 " + REFUSED, result.stderr)
        self.assertEqual(self.heard(), before)
        tree_is_head(self, repo)

    def test_a_different_node_first_on_path_with_an_empty_environment_is_refused(self):
        # As a package manager's own wrapper is (Yarn 1 puts one first on PATH): a `node` that starts the real one with an
        # empty environment, so no NODE_OPTIONS, no PATH and no proxy setting of the run is left. The kernel refuses it the same.
        before = self.heard()
        shim = '#!/bin/sh\necho shim >> shim.log\nexec env -i PORT="$PORT" TARGET_ADDRESS="$TARGET_ADDRESS" ' \
               'TARGET_PORT="$TARGET_PORT" "$REAL_NODE" "$@"\n'
        repo = self.project({"server.mjs": SERVER_CONNECTS, "bin/node": shim,
                             ".gitignore": "node_modules\nstarted.log\nglobal-setup.log\nshim.log\n"})
        os.chmod(Path(repo, "bin", "node"), 0o755)
        config = Path(repo, "playwright.config.mjs")
        config.write_text(config.read_text().replace('command: "node server.mjs"', 'command: "export REAL_NODE=$(command -v node); '
                                                     'export PATH=$PWD/bin:$PATH; node server.mjs"'))
        subprocess.run(["git", "-C", repo, "-c", "user.name=Test", "-c", "user.email=test@example.com", "-c", "commit.gpgsign=false",
                        "commit", "-qam", "another node"], check=True)
        result = self.fix(repo, {"src/price.mjs": PRICE_FIX, "e2e/price.spec.mjs": REPRODUCES}, env=self.target(self.address, 8443))
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertEqual(Path(repo, "shim.log").read_text(), "shim\n" * 2)  # the other node started the site, both runs
        self.assertEqual(Path(repo, "started.log").read_text(), "outside: ENETUNREACH\n" * 2)
        self.assertEqual(self.heard(), before)
        tree_is_head(self, repo)

    def test_this_machine_is_off_the_network_too(self):
        # This machine's published port, at the address its containers reach it on: no route from the seal.
        before = self.heard()
        repo = self.project({"server.mjs": SERVER_CONNECTS})
        result = self.fix(repo, {"src/price.mjs": PRICE_FIX, "e2e/price.spec.mjs": REPRODUCES},
                          env=self.target(self.gateway, self.published))
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertEqual(Path(repo, "started.log").read_text(), "outside: ENETUNREACH\n" * 2)
        self.assertEqual(self.heard(), before)
        tree_is_head(self, repo)

    def test_a_page_s_webrtc_sends_nothing_off_the_network(self):
        before = self.heard()
        repo = self.project()
        spec = SPEC + 'test("members get 10% off over 100", async ({ page }) => {\n  await page.goto("/");\n' \
                      '  await page.evaluate(async (stun) => {\n    const peer = new RTCPeerConnection({ iceServers: [{ urls: stun }] });\n' \
                      '    peer.createDataChannel("x");\n    await peer.setLocalDescription(await peer.createOffer());\n' \
                      '    await new Promise((resolve) => setTimeout(resolve, 2000));\n    peer.close();\n' \
                      '  }, "STUN");\n  await expect(page.locator("#total")).toHaveText("180", { timeout: 3000 });\n});\n'
        result = self.fix(repo, {"src/price.mjs": PRICE_FIX, "e2e/price.spec.mjs": spec.replace("STUN", "stun:%s:3478" % self.address)})
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertRegex(result.stderr, r"failed in a run whose sealed network refused \d+ attempt\(s\) to reach an address off")
        self.assertEqual(self.heard(), before)
        tree_is_head(self, repo)

    def test_a_cloud_metadata_address_is_refused(self):
        repo = self.project()
        spec = SPEC + 'test("reads the metadata", async ({ page }) => {\n' \
                      '  await page.goto("http://169.254.169.254/latest/meta-data/", { timeout: 5000 });\n});\n'
        result = self.fix(repo, {"src/price.mjs": PRICE_FIX, "e2e/metadata.spec.mjs": spec}, "--soak")
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn("e2e/metadata.spec.mjs: reads the metadata failed in a run whose sealed network refused", result.stderr)
        self.assertIn("net::ERR_ADDRESS_UNREACHABLE at http://169.254.169.254/latest/meta-data/", result.stderr)
        tree_is_head(self, repo)

    def test_a_spec_s_own_proxy_is_refused(self):
        # A spec may set any proxy it likes with test.use: there is no route to it.
        before = self.heard()
        repo = self.project()
        spec = SPEC + 'test.use({ proxy: { server: "http://%s:8443" } });\n' % self.address + \
            'test("members get 10% off over 100", async ({ page }) => {\n  await page.goto("http://example.org/");\n});\n'
        result = self.fix(repo, {"src/price.mjs": PRICE_FIX, "e2e/price.spec.mjs": spec}, "--soak")
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn("e2e/price.spec.mjs: members get 10% off over 100 failed in a run whose sealed network refused", result.stderr)
        self.assertEqual(self.heard(), before)
        tree_is_head(self, repo)

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
