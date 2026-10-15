"""Run Python test files and write what each test did, for regression-test-fails-on-base.

Executed as `python -c <this file's source> <out.json> <file>...` under the project's own
interpreter, from the repository root, so the catalog's directory is never on the project's import
path and the root is, as under `python -m pytest`. With pytest importable it runs pytest on the files
(the project's own configuration, a collection error not stopping the other files); without it, and
for a file named with a hyphen or a dot (run by path), each file is loaded the way
`python -m unittest discover -s <its directory>` would and run by unittest.

The report: {"files": {absolute path: {"load_error": "Type: message" or null,
"tests": [[test id, "passed" | "failed" | "skipped", "Type: message" or ""]]}}}. An error is
the exception's class name and the first line of its message, read from the exception object, never
from the runner's printed output. Standard library and pytest only, Python 3.8 and later.
"""
import importlib.util
import json
import os
import sys
import unittest

OUT, FILES = sys.argv[1], [os.path.realpath(path) for path in sys.argv[2:]]
REPORT = {"files": {path: {"load_error": None, "tests": []} for path in FILES}}


def described(error):
    """`Type: first line`. pytest wraps an error raised while importing a test module in its own
    CollectError; the error that stopped the import is its cause."""
    while type(error).__name__ == "CollectError" and (error.__cause__ or error.__context__):
        error = error.__cause__ or error.__context__
    lines = str(error).strip().splitlines()
    return "%s: %s" % (type(error).__name__, lines[0] if lines else "")


def entry(path):
    return REPORT["files"].get(os.path.realpath(path))


class Recorder:
    """pytest plugin: outcomes from the reports, errors from the exceptions. pytest logs a test's
    report before it hands over the exception, so both are joined at the end of the session."""

    def __init__(self):
        self.root, self.errors, self.tests, self.broken = None, {}, {}, {}

    def pytest_configure(self, config):
        self.root = str(getattr(config, "rootpath", None) or config.rootdir)

    def pytest_exception_interact(self, node, call, report):
        self.errors.setdefault(report.nodeid, described(call.excinfo.value))

    @staticmethod
    def crash(report):
        crash = getattr(report.longrepr, "reprcrash", None)
        return crash.message if crash else str(report.longrepr).strip().splitlines()[-1]

    def pytest_collectreport(self, report):
        if report.failed and report.nodeid:
            self.broken.setdefault(report.nodeid, self.crash(report))

    def pytest_runtest_logreport(self, report):
        outcome = self.tests.setdefault(report.nodeid, ["passed", ""])
        if report.failed and outcome[0] != "failed":
            outcome[:] = ["failed", self.crash(report)]
        elif report.skipped and report.when in ("setup", "call") and outcome[0] == "passed":
            outcome[0] = "skipped"

    def pytest_sessionfinish(self):
        def found(nodeid):
            return entry(os.path.join(self.root, nodeid.split("::")[0]))
        for nodeid, fallback in self.broken.items():
            if found(nodeid) is not None and found(nodeid)["load_error"] is None:
                found(nodeid)["load_error"] = self.errors.get(nodeid) or fallback
        for nodeid, (status, fallback) in self.tests.items():
            if found(nodeid) is not None:
                found(nodeid)["tests"].append([nodeid, status, self.errors.get(nodeid, fallback) if status == "failed" else ""])


class Result(unittest.TestResult):
    def __init__(self, tests):
        super().__init__()
        self.outcomes = tests

    def addSuccess(self, test):
        self.outcomes.append([test.id(), "passed", ""])

    def addFailure(self, test, err):
        self.outcomes.append([test.id(), "failed", described(err[1])])

    addError = addFailure

    def addSkip(self, test, reason):
        self.outcomes.append([test.id(), "skipped", ""])

    def addSubTest(self, test, subtest, err):
        if err is not None:
            self.outcomes.append([subtest.id(), "failed", described(err[1])])


def with_unittest(paths):
    for index, path in enumerate(paths):
        found = REPORT["files"][path]
        stem = os.path.splitext(os.path.basename(path))[0]
        name = stem if stem.isidentifier() else "vv_regression_test_%d" % index
        sys.path.insert(0, os.path.dirname(path))
        try:
            spec = importlib.util.spec_from_file_location(name, path)
            module = importlib.util.module_from_spec(spec)
            sys.modules[name] = module
            spec.loader.exec_module(module)
            suite = unittest.defaultTestLoader.loadTestsFromModule(module)
        except BaseException as error:  # a test module that raises, or exits, while it loads did not load
            found["load_error"] = described(error)
            continue
        suite.run(Result(found["tests"]))


def importable(path):
    return os.path.splitext(os.path.basename(path))[0].isidentifier()


try:
    import pytest
except ImportError:
    pytest = None
# A hyphen or dot in the name (test-x.py, x.test.py) makes a file that is run by path: pytest cannot
# import it under that name, so unittest loads it from its path, as running the script would.
by_pytest = [path for path in FILES if pytest and importable(path)]
with_unittest([path for path in FILES if path not in by_pytest])
if by_pytest:
    pytest.main(["-q", "-p", "no:cacheprovider", "--continue-on-collection-errors"] + by_pytest, plugins=[Recorder()])
with open(OUT, "w", encoding="utf-8") as handle:
    json.dump(REPORT, handle)
