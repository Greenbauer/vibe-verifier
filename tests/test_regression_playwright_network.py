"""regression-test-fails-on-base --playwright keeping a run inside its sealed Docker network, both ways, on the fixture
of tests/test_regression_playwright.py.

An outside listener (a container on an ordinary Docker network, its TCP and UDP ports also published on this
machine) records every packet that reaches it. A container that is not sealed reaches it directly and through this
machine's published port, so the listener does hear a way out (the control). Then each way out the review rounds
found is tried from a sealed run, and the listener hears none of them: a raw Node socket from the site's server, a
different node first on PATH that starts the real one with an empty environment, a page's WebRTC, a cloud metadata address, a spec's own
proxy, and this machine itself. Each was counted as refused, so a changed spec that failed in that run is exit 2.
The job's stack in the seal is tests/test_regression_playwright_stack.py.

Skips without Docker, as tests/test_regression_playwright.py does (VV_REGRESSION_SEALED)."""
import json
import os
import secrets
import shutil
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

from test_regression_playwright import PRICE_FIX, REPRODUCES, SERVER, SPEC, Browser, free_port
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
REFUSED = "failed in a run whose sealed network refused 1 attempt(s) to reach an address off the network"


def docker(*args):
    return subprocess.run(["docker", *args], capture_output=True, text=True, check=True).stdout.strip()


def address_of(container, network):
    return json.loads(docker("inspect", container))[0]["NetworkSettings"]["Networks"][network]["IPAddress"]


class Outside(Browser):
    """The outside listener, in the image the fixture's Playwright runs in (tests/test_regression_playwright_stack.py
    shares it)."""

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


class Network(Outside):
    """The control, and each way out, refused."""

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


if __name__ == "__main__":
    unittest.main()
