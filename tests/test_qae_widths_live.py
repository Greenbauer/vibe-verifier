"""The QAE harness's width captures (actions/qae-browser/widths.js, installed by the ui-audit hook) in the real
playwright-mcp and chromium: each viewport screenshot the explorer saves is captured at the run's other widths, in
place, inside the same tool call, measured by the layout audit there, and put back, so the explorer walks at one
width and the qae-artifacts gate still finds every criterion at every width. Needs the installed toolchain, like
tests/test_qae_console_pages.py: QAE_MCP names playwright-mcp's entry point (the catalog's `qae-browser` CI job)."""
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

# A page that stops drawing frames, as an explorer's browser_evaluate did to freeze an intro animation (2026-11-10).
FROZEN = ("<title>site</title><main><h1>Intro</h1><p>Paused</p></main>"
          "<script>window.requestAnimationFrame = () => 0;</script>")


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
        self.site = Server(self, {"/": DIALOG, "/narrow": NARROW, "/frozen": FROZEN,
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
        self.assertEqual([entry.get("skipped") for entry in self.record()], ["a menu, listbox or tooltip was open"])
        self.assertFalse(Path(self.root, "widths").exists())

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
