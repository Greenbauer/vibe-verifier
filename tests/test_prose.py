"""The prose gate: the real pinned Vale on fixture repositories, and a stand-in Vale for the edge where
it cannot produce a verdict."""
import json
import os
import shutil
import stat
import tempfile
import unittest
from pathlib import Path

from helpers import commit, gate, make_repo

EM, EN, LQ, RQ = "—", "–", "“", "”"


def judge(test, base, head, *args, env=None):
    repo = make_repo(test, dict(base, **{"README.md": "x\n"}))
    commit(repo, head)
    result = gate("prose", repo, "--base-ref", "HEAD~1", "--format", "json", *args, env=env)
    test.assertIn(result.returncode, (0, 1), result.stdout + result.stderr)
    return result.returncode, json.loads(result.stdout)


def rules(report, advisory=None):
    return sorted((f["path"], f["line"], f["message"].split(":")[0]) for f in report["findings"]
                  if advisory is None or f["advisory"] == advisory)


class Prose(unittest.TestCase):
    def test_a_new_em_dash_blocks_and_one_the_file_already_had_does_not(self):
        old = "Old %s line.\n" % EM
        code, report = judge(self, {"docs/a.md": old}, {"docs/a.md": old + "New %s line.\n" % EM})
        self.assertEqual(code, 1)
        self.assertEqual(rules(report), [("docs/a.md", 2, "EmDash")])

    def test_an_en_dash_aside_blocks_and_a_range_passes(self):
        code, report = judge(self, {}, {"a.mdx": "Pages 4%s9 and 2026%s10.\n\nThis %s that.\n" % (EN, EN, EN)})
        self.assertEqual(code, 1)
        self.assertEqual(rules(report), [("a.mdx", 3, "EnDashAside")])

    def test_code_tables_blockquotes_and_link_targets_are_not_judged(self):
        text = ("Run `a %s b` here.\n\n```\nfenced %s\n```\n\n| a | b |\n|---|---|\n| x | %s |\n\n"
                "> quoted %s text\n\n<!-- note %s -->\n\n[docs](https://example.com/a%sb)\n" % ((EM,) * 6))
        self.assertEqual(judge(self, {}, {"a.md": text}), (0, {"gate": "prose", "status": "pass", "findings": []}))

    def test_a_curly_quote_is_reported_but_does_not_block(self):
        code, report = judge(self, {}, {"a.md": "Say %shi%s.\n" % (LQ, RQ)})
        self.assertEqual(code, 0)
        self.assertEqual(rules(report, advisory=True), [("a.md", 1, "CurlyQuotes"), ("a.md", 1, "CurlyQuotes")])

    def test_the_generated_rules_block_is_skipped(self):
        block = ("<!-- BEGIN vibe-verifier rules-doc: generated -->\n\n- rule %s message\n\n"
                 "<!-- END vibe-verifier rules-doc -->\n" % EM)
        self.assertEqual(judge(self, {}, {"CLAUDE.md": block})[0], 0)

    def test_other_files_and_excluded_globs_are_not_judged(self):
        code, _ = judge(self, {}, {"notes.txt": "a %s b\n" % EM, "vendor/x.md": "a %s b\n" % EM}, "--exclude", "vendor/**")
        self.assertEqual(code, 0)

    def test_all_judges_lines_the_base_already_had(self):
        repo = make_repo(self, {"a.md": "Old %s line.\n" % EM})
        result = gate("prose", repo, "--all", "--format", "json")
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertEqual(rules(json.loads(result.stdout)), [("a.md", 1, "EmDash")])

    def test_the_description_is_judged_advisory_from_body_or_the_pull_request_event(self):
        scratch = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, scratch, True)
        body = Path(scratch, "body.md")
        body.write_text("Fixes it %s for now.\n" % EM)
        code, report = judge(self, {}, {"a.md": "fine\n"}, "--body", str(body))
        self.assertEqual((code, rules(report, advisory=True)), (0, [("pull request description", 1, "EmDash")]))
        event = Path(scratch, "event.json")
        event.write_text(json.dumps({"pull_request": {"body": "Also %s here.\n" % EM}}))
        code, report = judge(self, {}, {"a.md": "fine\n"}, env={"GITHUB_EVENT_PATH": str(event)})
        self.assertEqual((code, rules(report, advisory=True)), (0, [("pull request description", 1, "EmDash")]))


class StandInVale(unittest.TestCase):
    """A fake vale first on PATH that reports the pinned version, so the resolver takes it."""

    def setUp(self):
        self.bin = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.bin, True)
        fake = Path(self.bin, "vale")
        fake.write_text("#!/bin/sh\nif [ \"$1\" = --version ]; then echo 'vale version 3.22.0'; exit 0; fi\n"
                        "echo 'E100 [vale] Runtime error' >&2\nexit 2\n")
        fake.chmod(fake.stat().st_mode | stat.S_IEXEC)

    def test_a_vale_that_fails_is_could_not_run_even_under_soak(self):
        repo = make_repo(self, {"README.md": "x\n"})
        commit(repo, {"a.md": "text\n"})
        result = gate("prose", repo, "--base-ref", "HEAD~1", "--soak", env={"PATH": "%s:%s" % (self.bin, os.environ["PATH"])})
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn("vale exited 2: E100 [vale] Runtime error", result.stderr)


if __name__ == "__main__":
    unittest.main()
