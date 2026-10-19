"""Test runs for regression-test-fails-on-base: the project's own Vitest and Python, read from their
structured reports, and the one judgement on a failure the gate makes from its text.

Each run returns {repository-relative path: Run}, or None for a file the runner did not collect. A
Run is what one test file did: `load_error`, the first line of the error that stopped it before any
test ran (None when it loaded), and `tests`, {test id: (status, error)} with status "passed",
"failed" or "skipped" and the failed test's error as `Type: first line`.
"""
import json
import os
import re
import shutil
import tempfile
import time
from collections import namedtuple

from _contract import CannotRun
from _tools import run_tool

Run = namedtuple("Run", "load_error tests")
CONFIGS = ("vitest.config.", "vite.config.", "vitest.workspace.")
ADVICE = "Raise --timeout on the base manifest, or split the pull request"
PYTHON_RUNNER = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_regression_python.py")

# What a failure reads like when something the test needs does not exist, one capture group naming it:
# Node and Vite resolving a module, Vitest reading an export the module lacks (its import becomes
# `__vi_import_0__.name` or `__vite_ssr_import_0__.name`, and Babel's CommonJS output, which Playwright runs a
# TypeScript spec through, `(0 , _module.name)`), Python importing, any file a test opens or runs.
MISSING = [re.compile(pattern) for pattern in (
    r"Cannot find (?:module|package) '([^']+)'",
    r"Failed to load url (\S+)",
    r'Failed to resolve import "([^"]+)"',
    r"__vi(?:te_ssr)?_import_\d+__\.([\w$]+)\)? is not a (?:function|constructor)",
    r"\(0 , _[\w$]+\.([\w$]+)\) is not a (?:function|constructor)",
    r'No "([^"]+)" export is defined on the "[^"]+" mock',
    r"does not provide an export named '([^']+)'",
    r"No module named '([^']+)'",
    r"cannot import name '([^']+)'",
    r"module '([^']+)' has no attribute '([^']+)'",
    r"ENOENT: no such file or directory, \w+ '([^']+)'",
    r"No such file or directory: '([^']+)'",
    r"(\S+): No such file or directory",
)]


def missing(error):
    """What `error` says is missing, or None: the test ran into the code before the fix. An assertion
    that fails is never a missing thing, even when it quotes such a message."""
    if not error or re.match(r"AssertionError\b", error):
        return None
    for pattern in MISSING:
        found = pattern.search(error)
        if found:
            return ".".join(found.groups())
    return None


def first_line(text):
    """The first line of an error, without the absolute path of the file Vite says imported it."""
    lines = (text or "").strip().splitlines()
    return re.sub(r" imported from .*$", "", lines[0].strip()) if lines else ""


def remaining(deadline, timeout):
    left = deadline - time.monotonic()
    if left <= 0:
        raise CannotRun("the test runs did not finish within --timeout %ds; no verdict. %s" % (timeout, ADVICE))
    return left


def vitest_project(repo, path):
    """The directory Vitest runs `path` from: the nearest one up to the repository root holding a Vitest
    or Vite config, else the nearest holding a package.json, else the root."""
    directory, chain = os.path.dirname(path), []
    while True:
        chain.append(directory)
        if not directory:
            break
        directory = os.path.dirname(directory)
    for wanted in (lambda names: any(name.startswith(CONFIGS) for name in names), lambda names: "package.json" in names):
        for directory in chain:
            if wanted(os.listdir(os.path.join(repo, directory))):
                return directory
    return ""


def vitest_entry(directory):
    """The vitest.mjs Node would resolve from `directory`, or None."""
    directory = os.path.abspath(directory)
    while True:
        entry = os.path.join(directory, "node_modules", "vitest", "vitest.mjs")
        if os.path.isfile(entry):
            return entry
        parent = os.path.dirname(directory)
        if parent == directory:
            return None
        directory = parent


def vitest_file(result):
    """The Run of one file's entry in Vitest's JSON report: a failed file with no test result failed to load."""
    tests = {(test.get("fullName") or test.get("title")): (test.get("status") if test.get("status") in ("passed", "failed") else "skipped",
                                                           first_line((test.get("failureMessages") or [""])[0]))
             for test in result.get("assertionResults", [])}
    failed_to_load = result.get("status") == "failed" and not tests
    return Run((first_line(result.get("message")) or "the file failed before any test ran") if failed_to_load else None, tests)


def vitest_report(report, wanted):
    """{path: Run} for the wanted {real path: path} in Vitest's JSON report."""
    runs = dict.fromkeys(wanted.values())
    for result in report.get("testResults", []):
        path = wanted.get(os.path.realpath(result.get("name", "")))
        if path is not None:
            runs[path] = vitest_file(result)
    return runs


def run_vitest(repo, paths, deadline, timeout):
    """Each file under its project's own Vitest, one run per project. A file the run did not collect
    (outside the project's include globs) is None."""
    projects = {}
    for path in paths:
        projects.setdefault(vitest_project(repo, path), []).append(path)
    runs = {}
    for project, files in sorted(projects.items()):
        directory = os.path.join(repo, project) if project else repo
        entry = vitest_entry(directory)
        if entry is None or not shutil.which("node"):
            raise CannotRun("no vitest is installed for %s (for %s): install the project's dependencies before this gate, "
                            "which runs the project's own Vitest" % (project or "the repository root", files[0]))
        scratch = tempfile.mkdtemp(prefix="vv-regression-")
        try:
            out = os.path.join(scratch, "vitest.json")
            relative = [os.path.relpath(os.path.join(repo, path), directory) for path in files]
            command = ["node", entry, "run", *relative, "--reporter=json", "--outputFile=" + out,
                       "--coverage.enabled=false", "--passWithNoTests"]
            code, output = run_tool(command, directory, remaining(deadline, timeout), "Vitest", budget=timeout, advice=ADVICE)
            try:
                with open(out, encoding="utf-8") as handle:
                    report = json.load(handle)
            except (OSError, ValueError):
                raise CannotRun("Vitest wrote no JSON report in %s (exit %d): %s"
                                % (project or "the repository root", code, output.strip()[-2000:]))
        finally:
            shutil.rmtree(scratch, ignore_errors=True)
        runs.update(vitest_report(report, {os.path.realpath(os.path.join(repo, path)): path for path in files}))
    return runs


def interpreter(python, repo):
    """The project's interpreter: a name on PATH, or a path relative to the repository."""
    if os.sep in python and not os.path.isabs(python):
        python = os.path.join(repo, python)
    found = shutil.which(python)
    if not found:
        raise CannotRun("--python %s is not an interpreter on PATH or in the repository" % python)
    return os.path.abspath(found)


def run_python(repo, python, paths, deadline, timeout):
    """The files under the project's interpreter: its pytest when installed, else unittest."""
    with open(PYTHON_RUNNER, encoding="utf-8") as handle:
        source = handle.read()
    scratch = tempfile.mkdtemp(prefix="vv-regression-")
    try:
        out = os.path.join(scratch, "python.json")
        files = [os.path.join(os.path.realpath(repo), path) for path in paths]
        env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")  # a .pyc written for one tree is never read for the other
        code, output = run_tool([interpreter(python, repo), "-c", source, out, *files], repo,
                                remaining(deadline, timeout), "the Python tests", env=env, budget=timeout, advice=ADVICE)
        try:
            with open(out, encoding="utf-8") as handle:
                report = json.load(handle)
        except (OSError, ValueError):
            raise CannotRun("the Python test run wrote no report (exit %d): %s" % (code, output.strip()[-2000:]))
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
    runs = {}
    for path, full in zip(paths, files):
        found = report["files"][os.path.realpath(full)]
        runs[path] = Run(found["load_error"], {name: (status, error) for name, status, error in found["tests"]})
    return runs
