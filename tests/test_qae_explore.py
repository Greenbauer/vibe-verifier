"""The explore template: its write-scope, site and verify-input steps run as the shell they are, the
one site URL it carries from the site step to the prompt and the gate, and the order of its steps.
All read harnesses/qae/explore.yml itself, so the test judges what a consumer copies."""
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from helpers import ROOT, clean_env, commit, git, write
from test_qae_artifacts import CLEAN_REQUESTS, session_with
from test_review_harness import prompt_block

TEMPLATE = ROOT / "harnesses" / "qae" / "explore.yml"
PROMPT = ROOT / "harnesses" / "qae" / "prompt.md"
MANIFEST = ROOT / "harnesses" / "qae" / "manifest"


def step_script(start, end):
    text = TEMPLATE.read_text()
    step = text[text.index(start):text.index(end)]
    match = re.search(r"^( +)run: \|\n((?:\1 .*\n|\n)+)", step, re.MULTILINE)
    indent = len(match.group(1)) + 2
    return "".join(line[indent:] if line.strip() else "\n" for line in match.group(2).splitlines(True))


def scope_script():
    return step_script("- name: Enforce the write scope", "- name: Keep the evidence")


def stub_bin(test, commands):
    """A PATH directory holding one shell script per command name."""
    path = tempfile.mkdtemp(prefix="vv-bin-")
    test.addCleanup(shutil.rmtree, path, True)
    for name, body in commands.items():
        with open(os.path.join(path, name), "w") as handle:
            handle.write("#!/bin/sh\n" + body)
        os.chmod(os.path.join(path, name), 0o755)
    return path


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


class SiteUrl(unittest.TestCase):
    """One URL, declared by the site step's `url` output: the prompt names it, the job output carries
    it to the verify job, which writes qae-inputs/site-url, which the manifest's gate line reads."""

    def setUp(self):
        self.work = tempfile.mkdtemp(prefix="vv-work-")
        self.addCleanup(shutil.rmtree, self.work, True)

    def test_the_default_site_step_declares_the_local_site(self):
        bin_dir = stub_bin(self, {"npm": "exit 0\n", "curl": "exit 0\n"})
        output = Path(self.work) / "github_output"
        output.write_text("")
        script = step_script("- name: Build and start the site under test", "- name: Install the browser toolchain")
        result = subprocess.run(["bash", "-e", "-c", script], cwd=self.work, capture_output=True, text=True,
                                env=clean_env({"PATH": bin_dir + os.pathsep + os.environ["PATH"], "GITHUB_OUTPUT": str(output)}))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(output.read_text(), "url=http://localhost:3000\n")
        self.assertIn("        id: site\n", TEMPLATE.read_text())

    def test_the_prompt_is_the_harness_prompt_naming_the_declared_url(self):
        expected = (PROMPT.read_text().replace("PR_NUMBER", "${{ github.event.pull_request.number }}")
                    .replace("REPOSITORY", "${{ github.repository }}")
                    .replace("SITE_URL", "${{ steps.site.outputs.url }}"))
        self.assertEqual(prompt_block(TEMPLATE.read_text()), expected)

    def test_the_declared_url_reaches_the_gate_through_the_verify_job(self):
        text = TEMPLATE.read_text()
        self.assertIn("    outputs:\n      site-url: ${{ steps.site.outputs.url }}\n    steps:\n", text)
        self.assertIn("          SITE_URL: ${{ needs.explore.outputs.site-url }}\n", text)
        preview = "https://site-git-feat-team.vercel.app"
        bin_dir = stub_bin(self, {"gh": 'case "$1" in\n'
                                        '  pr) printf "## Acceptance criteria\\n\\n- The quote page loads\\n" ;;\n'
                                        '  api) printf "acceptance-check: AC1 -- PASS -- loaded (qae/AC1.md::step 1: x)\\n" ;;\n'
                                        'esac\n'})
        script = step_script("- name: Write the three declared inputs", "- uses: Greenbauer/vibe-verifier/actions/gates@")
        result = subprocess.run(["bash", "-e", "-c", script], cwd=self.work, capture_output=True, text=True,
                                env=clean_env({"PATH": bin_dir + os.pathsep + os.environ["PATH"], "GH_TOKEN": "x",
                                               "PR_NUMBER": "7", "REPO": "o/r", "SITE_URL": preview}))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(Path(self.work, "qae-inputs", "site-url").read_text(), preview + "\n")
        # The run's artifacts: the preview answered 500, the local default 404; only the declared site counts.
        write(self.work, {
            "qae-artifacts/qae/AC1.md": "- step 1: x\n", "qae-artifacts/qae/AC1-step-1.png": "png",
            "qae-artifacts/session-1/session.md": session_with(CLEAN_REQUESTS + [
                "[GET] %s/api/quote => [500] Internal Server Error" % preview,
                "[GET] http://localhost:3000/gone => [404] Not Found"]),
        })
        line = [entry for entry in MANIFEST.read_text().splitlines() if entry.startswith("qae-artifacts ")]
        self.assertEqual(len(line), 1)
        self.assertIn("--site-file qae-inputs/site-url", line[0])
        gate = subprocess.run([sys.executable, str(ROOT / "gates" / "qae_artifacts.py"), *shlex.split(line[0])[1:]],
                              cwd=self.work, capture_output=True, text=True, env=clean_env())
        self.assertEqual(gate.returncode, 1, gate.stdout + gate.stderr)
        self.assertIn("[GET] %s/api/quote" % preview, gate.stdout)
        self.assertNotIn("localhost:3000/gone", gate.stdout)


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
