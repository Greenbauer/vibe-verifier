"""The layout record's nested-inset rule (actions/qae-browser/inset-page.js) in a real browser.

It measures tests/fixtures/ui-audit/bad.html and good.html at a phone's width and at 1280, as tests/test_qae_ui_audit.py
measures every other rule on them. Like that file it needs the installed toolchain, so it runs where QAE_MCP names the
playwright-mcp entry point (the catalog's `qae-browser` CI job)."""
import json
import os
import subprocess
import unittest
from pathlib import Path

from helpers import ROOT

HOOK = ROOT / "actions" / "qae-browser" / "ui-audit.js"
FIXTURES = ROOT / "tests" / "fixtures" / "ui-audit"

MEASURE = r"""
const { chromium } = require(require.resolve("playwright", { paths: [process.env.TOOLS] }));
const hook = require(process.env.HOOK);
(async () => {
  const browser = await chromium.launch(process.env.QAE_CHANNEL ? { channel: process.env.QAE_CHANNEL } : {});
  const out = {};
  for (const name of ["bad", "good"]) {
    for (const width of [375, 1280]) {
      const page = await browser.newPage({ viewport: { width, height: 800 } });
      await page.goto("file://" + process.env.FIXTURES + "/" + name + ".html");
      out[name + "-" + width] = (await page.evaluate(hook.script)).filter(f => f.rule === "nested-inset")
        .map(f => f.at.replace(/:nth-of-type\(\d+\)$/, "") + ": " + f.detail.split(":")[0]).sort();
      await page.close();
    }
  }
  await browser.close();
  console.log(JSON.stringify(out));
})().catch(error => { console.error(error); process.exit(1); });
"""


@unittest.skipUnless(os.environ.get("QAE_MCP"), "needs the QAE browser toolchain: QAE_MCP names playwright-mcp's entry point")
class NestedInset(unittest.TestCase):
    def test_a_card_in_a_card_on_a_phone_is_found_and_the_same_content_flattened_is_not(self):
        env = {**os.environ, "TOOLS": str(Path(os.environ["QAE_MCP"]).resolve().parents[3]), "HOOK": str(HOOK), "FIXTURES": str(FIXTURES)}
        run = subprocess.run(["node", "-e", MEASURE], capture_output=True, text=True, env=env, timeout=120)
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
        measured = json.loads(run.stdout.strip().splitlines()[-1])
        # bad.html's lines to review (a table a phone stacks into a card per row) and its offers, each a card inside the
        # section's card, start 74px in from both sides of a 375px window (the fixture's own page padding included): one
        # finding per card, which the gate groups by row.
        inset = "its content starts 74px in from the left and ends 74px in from the right of a 375px window"
        self.assertEqual(measured["bad-375"], [
            "body > section:nth-of-type(1) > table > tbody > tr: " + inset,
            "body > section:nth-of-type(1) > table > tbody > tr: " + inset,
            "html > body > section:nth-of-type(2) > ul > li: " + inset,
            "html > body > section:nth-of-type(2) > ul > li: " + inset])
        # good.html's lines split by dividers, two-up tiles, a pill, a card in a full-width band and a lone card are not
        # flagged, and at 1280 nothing is: a desktop has the width to spare.
        self.assertEqual((measured["good-375"], measured["bad-1280"], measured["good-1280"]), ([], [], []))


if __name__ == "__main__":
    unittest.main()
