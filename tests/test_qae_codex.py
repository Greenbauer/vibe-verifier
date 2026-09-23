"""The Codex lane of the QAE harness: the same harness as explore.yml outside the explorer block,
the harness prompt inlined with one lane-specific step, and the composite action carrying the
settings a non-interactive Codex run needs. All read the shipped files, so the test judges what a
consumer copies."""
import re
import unittest

from helpers import ROOT

CLAUDE = ROOT / "harnesses" / "qae" / "explore.yml"
CODEX = ROOT / "harnesses" / "qae" / "explore-codex.yml"
KEEPALIVE = ROOT / "harnesses" / "qae" / "codex-keepalive.yml"
PROMPT = ROOT / "harnesses" / "qae" / "prompt.md"
ACTION = ROOT / "actions" / "qae-codex" / "action.yml"

CLAUDE_STEP_4 = ("4. Post the verdict as a pull request comment, with exactly this command (no other form is allowed):\n"
                 "     gh pr comment PR_NUMBER --body-file qae-artifacts/verdict.md\n")
CODEX_STEP_4 = ("4. Do not post anything: this repository's workflow posts qae-artifacts/verdict.md as the pull\n"
                "   request comment after you finish.\n")


def inlined_prompt():
    text = CODEX.read_text()
    match = re.search(r"cat > qae-inputs/prompt\.md <<'PROMPT'\n(.*?)\n +PROMPT\n", text, re.DOTALL)
    return "".join(line[12:] if line.strip() else "\n" for line in match.group(1).splitlines(True)) + "\n"


class Prompt(unittest.TestCase):
    def test_the_prompt_is_the_harness_prompt_with_step_4_for_a_model_that_holds_no_token(self):
        expected = PROMPT.read_text()
        self.assertIn(CLAUDE_STEP_4, expected)
        expected = expected.replace(CLAUDE_STEP_4, CODEX_STEP_4)
        expected = (expected.replace("#PR_NUMBER", "#${{ github.event.pull_request.number }}")
                    .replace("REPOSITORY", "${{ github.repository }}")
                    .replace("SITE_URL", "http://localhost:3000"))
        self.assertEqual(inlined_prompt(), expected)


class Template(unittest.TestCase):
    def test_everything_outside_the_explorer_block_is_the_claude_template(self):
        claude, codex = CLAUDE.read_text(), CODEX.read_text()
        def after_header(text):
            return text[text.index("on:\n"):]
        def split(text):
            body = after_header(text)
            start = body.index("- name: Explore the acceptance criteria in a real browser")
            end = body.index("- name: Enforce the write scope")
            return body[:start], body[end:]
        c_head, c_tail = split(claude)
        x_head, x_tail = split(codex)
        self.assertEqual(c_tail, x_tail)
        self.assertEqual(c_head.replace("runs-on: ubuntu-latest   # CONSUMER", "RUNS-ON"),
                         x_head[:x_head.index("- name: Write the explorer's prompt")].replace(
                             "runs-on: [self-hosted, qae-codex]   # CONSUMER: a runner that holds the Codex login (harnesses/qae/README.md)", "RUNS-ON"))

    def test_the_explorer_holds_no_secret_and_the_workflow_posts_the_verdict(self):
        text = CODEX.read_text()
        explore = text[text.index("- name: Explore the acceptance criteria in a real browser"):text.index("- name: Post the verdict the explorer wrote")]
        self.assertNotIn("secrets.", explore)
        self.assertNotIn("GH_TOKEN", explore)
        self.assertIn("uses: Greenbauer/vibe-verifier/actions/qae-codex@", explore)
        self.assertIn("if: steps.criteria.outputs.count != '0'", explore)
        post = text[text.index("- name: Post the verdict the explorer wrote"):text.index("- name: Enforce the write scope")]
        self.assertIn('gh pr comment "$PR_NUMBER" --repo "$REPO" --body-file qae-artifacts/verdict.md', post)
        self.assertIn("if: steps.criteria.outputs.count != '0'", post)

    def test_every_catalog_pin_is_the_placeholder_a_consumer_replaces(self):
        for template, count in ((CODEX, 4), (KEEPALIVE, 1)):
            pins = re.findall(r"vibe-verifier/actions/[\w-]+@(\S+)( #[^\n]*)?", template.read_text())
            self.assertEqual(len(pins), count, template.name)
            for sha, comment in pins:
                self.assertEqual(sha, "0" * 40)
                self.assertIn("CONSUMER: pin the commit you subscribe to", comment)

    def test_the_keepalive_runs_on_the_same_runner_through_the_same_action(self):
        text = KEEPALIVE.read_text()
        self.assertIn("runs-on: [self-hosted, qae-codex]", text)
        self.assertIn("uses: Greenbauer/vibe-verifier/actions/qae-codex@", text)
        self.assertIn("schedule:", text)
        self.assertNotIn("secrets.", text)


class Action(unittest.TestCase):
    def test_the_exec_carries_the_three_settings_a_headless_run_needs(self):
        text = ACTION.read_text()
        exec_line = text[text.index('"$CODEX" exec'):text.index("< /dev/null") + len("< /dev/null")]
        self.assertIn("--sandbox danger-full-access", exec_line)
        self.assertIn("""-c 'approval_policy="never"'""", exec_line)
        self.assertIn("--skip-git-repo-check", exec_line)
        self.assertTrue(exec_line.endswith("< /dev/null"))

    def test_the_login_is_checked_never_written(self):
        text = ACTION.read_text()
        check = text[text.index("- name: Check the runner's Codex login"):text.index("- name: Explore the acceptance criteria")]
        self.assertIn("codex login --device-auth", check)
        self.assertIn('"chatgpt True"', check)
        self.assertNotIn("secrets.", text)
        self.assertNotIn("CODEX_AUTH_JSON", text)

    def test_the_cli_comes_from_the_lockfile(self):
        self.assertIn('tool codex', ACTION.read_text())
        lock = (ROOT / "tools" / "codex" / "package-lock.json").read_text()
        self.assertIn('"node_modules/@openai/codex"', lock)
        self.assertIn('"node_modules/@openai/codex-linux-x64"', lock)


if __name__ == "__main__":
    unittest.main()
