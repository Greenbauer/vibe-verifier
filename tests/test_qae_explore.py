"""The explore template: its write-scope step run as the shell it is, and the order of its steps.
Both read harnesses/qae/explore.yml itself, so the test judges what a consumer copies."""
import os
import re
import subprocess
import tempfile
import unittest
from pathlib import Path

from helpers import ROOT, clean_env, commit, git, write

TEMPLATE = ROOT / "harnesses" / "qae" / "explore.yml"


def scope_script():
    text = TEMPLATE.read_text()
    step = text[text.index("- name: Enforce the write scope"):text.index("- name: Keep the evidence")]
    match = re.search(r"^( +)run: \|\n((?:\1 .*\n|\n)+)", step, re.MULTILINE)
    indent = len(match.group(1)) + 2
    return "".join(line[indent:] for line in match.group(2).splitlines(True))


class WriteScope(unittest.TestCase):
    """A clone of a PR branch after claude-code-action reset the config paths it distrusts:
    the PR changed CLAUDE.md and added .mcp.json, so the runner shows both as changed."""

    def setUp(self):
        origin = tempfile.mkdtemp(prefix="vv-origin-")
        self.addCleanup(lambda: subprocess.run(["rm", "-rf", origin]))
        git(origin, "init", "-q", "-b", "main")
        git(origin, "config", "user.email", "t@example.com")
        git(origin, "config", "user.name", "t")
        commit(origin, {"CLAUDE.md": "base rules\n", "app.js": "1\n"}, "base")
        git(origin, "checkout", "-q", "-b", "pr")
        commit(origin, {"CLAUDE.md": "pr rules\n", ".mcp.json": "{}\n", "app.js": "2\n"}, "pr")
        self.repo = tempfile.mkdtemp(prefix="vv-clone-")
        self.addCleanup(lambda: subprocess.run(["rm", "-rf", self.repo]))
        subprocess.run(["git", "clone", "-q", "-b", "pr", origin, self.repo], check=True, env=clean_env())
        # what restore-config.ts does: base's copy where base has one, gone where it does not
        git(self.repo, "checkout", "origin/main", "--", "CLAUDE.md")
        os.remove(os.path.join(self.repo, ".mcp.json"))
        git(self.repo, "reset", "-q", "--", "CLAUDE.md", ".mcp.json")

    def run_step(self):
        return subprocess.run(["bash", "-c", scope_script()], cwd=self.repo, capture_output=True, text=True,
                              env=clean_env({"BASE": "origin/main"}))

    def test_the_restore_alone_is_not_a_write(self):
        status = subprocess.run(["git", "status", "--porcelain"], cwd=self.repo, capture_output=True, text=True).stdout
        self.assertEqual(sorted(status.splitlines()), [" D .mcp.json", " M CLAUDE.md"])
        result = self.run_step()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("write scope held", result.stdout)

    def test_artifacts_are_in_scope(self):
        write(self.repo, {"qae-artifacts/qae/AC1.md": "- step 1: x -> y\n", "qae-inputs/pr-body.md": "x\n"})
        self.assertEqual(self.run_step().returncode, 0)

    def test_a_restored_path_the_explorer_then_edits_still_fails(self):
        write(self.repo, {"CLAUDE.md": "base rules\nand mine\n"})
        result = self.run_step()
        self.assertEqual(result.returncode, 1)
        self.assertIn("CLAUDE.md", result.stdout)
        self.assertNotIn(".mcp.json", result.stdout)

    def test_a_write_outside_the_scope_fails(self):
        write(self.repo, {"app.js": "3\n", "qae-artifacts/ok.png": ""})
        result = self.run_step()
        self.assertEqual(result.returncode, 1)
        self.assertIn(" M app.js", result.stdout)
        self.assertNotIn("CLAUDE.md", result.stdout)

    def test_an_unknown_base_excuses_nothing(self):
        result = subprocess.run(["bash", "-c", scope_script()], cwd=self.repo, capture_output=True, text=True,
                                env=clean_env({"BASE": "origin/nope"}))
        self.assertEqual(result.returncode, 1)
        self.assertIn("CLAUDE.md", result.stdout)


class Steps(unittest.TestCase):
    def test_the_explorer_runs_only_when_there_is_a_criterion(self):
        text = TEMPLATE.read_text()
        read = text.index("- name: Read the criteria")
        explore = text.index("- name: Explore the acceptance criteria in a real browser")
        self.assertLess(read, explore)
        self.assertIn("id: criteria", text[read:explore])
        self.assertIn("uses: Greenbauer/vibe-verifier/actions/criteria@", text[read:explore])
        self.assertIn("if: steps.criteria.outputs.count != '0'", text[explore:explore + 400])

    def test_the_only_comment_the_explorer_may_post_is_the_verdict_file(self):
        # gh opens --body-file itself, so a wildcard rule would let a hostile PR body post any file.
        text = TEMPLATE.read_text()
        tools = re.search(r'--allowed-tools "([^"]+)"', text).group(1).split(",")
        comment_rules = [rule for rule in tools if "gh pr comment" in rule]
        self.assertEqual(comment_rules, ["Bash(gh pr comment ${{ github.event.pull_request.number }} --body-file qae-artifacts/verdict.md)"])
        self.assertIn("gh pr comment ${{ github.event.pull_request.number }} --body-file qae-artifacts/verdict.md\n", text)

    def test_a_consumer_can_tell_the_explorer_how_to_sign_in(self):
        # The file is written by the consumer's build step, which runs before the model, and the
        # explorer may read qae-inputs/ and nothing else outside its artifacts.
        text = TEMPLATE.read_text()
        self.assertIn("If qae-inputs/site.md exists, read it before anything else.", text)
        self.assertIn("Read(qae-inputs/**)", text)
        self.assertLess(text.index("- name: Build and start the site under test"),
                        text.index("- name: Explore the acceptance criteria in a real browser"))

    def test_the_browser_toolchain_is_installed_from_the_lockfile_not_resolved_at_run_time(self):
        text = TEMPLATE.read_text()
        self.assertNotIn("npx -y", text)
        self.assertIn("uses: Greenbauer/vibe-verifier/actions/qae-browser@", text)
        self.assertIn('"command":"node","args":["${{ steps.browser.outputs.mcp }}"', text)
        self.assertLess(text.index("id: browser"), text.index("- name: Explore the acceptance criteria"))

    def test_every_catalog_pin_is_the_placeholder_a_consumer_replaces(self):
        pins = re.findall(r"vibe-verifier/actions/[\w-]+@(\S+)( #[^\n]*)?", TEMPLATE.read_text())
        self.assertEqual(len(pins), 3)
        for sha, comment in pins:
            self.assertEqual(sha, "0" * 40)
            self.assertIn("CONSUMER: pin the commit you subscribe to", comment)


if __name__ == "__main__":
    unittest.main()
