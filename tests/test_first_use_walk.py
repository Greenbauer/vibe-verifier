"""The first-use walk (harnesses/first-use/): its prompt, written from the body's `## First-use task` section
(gates/_first_use.py), and the first-use-walk gate that judges what the walk left (gates/first_use_walk.py).

`Gate` drives the gate on evidence shaped like playwright-mcp's session log. `Live` walks a site on a local port with
the pinned playwright-mcp the way the walk's session does, and runs the gate on what that left: it needs the installed
toolchain, so it runs where QAE_MCP names the playwright-mcp entry point (the catalog's `qae-browser` CI job)."""
import http.server
import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path

from helpers import gate, make_repo, png, runner
from test_qae_console_pages import Explorer

from _first_use import task  # noqa: E402  (gates/ is on the path once helpers is imported)

SITE = "http://localhost:3000"
TASK = "Give someone at Acme access to the portal, then get back to the customer list."
BODY = "## Summary\n\nPortal access.\n\n## First-use task\n\n%s\n\n## Acceptance criteria\n\n- An admin can invite\n" % TASK


def call(name, args, result):
    return "### Tool call: %s\n- Args\n```json\n%s\n```\n- Result\n```json\n%s\n```\n\n" % (
        name, json.dumps(args), json.dumps(result))


def at(path, name="browser_click", **args):
    return call(name, args or {"element": "a link", "target": "e3"}, {"page": "- Page URL: %s%s\n- Page Title: Portal" % (SITE, path)})


def shot(k):
    path = "qae-first-use/step-%d.png" % k
    return call("browser_take_screenshot", {"filename": path}, {"result": "- [Screenshot](%s)" % path})


# A subscribed app's customer-access pull request, walked cold: the customer card has a title and no breadcrumb, the dialog shows a "Send invite"
# choice beside the "Send invite" submit, a customer's address is refused with no reason, and the only way back to the
# list is the site's menu.
STUCK_STEPS = [
    ("step 1: start: the dashboard -> Dashboard, with Customers in the menu", "/", at("/", "browser_navigate", url=SITE + "/")),
    ("step 2: clicked Customers in the menu -> the customer list", "/customers", at("/customers")),
    ("step 3: clicked Acme -> the Acme card, headed Customers, with no breadcrumb", "/customers/7", at("/customers/7")),
    ("step 4: clicked Add someone, typed dana@acme.com and clicked Send invite -> two blue Send invite buttons; the "
     "first was the choice, not the submit", "/customers/7", at("/customers/7")),
    ("step 5: clicked the other Send invite -> a red toast: Failed to send invite email", "/customers/7", at("/customers/7")),
    ("step 6: back: looked for a way back to the list on the card and found none -> still on the Acme card",
     "/customers/7", at("/customers/7")),
]
STUCK_VERDICT = (
    "first-use: STUCK -- the invite failed and the message did not say why (walk.md::%s)\n"
    "first-use-back: STUCK -- the card has no way back to the customer list but the menu (walk.md::%s)\n"
    "first-use-snag: two buttons both read Send invite and only one sends it (walk.md::%s)\n" % (
        STUCK_STEPS[4][0], STUCK_STEPS[5][0], STUCK_STEPS[3][0]))
# The same task on the fixed screens: a breadcrumb back, a radio choice, an invite that goes.
DONE_STEPS = STUCK_STEPS[:3] + [
    ("step 4: clicked Add someone, typed dana@acme.com, kept Send an invite and clicked Send invite -> Invite sent to "
     "dana@acme.com", "/customers/7", at("/customers/7")),
    ("step 5: clicked Customers in the breadcrumb -> the customer list", "/customers", at("/customers")),
    ("step 6: back: clicked Dashboard in the breadcrumb -> the dashboard", "/", at("/")),
]
DONE_VERDICT = ("first-use: DONE -- Dana was invited to Acme (walk.md::%s)\n"
                "first-use-back: DONE -- the breadcrumbs led back to the list and the dashboard (walk.md::%s)\n" % (
                    DONE_STEPS[3][0], DONE_STEPS[5][0]))


class Gate(unittest.TestCase):
    def evidence(self, steps, verdict, session=None, body=BODY, shots=None):
        self.repo = make_repo(self, {"README.md": "x\n"})
        self.work = tempfile.mkdtemp(prefix="vv-first-use-")
        self.addCleanup(shutil.rmtree, self.work, True)
        walk = Path(self.work, "qae-artifacts", "first-use")
        (walk / "session-1").mkdir(parents=True)
        (walk / "walk.md").write_text("".join("- %s\n" % line for line, _, _ in steps))
        (walk / "verdict.md").write_text(verdict)
        for k in shots if shots is not None else range(1, len(steps) + 1):
            (walk / ("step-%d.png" % k)).write_bytes(png(1280, 800))
        log = session if session is not None else "".join(action + shot(k) for k, (_, _, action) in enumerate(steps, 1))
        (walk / "session-1" / "session.md").write_text(log)
        Path(self.work, "pr-body.md").write_text(body)

    def run_gate(self):
        return gate("first-use-walk", self.repo, "--artifacts", os.path.join(self.work, "qae-artifacts"),
                    "--body", os.path.join(self.work, "pr-body.md"))

    def test_the_walk_of_the_customer_access_screens_fails_on_each_thing_a_person_new_to_it_tripped_on(self):
        self.evidence(STUCK_STEPS, STUCK_VERDICT)
        result = self.run_gate()
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("could not finish the task: the invite failed and the message did not say why", result.stdout)
        self.assertIn("could not get back to where it started: the card has no way back", result.stdout)
        self.assertIn("tripped: two buttons both read Send invite", result.stdout)
        self.assertIn("ended on %s/customers/7, not where it started (%s)" % (SITE, SITE), result.stdout)

    def test_the_same_task_on_the_fixed_screens_passes(self):
        self.evidence(DONE_STEPS, DONE_VERDICT)
        result = self.run_gate()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_back_or_a_typed_address_after_the_start_fails_though_the_verdict_says_done(self):
        typed = [(line, path, at(path, "browser_navigate_back") if k == 6 else action)
                 for k, (line, path, action) in enumerate(DONE_STEPS, 1)]
        self.evidence(typed, DONE_VERDICT)
        result = self.run_gate()
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("called browser_navigate_back, which no person can do", result.stdout)

    def test_a_walk_that_ends_away_from_its_start_fails_though_the_verdict_says_done(self):
        steps = DONE_STEPS[:5] + [("step 6: back: clicked Customers -> the customer list", "/customers", at("/customers"))]
        self.evidence(steps, DONE_VERDICT.replace(DONE_STEPS[5][0], steps[5][0]))
        result = self.run_gate()
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("ended on %s/customers, not where it started (%s)" % (SITE, SITE), result.stdout)

    def test_a_verdict_must_cite_a_step_the_walk_has_and_name_each_outcome_once(self):
        self.evidence(DONE_STEPS, DONE_VERDICT.replace(DONE_STEPS[3][0], "step 4: invited Dana"))
        result = self.run_gate()
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("cites a step walk.md does not have", result.stdout)
        self.assertIn("0 `first-use:` lines anchored to the walk, not one", result.stdout)

    def test_a_step_without_its_screenshot_and_a_walk_without_start_or_back_fail(self):
        steps = [(line.replace("start: ", "").replace("back: ", ""), path, action) for line, path, action in DONE_STEPS]
        self.evidence(steps, DONE_VERDICT.replace("back: ", ""), shots=range(1, 6))
        result = self.run_gate()
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("step 6 has no screenshot", result.stdout)
        self.assertIn("step 1 is not its start", result.stdout)
        self.assertIn("last step is not its way back", result.stdout)

    def test_criteria_and_no_task_is_a_finding_and_nothing_to_walk_needs_none(self):
        self.evidence(DONE_STEPS, DONE_VERDICT, body=BODY.replace("## First-use task\n\n%s\n\n" % TASK, ""))
        result = self.run_gate()
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("no `## First-use task`", result.stdout)
        for body in ("## Acceptance criteria\n\n- None: a pin bump\n", "Bumps a dependency.\n"):
            Path(self.work, "pr-body.md").write_text(body)
            self.assertEqual(self.run_gate().returncode, 0, body)

    def test_a_task_with_no_walk_or_no_session_log_cannot_run(self):
        self.evidence(DONE_STEPS, DONE_VERDICT, session="")
        result = self.run_gate()
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn("no session log", result.stderr)
        os.remove(os.path.join(self.work, "qae-artifacts", "first-use", "verdict.md"))
        result = self.run_gate()
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn("the walk did not run or did not finish", result.stderr)


class Prompt(unittest.TestCase):
    def test_the_task_is_the_sections_text_up_to_the_next_heading_without_comments(self):
        self.assertEqual(task(BODY.replace(TASK, "<!-- from the plan -->\n" + TASK)), TASK)
        self.assertEqual(task("## First-use task\n\n<!-- say it here -->\n\n## Acceptance criteria\n- a\n"), "")
        self.assertEqual(task("## Summary\n\nx\n"), "")

    def test_the_prompt_holds_the_task_and_the_site_and_nothing_of_the_criteria(self):
        work = tempfile.mkdtemp(prefix="vv-first-use-prompt-")
        self.addCleanup(shutil.rmtree, work, True)
        body = Path(work, "pr-body.md")
        body.write_text(BODY.replace("## First-use task\n\n", "## First-use task\n\n<!-- quoted from the plan -->\n"))
        out = Path(work, "prompt.md")
        result = runner("first-use-prompt", "--body", str(body), "--site", SITE, "--out", str(out))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(result.stdout.strip(), "task: 15 words")
        text = out.read_text()
        self.assertIn("\n" + TASK + "\n", text)
        self.assertIn("The site is running at %s." % SITE, text)
        for absent in ("An admin can invite", "quoted from the plan", "TASK", "SITE_URL", "Acceptance criteria"):
            self.assertNotIn(absent, text)
        body.write_text("## Acceptance criteria\n\n- An admin can invite\n")
        out.unlink()
        result = runner("first-use-prompt", "--body", str(body), "--site", SITE, "--out", str(out))
        self.assertEqual((result.returncode, result.stdout.strip(), out.exists()), (0, "task: none", False))


MENU = '<nav><a href="/customers">Customers</a></nav>'
# The customer-access screens as first built: the customer card is headed Customers and has no breadcrumb. The fix gives it one.
BROKEN = {
    "/": "<title>Home</title>" + MENU + "<h1>Dashboard</h1>",
    "/customers": '<title>Customers</title>' + MENU + '<p><a href="/">Dashboard</a> / Customers</p>'
                  '<ul><li><a href="/customers/7">Acme</a></li></ul>',
    "/customers/7": "<title>Acme</title>" + MENU + "<h1>Customers</h1>",
}
FIXED = dict(BROKEN, **{"/customers/7": '<title>Acme</title>' + MENU + '<p><a href="/">Dashboard</a> / '
                                         '<a href="/customers">Customers</a> / Acme</p>'})
LINES = ["step 1: start: the dashboard -> Dashboard", "step 2: clicked Customers -> the list",
         "step 3: clicked Acme -> the card", "step 4: left the card -> the list",
         "step 5: back: clicked Dashboard -> the dashboard"]


class FirstUse(Explorer):
    """The walk's playwright-mcp, as actions/qae-codex starts it with `artifacts: qae-first-use`: its records go there."""

    def __init__(self, test, work):
        self.calls = 0
        self.process = subprocess.Popen(
            ["node", os.environ["QAE_MCP"], "--browser", "chromium", "--headless", "--isolated", "--output-dir",
             "qae-first-use", "--save-session", "--viewport-size", "1280x800"],
            cwd=work, stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
        test.addCleanup(self.close)
        self.send("initialize", {"protocolVersion": "2025-03-26", "capabilities": {}, "clientInfo": {"name": "test", "version": "0"}})
        self.process.stdin.write(json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}) + "\n")

    def close(self):
        if not self.process.stdin.closed:
            super().close()

    def click(self, name, last=False):
        """Click the link named `name` by its snapshot ref, as the walk does: the first such link, or the last (a
        breadcrumb's, after the menu's)."""
        text = self.call("browser_snapshot")["result"]["content"][0]["text"]
        refs = re.findall(r'link "%s" \[ref=((?:f[0-9]+)?e[0-9]+)\]' % re.escape(name), text)
        self.call("browser_click", element=name, target=refs[-1] if last else refs[0])


@unittest.skipUnless(os.environ.get("QAE_MCP"), "needs the installed browser toolchain: QAE_MCP names playwright-mcp's entry point")
class Live(unittest.TestCase):
    """The pinned playwright-mcp, walked as the first-use session walks, on a site on a local port."""

    def serve(self, pages):
        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                body = pages.get(self.path, "").encode()
                self.send_response(200 if self.path in pages else 404)
                self.send_header("Content-Type", "text/html")
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return "http://127.0.0.1:%d" % server.server_address[1]

    def walk(self, pages, leave):
        """Start on the dashboard, open Acme's card from the list, leave it with `leave` (a call that clicks the
        breadcrumb or goes Back), then click Dashboard, and run the gate on the files that left, placed where the
        verify job places the walk's artifact."""
        site, work = self.serve(pages), tempfile.mkdtemp(prefix="vv-first-use-live-")
        self.addCleanup(shutil.rmtree, work, True)
        walk = FirstUse(self, work)
        walk.call("browser_navigate", url=site + "/")
        for k, act in enumerate([None, lambda: walk.click("Customers"), lambda: walk.click("Acme"), lambda: leave(walk),
                                 lambda: walk.click("Dashboard")], 1):
            if act:
                act()
            walk.call("browser_take_screenshot", filename="qae-first-use/step-%d.png" % k)
        walk.call("browser_close")
        walk.close()
        left = Path(work, "qae-first-use")
        (left / "walk.md").write_text("".join("- %s\n" % line for line in LINES))
        (left / "verdict.md").write_text("first-use: DONE -- opened Acme (walk.md::%s)\n"
                                         "first-use-back: DONE -- back at the dashboard (walk.md::%s)\n" % (LINES[2], LINES[4]))
        Path(work, "qae-artifacts").mkdir()
        shutil.move(str(left), os.path.join(work, "qae-artifacts", "first-use"))
        Path(work, "pr-body.md").write_text(BODY)
        return gate("first-use-walk", make_repo(self, {"README.md": "x\n"}), "--artifacts", os.path.join(work, "qae-artifacts"),
                    "--body", os.path.join(work, "pr-body.md"))

    def test_a_walk_back_by_the_cards_breadcrumb_passes(self):
        result = self.walk(FIXED, lambda walk: walk.click("Customers", last=True))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_a_card_left_by_the_browsers_back_fails(self):
        result = self.walk(BROKEN, lambda walk: walk.call("browser_navigate_back"))
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("called browser_navigate_back, which no person can do", result.stdout)
