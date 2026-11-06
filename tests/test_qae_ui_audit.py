"""The QAE harness's layout record (actions/qae-browser/ui-audit.js and ui-audit-page.js) and the ui-audit gate
that judges it.

`Gate` drives the gate on records written here. The hook's own tests are the node tests beside it. `Browser`
measures the fixtures in tests/fixtures/ui-audit/ in a real chromium, and loads the hook into a real page: it
needs the installed toolchain, so it runs where QAE_MCP names the playwright-mcp entry point (the catalog's
`qae-browser` CI job, after actions/qae-browser). QAE_CHANNEL=chrome runs it on a machine with Google Chrome
and no Playwright chromium."""
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from helpers import ROOT, commit, gate, git, make_repo

HOOK = ROOT / "actions" / "qae-browser" / "ui-audit.js"
FIXTURES = ROOT / "tests" / "fixtures" / "ui-audit"
RULES = ["clipped-text", "clipped-value", "duplicate-name", "edge", "field-stagger", "glyph-off-center", "long-sentence", "raw-value",
         "row-height", "row-stagger", "spinner-off-center", "table-scroll", "text-wall", "touching", "uneven-gutter", "wrapped-label"]


def line(rule, at="td > div > button", page="https://site.test/quotes", width=1280, **extra):
    return {"page": page, "width": width, "rule": rule, "at": at, "text": "Activate", "other": "Open",
            "detail": "0px from the control beside it", **extra}


def pull_request(test, files=None):
    """A repository whose branch adds `files` over main: by default the screen the record's lines name (Activate
    beside Open), so its findings are the pull request's."""
    repo = make_repo(test, {"README.md": "x\n", "src/people.tsx": "<Button>Send invite</Button>\n"})
    git(repo, "checkout", "-q", "-b", "change")
    commit(repo, files or {"src/quotes.tsx": "<td><div><Button>Activate</Button><Button>Open</Button></div></td>\n"})
    return repo


# A subscribed app's add-someone dialog as the hook recorded it: the "Send invite" choice, pressed, and the "Send invite"
# submit (actions/qae-browser/name-page.js), on the lines its pull request added.
DIALOG = {"src/AddSomeoneChoice.tsx": "[Mode.INVITE, 'Send invite'],\n[Mode.CREATE, 'Create account now'],\n",
          "src/AddSomeoneDialog.tsx": "const SUBMIT = {\n  [Mode.INVITE]: 'Send invite',\n};\n"}
SAME_NAME = {"rule": "duplicate-name", "page": "https://site.test/customers/7", "width": 1280,
             "at": "div#headlessui-dialog-panel-3 > div > div:nth-of-type(2) > button:nth-of-type(1)", "text": "Send invite",
             "other": "Send invite",
             "detail": "\"Send invite\" and \"Send invite\" in one dialog: a person cannot tell which button does what"}


class Gate(unittest.TestCase):
    def run_gate(self, lines, *args, steps=True, repo=None):
        repo = repo or pull_request(self)
        artifacts = Path(repo) / "qae-artifacts"
        (artifacts / "qae").mkdir(parents=True)
        if steps:
            (artifacts / "qae" / "AC1.md").write_text("step 1: opened /quotes\n")
        if lines is not None:
            (artifacts / "ui-audit.jsonl").write_text("".join(json.dumps(x) + "\n" for x in lines))
        return gate("ui-audit", repo, "--base-ref", "main", "--artifacts", str(artifacts), *args)

    def test_a_measured_defect_fails_and_names_the_page_width_rule_and_element(self):
        result = self.run_gate([line("touching")])
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("touching on https://site.test/quotes at 1280px: Activate / Open: 0px from the control beside it "
                      "(td > div > button)", result.stdout)

    def test_a_defect_on_a_screen_the_base_already_had_is_listed_and_does_not_block(self):
        result = self.run_gate([line("touching", text="Send invite", other="Cancel")])
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("advisory: touching, existing on https://site.test/quotes", result.stdout)
        # --all makes it block, and so does a pull request that adds the neighbour it touches.
        self.assertEqual(self.run_gate([line("touching", text="Send invite", other="Cancel")], "--all").returncode, 1)
        self.assertEqual(self.run_gate([line("touching", text="Send invite", other="Open")]).returncode, 1)

    def test_a_defect_on_an_element_that_shows_no_text_does_not_block(self):
        result = self.run_gate([line("glyph-off-center", text="", other=None)])
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("advisory: glyph-off-center, existing", result.stdout)

    def test_two_buttons_of_one_name_the_pull_request_adds_block(self):
        result = self.run_gate([SAME_NAME], repo=pull_request(self, DIALOG))
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn('duplicate-name on https://site.test/customers/7 at 1280px: Send invite / Send invite: "Send invite" '
                      'and "Send invite" in one dialog', result.stdout)
        # The same two buttons on a screen the pull request does not touch are the base's.
        self.assertEqual(self.run_gate([SAME_NAME]).returncode, 0)

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
        repo = pull_request(self)
        artifacts = Path(repo) / "qae-artifacts"
        for n, rule in ((1, "edge"), (2, "touching")):
            (artifacts / ("shard-%d" % n)).mkdir(parents=True)
            (artifacts / ("shard-%d" % n) / "ui-audit.jsonl").write_text(json.dumps(line(rule)) + "\n")
        result = gate("ui-audit", repo, "--base-ref", "main", "--artifacts", str(artifacts))
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("edge on", result.stdout)
        self.assertIn("touching on", result.stdout)

    def test_a_run_that_walked_nothing_passes(self):
        result = self.run_gate(None, steps=False)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_a_run_that_walked_nothing_passes_on_the_empty_site_file_a_none_declaration_leaves(self):
        # A pull request that declares `- None:` runs no explorer, so the verify job writes an empty site file.
        site = Path(tempfile.mkdtemp(prefix="vv-ui-site-")) / "site-url"
        self.addCleanup(shutil.rmtree, site.parent)
        site.write_text("\n")
        result = self.run_gate(None, "--site-file", str(site), steps=False)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        # A clean walk still leaves a record, empty, and that one is judged against the site it declared.
        result = self.run_gate([], "--site-file", str(site), steps=False)
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)

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
  for (let waited = 0; waited < 5000 && !require("fs").readFileSync(recorded, "utf8").includes("spinner-off-center"); waited += 100) {
    await page.waitForTimeout(100);
  }
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
        record = [json.loads(x) for x in Path(out, "ui-audit.jsonl").read_text().splitlines()]
        self.assertEqual([x["rule"] for x in record], ["spinner-off-center"])
        self.assertTrue(record[0]["page"].endswith("/good.html"))
        self.assertEqual(record[0]["width"], 1280)


if __name__ == "__main__":
    unittest.main()
