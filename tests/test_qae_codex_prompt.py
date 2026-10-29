"""The Codex lane's prompt: the harness prompt inlined with one lane-specific step, written by the shell so a URL
the site step declared stays text. Split from test_qae_codex.py, which tests the rest of the lane."""
import os
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from helpers import ROOT, clean_env
from test_qae_codex import CODEX

PROMPT = ROOT / "harnesses" / "qae" / "prompt.md"

STEP_4 = ("4. Do not post anything: the workflow turns qae-artifacts/verdict.md into the pull request's one\n"
          "   QA review comment after you finish.\n")


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


class Prompt(unittest.TestCase):
    def test_the_prompt_is_the_harness_prompt_and_the_model_posts_nothing(self):
        # docs/comments.md: the verify job's QA review is the one comment; neither lane's model posts.
        expected = PROMPT.read_text()
        self.assertIn(STEP_4, expected)
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


if __name__ == "__main__":
    unittest.main()
