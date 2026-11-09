"""The ui-audit gate (gates/ui_audit.py) on layout records written here, and the hook's own node tests.

The hook that writes the record (actions/qae-browser/ui-audit.js) is measured in a real chromium by
tests/test_qae_ui_audit.py."""
import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from helpers import ROOT, commit, gate, git, make_repo

HOOK = ROOT / "actions" / "qae-browser" / "ui-audit.js"


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
        result = subprocess.run(["node", "--test", "--test-reporter=tap", str(HOOK.with_suffix(".test.js")),
                                 str(HOOK.parent / "ink.test.js"), str(HOOK.parent / "modal-dismiss.test.js")],
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertRegex(result.stdout, r"(?m)^# pass [1-9]")

    def test_both_lanes_load_the_hook(self):
        template = (ROOT / "harnesses" / "qae" / "explore.yml").read_text()
        self.assertIn('"--init-page","${{ steps.browser.outputs.console-pages }}","--init-page",'
                      '"${{ steps.browser.outputs.ui-audit }}"]', template)
        codex = (ROOT / "actions" / "qae-codex" / "action.yml").read_text()
        self.assertIn('\\"--init-page\\",\\"$GITHUB_ACTION_PATH/../qae-browser/ui-audit.js\\"', codex)


if __name__ == "__main__":
    unittest.main()
