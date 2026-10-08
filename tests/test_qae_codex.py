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


def flaky_gh(directory, failures):
    """A PATH directory whose `gh` fails `failures` times, then succeeds, logging each call, and whose
    `sleep` returns at once and logs what it was asked to wait."""
    os.makedirs(directory, exist_ok=True)
    scripts = {"gh": '#!/bin/sh\necho call >> "%s/calls"\n[ "$(wc -l < "%s/calls")" -gt %d ]\n' % (directory, directory, failures),
               "sleep": '#!/bin/sh\necho "$1" >> "%s/sleeps"\n' % directory}
    for name, body in scripts.items():
        with open(os.path.join(directory, name), "w") as handle:
            handle.write(body)
        os.chmod(os.path.join(directory, name), 0o755)
    return directory


def lines(path):
    return Path(path).read_text().split() if os.path.exists(path) else []


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
                                env=clean_env({"SITE_URL": url, "SHARD_SHARE": "Your share of the criteria is AC2."}))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        written = Path(work, "qae-inputs", "prompt.md").read_text()
        self.assertEqual("".join(line[2:] if line.strip() else "\n" for line in written.splitlines(True)),
                         inlined_prompt().replace("SITE_URL", url).replace("SHARD_SHARE", "Your share of the criteria is AC2."))
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
        self.assertEqual(c_head.replace("runs-on: ubuntu-latest   # CONSUMER\n", "RUNS-ON\n"),
                         x_head[:x_head.index("- name: Write the explorer's prompt")].replace(
                             "runs-on: [self-hosted, qae-codex]   # CONSUMER: a runner that holds the Codex login (harnesses/qae/README.md)\n", "RUNS-ON\n"))

    def test_the_explorer_holds_no_secret_and_the_workflow_posts_the_verdict(self):
        text = CODEX.read_text()
        explore = text[text.index("- name: Explore the acceptance criteria in a real browser"):text.index("- name: Post the verdict the explorer wrote")]
        self.assertNotIn("secrets.", explore)
        self.assertNotIn("GH_TOKEN", explore)
        self.assertIn("uses: Greenbauer/vibe-verifier/actions/qae-codex@", explore)
        post = text[text.index("- name: Post the verdict the explorer wrote"):text.index("- name: Enforce the write scope")]
        self.assertIn('gh pr comment "$PR_NUMBER" --repo "$REPO" --body-file qae-artifacts/verdict.md', post)

    def post_verdict(self, failures, verdict="acceptance-check: AC1 -- PASS -- x (qae/AC1.md::step 1: x)\n"):
        work = tempfile.mkdtemp(prefix="vv-verdict-")
        self.addCleanup(shutil.rmtree, work, True)
        os.makedirs(os.path.join(work, "qae-artifacts"))
        Path(work, "qae-artifacts", "verdict.md").write_text(verdict)
        script = run_script(CODEX.read_text(), "- name: Post the verdict the explorer wrote", "- name: Enforce the write scope")
        result = subprocess.run(["bash", "-c", script], cwd=work, capture_output=True, text=True,
                                env=clean_env({"PATH": flaky_gh(os.path.join(work, "bin"), failures) + os.pathsep + os.environ["PATH"],
                                               "GH_TOKEN": "x", "PR_NUMBER": "1", "REPO": "o/r"}))
        return result, len(lines(os.path.join(work, "bin", "calls"))), lines(os.path.join(work, "bin", "sleeps"))

    def test_one_failed_post_does_not_throw_a_finished_exploration_away(self):
        result, calls, sleeps = self.post_verdict(1)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual((calls, sleeps), (2, ["15"]))

    def test_a_verdict_that_never_posts_fails_after_five_tries_and_an_empty_one_is_never_posted(self):
        result, calls, sleeps = self.post_verdict(99)
        self.assertEqual(result.returncode, 1)
        self.assertEqual((calls, sleeps), (5, ["15", "30", "45", "60"]))
        self.assertIn("::error::the verdict could not be posted after 5 attempts", result.stdout)
        empty, calls, _ = self.post_verdict(0, verdict="")
        self.assertEqual((empty.returncode, calls), (1, 0))

    def test_every_catalog_pin_is_the_placeholder_a_consumer_replaces(self):
        for template, count in ((CODEX, 8), (KEEPALIVE, 1)):
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
        exec_line = text[text.index('codex -- "$CODEX" exec'):text.index("< /dev/null") + len("< /dev/null")]
        self.assertIn("exec --json", exec_line)
        self.assertIn("--sandbox danger-full-access", exec_line)
        self.assertIn("""-c 'approval_policy="never"'""", exec_line)
        self.assertIn("--skip-git-repo-check", exec_line)
        # No session rollout on the runner: the Codex home persists between jobs, and a rollout keeps
        # everything the model read, site.md included.
        self.assertIn("exec --json --ephemeral", exec_line)
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
    reach playwright-mcp's secrets redaction, and a file that is not a cookies-only storage state
    stops the run before any model session."""

    def setUp(self):
        self.work = tempfile.mkdtemp(prefix="vv-state-")
        self.addCleanup(shutil.rmtree, self.work, True)
        self.temp = os.path.join(self.work, "runner-temp")
        os.mkdir(self.temp)
        self.codex = os.path.join(self.work, "codex")
        # Records the playwright-mcp arguments, and leaves what a real explorer leaves when it typed a
        # secret: playwright-mcp's session log holds the typed value, and the model may copy it.
        with open(self.codex, "w") as handle:
            handle.write('#!/bin/sh\nfor a in "$@"; do case "$a" in mcp_servers.playwright.args=*) '
                         'printf %s "${a#mcp_servers.playwright.args=}" > "$(dirname "$0")/args" ;; esac; done\n'
                         'if [ -n "$TYPED" ]; then mkdir -p qae-artifacts/session-1 qae-artifacts/qae\n'
                         '  printf \'"value": "%s"\\n\' "$TYPED" > qae-artifacts/session-1/session.md\n'
                         '  printf \'acceptance-check: AC1 -- PASS -- typed %s\\n\' "$TYPED" > qae-artifacts/verdict.md\n'
                         '  printf \'PNG%s\' "$TYPED" > qae-artifacts/qae/AC1-step-1.png; fi\n')
        os.chmod(self.codex, 0o755)
        Path(self.work, "prompt.md").write_text("prompt\n")

    def run_step(self, state=None, raw=None, secrets=None, typed=""):
        path = ""
        if state is not None or raw is not None:
            path = "state.json"
            Path(self.work, path).write_text(raw if raw is not None else json.dumps(state))
        secrets_file = ""
        if secrets is not None:
            secrets_file = os.path.join(self.temp, "login.json")
            Path(secrets_file).write_text(secrets if isinstance(secrets, str) else json.dumps(secrets))
        script = run_script(ACTION.read_text(), "- name: Explore the acceptance criteria in a real browser",
                            "- name: Redact the secrets from the artifacts")
        return subprocess.run(["bash", "-c", script], cwd=self.work, capture_output=True, text=True,
                              env=clean_env({"CODEX": self.codex, "PROMPT_FILE": "prompt.md", "MCP": "/opt/mcp/cli.js",
                                             "ARTIFACTS": "qae-artifacts", "STORAGE_STATE": path,
                                             "SECRETS_FILE": secrets_file, "TYPED": typed,
                                             "RUNNER_TEMP": self.temp, "GITHUB_ACTION_PATH": str(ACTION.parent),
                                             "GITHUB_REPOSITORY": "octo/demo", "GITHUB_RUN_ID": "1",
                                             "GITHUB_RUN_ATTEMPT": "1", "VV_HEAD_SHA": "0" * 40,
                                             "USAGE_ACCOUNT_ALIAS": ""}))

    def browser_args(self):
        return json.loads(Path(self.work, "args").read_text())

    def test_no_storage_state_starts_the_browser_with_none(self):
        result = self.run_step()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        args = self.browser_args()
        self.assertEqual(args[:-1], ["/opt/mcp/cli.js", "--browser", "chromium", "--headless", "--isolated",
                                     "--output-dir", "qae-artifacts", "--save-session", "--viewport-size",
                                     "1280x800", "--init-page"])
        # The page record of the same catalog commit: which page logged each console error.
        self.assertEqual(Path(args[-1]).resolve(), Path(ROOT, "actions", "qae-browser", "console-pages.js"))
        usage = json.loads(Path(self.temp, "vv-usage", "usage.json").read_text())
        self.assertEqual((usage["status"], usage["status_reason"]), ("unavailable", "no_final_usage"))

    def test_both_lanes_start_the_chromium_that_qae_browser_installs(self):
        # Without --browser, playwright-mcp starts the Google Chrome channel. A self-hosted runner
        # without Chrome then fails every browser tool ("Chromium distribution 'chrome' is not
        # found") and writes no session log, so the qae-artifacts gate fails the run.
        result = self.run_step()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.browser_args()[1:3], ["--browser", "chromium"])
        template = Path(ROOT, "harnesses", "qae", "explore.yml").read_text()
        self.assertIn('"args":["${{ steps.browser.outputs.mcp }}","--browser","chromium",', template)

    def test_the_storage_state_reaches_the_browser_and_every_cookie_value_is_redacted(self):
        other = dict(COOKIE, name="consent", value="all", httpOnly=False)
        result = self.run_step({"cookies": [COOKIE, other], "origins": []})
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        config = os.path.join(self.temp, "qae-codex-mcp.json")
        self.assertEqual(self.browser_args()[12:-1], ["--storage-state", os.path.realpath(os.path.join(self.work, "state.json")),
                                                     "--config", config, "--init-page"])
        self.assert_secret_names_hook()
        self.assertEqual(json.loads(Path(config).read_text()),
                         {"secrets": {"VV_COOKIE_1": COOKIE["value"], "VV_COOKIE_2": "all"}})
        self.assertEqual(stat.S_IMODE(os.stat(config).st_mode), 0o600)
        self.assertNotIn(COOKIE["value"], result.stdout + result.stderr)

    def assert_secret_names_hook(self):
        # The last argument, after --config: the hook that types a secret's value for its NAME in
        # every text-entry call, not only in the two tools playwright-mcp resolves it in. Without it
        # a sign-in through browser_run_code_unsafe sends the name (tests/test_qae_secret_names.py).
        self.assertEqual(Path(self.browser_args()[-1]).resolve(), Path(ROOT, "actions", "qae-browser", "secret-names.js"))

    def redact(self):
        script = run_script(ACTION.read_text(), "- name: Redact the secrets from the artifacts", "- name: Keep numeric usage")
        return subprocess.run(["bash", "-c", script], cwd=self.work, capture_output=True, text=True,
                              env=clean_env({"ARTIFACTS": "qae-artifacts", "RUNNER_TEMP": self.temp}))

    def test_a_login_reaches_the_browser_by_name_and_is_redacted_from_every_text_artifact(self):
        # A generated password can hold all three quote characters, which a dotenv file cannot carry.
        cases = {"a quote-free value": "s3cret-Login-Value", "every quote, a hash and a backslash": "It's`a\"#\\mix"}
        for label, value in cases.items():
            with self.subTest(label):
                for leftover in ("qae-artifacts", "args"):
                    shutil.rmtree(os.path.join(self.work, leftover), True)
                    if os.path.exists(os.path.join(self.work, leftover)):
                        os.remove(os.path.join(self.work, leftover))
                result = self.run_step({"cookies": [COOKIE], "origins": []}, secrets={"QAE_TRAVELER_PASSWORD": value},
                                       typed=value)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                config = os.path.join(self.temp, "qae-codex-mcp.json")
                self.assertEqual(self.browser_args()[-4:-1], ["--config", config, "--init-page"])
                self.assert_secret_names_hook()
                self.assertEqual(json.loads(Path(config).read_text()),
                                 {"secrets": {"VV_COOKIE_1": COOKIE["value"], "QAE_TRAVELER_PASSWORD": value}})
                self.assertNotIn(value, result.stdout + result.stderr)
                redacted = self.redact()
                self.assertEqual(redacted.returncode, 0, redacted.stdout + redacted.stderr)
                self.assertIn("redacted 2 secret value(s) in 2 file(s)", redacted.stdout)
                for name in ("session-1/session.md", "verdict.md"):
                    text = Path(self.work, "qae-artifacts", name).read_text()
                    self.assertNotIn(value, text)
                    self.assertIn("<secret>QAE_TRAVELER_PASSWORD</secret>", text)
                self.assertEqual(Path(self.work, "qae-artifacts", "qae", "AC1-step-1.png").read_bytes(), b"PNG" + value.encode())
                for name in ("qae-codex-redact.json", "qae-codex-mcp.json"):
                    self.assertFalse(os.path.exists(os.path.join(self.temp, name)), name + " outlived the redaction")

    def test_escaped_and_encoded_forms_of_a_value_are_redacted_and_no_fragment_survives(self):
        # A JSON writer (the session log) or a URL leaves a value escaped, so the raw bytes alone
        # would let `\\"`, `\\\\`, `\\u00e9` or `%40` forms through; and a value that is part of a
        # longer one must not be replaced first, or the longer one's tail survives.
        import urllib.parse
        value = 'pa"ss\\wo @rd\u00e9!'
        longer = "abc123XYZ-long-tail"
        result = self.run_step(secrets={"QAE_PASSWORD": value, "QAE_SHORT": "abc123XYZ", "QAE_LONG": longer})
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        forms = [value, json.dumps(value)[1:-1], json.dumps(value, ensure_ascii=False)[1:-1],
                 urllib.parse.quote(value, safe=""), urllib.parse.quote_plus(value)]
        self.assertEqual(len(set(forms)), 5)
        artifacts = Path(self.work, "qae-artifacts")
        (artifacts / "session-1").mkdir(parents=True, exist_ok=True)
        (artifacts / "session-1" / "session.md").write_text(
            "\n".join('"value": "%s"' % form for form in forms) + "\nlong: %s\n" % longer, encoding="utf-8")
        redacted = self.redact()
        self.assertEqual(redacted.returncode, 0, redacted.stdout + redacted.stderr)
        text = (artifacts / "session-1" / "session.md").read_text(encoding="utf-8")
        for form in forms:
            self.assertNotIn(form, text)
        self.assertEqual(text.count("<secret>QAE_PASSWORD</secret>"), 5, text)
        self.assertIn("long: <secret>QAE_LONG</secret>\n", text)
        self.assertNotIn("long-tail", text)

    def test_a_value_inside_json_strings_nested_to_any_depth_is_redacted(self):
        # playwright-mcp's session log writes each tool result inside a JSON block, and the result of
        # browser_run_code_unsafe is itself JSON, so a value its snippet returned is escaped twice
        # there (the pinned 0.0.81, 2026-10-08; tests/test_qae_secret_names.py reads it from the
        # real log). A file that quotes the log escapes it once more. Each writer on the way may or
        # may not spell a non-ASCII character as \uXXXX, and the text around a value stays as it was.
        import itertools
        value = 'pa"ss\\wo\trd\u00e9\U0001f511!'
        result = self.run_step(secrets={"QAE_PASSWORD": value, "QAE_PLAIN": "plain-Value-0123"})
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        forms = set()
        for writers in itertools.product((True, False), repeat=3):
            form = value
            for ascii_only in writers:
                form = json.dumps(form, ensure_ascii=ascii_only)[1:-1]
                forms.add(form)
        self.assertEqual(len(forms), 9)
        artifacts = Path(self.work, "qae-artifacts")
        (artifacts / "session-1").mkdir(parents=True, exist_ok=True)
        log = artifacts / "session-1" / "session.md"
        log.write_text("".join('  "result": "\\"%s\\"", plain-Value-0123\n' % form for form in sorted(forms)), encoding="utf-8")
        redacted = self.redact()
        self.assertEqual(redacted.returncode, 0, redacted.stdout + redacted.stderr)
        self.assertEqual(log.read_text(encoding="utf-8"),
                         '  "result": "\\"<secret>QAE_PASSWORD</secret>\\"", <secret>QAE_PLAIN</secret>\n' * 9)

    def test_a_malformed_secrets_file_stops_the_run_before_the_model(self):
        cases = {
            "not json": "{",
            "a list": json.dumps(["x"]),
            "empty": json.dumps({}),
            "a lower-case name": json.dumps({"password": "x"}),
            "a cookie name": json.dumps({"VV_COOKIE_1": "x"}),
            "an empty value": json.dumps({"PASSWORD": ""}),
        }
        for label, raw in cases.items():
            with self.subTest(label):
                result = self.run_step(secrets=raw)
                self.assertEqual(result.returncode, 2, label + ": " + result.stdout + result.stderr)
                self.assertIn("::error::the secrets file", result.stdout)
                self.assertFalse(os.path.exists(os.path.join(self.work, "args")), "codex ran")

    def test_nothing_to_redact_without_secrets(self):
        result = self.run_step()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertNotIn("--config", self.browser_args())
        self.assertEqual(self.browser_args().count("--init-page"), 1)
        redacted = self.redact()
        self.assertEqual((redacted.returncode, redacted.stdout), (0, ""))

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
                                               "RUNNER_TEMP": self.temp, "GITHUB_ACTION_PATH": str(ACTION.parent),
                                               "GITHUB_REPOSITORY": "octo/demo", "GITHUB_RUN_ID": "1",
                                               "GITHUB_RUN_ATTEMPT": "1", "VV_HEAD_SHA": "0" * 40,
                                               "USAGE_ACCOUNT_ALIAS": ""}))
        self.assertEqual(result.returncode, 2)
        self.assertIn("::error::the storage state gone.json is not a readable JSON file", result.stdout)
        self.assertFalse(os.path.exists(os.path.join(self.work, "args")))

    def test_the_template_hands_the_site_steps_storage_state_to_the_explorer(self):
        text = CODEX.read_text()
        explore = text[text.index("- name: Explore the acceptance criteria in a real browser"):text.index("- name: Post the verdict the explorer wrote")]
        self.assertIn("          storage-state: ${{ steps.site.outputs.storage-state }}\n", explore)
        self.assertIn("          secrets-file: ${{ steps.site.outputs.secrets-file }}\n", explore)
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
