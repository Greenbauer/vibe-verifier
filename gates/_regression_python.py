"""Run Python test files and write what each test did, for regression-test-fails-on-base.

Executed as `python -c <this file's source> <out.json> <file>...` under the project's own
interpreter, from the repository root, so the catalog's directory is never on the project's import
path and the root is, as under `python -m pytest`. With pytest importable it runs pytest on the files
(the project's own configuration, a collection error not stopping the other files); without it, and
for a file named with a hyphen or a dot (run by path), each file is loaded the way
`python -m unittest discover -s <its directory>` would and run by unittest. VV_REGRESSION_STAND_INS
({module: [name]}) gives a module a stand-in for each name it lacks when it loads, and a module mapped to null,
which no finder finds, a stand-in module whose every name is a stand-in (StandInFinder).

The report: {"files": {absolute path: {"load_error": "Type: message" or null,
"tests": [[test id, "passed" | "failed" | "skipped", "Type: message" or ""]]}}}. An error is
the exception's class name and the first line of its message, read from the exception object, never
from the runner's printed output. Standard library and pytest only, Python 3.8 and later.
"""
import dis
import importlib.machinery
import importlib.util
import json
import os
import sys
import types
import unittest

OUT, FILES = sys.argv[1], [os.path.realpath(path) for path in sys.argv[2:]]
REPORT = {"files": {path: {"load_error": None, "tests": []} for path in FILES}}
STAND_INS = json.loads(os.environ.get("VV_REGRESSION_STAND_INS") or "{}")
ADDED = []  # [module, name] of each stand-in a module was given, and [module, null] of each stand-in module, for the report


class StandIn:
    """A name a test file imports that its module does not have yet on the code before the fix (the gate's
    VV_REGRESSION_STAND_INS, {module: [name]}), or any name of a module that does not exist there yet (StandInModule):
    the file loads and its other tests run. A call, an operator, an attribute or a base class raises the ImportError
    the import would have raised (the ModuleNotFoundError, for a module), which the gate reads as that name or module
    missing; a use that raises another error (issubclass, assertRaises) is read through the error's traceback
    (failure)."""

    def __init__(self, module, name, whole=False):
        object.__setattr__(self, "error", missing_module(module) if whole else
                           (ImportError, "cannot import name '%s' from '%s' (a stand-in: it does not exist before the fix)"
                            % (name, module)))

    def missing(self, *_args, **_kwargs):
        kind, message = object.__getattribute__(self, "error")
        raise kind(message)  # a new one each use: one raised again keeps every earlier use's traceback

    def __getattr__(self, name):
        if name.startswith("__") and name.endswith("__"):  # Python's own probes (issubclass reads __bases__)
            raise AttributeError(name)
        StandIn.missing(self)

    def __repr__(self):
        return "<%s>" % object.__getattribute__(self, "error")[1]


for _use in ("call getitem setitem delitem iter len bool contains enter exit instancecheck subclasscheck mro_entries eq "
             "ne lt le gt ge hash add radd sub rsub mul rmul truediv rtruediv floordiv rfloordiv mod rmod pow and or xor "
             "neg pos abs invert int float index").split():
    setattr(StandIn, "__%s__" % _use, StandIn.missing)


class StandInLoader:
    """The module's own loader, then a stand-in for each name of STAND_INS the module lacks once it has run."""

    def __init__(self, loader, module):
        self.loader, self.module = loader, module

    def __getattr__(self, name):
        return getattr(self.loader, name)

    def create_module(self, spec):
        return self.loader.create_module(spec)

    def exec_module(self, module):
        self.loader.exec_module(module)
        for name in STAND_INS[self.module]:
            if not hasattr(module, name):
                setattr(module, name, StandIn(self.module, name))
                ADDED.append([self.module, name])


def missing_module(module):
    return ModuleNotFoundError, "No module named '%s'" % module


class StandInModule:
    """The loader of a module that does not exist on the code before the fix (a helper the fix moves to a file of its
    own, say): a package, so a module under it can stand in too, whose every name is a StandIn that raises the
    ModuleNotFoundError its import would have raised. `from module import *` reads `__all__`, which raises it too:
    a stand-in module has no names to bind, and a test that then reads one would fail on a NameError, the bug shown."""

    @staticmethod
    def create_module(spec):
        return None

    @staticmethod
    def exec_module(module):
        def stand_in(name):
            if name == "__all__":
                kind, message = missing_module(module.__name__)
                raise kind(message)
            if name.startswith("__") and name.endswith("__"):
                raise AttributeError(name)
            return StandIn(module.__name__, name, whole=True)
        module.__getattr__, module.__vv_stand_in__ = stand_in, True
        ADDED.append([module.__name__, None])


class StandInFinder:
    """First on sys.meta_path: a module of STAND_INS is found by the finders after it, when and where the test
    file's own import finds it (after conftest.py, pytest's pythonpath, the file's own setup), and loaded through
    StandInLoader; one mapped to null that none of them finds is a StandInModule."""

    @staticmethod
    def find_spec(fullname, path=None, target=None):
        if fullname not in STAND_INS:
            return None
        for finder in sys.meta_path:
            spec = None if finder is StandInFinder or not hasattr(finder, "find_spec") else finder.find_spec(fullname, path, target)
            if spec is not None:
                if STAND_INS[fullname] is not None and hasattr(spec.loader, "exec_module"):
                    spec.loader = StandInLoader(spec.loader, fullname)
                return spec
        if STAND_INS[fullname] is None:
            return importlib.machinery.ModuleSpec(fullname, StandInModule, is_package=True)
        return None


def globals_read(code, line):
    """The globals `code` loads on `line`, each with the attributes read off it in a row (`shop.rounding.Thing` is
    ("shop", ["rounding", "Thing"])): no keyword, string, comment or local of that name."""
    found, current, open_row = [], None, False
    for instruction in dis.get_instructions(code):
        position = getattr(instruction, "positions", None)  # Python 3.11 and later; before, the line's first only
        current = position.lineno if position is not None else (instruction.starts_line or current)
        if current != line:
            continue
        if instruction.opname in ("LOAD_GLOBAL", "LOAD_NAME", "LOAD_FROM_DICT_OR_GLOBALS"):
            found.append((instruction.argval, []))
            open_row = True
        elif open_row and instruction.opname in ("LOAD_ATTR", "LOAD_METHOD"):
            found[-1][1].append(instruction.argval)
        else:
            open_row = False
    return found


def read_off(value, attributes):
    """Each value a global and the attributes read off it name, through modules only (a module's own dict, or a
    stand-in module's stand-in), so none of the test's own code runs."""
    yield value
    for attribute in attributes:
        if not isinstance(value, types.ModuleType):
            return
        names = vars(value)
        stood_in = attribute not in names and names.get("__vv_stand_in__")
        value = StandIn(names["__name__"], attribute, whole=True) if stood_in else names.get(attribute)
        yield value


def held(error):
    """The first stand-in the traceback of `error` holds: a local of a frame it passes through,
    other than a module's (assertRaises's expected exception), or a global a frame's code loads on its failing line, or
    what that line reads off it through modules (`issubclass(shop.rounding.Thing, ...)`). Read without using it.
    A module frame's locals are its globals, every name the file imported, so only its failing line counts there."""
    trace = getattr(error, "__traceback__", None)
    while trace is not None:
        frame = trace.tb_frame
        local = [] if frame.f_locals is frame.f_globals else list(frame.f_locals.values())
        named = [value for name, attributes in globals_read(frame.f_code, trace.tb_lineno)
                 for value in read_off(frame.f_globals.get(name), attributes)]
        for value in local + named:
            if type(value) is StandIn:
                return value
        trace = trace.tb_next
    return None


def failure(error):
    """`Type: first line` of what stopped a test or a file's load, or a stand-in's ImportError when the error's
    traceback holds one (issubclass and assertRaises raise TypeError on it). An ImportError keeps its own: it
    already names what is missing, and the line of a `from ... import` that failed names the stand-ins before it."""
    stand_in = None if isinstance(error, ImportError) else held(error)
    return described(error) if stand_in is None else "%s: %s" % (object.__getattribute__(stand_in, "error")[0].__name__,
                                                                   object.__getattribute__(stand_in, "error")[1])


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
        self.errors.setdefault(report.nodeid, failure(call.excinfo.value))

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
        self.outcomes.append([test.id(), "failed", failure(err[1])])

    addError = addFailure

    def addSkip(self, test, reason):
        self.outcomes.append([test.id(), "skipped", ""])

    def addSubTest(self, test, subtest, err):
        # A failed subtest fails its test, and unittest then reports nothing else for that test (no
        # addSuccess, no addFailure): it is recorded under the test's own id, with its first failure.
        if err is not None and all(row[0] != test.id() for row in self.outcomes):
            self.outcomes.append([test.id(), "failed", failure(err[1])])


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
            # and, as Python's own import does, leaves no half-run module behind: a test file importing it after this
            # one runs it again and stops on the same error, not on a name the half-run module never reached.
            sys.modules.pop(name, None)
            found["load_error"] = failure(error)
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
sys.meta_path.insert(0, StandInFinder)
by_pytest = [path for path in FILES if pytest and importable(path)]
with_unittest([path for path in FILES if path not in by_pytest])
if by_pytest:
    pytest.main(["-q", "-p", "no:cacheprovider", "--continue-on-collection-errors"] + by_pytest, plugins=[Recorder()])
REPORT["stand_ins"] = ADDED
with open(OUT, "w", encoding="utf-8") as handle:
    json.dump(REPORT, handle)
