"""The qae-artifacts gate and the Codex lane's redaction, run on what the pinned playwright-mcp really wrote.

tests/fixtures/playwright-mcp/ holds one recording per scenario, made by its record.py with each lane's own
playwright-mcp arguments against a local site whose pages fail in known ways. Its manifest.json says what the fixtures
were recorded with and what each page did. These tests need no browser and no network: each copies a recording into a
temporary qae-artifacts/ and runs the gate, or the lane's step, on it. The parsers fed every recorded file are in
tests/test_qae_recorded_parsers.py. `Recording` fails when the pinned toolchain or a lane's arguments change and the
fixtures were not recorded again, so a format a new build writes differently shows here, not in a consumer's run."""
import importlib.util
import json
import os
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from helpers import ROOT, clean_env, gate, make_repo
from test_qae_codex import ACTION, run_script

FIXTURES = ROOT / "tests" / "fixtures" / "playwright-mcp"
MANIFEST = json.loads((FIXTURES / "manifest.json").read_text())
SITE, OTHER = MANIFEST["origins"]["site"], MANIFEST["origins"]["other"]
RECORD = "tests/fixtures/playwright-mcp/record.py"
_spec = importlib.util.spec_from_file_location("record_fixtures", str(ROOT / RECORD))
RECORDER = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(RECORDER)
AGAIN = "record the fixtures again with `python3 %s` and commit what it writes" % RECORD


def recording(test, name):
    """A copy of scenario `name`'s recording, as the verify job holds the evidence: a directory named qae-artifacts,
    with a placeholder at each screenshot a step saved (the fixtures keep none)."""
    work = tempfile.mkdtemp(prefix="vv-recorded-")
    test.addCleanup(shutil.rmtree, work, True)
    root = os.path.join(work, "qae-artifacts")
    shutil.copytree(FIXTURES / name, root)
    for shot in MANIFEST["scenarios"][name]["screenshots"]:
        Path(root, shot).write_bytes(b"png")
    return root


class Recording(unittest.TestCase):
    def test_the_fixtures_were_recorded_with_the_pinned_toolchain(self):
        versions, digest = RECORDER.lockfile()
        self.assertEqual(versions, MANIFEST["versions"], "the pinned toolchain changed: " + AGAIN)
        self.assertEqual(digest, MANIFEST["lockfile_sha256"], "tools/qae-browser/package-lock.json changed: " + AGAIN)

    def test_the_fixtures_were_recorded_with_each_lanes_arguments(self):
        # A recording is the build's output for the arguments it was started with: another --init-page hook, viewport
        # or browser writes other files and other lines.
        self.assertEqual(RECORDER.lane_arguments(), MANIFEST["lanes"],
                         "a lane's playwright-mcp arguments changed (harnesses/qae/explore.yml, actions/qae-codex): " + AGAIN)


class Gate(unittest.TestCase):
    def setUp(self):
        self.repo = make_repo(self, {"README.md": "x"})

    def judge(self, root, *args):
        return gate("qae-artifacts", self.repo, "--artifacts", root, *args)

    def truth(self, name):
        return MANIFEST["scenarios"][name]

    def test_every_failure_on_the_errors_page_is_one_finding(self):
        # The site file declares the other origin as the site's own (an API on another host), so its 500 is judged;
        # the closed port's request is not, and Chromium's line for it is a console error like any other.
        root = recording(self, "errors")
        site = Path(root).parent / "site"
        site.write_text("%s/\n%s/\n" % (SITE, OTHER))
        result = self.judge(root, "--site-file", str(site))
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        truth = self.truth("errors")
        failed = [(method, url) for method, url, got in truth["requests"] if url.startswith((SITE, OTHER)) and got != "200"]
        for method, url in failed:
            self.assertRegex(result.stdout, r"\[%s\] %s(\s|$)" % (method, re.escape(url)))
        for kind, text in truth["console"]:
            self.assertIn("%s outside the allowlist: %s" % (kind, text.split("\n")[0]), result.stdout)
        self.assertNotIn(OTHER + "/api/data", result.stdout)
        self.assertNotIn("Password field is not contained in a form", result.stdout)  # a [VERBOSE] entry
        self.assertIn("%d finding(s)" % (len(failed) + len(truth["console"])), result.stdout)

    def test_a_cancelled_request_is_excused_only_by_its_own_answer(self):
        # /api/items and the logo answered on /dashboard and were cancelled on /developers beside /api/slow, which
        # never answered. The logo's answer is in the page record only: the explorer's record leaves out a successful
        # image. Each record was saved to a file, so the session log holds no request at all.
        result = self.judge(recording(self, "cancel"), "--site", SITE + "/")
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("request cancelled and never answered in this run, outside the allowlist: [GET] %s/api/slow " % SITE,
                      result.stdout)
        self.assertNotIn("/api/items", result.stdout)
        self.assertNotIn("/img/logo.svg", result.stdout)
        self.assertIn("1 finding(s)", result.stdout)

    def test_a_tab_a_link_opened_logs_its_own_errors_and_only_the_page_record_says_whose(self):
        root = recording(self, "tab")
        result = self.judge(root, "--site", SITE + "/")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        os.remove(os.path.join(root, "console-pages.jsonl"))
        result = self.judge(root, "--site", SITE + "/")
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        for kind, text in self.truth("tab")["console"]:
            self.assertIn("%s outside the allowlist: %s" % (kind, text.split("\n")[0]), result.stdout)

    def test_the_forms_422_to_the_marked_probe_is_excused(self):
        result = self.judge(recording(self, "probe"), "--site", SITE + "/")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("step 2, the invalid-input probe, was answered 422 /api/quantity while the page kept the controls "
                      "it used and showed new text: excused as a handled refusal", result.stdout)

    def test_a_step_retried_after_a_500_counts_once_the_changed_paths_are_known(self):
        root = recording(self, "retry")
        changed = Path(root).parent / "changed-files"
        changed.write_text("docs/unrelated.md\n")
        result = self.judge(root, "--site", SITE + "/", "--changed-files", str(changed))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("step 1 retried once as step 2 after an environment error: GET /api/orders answered 500", result.stdout)
        self.assertEqual(self.judge(root, "--site", SITE + "/").returncode, 1)

    def test_the_codex_lanes_three_sign_ins_by_name_pass(self):
        # Typed, filled and entered by a snippet: the site answered each 200, so no 400 is in either record.
        result = self.judge(recording(self, "secrets"), "--site", SITE + "/")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


class Redaction(unittest.TestCase):
    def test_no_spelling_of_a_secret_the_recording_holds_survives_the_codex_lanes_redaction(self):
        # The password field's value is in its snapshots (session.md and page-*.yml) and in what two scripts read back,
        # escaped once and twice; the action's step after the explorer is where it is kept out of the evidence.
        root = recording(self, "secrets")
        secret = MANIFEST["secret"]
        part = secret["value"].split('"')[0].encode()  # no JSON writer escapes any of it
        log = next(Path(root).glob("session-*/session.md"))
        self.assertIn(json.dumps(json.dumps(secret["value"]))[1:-1], log.read_text())
        self.assertTrue(any(part in path.read_bytes() for path in Path(root).glob("page-*.yml")))
        work = os.path.dirname(root)
        shutil.copyfile(FIXTURES / "secrets-redact.json", os.path.join(work, "qae-codex-redact.json"))
        script = run_script(ACTION.read_text(), "- name: Redact the secrets from the artifacts")
        result = subprocess.run(["bash", "-c", script], cwd=work, capture_output=True, text=True,
                                env=clean_env({"ARTIFACTS": "qae-artifacts", "RUNNER_TEMP": work}))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        for path in (path for path in Path(root).rglob("*") if path.is_file()):
            self.assertNotIn(part, path.read_bytes(), path.name)
        self.assertIn("<secret>%s</secret>" % secret["name"], log.read_text())


if __name__ == "__main__":
    unittest.main()
