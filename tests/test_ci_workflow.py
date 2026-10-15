"""This repository's own ci.yml: the step that lists the tests the full unittest run failed, for the
affected-test selection report, run under bash the way CI runs it, on a real unittest log."""
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

from helpers import ROOT, clean_env, commit, git, make_repo, runner

CI = ROOT / ".github" / "workflows" / "ci.yml"
STEP = "List the tests the full run failed"

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


if __name__ == "__main__":
    unittest.main()
