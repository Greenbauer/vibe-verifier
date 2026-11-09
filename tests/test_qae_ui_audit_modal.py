"""The layout record's modal-dismiss rule (actions/qae-browser/modal-dismiss.js and modal-page.js) in a real browser.

It serves tests/fixtures/ui-audit/modal.html on a local port, one case per address, and loads the hook into real tabs:
each modal open in a tab is replayed in windows of the hook's own, and the explorer's own tab must be left as it was.
Like tests/test_qae_ui_audit.py it needs the installed toolchain, so it runs where QAE_MCP names the playwright-mcp entry
point (the catalog's `qae-browser` CI job). The bookkeeping and the replay's guards are also tested without a browser,
in actions/qae-browser/modal-dismiss.test.js."""
import http.server
import json
import os
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path

from helpers import ROOT

HOOK = ROOT / "actions" / "qae-browser" / "ui-audit.js"
FIXTURES = ROOT / "tests" / "fixtures" / "ui-audit"
# The service worker modal.html registers: installed once per browser context that allows it.
SERVICE_WORKER = b"self.addEventListener('install', event => event.waitUntil(fetch('/sw-installed')));"

MEASURE = r"""
const fs = require("fs"), path = require("path");
const { chromium } = require(require.resolve("playwright", { paths: [process.env.TOOLS] }));
const hook = require(process.env.HOOK);
const source = ["ui-audit-page.js", "name-page.js", "modal-page.js"].map(f => fs.readFileSync(path.join(path.dirname(process.env.HOOK), f), "utf8")).join("\n");
const at = query => process.env.BASE + "/modal.html?" + query;
(async () => {
  const browser = await chromium.launch(process.env.QAE_CHANNEL ? { channel: process.env.QAE_CHANNEL } : {});
  // 1920 wide, so recordWide (a plain load in a 1920 window of its own) has nothing to do, and every request the server
  // counts is the explorer's or a modal replay's.
  const explorer = await browser.newContext({ viewport: { width: 1920, height: 1000 } });
  const out = {};
  // Modals open on load, each replayed from a tab of its own as the hook does after a measure; `entrance` is opened by
  // "Edit lane", which the tab presses and the hook notes, and ignores Escape and backdrop clicks for its entrance.
  const cases = {
    entrance: "opener=1&enter=400&escape=1&backdrop=1&name=Entrance", nativeFull: "opener=1&native=1&keep=1&margin=16&name=Rate+sheet",
    sibling: "sibling=1&name=Choose+a+plan", floating: "overlay=0&name=Floating+note",
    stuck: "later=2000&name=Welcome+tour", closes: "escape=1&backdrop=1&name=Closes", alert: "role=alertdialog&escape=1&name=Discard+changes",
    edited: "edited=1&name=Draft", editedRange: "edited=range&name=Weight", editedColour: "edited=color&name=Colour",
    editedSelect: "edited=select&name=Equipment", editedDays: "edited=days&name=Days", editedBox: "edited=box&name=Remember", native: "native=1&backdrop=1&name=Native", nativeStuck: "native=1&unnamed=1&name=Native+stuck",
    noBackdrop: "overlay=0&escape=1&name=No+backdrop", margin: "margin=16&escape=1&name=Full+window", shell: "shell=1&name=In+app", signIn: "flow=1&name=Log+in",
    decor: "decor=1&name=Sign+in", wrap: "wrap=1&escape=1&name=Share+quote", root: "root=1&escape=1&name=Invite+people",
  };
  await Promise.all(Object.entries(cases).map(async ([name, query]) => {
    const page = await explorer.newPage();
    await page.goto(at(query));
    const pressed = query.includes("opener=1") ? [{ tag: "button", name: "Edit lane", at: "button#opener" }] : [];
    if (pressed.length) await page.click("#opener");
    await page.waitForSelector("[role=dialog], [role=alertdialog], dialog[open]", { state: "attached", timeout: 5000 });
    const found = [];
    await hook.recordModals(page, line => found.push(line), new Set(), { pending: pressed, pages: new Map(), busy: false });
    out[name] = found.map(f => [f.rule, f.text, f.detail]);
    await page.close();
  }));
  // Which press names an opener a window of the hook's own may press again, and finding it there: never a submit button,
  // a press inside a dialog or on the page itself, and of two buttons of one name, the one at the path pressed; of two at
  // one path too, none.
  const page = await explorer.newPage();
  await page.goto(at("name=Openers"));
  out.openers = await page.evaluate(`(() => {\n${source}\n` + `
    const edit = document.querySelectorAll("td button"), more = document.querySelectorAll("section button");
    const save = [...document.querySelectorAll("button")].find(b => b.textContent === "Save lane");
    const found = d => { const el = findOpener(d); return el && cssPath(el); };
    const second = openerOf(edit[1]);
    return [openerOf(save), second.name, found(second), found({ ...second, at: "main > p" }), found(openerOf(more[1])),
      found({ tag: "button", name: "Save lane", at: cssPath(save) }), openerOf(document.querySelector("[role=dialog] button")),
      openerOf(document.body)];\n})()`);
  await page.close();
  // The hook as the explorer loads it, in four tabs: a walkthrough open on load that Escape closes, and three modals
  // "Edit lane" opens, each appearing a different way: added to the page, made aria-modal, opened with showModal().
  // Escape or a backdrop click in the explorer's own tab would close each of them.
  const record = path.join(process.env.OUT, "ui-audit.jsonl");
  process.argv = ["node", "cli.js", "--output-dir", process.env.OUT];
  const tabs = [];
  for (const query of ["escape=1&name=Escape+only", "opener=1&backdrop=1&name=Edit+lane", "opener=1&kept=1&backdrop=1&name=Carrier",
    "opener=1&native=1&name=Rate"]) {
    const tab = await explorer.newPage();
    await hook.default({ page: tab });
    await tab.goto(at(query));
    tabs.push(tab);
  }
  await tabs[0].waitForTimeout(1500);
  for (const tab of tabs.slice(1)) await tab.click("#opener");
  const lines = () => fs.readFileSync(record, "utf8").split("\n").filter(Boolean).map(JSON.parse).filter(x => x.rule === "modal-dismiss");
  for (let waited = 0; waited < 60000 && lines().length < 4; waited += 250) await tabs[0].waitForTimeout(250);
  await tabs[0].waitForTimeout(500);
  out.flow = lines().map(x => [x.text, x.detail, x.width]).sort();
  out.explorerStillOpen = await Promise.all(tabs.map(tab => tab.evaluate(() =>
    [...document.querySelectorAll("[role=dialog], dialog")].some(d => d.checkVisibility()))));
  out.tabs = explorer.pages().length;
  out.contexts = browser.contexts().length;
  await browser.close();
  console.log(JSON.stringify(out));
})().catch(error => { console.error(error); process.exit(1); });
"""


class Server:
    """The fixtures on a local port, counting each write (POST /posted), websocket (GET /socket) and service worker
    install (GET /sw-installed, once per browser context that registers /modal-sw.js) the pages make."""

    def __init__(self):
        self.counts = {"POST /posted": 0, "GET /socket": 0, "GET /sw-installed": 0}
        counts = self.counts

        class Handler(http.server.SimpleHTTPRequestHandler):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, directory=str(FIXTURES), **kwargs)

            def count(self, key, status, body=b"", kind="text/plain"):
                counts[key] += 1
                self.send_response(status)
                self.send_header("Content-Type", kind)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                path = self.path.split("?")[0]
                if path == "/socket":
                    return self.count("GET /socket", 400)
                if path == "/sw-installed":
                    return self.count("GET /sw-installed", 204)
                if path == "/modal-sw.js":
                    self.send_response(200)
                    self.send_header("Content-Type", "text/javascript")
                    self.end_headers()
                    return self.wfile.write(SERVICE_WORKER)
                return super().do_GET()

            def do_POST(self):
                self.rfile.read(int(self.headers.get("Content-Length") or 0))
                return self.count("POST /posted", 204)

            def log_message(self, *args):
                pass

        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.base = "http://127.0.0.1:%d" % self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


@unittest.skipUnless(os.environ.get("QAE_MCP"), "needs the QAE browser toolchain: QAE_MCP names playwright-mcp's entry point")
class Browser(unittest.TestCase):
    def test_a_modal_escape_or_a_backdrop_click_leaves_open_is_found_in_windows_of_the_hooks_own(self):
        server = Server()
        self.addCleanup(server.close)
        out = tempfile.mkdtemp(prefix="vv-ui-audit-modal-")
        env = {**os.environ, "TOOLS": str(Path(os.environ["QAE_MCP"]).resolve().parents[3]),
               "HOOK": str(HOOK), "BASE": server.base, "OUT": out}
        run = subprocess.run(["node", "-e", MEASURE], capture_output=True, text=True, env=env, timeout=240)
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
        measured = json.loads(run.stdout.strip().splitlines()[-1])
        # A walkthrough that appears 2s after the load and that neither Escape nor a backdrop click closes, and a native
        # <dialog> Escape closes by itself but a backdrop click does not (no handler, no closedby).
        self.assertEqual(measured["stuck"], [["modal-dismiss", "Welcome tour", "pressing Escape leaves the dialog open, and so does "
                                              "a click on the backdrop outside it (open on load)"]])
        # Named by its heading, having no aria-labelledby.
        click = "a click on the backdrop outside it leaves the dialog open (open on load)"
        self.assertEqual(measured["nativeStuck"], [["modal-dismiss", "Native stuck", click]])
        # Each kind of modal by itself: a dialog with no aria-modal over a backdrop beside it, judged by that backdrop; an
        # aria-modal dialog with no backdrop, judged by Escape alone; a native <dialog> as large as the window but 16px,
        # with no backdrop point, that Escape leaves open once a press allowed it to.
        self.assertEqual(measured["sibling"], [["modal-dismiss", "Choose a plan", "pressing Escape leaves the dialog open, and "
                                                "so does a click on the backdrop outside it (open on load)"]])
        self.assertEqual(measured["floating"], [["modal-dismiss", "Floating note", "pressing Escape leaves the dialog open (open on load)"]])
        self.assertEqual(measured["nativeFull"], [["modal-dismiss", "Rate sheet",
                                                   'pressing Escape leaves the dialog open (opened by pressing "Edit lane")']])
        # The role on the full-window backdrop holding the card, and on a root of no size whose backdrop and scrolling box
        # are fixed inside it: the card is the box a click lands outside.
        self.assertEqual(measured["wrap"], [["modal-dismiss", "Share quote", click]])
        self.assertEqual(measured["root"], [["modal-dismiss", "Invite people", click]])
        # Closed by both (after an exit animation); an alertdialog a backdrop click leaves open (the choice is the point);
        # dialogs that ignore both but hold edited input (text typed, a slider or colour moved, a select or a select of
        # several choices changed, a box ticked); a native <dialog> closed by both; an aria-modal dialog over the page with no backdrop,
        # and one leaving 16px of backdrop (under 24px, so no click). A role=dialog in a fixed full-window app, one over a
        # fixed background behind an empty page, and an aria-modal sign-in card in the page's flow (a subscribed app's
        # sign-in page marks its card so) are no modals. A modal that ignores both until its 400ms entrance ends is judged
        # once it has settled, as a person sees it.
        for name in ("closes", "alert", "edited", "editedRange", "editedColour", "editedSelect", "editedDays", "editedBox", "native",
                     "noBackdrop", "margin", "shell", "decor", "signIn", "entrance"):
            self.assertEqual(measured[name], [], name)
        # A submit button is never an opener, nor a press inside a dialog or on the page; of two Edit buttons the second is
        # found by its path, a path that names neither finds none, two More buttons at one path find none, and a descriptor
        # naming a submit button finds none.
        self.assertEqual(measured["openers"], [None, "Edit", "table > tbody > tr:nth-of-type(2) > td:nth-of-type(2) > button",
                                               None, None, None, None, None])
        # The hook in the explorer's tabs: each modal is found at the explorer's width, however it appeared, and each is
        # still open there.
        pressed = '(opened by pressing "Edit lane")'
        self.assertEqual(measured["flow"], [
            ["Carrier", "pressing Escape leaves the dialog open " + pressed, 1920],
            ["Edit lane", "pressing Escape leaves the dialog open " + pressed, 1920],
            ["Escape only", "a click on the backdrop outside it leaves the dialog open (open on load)", 1920],
            ["Rate", "a click on the backdrop outside it leaves the dialog open " + pressed, 1920]])
        self.assertEqual(measured["explorerStillOpen"], [True, True, True, True])
        # Every window of the hook's own is closed again: only the explorer's four tabs remain, in its one context.
        self.assertEqual((measured["tabs"], measured["contexts"]), (4, 1))
        # Only the explorer's own five presses wrote and opened a websocket, and only its context installed the service
        # worker: the replays did none of these, though every replay window of a modal it opens pressed "Edit lane".
        self.assertEqual(server.counts, {"POST /posted": 5, "GET /socket": 5, "GET /sw-installed": 1})

if __name__ == "__main__":
    unittest.main()
