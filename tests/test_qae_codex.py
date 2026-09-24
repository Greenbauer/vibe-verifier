"""The Codex lane of the QAE harness: the same harness as explore.yml outside the explorer block,
the harness prompt inlined with one lane-specific step, and the composite action carrying the
settings a non-interactive Codex run needs. All read the shipped files, so the test judges what a
consumer copies."""
import json
import os
import re
import shutil
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path

from helpers import ROOT, clean_env

CLAUDE = ROOT / "harnesses" / "qae" / "explore.yml"
CODEX = ROOT / "harnesses" / "qae" / "explore-codex.yml"
KEEPALIVE = ROOT / "harnesses" / "qae" / "codex-keepalive.yml"
PROMPT = ROOT / "harnesses" / "qae" / "prompt.md"
ACTION = ROOT / "actions" / "qae-codex" / "action.yml"
README = ROOT / "harnesses" / "qae" / "README.md"

CLAUDE_STEP_4 = ("4. Post the verdict as a pull request comment, with exactly this command (no other form is allowed):\n"
                 "     gh pr comment PR_NUMBER --body-file qae-artifacts/verdict.md\n")
CODEX_STEP_4 = ("4. Do not post anything: this repository's workflow posts qae-artifacts/verdict.md as the pull\n"
                "   request comment after you finish.\n")


def inlined_prompt():
    text = CODEX.read_text()
    match = re.search(r"cat > qae-inputs/prompt\.md <<'PROMPT'\n(.*?)\n +PROMPT\n", text, re.DOTALL)
    return "".join(line[12:] if line.strip() else "\n" for line in match.group(1).splitlines(True)) + "\n"


def prompt_script():
    text = CODEX.read_text()
    step = text[text.index("- name: Write the explorer's prompt"):text.index("- name: Explore the acceptance criteria in a real browser")]
    match = re.search(r"^( +)run: \|\n((?:\1 .*\n|\n)+)", step, re.MULTILINE)
    indent = len(match.group(1)) + 2
    return "".join(line[indent:] if line.strip() else "\n" for line in match.group(2).splitlines(True))


def run_script(text, start, end=None):
    """The `run: |` body of the step named `start`, dedented, as the shell will see it."""
    step = text[text.index(start):text.index(end) if end else None]
    match = re.search(r"^( +)run: \|\n((?:\1 .*\n|\n)+)", step, re.MULTILINE)
    indent = len(match.group(1)) + 2
    return "".join(line[indent:] if line.strip() else "\n" for line in match.group(2).splitlines(True))


def readme_site_step():
    """The site-step recipe in the README's "A preview behind Vercel SSO", as the shell will see it."""
    section = README.read_text().split("## A preview behind Vercel SSO", 1)[1]
    block = section.split("```yaml\n", 1)[1].split("```\n", 1)[0]
    return "set -euo pipefail\n" + "".join(line[10:] if line.strip() else "\n" for line in block.splitlines(True))


class Prompt(unittest.TestCase):
    def test_the_prompt_is_the_harness_prompt_with_step_4_for_a_model_that_holds_no_token(self):
        expected = PROMPT.read_text()
        self.assertIn(CLAUDE_STEP_4, expected)
        expected = expected.replace(CLAUDE_STEP_4, CODEX_STEP_4)
        expected = (expected.replace("#PR_NUMBER", "#${{ github.event.pull_request.number }}")
                    .replace("REPOSITORY", "${{ github.repository }}"))
        self.assertEqual(inlined_prompt(), expected)

    def test_the_prompt_names_the_url_the_site_step_declared_and_the_shell_never_parses_it(self):
        # Run as the shell it is: the URL arrives through env, so `&` and `$(...)` in it stay text.
        work = tempfile.mkdtemp(prefix="vv-prompt-")
        self.addCleanup(shutil.rmtree, work, True)
        os.mkdir(os.path.join(work, "qae-inputs"))
        url = "https://site-git-feat-team.vercel.app/?a=1&b=$(id)"
        self.assertIn("SITE_URL: ${{ steps.site.outputs.url }}", CODEX.read_text())
        result = subprocess.run(["bash", "-e", "-c", prompt_script()], cwd=work, capture_output=True, text=True,
                                env=clean_env({"SITE_URL": url}))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        written = Path(work, "qae-inputs", "prompt.md").read_text()
        self.assertEqual("".join(line[2:] if line.strip() else "\n" for line in written.splitlines(True)),
                         inlined_prompt().replace("SITE_URL", url))
        self.assertIn("The site built from this PR is running at %s.\n" % url, written)


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



COOKIE = {"name": "_vercel_jwt", "value": "test-cookie-value.0123456789_abcdef-ghijkl",
          "domain": "site-git-feat-team.vercel.app", "path": "/", "expires": 1790879832,
          "httpOnly": True, "secure": True, "sameSite": "Lax"}


class StorageState(unittest.TestCase):
    """The action's explore step run as the shell it is, with a stub codex that records the
    playwright-mcp arguments it was handed: the storage state reaches the browser, its cookie values
    reach playwright-mcp's --secrets redaction, and a file that is not a cookies-only storage state
    stops the run before any model session."""

    def setUp(self):
        self.work = tempfile.mkdtemp(prefix="vv-state-")
        self.addCleanup(shutil.rmtree, self.work, True)
        self.temp = os.path.join(self.work, "runner-temp")
        os.mkdir(self.temp)
        self.codex = os.path.join(self.work, "codex")
        with open(self.codex, "w") as handle:
            handle.write('#!/bin/sh\nfor a in "$@"; do case "$a" in mcp_servers.playwright.args=*) '
                         'printf %s "${a#mcp_servers.playwright.args=}" > "$(dirname "$0")/args" ;; esac; done\n')
        os.chmod(self.codex, 0o755)
        Path(self.work, "prompt.md").write_text("prompt\n")

    def run_step(self, state=None, raw=None):
        path = ""
        if state is not None or raw is not None:
            path = "state.json"
            Path(self.work, path).write_text(raw if raw is not None else json.dumps(state))
        script = run_script(ACTION.read_text(), "- name: Explore the acceptance criteria in a real browser")
        return subprocess.run(["bash", "-c", script], cwd=self.work, capture_output=True, text=True,
                              env=clean_env({"CODEX": self.codex, "PROMPT_FILE": "prompt.md", "MCP": "/opt/mcp/cli.js",
                                             "ARTIFACTS": "qae-artifacts", "STORAGE_STATE": path,
                                             "RUNNER_TEMP": self.temp}))

    def browser_args(self):
        return json.loads(Path(self.work, "args").read_text())

    def test_no_storage_state_starts_the_browser_with_none(self):
        result = self.run_step()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.browser_args(), ["/opt/mcp/cli.js", "--headless", "--isolated", "--output-dir", "qae-artifacts",
                                               "--save-session", "--viewport-size", "1280x800"])
        self.assertEqual(os.listdir(self.temp), [])

    def test_the_storage_state_reaches_the_browser_and_every_cookie_value_is_redacted(self):
        other = dict(COOKIE, name="consent", value="all", httpOnly=False)
        result = self.run_step({"cookies": [COOKIE, other], "origins": []})
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        secrets = os.path.join(self.temp, "qae-codex-cookies.env")
        self.assertEqual(self.browser_args()[8:], ["--storage-state", os.path.realpath(os.path.join(self.work, "state.json")),
                                                   "--secrets", secrets])
        self.assertEqual(Path(secrets).read_text(), 'VV_COOKIE_1="%s"\nVV_COOKIE_2="all"\n' % COOKIE["value"])
        self.assertEqual(stat.S_IMODE(os.stat(secrets).st_mode), 0o600)
        self.assertNotIn(COOKIE["value"], result.stdout + result.stderr)

    def test_a_file_that_is_not_a_cookies_only_storage_state_stops_the_run(self):
        cases = {
            "not json": (None, "{\"cookies\": ["),
            "a list": ([COOKIE], None),
            "another key": ({"cookies": [COOKIE], "origins": [], "extra": 1}, None),
            "local storage": ({"cookies": [COOKIE], "origins": [{"origin": "https://x", "localStorage": []}]}, None),
            "no cookies": ({"cookies": [], "origins": []}, None),
            "no domain": ({"cookies": [dict(COOKIE, domain="")]}, None),
            "a value with a separator": ({"cookies": [dict(COOKIE, value=COOKIE["value"] + ";x")]}, None),
            "a value with a quote": ({"cookies": [dict(COOKIE, value=COOKIE["value"] + '"')]}, None),
        }
        for name, (state, raw) in cases.items():
            with self.subTest(name):
                result = self.run_step(state, raw)
                self.assertEqual(result.returncode, 2, name + ": " + result.stdout + result.stderr)
                self.assertIn("::error::the storage state state.json", result.stdout)
                self.assertNotIn(COOKIE["value"], result.stdout + result.stderr)
                self.assertFalse(os.path.exists(os.path.join(self.work, "args")), "codex ran")
                self.assertEqual(os.listdir(self.temp), [])

    def test_a_missing_file_stops_the_run(self):
        script = run_script(ACTION.read_text(), "- name: Explore the acceptance criteria in a real browser")
        result = subprocess.run(["bash", "-c", script], cwd=self.work, capture_output=True, text=True,
                                env=clean_env({"CODEX": self.codex, "PROMPT_FILE": "prompt.md", "MCP": "/opt/mcp/cli.js",
                                               "ARTIFACTS": "qae-artifacts", "STORAGE_STATE": "gone.json",
                                               "RUNNER_TEMP": self.temp}))
        self.assertEqual(result.returncode, 2)
        self.assertIn("::error::the storage state gone.json is not a readable JSON file", result.stdout)
        self.assertFalse(os.path.exists(os.path.join(self.work, "args")))

    def test_the_template_hands_the_site_steps_storage_state_to_the_explorer(self):
        text = CODEX.read_text()
        explore = text[text.index("- name: Explore the acceptance criteria in a real browser"):text.index("- name: Post the verdict the explorer wrote")]
        self.assertIn("          storage-state: ${{ steps.site.outputs.storage-state }}\n", explore)
        self.assertIn("  storage-state:\n", ACTION.read_text())

    def test_the_readme_recipe_writes_a_storage_state_the_action_accepts(self):
        # curl stands in for Vercel: it answers the bypass headers, read from stdin, with the cookie in
        # the jar, in curl's own format for an HttpOnly cookie.
        bin_dir = os.path.join(self.work, "bin")
        os.mkdir(bin_dir)
        jar_line = "#HttpOnly_%s\tFALSE\t/\tTRUE\t%d\t_vercel_jwt\t%s" % (COOKIE["domain"], COOKIE["expires"], COOKIE["value"])
        with open(os.path.join(bin_dir, "curl"), "w") as handle:
            handle.write('#!/bin/sh\nheaders=$(cat)\ncase "$headers" in *"x-vercel-protection-bypass: s3cret"*"x-vercel-set-bypass-cookie: true"*) ;; *) exit 22 ;; esac\n'
                         'while [ $# -gt 0 ]; do [ "$1" = -c ] && printf "# Netscape HTTP Cookie File\\n\\n%s\\n" "' + jar_line + '" > "$2"; shift; done\n')
        os.chmod(os.path.join(bin_dir, "curl"), 0o755)
        output = Path(self.work, "github_output")
        output.write_text("")
        result = subprocess.run(["bash", "-c", readme_site_step()], cwd=self.work, capture_output=True, text=True,
                                env=clean_env({"PATH": bin_dir + os.pathsep + os.environ["PATH"], "BYPASS": "s3cret",
                                               "url": "https://" + COOKIE["domain"], "RUNNER_TEMP": self.temp,
                                               "GITHUB_OUTPUT": str(output)}))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        state_path = os.path.join(self.temp, "storage-state.json")
        self.assertEqual(output.read_text(), "storage-state=%s\n" % state_path)
        self.assertEqual(json.loads(Path(state_path).read_text()), {"cookies": [COOKIE], "origins": []})
        shutil.copy(state_path, os.path.join(self.work, "state.json"))
        accepted = self.run_step(raw=Path(state_path).read_text())
        self.assertEqual(accepted.returncode, 0, accepted.stdout + accepted.stderr)
        self.assertIn("--storage-state", self.browser_args())


if __name__ == "__main__":
    unittest.main()
