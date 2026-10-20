"""regression-test-fails-on-base --playwright keeping a run on this machine, in the pinned chromium on the fixture of
tests/test_regression_playwright.py: a link-local address goes to the gate's proxy, a Node process's own
connections of any kind are refused and recorded by the guard (gates/_local_network.py), also when a webServer's env
sets its own NODE_OPTIONS, a site on IPv6 loopback is this machine, and a page's WebRTC sends nothing past the
proxy. Skips without the browser, as tests/test_regression_playwright.py does."""
import socket
import subprocess
import threading
import unittest
from pathlib import Path

from test_regression_playwright import PRICE_FIX, REPRODUCES, SERVER, SPEC, Browser
from test_regression_test import tree_is_head

# The same site, whose server first opens two connections no proxy setting reaches: http with its own Agent, and a raw
# socket to a database. Every Node process of a run is guarded, so both are refused and recorded.
SERVER_CONNECTS = SERVER.replace('appendFileSync("started.log", "started\\n");', """import http from "node:http";
import net from "node:net";
const agent = await new Promise((done) => http.get("http://rates.example.com/", { agent: new http.Agent() }, () => done("reached"))
  .on("error", () => done("blocked")));
const raw = await new Promise((done) => net.connect(5432, "db.example.com", () => done("reached")).on("error", () => done("blocked")));
appendFileSync("started.log", "agent: " + agent + ", raw: " + raw + "\\n");""")


class Network(Browser):
    """What a run may reach: this machine only, whatever opens the connection."""

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

    def test_a_page_s_webrtc_sends_nothing_past_the_proxy(self):
        # STUN goes over UDP, which no proxy sees: Chromium is told to use only what goes through the proxy. A listener on
        # this machine's own non-loopback address stands for the host the page tries.
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
            probe.connect(("10.255.255.255", 1))  # no datagram is sent: this only picks the outgoing interface
            address = probe.getsockname()[0]
        if address.startswith("127."):
            self.skipTest("this machine has no address but loopback")
        listener = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        listener.bind((address, 0))
        listener.settimeout(0.5)
        heard, done = [], threading.Event()

        def listen():
            while not done.is_set():
                try:
                    heard.append(listener.recvfrom(2048)[1])
                except OSError:
                    pass
        thread = threading.Thread(target=listen, daemon=True)
        thread.start()
        self.addCleanup(listener.close)
        repo = self.project()
        spec = SPEC + 'test("members get 10% off over 100", async ({ page }) => {\n  await page.goto("/");\n' \
                      '  await page.evaluate(async (stun) => {\n    const peer = new RTCPeerConnection({ iceServers: [{ urls: stun }] });\n' \
                      '    peer.createDataChannel("x");\n    await peer.setLocalDescription(await peer.createOffer());\n' \
                      '    await new Promise((resolve) => setTimeout(resolve, 2000));\n    peer.close();\n' \
                      '  }, "STUN");\n  await expect(page.locator("#total")).toHaveText("180", { timeout: 3000 });\n});\n'
        spec = spec.replace("STUN", "stun:%s:%d" % (address, listener.getsockname()[1]))
        result = self.fix(repo, {"src/price.mjs": PRICE_FIX, "e2e/price.spec.mjs": spec})
        done.set()
        thread.join()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(heard, [])
        tree_is_head(self, repo)


if __name__ == "__main__":
    unittest.main()
