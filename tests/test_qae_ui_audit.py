"""The QAE harness's layout record (actions/qae-browser/ui-audit.js and ui-audit-page.js) and the ui-audit gate
that judges it.

`Gate` drives the gate on records written here. The hook's own tests are the node tests beside it. `Browser`
measures the fixtures in tests/fixtures/ui-audit/ in a real chromium, and loads the hook into a real page: it
needs the installed toolchain, so it runs where QAE_MCP names the playwright-mcp entry point (the catalog's
`qae-browser` CI job, after actions/qae-browser). QAE_CHANNEL=chrome runs it on a machine with Google Chrome
and no Playwright chromium."""
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from helpers import ROOT, gate, make_repo

HOOK = ROOT / "actions" / "qae-browser" / "ui-audit.js"
FIXTURES = ROOT / "tests" / "fixtures" / "ui-audit"
RULES = ["clipped-text", "clipped-value", "edge", "field-stagger", "long-sentence", "raw-value", "row-height",
         "row-stagger", "spinner-off-center", "text-wall", "touching"]


def line(rule, at="td > div > button", page="https://site.test/quotes", width=1280, **extra):
    return {"page": page, "width": width, "rule": rule, "at": at, "text": "Activate", "other": "Open",
            "detail": "0px from the control beside it", **extra}


class Gate(unittest.TestCase):
    def run_gate(self, lines, *args, steps=True):
        repo = make_repo(self, {"README.md": "x\n"})
        artifacts = Path(repo) / "qae-artifacts"
        (artifacts / "qae").mkdir(parents=True)
        if steps:
            (artifacts / "qae" / "AC1.md").write_text("step 1: opened /quotes\n")
        if lines is not None:
            (artifacts / "ui-audit.jsonl").write_text("".join(json.dumps(x) + "\n" for x in lines))
        return gate("ui-audit", repo, "--artifacts", str(artifacts), *args)

    def test_a_measured_defect_fails_and_names_the_page_width_rule_and_element(self):
        result = self.run_gate([line("touching")])
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("touching on https://site.test/quotes at 1280px: Activate / Open: 0px from the control beside it "
                      "(td > div > button)", result.stdout)

    def test_a_clean_record_passes(self):
        result = self.run_gate([])
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_rows_of_one_table_are_one_finding_with_a_count(self):
        result = self.run_gate([line("touching", "tr:nth-of-type(%d) > td > button" % n) for n in range(1, 10)])
        self.assertEqual(result.stdout.count("touching on"), 1, result.stdout)
        self.assertIn("(and 8 more like it)", result.stdout)

    def test_each_width_is_its_own_finding(self):
        result = self.run_gate([line("touching"), line("touching", width=375)])
        self.assertEqual(result.stdout.count("touching on"), 2, result.stdout)

    def test_a_page_off_the_declared_site_is_not_judged(self):
        record = [line("edge", page="https://docs.other.test/help"), line("edge", page="https://site.test.other.test/")]
        result = self.run_gate(record, "--site", "https://site.test")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_rules_and_allow_narrow_what_is_judged(self):
        record = [line("edge"), line("raw-value", at="footer > span", text="sync_state")]
        only = self.run_gate(record, "--rules", "raw-value", "--allow", "^footer ")
        self.assertEqual(only.returncode, 0, only.stdout + only.stderr)
        self.assertEqual(self.run_gate(record, "--rules", "edge").returncode, 1)

    def test_soak_reports_without_failing(self):
        result = self.run_gate([line("touching")], "--soak")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("touching on", result.stdout)

    def test_step_logs_without_a_record_cannot_run(self):
        # The hook was not loaded: passing would make a broken soak look like clean pages.
        result = self.run_gate(None)
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn("load actions/qae-browser/ui-audit.js", result.stdout + result.stderr)

    def test_each_explorers_record_is_read_from_its_shard(self):
        repo = make_repo(self, {"README.md": "x\n"})
        artifacts = Path(repo) / "qae-artifacts"
        for n, rule in ((1, "edge"), (2, "touching")):
            (artifacts / ("shard-%d" % n)).mkdir(parents=True)
            (artifacts / ("shard-%d" % n) / "ui-audit.jsonl").write_text(json.dumps(line(rule)) + "\n")
        result = gate("ui-audit", repo, "--artifacts", str(artifacts))
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("edge on", result.stdout)
        self.assertIn("touching on", result.stdout)

    def test_a_run_that_walked_nothing_passes(self):
        result = self.run_gate(None, steps=False)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_the_hooks_node_tests_pass(self):
        result = subprocess.run(["node", "--test", "--test-reporter=tap", str(HOOK.with_suffix(".test.js"))],
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertRegex(result.stdout, r"(?m)^# pass [1-9]")

    def test_both_lanes_load_the_hook(self):
        template = (ROOT / "harnesses" / "qae" / "explore.yml").read_text()
        self.assertIn('"--init-page","${{ steps.browser.outputs.console-pages }}","--init-page",'
                      '"${{ steps.browser.outputs.ui-audit }}"]', template)
        codex = (ROOT / "actions" / "qae-codex" / "action.yml").read_text()
        self.assertIn('\\"--init-page\\",\\"$GITHUB_ACTION_PATH/../qae-browser/ui-audit.js\\"', codex)


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
    out[name] = [...new Set(found.map(f => f.rule))].sort();
    out[name + "-details"] = found.filter(f => ["raw-value", "touching"].includes(f.rule)).map(f => f.rule + ": " + f.text);
    await page.close();
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
  await page.waitForTimeout(500);
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
        self.assertEqual(measured["good"], [])
        # A raw value in a heading cell or a button counts, and a square button touching a rounded one is no segmented group.
        for expected in ("raw-value: undefined", "raw-value: 3f2b1c4d-1111-4abc-9def-0123456789ab", "raw-value: Status: in_review",
                         "touching: Activate", "touching: Square"):
            self.assertIn(expected, measured["bad-details"])
        record = [json.loads(x) for x in Path(out, "ui-audit.jsonl").read_text().splitlines()]
        self.assertEqual([x["rule"] for x in record], ["spinner-off-center"])
        self.assertTrue(record[0]["page"].endswith("/good.html"))
        self.assertEqual(record[0]["width"], 1280)


if __name__ == "__main__":
    unittest.main()
