"""The browser's width captures (actions/qae-browser/widths.js) of the step screenshots an explorer's own
browser_run_code_unsafe code saves to a file; split from tests/test_qae_widths.py, which holds the rest."""
import subprocess
import unittest

from helpers import ROOT


class ScriptedSteps(unittest.TestCase):
    def test_a_step_screenshot_saved_by_the_explorers_own_code_is_captured(self):
        # An explorer walking in browser_run_code_unsafe saves each step with page.screenshot({path}). The hook skipped
        # every screenshot with a path, so a consumer's criteria walked that way had no capture at 375 (2026-11-10).
        script = """
const fs = require("fs"), os = require("os"), path = require("path");
const widths = require(process.argv[1]);
const dir = fs.mkdtempSync(path.join(os.tmpdir(), "vv-scripted-"));
fs.writeFileSync(path.join(dir, "list"), "1280\\n375\\n");
process.env.VV_QAE_WIDTHS = path.join(dir, "list");
let size = { width: 1280, height: 800 };
class Tab { async screenshot(options = {}) { const data = Buffer.from("at " + size.width); if (options.path) fs.writeFileSync(options.path, data); return data; } }
const tab = Object.assign(new Tab(), { viewportSize: () => size, setViewportSize: async given => { size = given; },
  evaluate: async (fn, arg) => (arg !== undefined ? false : undefined), waitForTimeout: async () => {},
  locator: () => ({ ariaSnapshot: async () => "tree" }) });
widths.install(tab, dir);
fs.mkdirSync(path.join(dir, "qae"));
(async () => {
  await tab.screenshot({ path: path.join(dir, "qae", "AC1-step-1.png"), fullPage: true });
  await tab.screenshot({ path: path.join(dir, "row.png") });
  console.log(fs.readFileSync(path.join(dir, widths.RECORD), "utf8").trim().split("\\n").filter(Boolean)
    .map(line => JSON.parse(line).width).join(","));
})();
"""
        result = subprocess.run(["node", "-e", script, str(ROOT / "actions" / "qae-browser" / "widths.js")],
                                capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "375", "one capture at 375, for the step file only")
