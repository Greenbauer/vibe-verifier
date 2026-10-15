"""This repository's own ci.yml: the step that lists the tests the full unittest run failed, for the
affected-test selection report, run under bash the way CI runs it, on a real unittest log; this suite run
inside a gate, as regression-test-fails-on-base (.vibe-verifier-tests) and coverage-not-lost
(.vibe-verifier-coverage) run it; and the coverage-not-lost job's shape."""
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from helpers import ROOT, clean_env, commit, gate, git, make_repo, runner

CI = ROOT / ".github" / "workflows" / "ci.yml"
STEP = "List the tests the full run failed"
COVERAGE = ROOT / ".vibe-verifier-coverage"

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


def step_script():
    """The `run:` block of the step, as the runner executes it."""
    text = CI.read_text()
    match = re.search(r"\n      - name: " + re.escape(STEP) + r"\n(?:        [^\n]*\n)*?        run: \|\n((?:          [^\n]*\n|\n)+)", text)
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


def job(name):
    """The lines of one job of ci.yml, its own `  name:` line included."""
    match = re.search(r"\n  %s:\n((?:    [^\n]*\n|\n)+)" % re.escape(name), CI.read_text())
    return match.group(1)


def gate_args(manifest, name):
    """The arguments of a gate's line in a manifest, or None when it lists none."""
    for line in Path(manifest).read_text().splitlines():
        words = shlex.split(line, comments=True)
        if words and words[0] == name:
            return words[1:]
    return None


class CoverageJob(unittest.TestCase):
    def test_the_coverage_job_runs_the_whole_suite_beside_the_test_job_within_its_timeout(self):
        coverage = job("coverage-not-lost")
        # On every event and beside the test job: a job-level `if:` that skips reads as green, and `needs:` would
        # wait for the test job.
        self.assertIsNone(re.search(r"^    (if|needs):", coverage, re.MULTILINE), coverage)
        self.assertIn("manifest: .vibe-verifier-coverage", coverage)
        self.assertIn("> pr-inputs/body.md", coverage)
        args = gate_args(COVERAGE, "coverage-not-lost")
        self.assertEqual(args[:4], ["--soak", "--runner", "unittest", "--args"])
        self.assertEqual(args[4], "discover -s tests")  # the whole suite: no -p
        self.assertEqual(args[args.index("--body") + 1], "pr-inputs/body.md")
        timeout = int(args[args.index("--timeout") + 1])
        minutes = int(re.search(r"^    timeout-minutes: (\d+)$", coverage, re.MULTILINE).group(1))
        # The gate's limit covers its runs; the job's adds the checkout and the worktrees, so the gate says it
        # ran out (exit 2) before the job is killed.
        self.assertGreaterEqual(minutes * 60, timeout + 300)
        self.assertIsNone(gate_args(ROOT / ".vibe-verifier-tests", "coverage-not-lost"))


if __name__ == "__main__":
    unittest.main()
