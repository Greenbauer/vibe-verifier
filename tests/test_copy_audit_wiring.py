"""Where the copy audit sits in the QAE harness: the Codex lane's grade step (harnesses/qae/explore-codex.yml), what it
hands actions/copy-audit, the action's call of the grader, and the manifest template's line. The audit itself is
tests/test_copy_audit.py's."""
import json
import os
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from helpers import ROOT


class Wiring(unittest.TestCase):
    def test_the_codex_lane_grades_after_the_explorer_and_before_the_evidence_is_checked_and_kept(self):
        text = (ROOT / "harnesses" / "qae" / "explore-codex.yml").read_text()
        order = [text.index(step) for step in ("- name: Explore the acceptance criteria in a real browser",
                                               "- name: Grade the pages' copy", "- name: Enforce the write scope",
                                               "- name: Keep the evidence")]
        self.assertEqual(order, sorted(order))
        self.assertIn("uses: Greenbauer/vibe-verifier/actions/copy-audit@", text)

    def test_the_codex_lane_grades_the_pages_of_the_site_its_site_step_declared(self):
        # The explore job has no qae-inputs/site-url (the verify job writes it, later), so a step that read that file
        # graded nothing on every consumer's first run: the step hands the action the site step's outputs instead.
        text = (ROOT / "harnesses" / "qae" / "explore-codex.yml").read_text()
        graded = text[text.index("- name: Grade the pages' copy"):text.index("- name: Enforce the write scope")]
        self.assertIn("          site: |\n            ${{ steps.site.outputs.url }}\n            ${{ steps.site.outputs.origins }}\n",
                      graded)
        self.assertNotIn("site-file", graded)

    def test_the_action_hands_the_declared_site_to_the_grader_as_a_site_file(self):
        action = (ROOT / "actions" / "copy-audit" / "action.yml").read_text()
        body = re.search(r"^      run: \|\n((?:        .*\n|\n)+)", action, re.MULTILINE).group(1)
        script = "".join(line[8:] if line.strip() else "\n" for line in body.splitlines(True))
        work = Path(tempfile.mkdtemp(prefix="vv-copy-site-"))
        self.addCleanup(shutil.rmtree, work)
        (work / "bin").mkdir()
        (work / "a" / "b").mkdir(parents=True)
        log = work / "args.json"
        stub = work / "bin" / "vibe-verifier"
        stub.write_text("import json, sys\nargs = sys.argv[1:]\nsite = args[args.index('--site-file') + 1] "
                        "if '--site-file' in args else None\njson.dump([args, open(site).read() if site else None], "
                        "open(%r, 'w'))\n" % str(log))

        def run(site, site_file=""):
            env = dict(os.environ, GITHUB_ACTION_PATH=str(work / "a" / "b"), RUNNER_TEMP=str(work), VV_ARTIFACTS="art",
                       VV_SITE=site, VV_SITE_FILE=site_file, VV_CODEX_HOME="", VV_MODEL="", VV_EFFORT="", VV_TIMEOUT="9",
                       VV_SANDBOX="read-only")
            subprocess.run(["bash", "-c", script], env=env, check=True)
            return json.loads(log.read_text())

        args, declared = run("https://pr-1.example.test\nhttps://api.example.test\n")
        self.assertEqual(declared.split(), ["https://pr-1.example.test", "https://api.example.test"])
        args, declared = run("\n\n")  # a site step that declared nothing: every page, as with no site at all
        self.assertNotIn("--site-file", args)
        (work / "given").write_text("https://given.example.test\n")
        args, declared = run("", site_file=str(work / "given"))
        self.assertEqual((args[args.index("--site-file") + 1], declared), (str(work / "given"), "https://given.example.test\n"))

    def test_the_prompt_has_one_copy_and_the_action_runs_the_catalogs_grader(self):
        sentence = "End with the grades as one JSON object, in the shape the output schema gives"
        holders = subprocess.run(["git", "-C", str(ROOT), "grep", "--untracked", "-l", sentence, "--", ":!tests"],
                                 capture_output=True, text=True).stdout.split()
        self.assertEqual(holders, ["harnesses/copy-audit/prompt.md"])
        self.assertIn('bin/vibe-verifier" copy-audit --grade-only', (ROOT / "actions" / "copy-audit" / "action.yml").read_text())

    def test_the_manifest_template_offers_the_line_to_the_codex_lane_in_soak(self):
        # Commented out: the Claude lane shares this template and has no grade step, so the line would cannot-run there.
        manifest = (ROOT / "harnesses" / "qae" / "manifest").read_text().splitlines()
        self.assertIn("# copy-audit --artifacts qae-artifacts --site-file qae-inputs/site-url --soak", manifest)
