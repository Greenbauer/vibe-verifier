"""The copy audit: the QAE hook's copy record (actions/qae-browser/copy-page.js), its grader (gates/_copy_audit.py,
`bin/vibe-verifier copy-audit`) and the copy-audit gate that holds the grades to the record.

`Gate` drives the gate on records and grades written here. `Grader` runs the grader with a fake Codex CLI: what
the model is given, and what it is not. `Browser` reads the fixtures in tests/fixtures/copy-audit/ in a real
chromium (QAE_MCP names the playwright-mcp entry point, as in tests/test_qae_ui_audit.py). `Model` grades those
fixtures with the real model: it needs a Codex login, so it runs only where COPY_AUDIT_CODEX_HOME names one."""
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from helpers import ROOT, commit, gate, git, make_repo, runner, write

import _copy_audit

FIXTURES = ROOT / "tests" / "fixtures" / "copy-audit"
DIALOG = "They get the portal's invite email and join Acme as User when they set a password."
BAD = [DIALOG, "Sync NS record", "Enter customer_id", "quote_status",
       "If you would like to make changes to the billing information that is associated with this account, you will "
       "need to contact the administrator of your organization, who can update it for you from the settings page.",
       "Please utilize the form below in order to facilitate the submission of your request prior to the deadline.",
       "No rows returned from the customers table."]


def block(text, page="https://site.test/users", kind="sentence"):
    return {"page": page, "width": 1280, "kind": kind, "text": text, "at": "dialog > p"}


def passing(text):
    return {"id": _copy_audit.block_id(text), "verdict": "PASS", "rule": "none", "quote": "", "rewrite": ""}


def failing(text, quote=None, rule="no-system-terms", rewrite="We'll email them an invite."):
    return {"id": _copy_audit.block_id(text), "verdict": "FAIL", "rule": rule, "quote": text if quote is None else quote,
            "rewrite": rewrite}


def envelope(grades, shard=1, error=None):
    return {"shard": shard, "ids": [g["id"] for g in grades], "error": error,
            "output": None if error else {"blocks": grades}, "seconds": 1.0}


class Gate(unittest.TestCase):
    def setUp(self):
        self.repo = make_repo(self, {"src/users.tsx": "export const title = 'Users';\n"})
        git(self.repo, "checkout", "-q", "-b", "feat/invite")
        commit(self.repo, {"src/invite.tsx": "<p>They get the portal&apos;s invite email and join {org} as {role}</p>\n"
                                             "<button>Sync NS record</button>\n"})
        self.artifacts = Path(self.repo) / "qae-artifacts"

    def run_gate(self, record, shards, *args, folder="", steps=True):
        where = self.artifacts / folder
        where.mkdir(parents=True, exist_ok=True)
        if steps:
            (self.artifacts / "qae").mkdir(exist_ok=True)
            (self.artifacts / "qae" / "AC1.md").write_text("step 1: opened /users\n")
        if record is not None:
            (where / "copy.jsonl").write_text("".join(json.dumps(line) + "\n" for line in record))
        if shards is not None:
            (where / "copy-grades.json").write_text(json.dumps({"shards": shards}))
        return gate("copy-audit", self.repo, "--base-ref", "main", "--artifacts", str(self.artifacts), *args)

    def assert_code(self, result, code, text=None):
        self.assertEqual(result.returncode, code, result.stdout + result.stderr)
        if text:
            self.assertIn(text, result.stdout + result.stderr)

    def test_failing_copy_the_pull_request_adds_blocks_with_its_quote_and_rewrite(self):
        text = DIALOG + " The link lasts 24 hours; you can resend it."
        result = self.run_gate([block(text)], [envelope([failing(text, quote=DIALOG)])])
        self.assert_code(result, 1, 'no-system-terms on https://site.test/users: "%s". Try: "We\'ll email them an invite." '
                                    "(dialog > p)" % DIALOG)

    def test_a_short_label_is_the_pull_requests_when_all_of_it_is_on_an_added_line(self):
        result = self.run_gate([block("Sync NS record", kind="label")], [envelope([failing("Sync NS record")])])
        self.assert_code(result, 1, '"Sync NS record"')

    def test_failing_copy_the_base_already_had_is_listed_and_does_not_block(self):
        result = self.run_gate([block("Users")], [envelope([failing("Users")])])
        self.assert_code(result, 0, "no-system-terms, existing copy on https://site.test/users")

    def test_a_label_on_an_added_line_of_its_own_is_the_pull_requests(self):
        # The base has the button; the pull request changes only its text, on a line of its own, as Prettier formats JSX.
        git(self.repo, "checkout", "-q", "main")
        commit(self.repo, {"src/sync.tsx": "<Button onClick={sync}>\n  Save\n</Button>\n"})
        git(self.repo, "checkout", "-q", "feat/invite")
        git(self.repo, "merge", "-q", "--no-edit", "main")
        commit(self.repo, {"src/sync.tsx": "<Button onClick={sync}>\n  Refresh NS row\n</Button>\n"})
        result = self.run_gate([block("Refresh NS row", kind="label")], [envelope([failing("Refresh NS row")])])
        self.assert_code(result, 1, '"Refresh NS row"')

    def test_a_labels_word_inside_code_the_pull_request_adds_is_not_the_label(self):
        commit(self.repo, {"src/row.ts": "const unsaved = true;\nreturn { status: row.quote_status, a: 1 };\n"})
        record = [block("Save", kind="label"), block("quote_status", kind="label")]
        result = self.run_gate(record, [envelope([failing("Save"), failing("quote_status")])])
        self.assert_code(result, 0, "2 advisory finding(s)")

    def test_all_makes_existing_copy_block(self):
        result = self.run_gate([block("Users")], [envelope([failing("Users")])], "--all")
        self.assert_code(result, 1, "existing copy")

    def test_soak_reports_without_failing(self):
        result = self.run_gate([block("Sync NS record")], [envelope([failing("Sync NS record")])], "--soak")
        self.assert_code(result, 0, '"Sync NS record"')

    def test_passing_copy_passes(self):
        self.assert_code(self.run_gate([block("Sync NS record")], [envelope([passing("Sync NS record")])]), 0)

    def test_a_quote_that_is_not_in_the_block_is_dropped_and_said_so(self):
        result = self.run_gate([block("Sync NS record")], [envelope([failing("Sync NS record", quote="Sync the NS record")])])
        self.assert_code(result, 0, "dropped 1 FAIL grade(s) whose quote is not in the block's text")
        self.assertNotIn("Try:", result.stdout)

    def test_the_same_text_on_several_pages_is_one_finding(self):
        record = [block("Sync NS record"), block("Sync NS record", page="https://site.test/team")]
        result = self.run_gate(record, [envelope([failing("Sync NS record")])])
        self.assertEqual(result.stdout.count("Try:"), 1, result.stdout)
        self.assertIn("(and on 1 more page(s))", result.stdout)

    def test_allow_skips_wording_the_repository_chose(self):
        result = self.run_gate([block("Sync NS record")], [envelope([failing("Sync NS record")])], "--allow", "^Sync NS")
        self.assert_code(result, 0)

    def test_a_block_with_no_grade_cannot_run(self):
        result = self.run_gate([block("Sync NS record"), block("Save", kind="label")], [envelope([passing("Save")])])
        self.assert_code(result, 2, "no grade for 1 block(s)")

    def test_a_block_graded_twice_fails_when_either_grade_is_a_fail(self):
        self.assert_code(self.run_gate([block("Sync NS record")], [envelope([passing("Sync NS record"), passing("Sync NS record")])]), 0)
        twice = envelope([passing("Sync NS record"), failing("Sync NS record")])
        self.assert_code(self.run_gate([block("Sync NS record")], [twice]), 1, '"Sync NS record"')

    def test_a_grade_outside_its_shard_cannot_run(self):
        stray = envelope([passing("Save")])
        stray["ids"] = []
        self.assert_code(self.run_gate([block("Save")], [stray]), 2, "not given it")

    def test_a_shard_the_grader_could_not_run_cannot_run(self):
        result = self.run_gate([block("Save")], [envelope([passing("Save")], error="Codex ended with an error: You've hit your usage limit")])
        self.assert_code(result, 2, "usage limit")

    def test_grades_that_break_the_schema_cannot_run(self):
        bad = envelope([dict(passing("Save"), verdict="MAYBE")])
        self.assert_code(self.run_gate([block("Save")], [bad]), 2)

    def test_a_record_with_no_grades_cannot_run(self):
        self.assert_code(self.run_gate([block("Save")], None), 2, "grade it with bin/vibe-verifier copy-audit --grade-only")

    def test_step_logs_without_a_record_cannot_run(self):
        self.assert_code(self.run_gate(None, None), 2, "holds step logs and no copy.jsonl")

    def test_a_run_that_walked_nothing_passes(self):
        self.assert_code(self.run_gate(None, None, steps=False), 0)

    def test_a_page_off_the_declared_site_needs_no_grade_and_is_not_judged(self):
        record = [block("Sync NS record", page="https://login.other.test/")]
        self.assert_code(self.run_gate(record, [], "--site", "https://site.test"), 0)

    def test_each_explorers_record_is_judged_with_its_own_grades(self):
        self.run_gate([block("Save")], [envelope([passing("Save")])], folder="shard-1")
        result = self.run_gate([block("Sync NS record")], [envelope([failing("Sync NS record")])], folder="shard-2")
        self.assert_code(result, 1, '"Sync NS record"')


FAKE_CODEX = r'''#!/usr/bin/env python3
import json, os, sys
args = sys.argv[1:]
with open(os.environ["FAKE_LOG"], "a") as log:
    log.write(json.dumps([args, sorted(os.listdir(args[args.index("--cd") + 1]))]) + "\n")
if os.environ.get("FAKE_BEHAVIOUR") == "limit":
    print(json.dumps({"type": "turn.failed", "error": {"message": "You've hit your usage limit. Try again at 3:05 PM."}}), flush=True)
    sys.exit(1)
blocks = [json.loads(line) for line in args[-1].splitlines() if line.startswith('{"id"')]
grades = [{"id": b["id"], "verdict": "FAIL" if "NS" in b["text"] else "PASS", "rule": "no-system-terms" if "NS" in b["text"] else "none",
           "quote": b["text"] if "NS" in b["text"] else "", "rewrite": "Refresh details" if "NS" in b["text"] else ""} for b in blocks]
open(args[args.index("-o") + 1], "w").write(json.dumps({"blocks": grades}))
'''


class Grader(unittest.TestCase):
    def setUp(self):
        self.work = Path(tempfile.mkdtemp(prefix="vv-copy-grader-"))
        self.addCleanup(shutil.rmtree, self.work, True)
        record = [block("Sync NS record", kind="label"), block("Save", kind="label"), block("Save", page="https://site.test/team"),
                  block("Sign in with SSO", page="https://login.other.test/")]
        write(self.work, {"art/copy.jsonl": "".join(json.dumps(line) + "\n" for line in record), "site-url": "https://site.test\n",
                          "home/auth.json": json.dumps({"auth_mode": "chatgpt", "tokens": {"refresh_token": "r"}}), "codex": FAKE_CODEX})
        os.chmod(self.work / "codex", 0o755)

    def run_grader(self, *args, behaviour=""):
        return runner("copy-audit", "--artifacts", str(self.work / "art"), "--site-file", str(self.work / "site-url"),
                      "--codex", str(self.work / "codex"), "--codex-home", str(self.work / "home"), *args,
                      env={"FAKE_LOG": str(self.work / "calls.jsonl"), "FAKE_BEHAVIOUR": behaviour})

    def test_the_model_gets_each_distinct_on_site_block_once_in_an_empty_directory(self):
        result = self.run_grader("--grade-only", "--max-blocks", "1")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        calls = [json.loads(line) for line in (self.work / "calls.jsonl").read_text().splitlines()]
        self.assertEqual(len(calls), 2)   # two distinct texts on the site, one a shard
        prompts = "\n".join(call[-1] for call, _ in calls)
        self.assertIn(json.dumps({"id": _copy_audit.block_id("Save"), "kind": "label", "text": "Save"}), prompts)
        self.assertNotIn("SSO", prompts)
        for call, listed in calls:
            self.assertEqual(listed, [".git"])
            self.assertIn(str(ROOT / "harnesses" / "copy-audit" / "schema.json"), call)
        shards = json.loads((self.work / "art" / "copy-grades.json").read_text())["shards"]
        self.assertEqual([s["ids"] for s in shards], [[_copy_audit.block_id("Sync NS record")], [_copy_audit.block_id("Save")]])

    def test_without_grade_only_it_runs_the_gate_on_what_it_graded(self):
        result = self.run_grader("--all")
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn('"Sync NS record". Try: "Refresh details"', result.stdout)

    def test_a_usage_limit_is_written_as_the_shards_error_and_exits_2(self):
        result = self.run_grader("--grade-only", behaviour="limit")
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn("usage limit", result.stdout)
        shard = json.loads((self.work / "art" / "copy-grades.json").read_text())["shards"][0]
        self.assertIn("usage limit", shard["error"])
        self.assertIsNone(shard["output"])


READ = r"""
const { chromium } = require(require.resolve("playwright", { paths: [process.env.TOOLS] }));
const hook = require(process.env.HOOK);
(async () => {
  const browser = await chromium.launch(process.env.QAE_CHANNEL ? { channel: process.env.QAE_CHANNEL } : {});
  const out = {};
  for (const name of ["bad", "good"]) {
    const page = await browser.newPage({ viewport: { width: 1280, height: 900 } });
    await page.goto("file://" + process.env.FIXTURES + "/" + name + ".html");
    out[name] = await page.evaluate(hook.copyScript);
    await page.close();
  }
  await browser.close();
  console.log(JSON.stringify(out));
})().catch(error => { console.error(error); process.exit(1); });
"""


def read_fixtures():
    env = {**os.environ, "TOOLS": str(Path(os.environ["QAE_MCP"]).resolve().parents[3]),
           "HOOK": str(ROOT / "actions" / "qae-browser" / "ui-audit.js"), "FIXTURES": str(FIXTURES)}
    run = subprocess.run(["node", "-e", READ], capture_output=True, text=True, env=env, timeout=120)
    if run.returncode:
        raise AssertionError(run.stdout + run.stderr)
    return json.loads(run.stdout.strip().splitlines()[-1])


@unittest.skipUnless(os.environ.get("QAE_MCP"), "needs the QAE browser toolchain: QAE_MCP names playwright-mcp's entry point")
class Browser(unittest.TestCase):
    def test_the_record_holds_the_pages_copy_and_leaves_out_values_code_and_hidden_text(self):
        bad = {b["text"]: b["kind"] for b in read_fixtures()["bad"]}
        # A link inside a sentence is part of it; a button, a heading, a table header and a placeholder are labels.
        self.assertEqual(bad[DIALOG + " The link lasts 24 hours; you can resend it."], "sentence")
        for label in ("Sync NS record", "quote_status", "Customer", "Balance", "Enter customer_id", "Email"):
            self.assertEqual(bad[label], "label")
        for left_out in ("Acme Corp", "$1,200.00", "npm install", "support@acme.test", "resend it",
                         "This hidden menu is not on the page.", "Only a screen reader reads this."):
            self.assertNotIn(left_out, bad)
        self.assertEqual(len(bad), 11, sorted(bad))


@unittest.skipUnless(os.environ.get("QAE_MCP") and os.environ.get("COPY_AUDIT_CODEX_HOME"),
                     "grades with the real model: needs the browser toolchain and COPY_AUDIT_CODEX_HOME, a Codex login")
class Model(unittest.TestCase):
    def test_the_model_fails_each_bad_block_and_passes_the_same_screens_written_plainly(self):
        for name, blocks in read_fixtures().items():
            folder = Path(tempfile.mkdtemp(prefix="vv-copy-model-"))
            self.addCleanup(shutil.rmtree, folder, True)
            (folder / "copy.jsonl").write_text("".join(json.dumps(dict(b, page="file:///" + name, width=1280)) + "\n" for b in blocks))
            result = runner("copy-audit", "--artifacts", str(folder), "--grade-only", "--codex-home", os.environ["COPY_AUDIT_CODEX_HOME"])
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            (shard,) = json.loads((folder / "copy-grades.json").read_text())["shards"]
            failed = [b["text"] for b in blocks if any(g["id"] == _copy_audit.block_id(b["text"]) and g["verdict"] == "FAIL"
                                                       for g in shard["output"]["blocks"])]
            if name == "good":
                self.assertEqual(failed, [])
            else:
                self.assertEqual(sorted(text.split(" The link")[0] for text in failed), sorted(BAD))


if __name__ == "__main__":
    unittest.main()
