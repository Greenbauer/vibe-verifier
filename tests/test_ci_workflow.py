"""This repository's own workflows: ci.yml's step that lists the tests the full unittest run failed, for the
affected-test selection report, run under bash the way CI runs it, on a real unittest log; this suite run
inside a gate, as regression-test-fails-on-base and coverage-not-lost run it; and nightly.yml, which runs
coverage-not-lost on the whole suite once a night: its shape, and the step that picks the commit to compare from."""
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from helpers import ROOT, clean_env, commit, gate, git, make_repo, runner

CI = ROOT / ".github" / "workflows" / "ci.yml"
STEP = "List the tests the full run failed"
NIGHTLY = ROOT / ".github" / "workflows" / "nightly.yml"
BASE_STEP = "The commit to compare from"

# One suite with every way unittest reports a failure: an assertion, an error, a failed subtest (its
# parameters follow the id), a failed setUpClass (named by its class), and a module that does not import.
FAILING = {
    "tests/test_alpha.py": (
        "import unittest\nfrom app import alpha\n\n\nclass Alpha(unittest.TestCase):\n"
        "    def test_passes(self):\n        \"\"\"A docstring is printed on the next line.\"\"\"\n        self.assertEqual(alpha(), 1)\n\n"
        "    def test_fails(self):\n        self.assertEqual(alpha(), 2)\n\n"
        "    def test_errors(self):\n        raise RuntimeError(\"boom (in parentheses)\")\n\n"
        "    def test_cases(self):\n        for case in (1, 2):\n            with self.subTest(case=case):\n"
        "                self.assertEqual(case, alpha())\n"),
    "tests/test_beta.py": (
        "import unittest\n\n\nclass Beta(unittest.TestCase):\n    @classmethod\n    def setUpClass(cls):\n"
        "        raise RuntimeError(\"setup broke\")\n\n    def test_never_runs(self):\n        pass\n"),
    "tests/test_gamma.py": "import module_that_does_not_exist  # noqa: F401\n",
    "tests/test_delta.py": "import unittest\n\n\nclass Delta(unittest.TestCase):\n    def test_ok(self):\n        pass\n",
    "app.py": "def alpha():\n    return 1\n",
}


def step_script(name=STEP, workflow=CI):
    """The `run:` block of a workflow's step, as the runner executes it."""
    text = workflow.read_text()
    match = re.search(r"\n      - name: " + re.escape(name) + r"\n(?:        [^\n]*\n)*?        run: \|\n((?:          [^\n]*\n|\n)+)", text)
    return "".join(line[10:] for line in match.group(1).splitlines(True))


def unittest_log(test, files):
    """The verbose log of `python -m unittest discover -s tests -v` on `files`, as the suite step tees it."""
    tree = tempfile.mkdtemp(prefix="vv-ci-suite-")
    test.addCleanup(shutil.rmtree, tree, True)
    for path, text in files.items():
        os.makedirs(os.path.join(tree, os.path.dirname(path)), exist_ok=True)
        with open(os.path.join(tree, path), "w") as handle:
            handle.write(text)
    result = subprocess.run([sys.executable, "-m", "unittest", "discover", "-s", "tests", "-v"], cwd=tree,
                            capture_output=True, text=True, env=clean_env({"PYTHONPATH": tree}))
    log = os.path.join(tree, "unittest.log")
    with open(log, "w") as handle:
        handle.write(result.stdout + result.stderr)
    return log, result.returncode


def list_failed(test, log, outcome):
    """(the step's result, the ids it listed)."""
    out = tempfile.mkdtemp(prefix="vv-ci-temp-")
    test.addCleanup(shutil.rmtree, out, True)
    listed = os.path.join(out, "failed-tests.txt")
    result = subprocess.run(["bash", "-e", "-c", step_script()], capture_output=True, text=True,
                            env=clean_env({"LOG": log, "LIST": listed, "SUITE": outcome}))
    if not os.path.exists(listed):
        return result, None
    with open(listed) as handle:
        return result, handle.read().splitlines()


class FailedTestsStep(unittest.TestCase):
    def test_every_failure_of_a_unittest_run_is_listed_by_its_id(self):
        log, code = unittest_log(self, FAILING)
        self.assertEqual(code, 1)
        result, ids = list_failed(self, log, "failure")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(ids, ["test_alpha.Alpha.test_cases", "test_alpha.Alpha.test_errors", "test_alpha.Alpha.test_fails",
                               "test_beta.Beta", "test_gamma"])
        self.assertIn("Full run: failure. Failed tests read from its log: 5\n", result.stdout)

    def test_a_green_run_lists_nothing(self):
        log, code = unittest_log(self, {"tests/test_delta.py": FAILING["tests/test_delta.py"]})
        self.assertEqual(code, 0)
        result, ids = list_failed(self, log, "success")
        self.assertEqual((result.returncode, ids), (0, []), result.stdout + result.stderr)

    def test_a_failed_run_with_no_failure_in_its_log_fails_the_step(self):
        # A log unittest never finished writing (killed, or a format this step does not read) must not
        # become a report that nothing was missed.
        log = os.path.join(tempfile.mkdtemp(prefix="vv-ci-log-"), "unittest.log")
        self.addCleanup(shutil.rmtree, os.path.dirname(log), True)
        with open(log, "w") as handle:
            handle.write("test_ok (test_delta.Delta.test_ok) ... ok\nKilled\n")
        result, ids = list_failed(self, log, "failure")
        self.assertEqual((result.returncode, ids), (1, []))
        self.assertIn("::error title=affected-tests::the full run failed and no failed test could be read from its log",
                      result.stdout)

    def test_the_report_finds_each_listed_id_and_names_the_misses(self):
        # The pull request changes app.py, which only test_alpha imports: the selection is test_alpha, so the
        # other two files that failed are the misses, and test_alpha's own failures are not.
        repo = make_repo(self, {path: text for path, text in FAILING.items() if path != "app.py"} | {"app.py": "def alpha():\n    return 0\n"})
        git(repo, "checkout", "-q", "-b", "change")
        commit(repo, {"app.py": FAILING["app.py"]}, "the pull request")
        log, _ = unittest_log(self, FAILING)
        _, ids = list_failed(self, log, "failure")
        listed = os.path.join(os.path.dirname(log), "failed-tests.txt")
        with open(listed, "w") as handle:
            handle.write("\n".join(ids) + "\n")
        selected = runner("affected-tests", "--repo", repo, "--base-ref", "main", "--tests", "tests/test*.py")
        self.assertEqual(selected.stdout.splitlines(), ["tests/test_alpha.py"], selected.stderr)
        self.assertIn("selected 1 of 4 test files", selected.stderr)
        result = runner("affected-tests", "--repo", repo, "--base-ref", "main", "--tests", "tests/test*.py", "--report", listed)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.splitlines(), [
            "affected-tests: report: 3 test files failed in the full run, 1 of them selected; 2 missed",
            "affected-tests: miss: tests/test_beta.py failed in the full run and was not selected",
            "affected-tests: miss: tests/test_gamma.py failed in the full run and was not selected",
        ])


class SuiteInsideAGate(unittest.TestCase):
    def test_a_gate_a_test_runs_never_writes_the_note_of_the_gate_running_the_suite(self):
        # The runner hands each gate VIBE_VERIFIER_QUALIFIER, a file for its `PASS (<note>)`. A gate a test of this
        # suite runs inherited it, so coverage-not-lost's row read "PASS (soak) (no regression test, as the pull
        # request body declares)", the note of a gate one of its tests ran.
        work = tempfile.mkdtemp(prefix="vv-ci-note-")
        self.addCleanup(shutil.rmtree, work, True)
        outer, title, body = (os.path.join(work, name) for name in ("note", "title.txt", "body.md"))
        Path(outer).write_text("")
        Path(title).write_text("fix: a thing\n")
        Path(body).write_text("regression-test: none -- a reason\n")
        with mock.patch.dict(os.environ, {"VIBE_VERIFIER_QUALIFIER": outer}):
            result = gate("regression-test-fails-on-base", work, "--title", title, "--body", body)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("no regression test, as the pull request body declares: a reason", result.stdout)
        self.assertEqual(Path(outer).read_text(), "")


def job(name, workflow=CI):
    """The lines of one job of a workflow, its own `  name:` line included."""
    match = re.search(r"\n  %s:\n((?:    [^\n]*\n|\n)+)" % re.escape(name), workflow.read_text())
    return match.group(1)


def gate_args(manifest, name):
    """The arguments of a gate's line in a manifest, or None when it lists none."""
    for line in Path(manifest).read_text().splitlines():
        words = shlex.split(line, comments=True)
        if words and words[0] == name:
            return words[1:]
    return None


class NightlyCoverage(unittest.TestCase):
    def test_the_whole_suite_runs_nightly_and_per_pull_request_only_the_dashboard_tests(self):
        text = NIGHTLY.read_text()
        triggers = re.search(r"\non:\n((?:  [^\n]*\n)+)", text).group(1)
        self.assertEqual(re.findall(r"^  ([a-z_]+):", triggers, re.MULTILINE), ["schedule", "workflow_dispatch"])
        coverage = job("coverage-not-lost", NIGHTLY)
        # A job-level `if:` that skips reads as green.
        self.assertIsNone(re.search(r"^    (if|needs):", coverage, re.MULTILINE), coverage)
        self.assertIn("manifest: .vibe-verifier-nightly", coverage)
        self.assertIn("base-ref: ${{ steps.base.outputs.base }}", coverage)
        args = gate_args(ROOT / ".vibe-verifier-nightly", "coverage-not-lost")
        self.assertEqual(args[:5], ["--soak", "--runner", "unittest", "--args", "discover -s tests"])  # whole suite: no -p
        timeout = int(args[args.index("--timeout") + 1])
        minutes = int(re.search(r"^    timeout-minutes: (\d+)$", coverage, re.MULTILINE).group(1))
        # The gate's limit covers its runs; the job's adds the checkout and the worktrees, so the gate says it
        # ran out (exit 2) before the job is killed.
        self.assertGreaterEqual(minutes * 60, timeout + 300)
        self.assertIsNone(re.search(r"^  coverage-not-lost:", CI.read_text(), re.MULTILINE))
        per_pull_request = gate_args(ROOT / ".vibe-verifier-tests", "coverage-not-lost")
        self.assertEqual(per_pull_request[per_pull_request.index("--args") + 1], "discover -s tests -p 'test_dashboard_*.py'")

    def test_no_step_that_runs_tests_holds_a_credential(self):
        # The operator's rule: nothing this runs may reach a production database, credential, URL or person. The
        # job's own read-only token is the only credential, and only the step that looks up the last nightly has it.
        text = NIGHTLY.read_text()
        self.assertNotIn("secrets.", text)
        self.assertEqual(text.count("github.token"), 1)
        base_step = re.search(r"\n      - name: %s\n((?:        [^\n]*\n)+)" % re.escape(BASE_STEP), text).group(1)
        self.assertIn("GH_TOKEN: ${{ github.token }}", base_step)
        self.assertEqual(re.findall(r"^      ([a-z-]+):", re.search(r"\n    permissions:\n((?:      [^\n]*\n)+)", text).group(1),
                                    re.MULTILINE), ["contents", "actions"])
        self.assertNotIn("write", re.search(r"\n    permissions:\n((?:      [^\n]*\n)+)", text).group(1))


class NightlyBase(unittest.TestCase):
    """The step that picks the commit to compare from, run under bash the way the runner runs it, with a stub gh."""

    def setUp(self):
        self.repo = tempfile.mkdtemp(prefix="vv-nightly-")
        self.addCleanup(shutil.rmtree, self.repo, True)
        self.commits = []
        subprocess.run(["git", "-C", self.repo, "init", "-q", "-b", "main"], check=True, capture_output=True, env=clean_env())
        for hours, message in ((72, "older"), (48, "adds the nightly workflow"), (1, "today")):
            when = "@%d +0000" % (time.time() - hours * 3600)
            if message.startswith("adds"):
                os.makedirs(os.path.join(self.repo, ".github", "workflows"))
                Path(self.repo, ".github", "workflows", "nightly.yml").write_text("name: Nightly\n")
                subprocess.run(["git", "-C", self.repo, "add", "-A"], check=True, capture_output=True, env=clean_env())
            subprocess.run(["git", "-C", self.repo, "-c", "user.email=t@example.com", "-c", "user.name=T", "-c", "commit.gpgsign=false",
                            "commit", "-q", "--allow-empty", "-m", message], check=True, capture_output=True,
                           env=clean_env({"GIT_AUTHOR_DATE": when, "GIT_COMMITTER_DATE": when}))
            self.commits.append(subprocess.run(["git", "-C", self.repo, "rev-parse", "HEAD"], capture_output=True,
                                               text=True, check=True).stdout.strip())
        self.bin = tempfile.mkdtemp(prefix="vv-nightly-bin-")
        self.addCleanup(shutil.rmtree, self.bin, True)
        self.calls = os.path.join(self.bin, "calls")
        stub = os.path.join(self.bin, "gh")
        with open(stub, "w") as handle:
            handle.write('#!/bin/sh\necho "$*" >> "%s"\nprintf "%%s" "$GH_ANSWER"\n' % self.calls)
        os.chmod(stub, 0o755)

    def pick(self, asked="", answer=""):
        """(the step's result, the base it wrote to GITHUB_OUTPUT, the gh calls it made)."""
        output = os.path.join(self.bin, "output")
        open(output, "w").close()
        env = clean_env({"PATH": self.bin + os.pathsep + os.environ["PATH"], "GITHUB_OUTPUT": output, "ASKED": asked,
                         "GH_ANSWER": answer, "REPO": "owner/repo", "GH_TOKEN": "a-token"})
        result = subprocess.run(["bash", "-e", "-c", step_script(BASE_STEP, NIGHTLY)], cwd=self.repo, capture_output=True,
                                text=True, env=env)
        written = Path(output).read_text()
        calls = Path(self.calls).read_text().splitlines() if os.path.exists(self.calls) else []
        return result, written, calls

    def test_a_dispatch_with_a_base_compares_from_it(self):
        result, written, calls = self.pick(asked=self.commits[0])
        self.assertEqual((result.returncode, written, calls), (0, "base=%s\n" % self.commits[0], []), result.stderr)

    def test_a_night_compares_from_the_last_successful_scheduled_nightly_on_main(self):
        result, written, calls = self.pick(answer=self.commits[1])
        self.assertEqual((result.returncode, written), (0, "base=%s\n" % self.commits[1]), result.stderr)
        (call,) = calls
        # Scheduled runs only: a dispatched run may have judged a narrower range (its own `base`).
        self.assertIn("run list --repo owner/repo --workflow nightly.yml --branch main --event schedule --status success "
                      "--limit 1", call)
        self.assertIn("Comparing %s with %s" % (self.commits[1], self.commits[2]), result.stdout)

    def test_until_a_night_succeeds_it_compares_from_before_the_workflow_was_added(self):
        # No successful scheduled nightly yet (the first night, or every one so far could not run): the commit
        # before the one that added the workflow, the same every night, so no night's range goes unjudged.
        result, written, _ = self.pick(answer="")
        self.assertEqual((result.returncode, written), (0, "base=%s\n" % self.commits[0]), result.stderr)


if __name__ == "__main__":
    unittest.main()
