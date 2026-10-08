"""The QAE harness's secret names (actions/qae-browser/secret-names.js): a credential's NAME becomes
its value however the explorer enters it, so a sign-in never depends on which browser tool a model
picked.

The hook's own tests are the node tests beside it, run here. `Live` drives the pinned playwright-mcp
the way the Codex lane starts it, against a site with a login on a local port, signs in the way the
explorer did on the run that failed (browser_run_code_unsafe with the name as the password), and runs
the gate on the artifacts that run wrote. It needs the installed toolchain, so it runs where QAE_MCP
names the playwright-mcp entry point: the catalog's `qae-browser` CI job."""
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
from test_qae_console_pages import Explorer

HOOK = ROOT / "actions" / "qae-browser" / "secret-names.js"
NAME = "QAE_PASSWORD"
PASSWORD = "s3cret-Login-Value-0123456789"
REFUSED = "Failed to load resource: the server responded with a status of 400 (Bad Request) @ %s/auth/token:0"
LOGIN = """<title>site</title><h1>Welcome back</h1>
<form><label>Email address <input type="email" name="email"></label>
<label>Password <input type="password" name="password"></label><button>Sign in</button></form>
<p role="alert"></p>
<script>
document.querySelector("form").addEventListener("submit", async event => {
  event.preventDefault();
  const sent = Object.fromEntries(new FormData(event.target));
  const answer = await fetch("/auth/token", { method: "POST", body: JSON.stringify(sent) });
  if (answer.ok) location.assign("/home"); else document.querySelector("[role=alert]").textContent = "Invalid login credentials";
});
</script>"""
# The sign-in of the run that failed, as the explorer wrote it: the name, through the page it was handed.
RUN_CODE = ("async (page) => { await page.locator('input[type=email]').fill('qae@example.test'); "
            "await page.locator('input[type=password]').fill('%s'); "
            "await page.getByRole('button', { name: 'Sign in' }).click(); await page.waitForTimeout(1500); "
            "return { url: page.url() }; }" % NAME)
# Each other way a snippet can put text in the password field, then what the field holds.
FIELD = "input[type=password]"
ENTRIES = {
    "page.fill": "await page.fill(F, N);",
    "page.type": "await page.type(F, N);",
    "locator.type": "await page.locator(F).type(N);",
    "locator.pressSequentially": "await page.locator(F).pressSequentially(N);",
    "a frame's locator": "await page.mainFrame().locator(F).fill(N);",
    "elementHandle.fill": "await (await page.$(F)).fill(N);",
    "elementHandle.type": "await (await page.$(F)).type(N);",
    "keyboard.type": "await page.focus(F); await page.keyboard.type(N);",
    "keyboard.insertText": "await page.focus(F); await page.keyboard.insertText(N);",
}


class Hook(unittest.TestCase):
    def test_the_hooks_node_tests_pass(self):
        result = subprocess.run(["node", "--test", "--test-reporter=tap", str(HOOK.with_suffix(".test.js"))], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertRegex(result.stdout, r"(?m)^# pass [1-9]")


class Site:
    """A site with a login on a local port: /auth/token answers 200 to the password and 400 to anything
    else, and keeps every password it was sent."""

    def __init__(self, test):
        sent = self.sent = []

        class Handler(http.server.BaseHTTPRequestHandler):
            def answer(self, status, body=""):
                self.send_response(status)
                self.send_header("Content-Type", "text/html")
                self.send_header("Content-Length", str(len(body.encode())))
                self.end_headers()
                self.wfile.write(body.encode())

            def do_GET(self):
                pages = {"/login": LOGIN, "/home": "<title>site</title><h1>Signed in</h1>", "/favicon.ico": ""}
                self.answer(200 if self.path in pages else 404, pages.get(self.path, ""))

            def do_POST(self):
                sent.append(json.loads(self.rfile.read(int(self.headers["Content-Length"])))["password"])
                self.answer(200 if sent[-1] == PASSWORD else 400)

            def log_message(self, *args):
                pass

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = "http://127.0.0.1:%d" % self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        test.addCleanup(self.server.server_close)
        test.addCleanup(self.server.shutdown)


@unittest.skipUnless(os.environ.get("QAE_MCP"), "needs the installed browser toolchain: QAE_MCP names playwright-mcp's entry point")
class Live(unittest.TestCase):
    def setUp(self):
        self.repo = make_repo(self, {"README.md": "x"})
        self.work = tempfile.mkdtemp(prefix="vv-qae-live-")
        self.addCleanup(shutil.rmtree, self.work, True)
        self.root = os.path.join(self.work, "qae-artifacts")
        os.makedirs(os.path.join(self.root, "qae"))
        # What actions/qae-codex writes from a site step's secrets file, and hands over as --config.
        self.config = os.path.join(self.work, "mcp.json")
        Path(self.config).write_text(json.dumps({"secrets": {NAME: PASSWORD}}))
        self.site = Site(self)

    def explorer(self, hook=True):
        return Explorer(self, self.root, ("--config", self.config) + (("--init-page", str(HOOK)) if hook else ()))

    def sign_in(self, hook=True):
        """One criterion behind the login, walked the way the prompt asks, signing in with RUN_CODE."""
        explorer = self.explorer(hook)
        explorer.call("browser_navigate", url=self.site.url + "/login")
        answer = explorer.call("browser_run_code_unsafe", code=RUN_CODE)
        explorer.call("browser_take_screenshot", filename="qae-artifacts/qae/AC1-step-1.png")
        explorer.call("browser_network_requests")
        explorer.call("browser_console_messages")
        explorer.call("browser_close")
        explorer.close()
        Path(self.root, "qae", "AC1.md").write_text("- step 1: signed in -> saw the page\n")
        return json.dumps(answer)

    def console(self):
        return "".join(log.read_text() for log in sorted(Path(self.root).glob("console-*.log")))

    def run_gate(self):
        return gate("qae-artifacts", self.repo, "--artifacts", self.root, "--site", self.site.url + "/")

    def test_the_name_entered_through_run_code_signs_in(self):
        answer = self.sign_in()
        self.assertEqual(self.site.sent, [PASSWORD])
        self.assertIn(self.site.url + "/home", answer)
        self.assertNotIn("[ERROR]", self.console())
        result = self.run_gate()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        # The session log holds the name the explorer wrote, never the value the browser typed.
        session = "".join(log.read_text() for log in Path(self.root).glob("session-*/session.md"))
        self.assertIn("fill('%s')" % NAME, session)
        self.assertNotIn(PASSWORD, session)

    def test_without_the_hook_the_same_sign_in_sends_the_name_and_fails_the_gate(self):
        # The run that failed: every criterion passed, and the gate refused it on this one line.
        self.sign_in(hook=False)
        self.assertEqual(self.site.sent, [NAME])
        self.assertIn("[ERROR] " + REFUSED % self.site.url, self.console())
        result = self.run_gate()
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("console error outside the allowlist: " + REFUSED % self.site.url, result.stdout)

    def test_every_text_entry_call_types_the_value_and_the_result_shows_the_name(self):
        explorer = self.explorer()
        explorer.call("browser_navigate", url=self.site.url + "/login")
        for label, entry in ENTRIES.items():
            with self.subTest(label):
                code = "async (page) => { const F = %s, N = %s; await page.fill(F, ''); %s return await page.inputValue(F); }" % (
                    json.dumps(FIELD), json.dumps(NAME), entry)
                answer = explorer.call("browser_run_code_unsafe", code=code)
                texts = [part["text"] for part in answer["result"]["content"] if part["type"] == "text"]
                # playwright-mcp shows a secret's value as its name in every tool result, so this is
                # the field holding the value: the bare name would come back as it was written.
                self.assertIn("<secret>%s</secret>" % NAME, "".join(texts))
                self.assertNotIn(PASSWORD, json.dumps(answer))
        explorer.call("browser_close")

    def test_a_failed_entry_names_the_secret_in_its_error_never_the_value(self):
        # Playwright quotes the text it was typing in a failed call's error, and playwright-mcp
        # returns a tool's error unredacted: without the hook's own redaction the explorer would be
        # handed the password by a fill that missed. The second call is the tool that types the
        # value itself, whose error held it before this hook existed.
        explorer = self.explorer()
        explorer.call("browser_navigate", url=self.site.url + "/login")
        missed = {
            "browser_run_code_unsafe": {"code": "async (page) => { await page.locator('h1').fill('%s', { timeout: 2000 }); }" % NAME},
            "browser_fill_form": {"fields": [{"name": "Heading", "type": "textbox", "target": "h1", "value": NAME}]},
        }
        for tool, arguments in missed.items():
            with self.subTest(tool):
                answer = explorer.send("tools/call", {"name": tool, "arguments": arguments})
                self.assertTrue(answer["result"].get("isError"), json.dumps(answer)[:600])
                self.assertIn('fill(\\"<secret>%s</secret>\\")' % NAME, json.dumps(answer))
                self.assertNotIn(PASSWORD, json.dumps(answer))
        explorer.call("browser_close")

    def test_text_that_only_holds_a_name_is_typed_as_written(self):
        explorer = self.explorer()
        explorer.call("browser_navigate", url=self.site.url + "/login")
        answer = explorer.call("browser_run_code_unsafe", code=(
            "async (page) => { await page.fill('input[type=email]', 'my %s'); return await page.inputValue('input[type=email]'); }" % NAME))
        self.assertIn("my %s" % NAME, json.dumps(answer))
        self.assertNotIn("<secret>", json.dumps(answer))
        explorer.call("browser_close")

    def test_the_page_record_is_still_written_beside_it(self):
        # Two --init-page hooks on the Codex lane: playwright-mcp must run both, or the gate loses
        # the record of which page logged an error (tests/test_qae_console_pages.py).
        explorer = self.explorer()
        explorer.call("browser_navigate", url=self.site.url + "/login")
        explorer.call("browser_evaluate", function="() => fetch('/nowhere').then(answer => answer.status)")
        explorer.call("browser_console_messages")
        explorer.call("browser_close")
        explorer.close()
        records = [json.loads(line) for line in Path(self.root, "console-pages.jsonl").read_text().splitlines()]
        self.assertEqual({record["page"] for record in records}, {self.site.url + "/login"})

    def test_the_tool_that_already_resolved_the_name_still_does(self):
        explorer = self.explorer()
        explorer.call("browser_navigate", url=self.site.url + "/login")
        explorer.call("browser_fill_form", fields=[{"name": "Password", "type": "textbox", "target": FIELD, "value": NAME}])
        explorer.call("browser_click", element="Sign in", target="button")
        explorer.call("browser_wait_for", time=1)
        explorer.call("browser_close")
        self.assertEqual(self.site.sent, [PASSWORD])


if __name__ == "__main__":
    unittest.main()
