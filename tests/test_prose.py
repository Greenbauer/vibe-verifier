"""The prose gate: the real pinned Vale on fixture repositories, and a stand-in Vale for the edge where
it cannot produce a verdict."""
import json
import os
import shutil
import stat
import tempfile
import unittest
from pathlib import Path

from helpers import commit, gate, git, make_repo

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

    def test_a_renamed_file_blocks_only_on_the_lines_it_gains(self):
        repo = make_repo(self, {"a.md": "Old %s line.\n" % EM + "".join("Line %d.\n" % n for n in range(20))})
        git(repo, "mv", "a.md", "b.md")
        commit(repo, {"b.md": Path(repo, "b.md").read_text() + "New %s line.\n" % EM})
        result = gate("prose", repo, "--base-ref", "HEAD~1", "--format", "json")
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertEqual(rules(json.loads(result.stdout)), [("b.md", 22, "EmDash")])

    def test_a_document_cannot_switch_the_rules_off(self):
        text = ("<!-- vale off -->\nOne %s here.\n<!-- vale on -->\n\n<!-- vale VibeVerifier.EmDash = NO -->\nTwo %s here.\n"
                % (EM, EM))
        code, report = judge(self, {}, {"a.md": text, "b.mdx": "{/* vale off */}\nThree %s here.\n" % EM})
        self.assertEqual(code, 1)
        self.assertEqual(rules(report), [("a.md", 2, "EmDash"), ("a.md", 6, "EmDash"), ("b.mdx", 2, "EmDash")])

    def test_an_mdx_comment_is_not_judged(self):
        self.assertEqual(judge(self, {}, {"a.mdx": "{/* draft %s note */}\nText.\n" % EM})[0], 0)

    def test_an_odd_file_name_encoding_or_binary_file_does_not_stop_the_gate(self):
        repo = make_repo(self, {"README.md": "x\n"})
        Path(repo, "-x.md").write_text("Dash %s here.\n" % EM)
        Path(repo, "latin.md").write_bytes("café %s ok\n".encode("latin-1", errors="replace").replace(b"?", b"-"))
        Path(repo, "image.md").write_bytes(b"\x89PNG\0\0\0")
        commit(repo, {})
        result = gate("prose", repo, "--base-ref", "HEAD~1", "--format", "json")
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertEqual(rules(json.loads(result.stdout)), [("-x.md", 1, "EmDash")])

    def test_a_symbolic_link_is_not_judged_and_does_not_stop_the_gate(self):
        repo = make_repo(self, {"AGENTS.md": "Old %s line.\n" % EM})
        os.symlink("AGENTS.md", os.path.join(repo, "CLAUDE.md"))
        os.symlink("missing.md", os.path.join(repo, "gone.md"))
        commit(repo, {"new.md": "New %s line.\n" % EM})
        result = gate("prose", repo, "--base-ref", "HEAD~1", "--format", "json")
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertEqual(rules(json.loads(result.stdout)), [("new.md", 1, "EmDash")])

    def test_a_link_turned_into_a_file_is_judged(self):
        repo = make_repo(self, {"AGENTS.md": "Shared.\n"})
        os.symlink("AGENTS.md", os.path.join(repo, "CLAUDE.md"))
        commit(repo, {})
        os.remove(os.path.join(repo, "CLAUDE.md"))
        commit(repo, {"CLAUDE.md": "Own file %s new.\n" % EM})
        result = gate("prose", repo, "--base-ref", "HEAD~1", "--format", "json")
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertEqual(rules(json.loads(result.stdout)), [("CLAUDE.md", 1, "EmDash")])

    def test_a_repository_vale_config_does_not_change_the_verdict(self):
        config = "StylesPath = styles\nMinAlertLevel = suggestion\n[*.md]\nBasedOnStyles =\nVibeVerifier.EmDash = NO\n"
        code, report = judge(self, {".vale.ini": config}, {"a.md": "One %s here.\n" % EM})
        self.assertEqual((code, rules(report)), (1, [("a.md", 1, "EmDash")]))

    def test_a_rule_opening_the_file_is_not_read_as_front_matter(self):
        # The closing table keeps its |---| row: a gate that blanked every --- line, not only the opening one,
        # would break it and judge the em dash in its cell.
        text = ("---\n\nA heading line.\n\n| a | b |\n---\n\nAn aside %s here.\n\n| a | b |\n|---|---|\n| x %s y | z |\n"
                % (EM, EM))
        # b.md's front matter is valid, so it stays front matter: Vale skips a YAML comment there, and would judge
        # the comment's em dash only if the opening --- had been blanked.
        code, report = judge(self, {}, {"a.md": text, "b.md": "---\n# note %s x\ntitle: fine\n---\n\nText.\n" % EM})
        self.assertEqual((code, rules(report)), (1, [("a.md", 8, "EmDash")]))

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

    def run_with(self, stderr):
        """The gate on a one-file change, with a stand-in vale that writes stderr and exits 2."""
        bin_dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, bin_dir, True)
        fake = Path(bin_dir, "vale")
        fake.write_text("#!/bin/sh\nif [ \"$1\" = --version ]; then echo 'vale version 3.22.0'; exit 0; fi\n"
                        "printf '%%s\\n' '%s' >&2\nexit 2\n" % stderr)
        fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
        repo = make_repo(self, {"README.md": "x\n"})
        commit(repo, {"a.md": "text\n"})
        return gate("prose", repo, "--base-ref", "HEAD~1", "--soak", env={"PATH": "%s:%s" % (bin_dir, os.environ["PATH"])})

    def test_a_vale_that_fails_is_could_not_run_even_under_soak(self):
        result = self.run_with("E100 [vale] Runtime error")
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn("vale exited 2: E100 [vale] Runtime error", result.stderr)

    def test_a_vale_json_error_names_its_code_file_and_text(self):
        cases = {'{"Code": "E100", "Path": "docs/a.md", "Text": "boom"}': "vale exited 2: E100 a.md: boom",
                 '{"Code": "E100", "Path": "", "Text": "boom"}': "vale exited 2: E100: boom"}
        for stderr, message in cases.items():
            with self.subTest(stderr=stderr):
                result = self.run_with(stderr)
                self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
                self.assertIn(message, result.stderr)
                self.assertNotIn("crashed", result.stderr)

    def test_a_vale_error_that_is_json_but_not_an_object_is_could_not_run(self):
        for stderr in ("null", "[]", '"text"'):
            with self.subTest(stderr=stderr):
                result = self.run_with(stderr)
                self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
                self.assertIn("vale exited 2: %s" % stderr, result.stderr)
                self.assertNotIn("crashed", result.stderr)


if __name__ == "__main__":
    unittest.main()
