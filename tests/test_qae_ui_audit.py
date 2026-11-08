"""The QAE harness's layout record (actions/qae-browser/ui-audit.js and the rule files it runs) in a real browser.

`Browser` measures the fixtures in tests/fixtures/ui-audit/ in a real chromium, and loads the hook into a real page: it
needs the installed toolchain, so it runs where QAE_MCP names the playwright-mcp entry point (the catalog's
`qae-browser` CI job, after actions/qae-browser). QAE_CHANNEL=chrome runs it on a machine with Google Chrome
and no Playwright chromium. The ui-audit gate that judges the record, and the hook's node tests, are
tests/test_ui_audit.py."""
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from helpers import ROOT

HOOK = ROOT / "actions" / "qae-browser" / "ui-audit.js"
FIXTURES = ROOT / "tests" / "fixtures" / "ui-audit"
RULES = ["clipped-text", "clipped-value", "cramped-field", "duplicate-name", "edge", "field-stagger", "glyph-off-center",
         "icon-off-center", "long-sentence", "raw-value", "row-height", "row-stagger", "spinner-off-center", "table-scroll", "text-wall",
         "touching", "uneven-gutter", "wrapped-label"]


MEASURE = r"""
const { chromium } = require(require.resolve("playwright", { paths: [process.env.TOOLS] }));
const hook = require(process.env.HOOK);
(async () => {
  const browser = await chromium.launch(process.env.QAE_CHANNEL ? { channel: process.env.QAE_CHANNEL } : {});
  const out = {};
  for (const name of ["bad", "good"]) {
    const page = await browser.newPage({ viewport: { width: 1280, height: 900 } });
    await page.goto("file://" + process.env.FIXTURES + "/" + name + ".html");
    const found = await page.evaluate(hook.script);
    // icon-off-center reads screenshots of the window: make it as tall as the page, so every control is in it.
    await page.setViewportSize({ width: 1280, height: await page.evaluate(() => document.documentElement.scrollHeight) });
    const icons = [];
    await hook.recordIcons(page, f => icons.push(f), new Set(), new Set(), Infinity);
    out[name] = [...new Set([...found, ...icons].map(f => f.rule))].sort();
    out[name + "-cramped-field"] = found.filter(f => f.rule === "cramped-field").map(f => f.text || f.at).sort();
    out[name + "-cramped-labels"] = found.filter(f => f.rule === "cramped-field" && f.other).map(f => f.other).sort();
    out[name + "-icon-off-center"] = icons.map(f => f.text).sort();
    out[name + "-table-scroll"] = found.filter(f => f.rule === "table-scroll").length;
    out[name + "-uneven-gutter"] = found.filter(f => f.rule === "uneven-gutter").length;
    out[name + "-details"] = found.filter(f => ["raw-value", "touching", "glyph-off-center", "duplicate-name", "wrapped-label"].includes(f.rule)).map(f => f.rule + ": " + f.text + (f.rule === "duplicate-name" ? " / " + f.other : ""));
    await page.close();
    // At a phone's width a wide table has no room to take, so it may scroll.
    const phone = await browser.newPage({ viewport: { width: 375, height: 800 } });
    await phone.goto("file://" + process.env.FIXTURES + "/" + name + ".html");
    const small = await phone.evaluate(hook.script);
    out[name + "-375-table-scroll"] = small.filter(f => f.rule === "table-scroll").length;
    out[name + "-375-uneven-gutter"] = small.filter(f => f.rule === "uneven-gutter").length;
    await phone.close();
  }
  // The hook in a real page: it measures after the load, and again the moment a button gains a spinner.
  const page = await browser.newPage({ viewport: { width: 1280, height: 900 } });
  process.argv = ["node", "cli.js", "--output-dir", process.env.OUT];
  await hook.default({ page });
  await page.goto("file://" + process.env.FIXTURES + "/good.html");
  await page.waitForTimeout(1500);
  await page.evaluate(() => {
    const button = document.querySelector("button");
    button.style.height = "48px";
    const spinner = document.createElement("span");
    spinner.className = "loading";
    spinner.style.cssText = "display:inline-block;width:10px;height:10px;position:relative;top:8px";
    button.prepend(spinner);
  });
  // The spinner's measure starts at once but may queue behind the load's: wait for its line, not a fixed time.
  const recorded = require("path").join(process.env.OUT, "ui-audit.jsonl");
  const waitFor = async text => {
    for (let waited = 0; waited < 5000; waited += 100) {
      if (require("fs").readFileSync(recorded, "utf8").includes(text)) return true;
      await page.waitForTimeout(100);
    }
    return false;
  };
  out.measured = [await waitFor("spinner-off-center")];
  // A button whose label is swapped for a spinner of three tilted orbits: measured at once, read from its pixels.
  await page.evaluate(() => {
    const style = document.createElement("style");
    style.textContent = ".loader{position:relative;display:inline-block;width:16px;height:16px;perspective:800px}" +
      ".orbit{position:absolute;left:0;top:0;box-sizing:border-box;width:100%;height:100%;border-radius:50%}" +
      ".o1{border-bottom:1.5px solid #fff;transform:rotateX(35deg) rotateY(-70deg) rotateZ(0deg)}" +
      ".o2{border-right:1.5px solid #fff;transform:rotateX(50deg) rotateY(10deg) rotateZ(0deg)}" +
      ".o3{border-top:1.5px solid #fff;transform:rotateX(35deg) rotateY(70deg) rotateZ(0deg)}";
    document.head.append(style);
    const open = [...document.querySelectorAll("button")].find(b => b.textContent === "Open");
    open.style.width = open.offsetWidth + "px";
    open.innerHTML = '<span class=loader><i class="orbit o1"></i><i class="orbit o2"></i><i class="orbit o3"></i></span>';
  });
  out.measured.push(await waitFor("icon-off-center"));
  // What the explorer types, a field a ticked box shows, and a field that appears are each measured, with no load.
  await page.fill('[aria-label="Reason code"]', "NB01, agreed with the buyer on the call");
  out.measured.push(await waitFor('"Reason code"'));
  await page.check("#nobid");
  out.measured.push(await waitFor("No-bid reason, line 4"));
  await page.evaluate(() => {
    const field = document.createElement("input");
    field.setAttribute("aria-label", "Comment, line 5");
    field.style.width = "80px";
    document.querySelector(".fields").append(field);
  });
  out.measured.push(await waitFor("Comment, line 5"));
  // A page whose table scrolls beside room only in a wide window: walked at 1280, the hook measures it again at 1920.
  const wideOut = process.env.OUT + "-wide";
  const narrow = await browser.newPage({ viewport: { width: 1280, height: 900 } });
  process.argv = ["node", "cli.js", "--output-dir", wideOut];
  await hook.default({ page: narrow });
  await narrow.goto("file://" + process.env.FIXTURES + "/wide.html");
  const wideRecord = require("path").join(wideOut, "ui-audit.jsonl");
  for (let waited = 0; waited < 15000 && !require("fs").readFileSync(wideRecord, "utf8").includes("table-scroll"); waited += 200) {
    await narrow.waitForTimeout(200);
  }
  out.wide = require("fs").readFileSync(wideRecord, "utf8").split("\n").filter(Boolean).map(JSON.parse).map(x => [x.rule, x.width]);
  out.pages = browser.contexts().reduce((n, c) => n + c.pages().length, 0);
  await browser.close();
  console.log(JSON.stringify(out));
})().catch(error => { console.error(error); process.exit(1); });
"""


@unittest.skipUnless(os.environ.get("QAE_MCP"), "needs the QAE browser toolchain: QAE_MCP names playwright-mcp's entry point")
class Browser(unittest.TestCase):
    def test_every_rule_finds_its_defect_and_the_same_screens_done_right_are_clean(self):
        out = tempfile.mkdtemp(prefix="vv-ui-audit-")
        env = {**os.environ, "TOOLS": str(Path(os.environ["QAE_MCP"]).resolve().parents[3]),
               "HOOK": str(HOOK), "FIXTURES": str(FIXTURES), "OUT": out}
        run = subprocess.run(["node", "-e", MEASURE], capture_output=True, text=True, env=env, timeout=120)
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
        measured = json.loads(run.stdout.strip().splitlines()[-1])
        self.assertEqual(measured["bad"], RULES)
        # good.html's two-column dashboard, each column a stack of titled cards, holds a select a card down on the left and a
        # field in the first card on the right: a page section, not one form row, so field-stagger does not compare them.
        self.assertEqual(measured["good"], [])
        # A raw value in a heading cell or a button counts; a square button touching a rounded one is no segmented group,
        # and neither is a padded card holding two buttons.
        for expected in ("raw-value: undefined", "raw-value: 3f2b1c4d-1111-4abc-9def-0123456789ab", "raw-value: Status: in_review", "raw-value: Owner: [object Object]",
                         "touching: Activate", "touching: Square", "touching: Save"):
            self.assertIn(expected, measured["bad-details"])
        # A count badge's digit, an svg check, a CSS-drawn tick and avatar initials, each off its circle's middle. good.html has them
        # centered, ticks sized content-box, placed by the translate and rotate properties or with a margin, a switch's knob
        # and an empty avatar's status dot (fills, not marks) and large tabular digits, and none is flagged.
        self.assertEqual(sorted(x for x in measured["bad-details"] if x.startswith("glyph-off-center")),
                         ["glyph-off-center: ", "glyph-off-center: 6", "glyph-off-center: LT", "glyph-off-center: Pick"])
        # The subscribed app's dialog: a pressed "Send invite" choice and the "Send invite" submit, and a form whose two buttons
        # differ by an added word. good.html makes the choice a pair of radios, and its rows' Remove buttons, told apart
        # by their rows, and a disabled twin are not flagged.
        self.assertEqual(sorted(x for x in measured["bad-details"] if x.startswith("duplicate-name")),
                         ["duplicate-name: Create account / Create account now", "duplicate-name: Send invite / Send invite"])
        # The subscribed app's margin pill, its arrow above "12.5%", and a button whose short label wraps, in a system font stack
        # that ends in "Segoe UI Symbol", as Tailwind's and Bootstrap's do (no icon font). good.html keeps the pill on
        # one line, puts an upload button's icon (an svg, then an icon font's ligature) above its label by design, has a pill of a
        # sentence, which may wrap, and an icon button whose name only a screen reader reads.
        # A table that scrolls beside 890px of empty card is flagged; good.html's tables wider than the page, beside a card that fills
        # the row (in a flex row, or a display:contents wrapper in a grid), in a dialog's panel, or in a box that scrolls for a log
        # line beside a table that fits, are not, and at a phone's width nothing is.
        # A rate page's header 32px in above its matrix 16px in, a dashboard's bot strip 10px in under a header 18px in.
        # good.html's page bands share a 16px gutter beside what is no gutter: a full-bleed strip, a centered note and row,
        # a card with its own padding, bands 4px apart, a badge set out of the flow, a 12px band, a first-line indent, a
        # chart's own labels, and a table's header cells padded wider than its rows.
        # Two more: a toolbar a 16px page container holds under a 32px header, and a right-aligned row 32px in under one 16px in.
        self.assertEqual(measured["bad-uneven-gutter"], 4)
        # At a phone's width good.html's paragraphs wrap, justified text included: a ragged right is not a gutter.
        self.assertEqual(measured["good-375-uneven-gutter"], 0)
        # bad.html's three: in a card, behind a narrow first table in the same box, and in an app's fixed main region.
        self.assertEqual(measured["bad-table-scroll"], 3)
        self.assertEqual((measured["bad-375-table-scroll"], measured["good-375-table-scroll"]), (0, 0))
        self.assertEqual(sorted(x for x in measured["bad-details"] if x.startswith("wrapped-label")),
                         ["wrapped-label: 12.5%", "wrapped-label: Save draft"])
        # Walked at 1280 the wide fixture's table has no room and is clean; the hook's own 1920 window finds it, and that
        # window is closed again: only the tabs the test opened remain.
        self.assertEqual(measured["wide"], [["table-scroll", 1920]])
        self.assertEqual(measured["pages"], 2)
        # The reason field the subscribed app squeezed into a table column, a one-line box asking for an explanation, a
        # field whose value is wider than it shows, a 180px reason field in an 18px font, and narrow free-text fields named
        # by each other source: a label, a placeholder, a title, aria-labelledby, a name and an id (camelCase and
        # snake_case), and a search box. good.html's wide reason textarea, the same 180px reason field in an 11px font,
        # short answers in narrow fields (a 2-digit quantity, a 4-character code, a PO number, a search box), a numeric
        # field and a url field whose names hold a free-text word, a disabled reason field, a note capped at what it shows
        # and a one-line textarea sized by script (its height set inline, as autosize scripts do) are clean, and bad.html's
        # read-only note cut off is clipped-value's alone. A finding names the label too, so the gate can tell a pull
        # request that adds it.
        self.assertEqual(measured["bad-cramped-field"],
                         ["A value much longer than this narrow field can show", "Add a note", "Explain the change",
                          "Explain what you looked for", "No-bid reason, line 1", "Reason for the change", "input#f-label",
                          "input#f-labelledby", "input#f-n", "input#f-title", "input#no_bid_reason_2"])
        self.assertEqual(measured["bad-cramped-labels"], ["Feedback", "Why is this late?"])
        # A spinner of three tilted orbits whose ink sits right of its box's middle, an svg nudged 3px right in a button, a
        # button-styled link, a tab, a radio segment, a role=button, a label styled as a button, an icon font's glyph nudged
        # the same, and a chevron 3px low in a button drawn by its shadow alone. good.html's centered icon, a play icon
        # centered in a content box padded 4px on its left (the middle is the content box's, not the border box's), a ring
        # with one bright arc and a ring with a gap (both turning), an icon beside a label, a borderless icon button and one
        # whose only shadows are Tailwind's zero-size rings, a bell with a dot, an icon beside a drawn caret, a tab with a
        # bar along its foot, a button under an open menu and one cut by the window's edge are not flagged.
        self.assertEqual(measured["bad-icon-off-center"], ["Add", "Close", "Edit", "Expand", "List", "Map", "Next", "Saving the quote", "Upload"])
        self.assertEqual(measured["good-icon-off-center"], [])
        self.assertEqual(measured["measured"], [True, True, True, True, True])
        record = [json.loads(x) for x in Path(out, "ui-audit.jsonl").read_text().splitlines()]
        self.assertEqual([(x["rule"], x["text"]) for x in record],
                         [("spinner-off-center", ""), ("icon-off-center", ""), ("cramped-field", "Reason code"),
                          ("cramped-field", "No-bid reason, line 4"), ("cramped-field", "Comment, line 5")])
        self.assertTrue(record[0]["page"].endswith("/good.html"))
        self.assertEqual(record[0]["width"], 1280)


if __name__ == "__main__":
    unittest.main()
