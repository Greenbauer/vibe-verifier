"""The QAE harness's width captures (actions/qae-browser/widths.js, installed by the ui-audit hook) in the real
playwright-mcp and chromium: each viewport screenshot the explorer saves is captured at the run's other widths, in
place, inside the same tool call, measured by the layout audit there, and put back, so the explorer walks at one
width and the qae-artifacts gate still finds every criterion at every width. Needs the installed toolchain, like
tests/test_qae_console_pages.py: QAE_MCP names playwright-mcp's entry point (the catalog's `qae-browser` CI job)."""
import http.server
import json
import os
import shutil
import tempfile
import threading
import unittest
from pathlib import Path

from helpers import ROOT, gate, make_repo
from test_qae_console_pages import Explorer, Server

from _acceptance import png_size  # noqa: E402  (gates/, which helpers puts on the path)

UI_AUDIT = ROOT / "actions" / "qae-browser" / "ui-audit.js"
# A dialog a button opens, and a menu another opens: the dialog is a state no address loads; the menu is a popup
# the explorer's next action may use, which a resize could close. Two buttons touch: a layout finding at any width.
# What stays on the page never stops a capture: a menu in the page's flow, a sidebar toggle that says expanded while
# the sidebar shows (a subscribed app's), and a tooltip faded out.
DIALOG = ("<title>site</title><header style='position:fixed;top:0;right:0'><nav><ul role=menu>"
          "<li role=menuitem>Home</li></ul></nav></header><main><nav role=menu><a role=menuitem href=/>Home</a></nav>"
          "<button aria-haspopup=true aria-expanded=true>Collapse navigation</button>"
          "<div role=tooltip style='position:absolute;opacity:0'>Collapse</div>"
          "<div role=tooltip style='position:absolute;width:1px;height:1px;overflow:hidden'>Opens the menu</div><h1>Quotes</h1>"
          "<p><button id=open>Open</button><button id=menu aria-haspopup=menu aria-expanded=false>More</button></p>"
          "<dialog id=d><p>Quote Q-1</p><button id=close>Close</button></dialog>"
          "<div id=m role=menu hidden style='position:absolute'><div role=menuitem>Copy</div></div></main>"
          "<script>const on = (id, act) => document.getElementById(id).addEventListener('click', act);"
          "on('open', () => d.showModal()); on('close', () => d.close());"
          "on('menu', () => { m.hidden = false; menu.setAttribute('aria-expanded', 'true'); });</script>")
# Two bordered buttons side by side, 12px apart, stacked with no gap between them once the window is a phone's: a
# layout defect only the width the explorer never uses shows.
NARROW = ("<title>site</title><style>.row{display:flex;gap:12px;padding:24px}.row button{border:1px solid #333;"
          "border-radius:6px;padding:8px 16px;background:#fff}@media (max-width:500px){.row{flex-direction:column;gap:0}}</style>"
          "<main><h1>Plans</h1><div class=row><button>Monthly plan</button><button>Yearly plan</button></div></main>")

# A form the explorer fills in: a field a framework keeps in sync (as React does, it copies each value into
# defaultValue), a contenteditable editor, and a field the page fills in itself, which is not typing. Send keeps the
# page, as a single-page app's form does.
DRAFT = ("<title>site</title><main><h1>Survey</h1><form><input id=title aria-label=Title>"
         "<div id=editor contenteditable aria-label=Editor></div><input id=prefilled aria-label=Prefilled>"
         "<select id=size aria-label=Size><option>Small</option><option>Large</option></select>"
         "<button id=send type=submit>Send</button></form></main>"
         "<script>title.addEventListener('input', () => { title.defaultValue = title.value; });"
         "prefilled.value = 'set by the page';"
         "document.querySelector('form').addEventListener('submit', event => event.preventDefault());</script>")

# A page that stops drawing frames, as an explorer's browser_evaluate did to freeze an intro animation (2026-11-10).
FROZEN = ("<title>site</title><main><h1>Intro</h1><p>Paused</p></main>"
          "<script>window.requestAnimationFrame = () => 0;</script>")

# A map whose group marker opens a popup near the map's top edge, as Leaflet draws one (`.leaflet-popup`, absolute,
# inside the map's overflow:hidden box), and that refits itself each time the window is resized, as a consumer's
# shipment map does: each refit moves the popup up with its marker. One width round trip carried the popup's Close
# above the map's edge, where the map clips it, and the explorer's next click landed on the page (2026-11-15).
MAP = ("<title>site</title><main><h1>Shipments</h1><div id=map style='position:relative;height:300px;overflow:hidden;"
       "margin-top:40px;background:#acd'><button id=marker style='position:absolute;left:200px;top:200px'>55</button>"
       "<div id=popup class=leaflet-popup hidden style='position:absolute;left:150px;top:10px;width:200px;"
       "background:#fff'><a id=x role=button aria-label='Close popup' href='#close' style='display:block;"
       "text-align:right'>x</a><p>32 containers</p></div></div></main>"
       "<script>marker.onclick = () => { popup.hidden = false; };"
       "x.onclick = event => { event.preventDefault(); popup.hidden = true; };"
       "addEventListener('resize', () => { popup.style.top = (parseInt(popup.style.top) - 40) + 'px'; });</script>")

# A Download link that opens its file in a new tab, as a consumer's shipment files do (target=_blank to an
# attachment): the browser saves the file and closes that tab at once, before the harness's hooks finish setting it
# up. The hooks' setup failing on the closed tab came back as the click's own error, "Target page, context or browser
# has been closed", though the file was saved (2026-11-15).
FILES = ("<title>site</title><main><h1>Files</h1><a id=bl href='{0}/bl.pdf' target=_blank rel=noopener>Download</a>"
         "<a id=cipl href='{0}/cipl.pdf' target=_blank rel=noopener>Download</a></main>")


def file_server(test, files):
    """The origin of a local server that answers each path in `files` with its bytes, saved as an attachment."""
    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            body = files.get(self.path, b"")
            self.send_response(200 if self.path in files else 404)
            self.send_header("Content-Type", "application/pdf")
            self.send_header("Content-Disposition", "attachment; filename=" + self.path.lstrip("/"))
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    test.addCleanup(server.server_close)
    test.addCleanup(server.shutdown)
    return "http://127.0.0.1:%d" % server.server_address[1]


@unittest.skipUnless(os.environ.get("QAE_MCP"), "needs the installed browser toolchain: QAE_MCP names playwright-mcp's entry point")
class Captures(unittest.TestCase):
    def setUp(self):
        self.repo = make_repo(self, {"README.md": "x"})
        self.work = tempfile.mkdtemp(prefix="vv-qae-widths-")
        self.addCleanup(shutil.rmtree, self.work, True)
        self.root = os.path.join(self.work, "qae-artifacts")
        os.makedirs(os.path.join(self.root, "qae"))
        os.makedirs(os.path.join(self.work, "qae-inputs"))
        Path(self.work, "qae-inputs", "widths").write_text("1280\n375\n")
        self.site = Server(self, {"/": DIALOG, "/narrow": NARROW, "/frozen": FROZEN, "/draft": DRAFT, "/map": MAP,
                                  "/map-clipped": MAP.replace("top:10px", "top:-30px"),
                                  "/bad": (ROOT / "tests" / "fixtures" / "ui-audit" / "bad.html").read_text()})
        self.explorer = Explorer(self, self.root, ("--init-page", os.environ.get("QAE_UI_AUDIT", str(UI_AUDIT))))

    def shoot(self, name):
        self.explorer.call("browser_take_screenshot", filename="qae-artifacts/qae/%s.png" % name)

    def record(self):
        return [json.loads(line) for line in Path(self.root, "widths.jsonl").read_text().splitlines()]

    def test_a_dialog_is_captured_at_the_other_width_and_the_explorer_walks_on_at_its_own(self):
        self.explorer.call("browser_navigate", url=self.site.url + "/")
        self.explorer.call("browser_click", element="Open", target="#open")
        self.shoot("AC1-step-1")
        # The dialog is still open at the explorer's width: its Close button works.
        self.explorer.call("browser_click", element="Close", target="#close")
        self.shoot("AC1-step-2")
        # A page with layout defects: the audit measures it at 375 too, though the explorer never left 1280.
        self.explorer.call("browser_navigate", url=self.site.url + "/bad")
        self.shoot("AC1-step-3")
        self.explorer.call("browser_close")
        self.explorer.close()
        Path(self.root, "qae", "AC1.md").write_text("- step 1: opened the quote -> the dialog\n- step 2: closed it -> the list\n"
                                                    "- step 3: opened the other page -> its form\n")
        captures = self.record()
        self.assertEqual([(entry["width"], entry["disturbed"]) for entry in captures], [(375, False)] * 3)
        for entry in captures:
            self.assertEqual(png_size(os.path.join(self.root, entry["file"]))[0], 375)
        self.assertEqual(png_size(os.path.join(self.root, "qae", "AC1-step-2.png"))[0], 1280)
        audit = [json.loads(line) for line in Path(self.root, "ui-audit.jsonl").read_text().splitlines()]
        self.assertIn(375, {line["width"] for line in audit})
        result = gate("qae-artifacts", self.repo, "--artifacts", self.root, "--site", self.site.url + "/", "--widths", "1280,375")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_a_step_its_own_code_saves_is_captured_and_counts_for_its_criterion(self):
        # An explorer walking in browser_run_code_unsafe saves each step with page.screenshot({path}). The hook
        # skipped every screenshot with a path, so such a criterion had no capture at 375 and the gate refused it
        # (a consumer, 2026-11-10). A file of the script's own that is no step's screenshot is still left alone.
        self.explorer.call("browser_navigate", url=self.site.url + "/")
        self.explorer.call("browser_run_code_unsafe", code=(
            "async page => { await page.click('#open');"
            " await page.screenshot({ path: 'qae-artifacts/qae/AC1-step-1.png', fullPage: true });"
            " await page.click('#close');"
            " await page.screenshot({ path: 'qae-artifacts/qae/AC1-step-2.png' });"
            " await page.screenshot({ path: 'qae-artifacts/row.png' }); }"))
        self.explorer.call("browser_close")
        self.explorer.close()
        Path(self.root, "qae", "AC1.md").write_text("- step 1: opened the quote -> the dialog\n- step 2: closed it -> the list\n")
        captures = self.record()
        self.assertEqual([(entry["width"], entry["disturbed"]) for entry in captures], [(375, False)] * 2)
        for entry in captures:
            self.assertEqual(png_size(os.path.join(self.root, entry["file"]))[0], 375)
        self.assertEqual(png_size(os.path.join(self.root, "qae", "AC1-step-1.png"))[0], 1280)
        result = gate("qae-artifacts", self.repo, "--artifacts", self.root, "--site", self.site.url + "/", "--widths", "1280,375")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_a_defect_only_a_phone_width_shows_is_found_though_the_explorer_never_left_1280(self):
        self.explorer.call("browser_navigate", url=self.site.url + "/narrow")
        self.shoot("AC1-step-1")
        self.explorer.call("browser_close")
        self.explorer.close()
        audit = [json.loads(line) for line in Path(self.root, "ui-audit.jsonl").read_text().splitlines()]
        touching = {line["width"] for line in audit if line["rule"] == "touching"}
        self.assertEqual(touching, {375}, audit)

    def test_an_open_menu_is_not_resized_under_the_explorer(self):
        self.explorer.call("browser_navigate", url=self.site.url + "/")
        self.explorer.call("browser_click", element="More", target="#menu")
        self.shoot("AC1-step-1")
        self.explorer.call("browser_close")
        self.explorer.close()
        self.assertEqual([entry.get("skipped") for entry in self.record()], ["a menu, listbox, tooltip or map popup was open"])
        self.assertFalse(Path(self.root, "widths").exists())

    def test_a_map_popup_whose_close_the_map_clips_still_cannot_be_closed(self):
        # The skip keeps the explorer's own page as it left it; it never hides a popup whose Close the map really
        # clips: after the step's screenshot that click still fails, at the explorer's own width.
        self.explorer.call("browser_navigate", url=self.site.url + "/map-clipped")
        self.explorer.call("browser_click", element="55", target="#marker")
        self.shoot("AC1-step-1")
        with self.assertRaisesRegex(AssertionError, "intercepts pointer events"):
            self.explorer.call("browser_click", element="Close popup", target="#x")

    def test_an_open_map_popup_is_not_resized_under_the_explorer(self):
        # A resize refits a map, which moves its popup with the marker; the round trip left the popup's Close
        # clipped by the map, and the explorer could not close the popup it had just opened (a consumer, 2026-11-15).
        self.explorer.call("browser_navigate", url=self.site.url + "/map")
        self.explorer.call("browser_click", element="55", target="#marker")
        self.shoot("AC1-step-1")
        self.explorer.call("browser_click", element="Close popup", target="#x")
        hidden = self.explorer.call("browser_evaluate", function="() => document.getElementById('popup').hidden")
        self.assertIn("true", json.dumps(hidden))
        self.explorer.call("browser_close")
        self.explorer.close()
        self.assertEqual([entry.get("skipped") for entry in self.record()], ["a menu, listbox, tooltip or map popup was open"])

    def test_a_download_that_opens_and_closes_a_tab_is_a_click_that_worked(self):
        # The first tab is set up by both hooks' usual work, where the layout hook failed; the second as one explorer
        # of a split run (qae-inputs/isolation.json), where the page record's request guard failed first.
        files = file_server(self, {"/bl.pdf": b"%PDF-1.4 bl", "/cipl.pdf": b"%PDF-1.4 cipl"})
        self.site.pages["/files"] = FILES.format(files)
        self.explorer.call("browser_navigate", url=self.site.url + "/files")
        self.assertIn("Downloaded file bl.pdf", json.dumps(self.explorer.call("browser_click", element="Download", target="#bl")))
        Path(self.work, "qae-inputs", "isolation.json").write_text(json.dumps({"tag": "qae-pr-1-1"}))
        self.assertIn("Downloaded file cipl.pdf", json.dumps(self.explorer.call("browser_click", element="Download", target="#cipl")))
        self.explorer.call("browser_close")
        self.explorer.close()
        self.assertEqual(Path(self.root, "bl.pdf").read_bytes(), b"%PDF-1.4 bl")
        self.assertEqual(Path(self.root, "cipl.pdf").read_bytes(), b"%PDF-1.4 cipl")

    def test_a_typed_unsent_draft_is_not_resized_until_the_form_is_sent(self):
        # A resize can make a page render its route again and drop the draft (a consumer's survey builder,
        # 2026-11-10), so a screen holding typed input is not captured; one a script filled in still is.
        self.explorer.call("browser_navigate", url=self.site.url + "/draft")
        self.shoot("AC1-step-1")
        self.explorer.call("browser_type", element="Title", target="#title", text="Where to eat")
        self.shoot("AC1-step-2")
        # Send ends the draft: the form's fields are captured again, and a select's option is a choice, not a draft.
        self.explorer.call("browser_click", element="Send", target="#send")
        self.explorer.call("browser_select_option", element="Size", target="#size", values=["Large"])
        self.shoot("AC1-step-3")
        # A contenteditable editor holds a draft too.
        self.explorer.call("browser_type", element="Editor", target="#editor", text="Bring snacks")
        self.shoot("AC1-step-4")
        self.explorer.call("browser_close")
        self.explorer.close()
        self.assertEqual([entry.get("skipped") for entry in self.record()],
                         [None, "a text field held unsent input", None, "a text field held unsent input"])
        self.assertEqual(png_size(os.path.join(self.root, "qae", "AC1-step-2.png"))[0], 1280)

    def test_a_page_that_draws_no_frame_is_captured_and_the_screenshot_returns(self):
        # The capture waited for two of the page's frames with no bound, so this screenshot never returned.
        self.explorer.call("browser_navigate", url=self.site.url + "/frozen")
        hung = threading.Timer(60, self.explorer.process.kill)   # a hang fails the test instead of the job
        hung.start()
        try:
            self.shoot("AC1-step-1")
        finally:
            hung.cancel()
        self.explorer.call("browser_close")
        self.explorer.close()
        self.assertEqual([entry["width"] for entry in self.record()], [375])
        self.assertEqual(png_size(os.path.join(self.root, self.record()[0]["file"]))[0], 375)

    def test_without_a_second_width_nothing_is_captured(self):
        Path(self.work, "qae-inputs", "widths").write_text("1280\n")
        self.explorer.close()
        self.explorer = Explorer(self, self.root, ("--init-page", os.environ.get("QAE_UI_AUDIT", str(UI_AUDIT))))
        self.explorer.call("browser_navigate", url=self.site.url + "/")
        self.shoot("AC1-step-1")
        self.explorer.call("browser_close")
        self.explorer.close()
        self.assertFalse(Path(self.root, "widths.jsonl").exists())


if __name__ == "__main__":
    unittest.main()
