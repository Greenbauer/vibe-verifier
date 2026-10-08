"""The QAE harness's page record (actions/qae-browser/console-pages.js): which page logged each
console error, so the artifacts gate can tell a third-party page's own error from the site's.

The hook's own tests are the node tests beside it, run here. `Live` drives the pinned playwright-mcp
the way the explorer does, against a site and a third-party page on local ports, and runs the gate
on the artifacts that run wrote. It needs the installed toolchain, so it runs where QAE_MCP names
the playwright-mcp entry point: the catalog's `qae-browser` CI job, after actions/qae-browser, which
also hands it that action's `console-pages` output as QAE_CONSOLE_PAGES."""
import http.server
import json
import os
import shutil
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path

from helpers import ROOT, gate, make_repo

HOOK = ROOT / "actions" / "qae-browser" / "console-pages.js"
# What an analytics endpoint answers a page of another origin: Chromium refuses the response and logs
# `Failed to load resource: net::ERR_BLOCKED_BY_RESPONSE.NotSameOrigin @ <its URL>:0`, with no status.
BLOCKED = "net::ERR_BLOCKED_BY_RESPONSE.NotSameOrigin"


class Hook(unittest.TestCase):
    def test_the_hooks_node_tests_pass(self):
        result = subprocess.run(["node", "--test", "--test-reporter=tap", str(HOOK.with_suffix(".test.js"))], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertRegex(result.stdout, r"(?m)^# pass [1-9]")

    def test_the_claude_lane_template_loads_the_hook_the_browser_action_hands_out(self):
        # The Codex lane's action passes it itself (tests/test_qae_codex.py). A copy of this template
        # without the argument writes no record, and the gate then judges every console error.
        template = (ROOT / "harnesses" / "qae" / "explore.yml").read_text()
        self.assertIn('"--output-dir","qae-artifacts","--save-session","--viewport-size","1280x800",'
                      '"--init-page","${{ steps.browser.outputs.console-pages }}"]', template)


class Server:
    """One origin on a local port: `pages` maps a path to the HTML it serves, and /pageviews answers
    an image no other origin may read."""

    def __init__(self, test, pages=None):
        pages = pages or {}

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                path = self.path.split("?")[0]
                body = pages.get(path, "").encode()
                self.send_response(200 if path in pages or path in ("/pageviews", "/favicon.ico") else 404)
                self.send_header("Content-Type", "image/png" if path == "/pageviews" else "text/html")
                if path == "/pageviews":
                    self.send_header("Cross-Origin-Resource-Policy", "same-origin")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        self.pages = pages
        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = "http://127.0.0.1:%d" % self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        test.addCleanup(self.server.server_close)
        test.addCleanup(self.server.shutdown)


class Explorer:
    """A playwright-mcp process started with the harness's arguments, spoken to over stdio. `more` is
    what a lane adds after them (the Codex lane's --config and its hook, tests/test_qae_secret_names.py)."""

    def __init__(self, test, artifacts, more=()):
        self.calls = 0
        self.process = subprocess.Popen(
            ["node", os.environ["QAE_MCP"], "--browser", "chromium", "--headless", "--isolated", "--output-dir", "qae-artifacts",
             "--save-session", "--viewport-size", "1280x800", "--init-page", os.environ.get("QAE_CONSOLE_PAGES", str(HOOK)), *more],
            cwd=os.path.dirname(artifacts), stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
        test.addCleanup(self.close)
        self.send("initialize", {"protocolVersion": "2025-03-26", "capabilities": {}, "clientInfo": {"name": "test", "version": "0"}})
        self.process.stdin.write(json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}) + "\n")

    def close(self):
        self.process.stdin.close()
        self.process.wait(timeout=30)
        self.process.stdout.close()

    def send(self, method, params):
        self.calls += 1
        self.process.stdin.write(json.dumps({"jsonrpc": "2.0", "id": self.calls, "method": method, "params": params}) + "\n")
        self.process.stdin.flush()
        while True:
            answer = json.loads(self.process.stdout.readline())
            if answer.get("id") == self.calls:
                return answer

    def call(self, name, **arguments):
        answer = self.send("tools/call", {"name": name, "arguments": arguments})
        if "error" in answer or answer["result"].get("isError"):
            raise AssertionError("%s failed: %s" % (name, json.dumps(answer)[:600]))
        return answer


@unittest.skipUnless(os.environ.get("QAE_MCP"), "needs the installed browser toolchain: QAE_MCP names playwright-mcp's entry point")
class Live(unittest.TestCase):
    def setUp(self):
        self.repo = make_repo(self, {"README.md": "x"})
        self.work = tempfile.mkdtemp(prefix="vv-qae-live-")
        self.addCleanup(shutil.rmtree, self.work, True)
        self.root = os.path.join(self.work, "qae-artifacts")
        os.makedirs(os.path.join(self.root, "qae"))
        self.analytics = Server(self)
        beacon = '<img src="%s/pageviews?from=%%s">' % self.analytics.url
        self.hosted = Server(self, {"/checkout": "<title>hosted</title><h1>Hosted checkout</h1>" + beacon % "hosted"})
        self.site = Server(self, {
            "/": '<title>site</title><h1>Shop</h1><a id="pay" target="_blank" href="%s/checkout?session=1">Pay</a>' % self.hosted.url,
            "/broken": "<title>site</title><h1>Broken</h1>" + beacon % "site",
        })

    def explore(self, path, click=None):
        """One criterion, walked the way the prompt asks: open the page, optionally follow a link,
        screenshot the step, then read the network and the console."""
        explorer = Explorer(self, self.root)
        explorer.call("browser_navigate", url=self.site.url + path)
        if click:
            explorer.call("browser_click", element="the link", target=click)
        explorer.call("browser_wait_for", time=2)
        explorer.call("browser_take_screenshot", filename="qae-artifacts/qae/AC1-step-1.png")
        explorer.call("browser_network_requests")
        explorer.call("browser_console_messages")
        explorer.call("browser_close")
        explorer.close()
        Path(self.root, "qae", "AC1.md").write_text("- step 1: opened %s -> saw the page\n" % path)

    def console(self):
        return "".join(log.read_text() for log in sorted(Path(self.root).glob("console-*.log")))

    def run_gate(self):
        return gate("qae-artifacts", self.repo, "--artifacts", self.root, "--site", self.site.url + "/")

    def test_a_hosted_pages_own_error_passes_and_only_because_of_the_record(self):
        self.explore("/", click="#pay")
        self.assertIn("[ERROR] Failed to load resource: %s @ %s/pageviews?from=hosted:0" % (BLOCKED, self.analytics.url), self.console())
        records = [json.loads(line) for line in Path(self.root, "console-pages.jsonl").read_text().splitlines()]
        self.assertEqual({record["page"] for record in records}, {self.hosted.url + "/checkout"})
        result = self.run_gate()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        os.remove(os.path.join(self.root, "console-pages.jsonl"))
        result = self.run_gate()
        self.assertEqual(result.returncode, 1)
        self.assertIn("console error outside the allowlist: Failed to load resource: " + BLOCKED, result.stdout)

    def test_the_same_error_on_the_sites_own_page_still_fails(self):
        self.explore("/broken")
        self.assertIn("[ERROR] Failed to load resource: %s @ %s/pageviews?from=site:0" % (BLOCKED, self.analytics.url), self.console())
        result = self.run_gate()
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("console error outside the allowlist: Failed to load resource: " + BLOCKED, result.stdout)


if __name__ == "__main__":
    unittest.main()
