"""One coverage run of a project's own test suite, for the coverage-not-lost gate.

Each runner runs the whole suite once in a checkout and returns a Coverage: for every source file the suite
measured, {line: whether it ran}, keyed by the file's path in the repository, whether any test ran and,
for Python, which tests ran each line.

- vitest: the project's own Vitest, with @vitest/coverage-v8 at exactly its version: the project's own when it
  has one, else the catalog's pin of that version (tools/coverage-not-lost). Any other provider is refused: one
  a minor release away from its Vitest measured the wrong lines without an error (3.2.7 under Vitest 3.0.0,
  2026-10-15). The provider is loaded through a wrapper registered as Vitest's custom provider, so the
  project's own coverage settings (its provider, reporters, thresholds) never decide the run.
- pytest, unittest: coverage.py, pinned in tools/coverage-not-lost-python, appended to the end of the project's
  interpreter's path, under the gate's own settings file (the project's .coveragerc and the like are not read),
  with each test function recorded as the context of the lines it ran. Every Python process the suite starts
  is measured too, by coverage.py's own subprocess support (`[run] patch = subprocess`): each process writes a
  data file of its own, and the run's files are combined before the report is read.

Every run of the suite is guarded (gates/_local_guard.py): a credential for anywhere else in what it inherits is exit
2, a connection to anything but this machine is refused, and a suite that fails in a run that refused one is exit 2.
"""
import json
import os
import re
import shlex
import shutil
import sys
import tempfile
import time

from _contract import PY_TESTS, CannotRun, matches
from _local_guard import HIDDEN, Guard
from _tools import NODE_TOOLS, ensure_node_tool, ensure_python_tool, run_tool

NODE_TOOL = "coverage-not-lost"
PYTHON_TOOL = "coverage-not-lost-python"
# A line in these never counts: test code as regression-test-fails-on-base reads it, declarations, configuration.
NOT_SOURCE = ["**/*.test.*", "**/*.spec.*", "**/conftest.py", "**/*.d.ts", "**/*.config.*", "**/*.stories.*"] + PY_TESTS + [
    "**/%s/**" % name for name in ("test", "tests", "__tests__", "__mocks__", "__snapshots__", "__fixtures__", "node_modules")]
# Past --timeout: the suite runs two to four times, so the size of the pull request is not what to change.
ADVICE = "Raise --timeout on the base manifest"
JS_TESTS = ["**/*.test.*", "**/*.spec.*", "**/__tests__/**"]
# The wrapper Vitest loads as its custom coverage provider. Named "custom", the provider makes Vitest's
# workers load this module too, instead of resolving @vitest/coverage-v8 by name from the project. The
# project's thresholds are dropped before the provider reads them: a project below its own threshold
# would otherwise fail every run for a reason that is not a failing test.
WRAPPER = """import v8 from %s;
export default {
  ...v8,
  async getProvider() {
    const provider = await v8.getProvider();
    provider.name = "custom";
    const initialize = provider.initialize.bind(provider);
    provider.initialize = (ctx) => { delete ctx.config.coverage.thresholds; return initialize(ctx); };
    return provider;
  },
};
"""
BOOT = "import sys; sys.path.append(%r); sys.argv = ['coverage'] + sys.argv[1:]; from coverage.cmdline import main; sys.exit(main())"
ENV = dict(os.environ, NO_COLOR="1", FORCE_COLOR="0")
# Where coverage.py's data goes is the gate's settings file alone: one the suite's environment names (an outer
# coverage.py measuring the process that runs this gate, say) would send some processes' data elsewhere.
COVERAGE_ENV = ("COVERAGE_FILE", "COVERAGE_PROCESS_CONFIG", "COVERAGE_PROCESS_START")
# `patch = subprocess` hands its settings to each child in COVERAGE_PROCESS_CONFIG, and coverage.py starts in
# a child from the .pth file it installs in its own site-packages, which is not the project's interpreter's.
# So this file, on the suite's PYTHONPATH right after the guard's (gates/_local_guard.py, which runs it), is a
# sitecustomize every Python process runs at start: it appends coverage.py's package the way BOOT does and
# starts coverage.py the way that .pth file does, then runs the sitecustomize it hides (Homebrew's Python and
# Ubuntu's ship one), if there is one. A child records no test function: no test is on its stack, so a
# function of its own named test* would be.
SITECUSTOMIZE = '''import os
import sys


def _measure():
    settings = os.environ.get("COVERAGE_PROCESS_CONFIG")
    if not settings:
        return
    sys.path.append(%r)
    import coverage
    from coverage.config import CoverageConfig
    config = CoverageConfig.deserialize(settings)
    config.dynamic_context = None
    os.environ["COVERAGE_PROCESS_CONFIG"] = config.serialize()
    coverage.process_startup()


try:
    _measure()
except Exception:  # an interpreter this coverage.py cannot load in (a Python before 3.10) runs unmeasured
    pass
'''


class Coverage:
    """One run: {repository path: {line: ran}}, whether any test ran, (Python) {path: {line: [test names]}}, and
    (node:test) the files it ran as tests, which never count at the other side either."""

    def __init__(self, lines, tested, contexts=None, tests=()):
        self.lines, self.tested, self.contexts, self.tests = lines, tested, contexts or {}, set(tests)


class Red(Exception):
    """The suite failed: (what exited how, the output that says why)."""


def note(text):
    print("coverage-not-lost: %s" % text, file=sys.stderr)


def read_json(path):
    try:
        with open(path, encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return None


def repository_path(path, cwd, tree, repo):
    """`path` as a coverage report names it, as a path in the repository; None outside the checkout. A file
    the suite read from the working copy instead of this checkout cannot be measured here: exit 2."""
    absolute = os.path.realpath(os.path.join(cwd, path))
    for root, inside in ((os.path.realpath(tree), True), (os.path.realpath(repo), False)):
        if absolute == root or absolute.startswith(root + os.sep):
            if inside:
                return os.path.relpath(absolute, root).replace(os.sep, "/")
            raise CannotRun("the suite in the checkout of %s ran %s from the working copy instead (an editable install, "
                            "or a path that points outside the checkout), so its lines cannot be told apart from the "
                            "other side's" % (os.path.basename(tree), os.path.relpath(absolute, root)))
    return None


def time_left(deadline):
    return max(deadline - time.monotonic(), 1)


def resolve_package(directory, name):
    """The real directory of package `name` as Node resolves it from `directory`, or None."""
    directory = os.path.abspath(directory)
    while True:
        manifest = os.path.join(directory, "node_modules", name, "package.json")
        if os.path.isfile(manifest):
            return os.path.dirname(os.path.realpath(manifest))
        parent = os.path.dirname(directory)
        if parent == directory:
            return None
        directory = parent


def version_of(package):
    return str((read_json(os.path.join(package, "package.json")) or {}).get("version", ""))


def first_line(text):
    lines = [line.strip() for line in str(text or "").splitlines() if line.strip()]
    return lines[0][:200] if lines else "failed"


def vitest_failures(summary, cwd):
    """`file > test: first line of its failure` for each failed test or test file of Vitest's JSON report."""
    found = []
    for result in summary.get("testResults", []):
        if result.get("status") != "failed":
            continue
        name = os.path.relpath(os.path.realpath(result.get("name", "?")), os.path.realpath(cwd))
        failed = [a for a in result.get("assertionResults", []) if a.get("status") == "failed"]
        found += ["%s > %s: %s" % (name, a.get("fullName", "?"), first_line((a.get("failureMessages") or [""])[0]))
                  for a in failed] or ["%s: %s" % (name, first_line(result.get("message")))]
    return found


class Vitest:
    """The project's own Vitest, run with coverage-v8 at exactly its version."""

    def __init__(self, args, repo, scratch):
        self.args, self.repo, self.scratch, self.prepared = shlex.split(args.args), repo, scratch, {}
        self.local = args.local_env

    def prepare(self, project):
        """(the Vitest CLI, the provider wrapper) for `project`, resolved in the working copy."""
        vitest = resolve_package(os.path.join(self.repo, project), "vitest")
        if vitest is None:
            raise CannotRun("no vitest is installed for %s: install the project's dependencies before this gate, which "
                            "runs the project's own Vitest" % project)
        if vitest not in self.prepared:
            self.prepared[vitest] = (os.path.join(vitest, "vitest.mjs"), self.wrapper(project, vitest))
        return self.prepared[vitest]

    def provider(self, project, vitest, home):
        """The provider module for `vitest`: the project's own coverage-v8 of its version, else the catalog's."""
        version = version_of(vitest)
        own = resolve_package(os.path.join(self.repo, project), "@vitest/coverage-v8")
        if own and version_of(own) == version:
            note("measuring with the project's own @vitest/coverage-v8 %s" % version)
            return os.path.join(own, "dist", "index.js")
        alias = "coverage-v8-" + version
        pinned = sorted((read_json(os.path.join(NODE_TOOLS, NODE_TOOL, "package.json")) or {}).get("dependencies", {}))
        if alias not in pinned:
            raise CannotRun("vitest %s has no coverage provider of its version: the project's own @vitest/coverage-v8 is %s, "
                            "and the catalog pins one only for vitest %s. Add @vitest/coverage-v8 %s to the project's "
                            "devDependencies; a provider of another version can measure the wrong lines"
                            % (version, version_of(own) if own else "not installed",
                               ", ".join(name[len("coverage-v8-"):] for name in pinned), version))
        # The provider imports vitest from beside itself, so it runs from a copy that links the project's.
        shutil.copytree(os.path.join(ensure_node_tool(NODE_TOOL), "node_modules"), os.path.join(home, "node_modules"), symlinks=True)
        os.symlink(vitest, os.path.join(home, "node_modules", "vitest"))
        note("measuring with the catalog's @vitest/coverage-v8 %s" % version)
        return os.path.join(home, "node_modules", alias, "dist", "index.js")

    def wrapper(self, project, vitest):
        if not shutil.which("node"):
            raise CannotRun("the vitest runner needs node on PATH")
        home = tempfile.mkdtemp(prefix="provider-", dir=self.scratch)
        provider = self.provider(project, vitest, home)
        path = os.path.join(home, "node_modules", "vibe-verifier-coverage.mjs")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(WRAPPER % json.dumps(provider))
        return path

    def run(self, tree, project, deadline, budget, files=()):
        cwd = os.path.join(tree, project)
        cli, wrapper = self.prepare(project)
        out = tempfile.mkdtemp(prefix="run-", dir=self.scratch)
        results = os.path.join(out, "results.json")
        command = ["node", cli, "run", "--passWithNoTests", "--coverage.enabled", "--coverage.provider=custom",
                   "--coverage.customProviderModule=" + wrapper, "--coverage.reporter=json",
                   "--coverage.reportsDirectory=" + os.path.join(out, "coverage"), "--reporter=json",
                   "--outputFile.json=" + results] + self.args + list(files)
        what = "the vitest run in the %s checkout" % os.path.basename(tree)
        with Guard("coverage-not-lost", self.repo, self.local, more=(tree,)) as guard:
            code, output = run_tool(command, cwd, time_left(deadline), "vitest", env=guard.env(what, base=ENV), budget=budget,
                                    advice=ADVICE)
            summary = read_json(results) or {}
            red = code != 0 or not summary.get("success")
            guard.judge((vitest_failures(summary, cwd) or ["the suite (vitest exited %d)" % code])[0] if red else None, what)
        if red:
            raise Red("vitest exited %d" % code, "\n".join(vitest_failures(summary, cwd)[:20]) or output[-3000:])
        tested = int(summary.get("numTotalTests", 0)) > 0
        report = read_json(os.path.join(out, "coverage", "coverage-final.json"))
        if report is None and tested:
            raise CannotRun("vitest ran the tests of %s and wrote no coverage report" % project)
        lines = {}
        for name, entry in (report or {}).items():
            path = repository_path(name, cwd, tree, self.repo)
            if path is None or matches(path, NOT_SOURCE):
                continue
            ran = lines.setdefault(path, {})
            for key, where in entry.get("statementMap", {}).items():
                line = where["start"]["line"]
                ran[line] = ran.get(line, False) or entry.get("s", {}).get(key, 0) > 0
        return Coverage(lines, tested)

    @staticmethod
    def test_files(paths, tree):
        """The test files among `paths`: run again at the base (`tree`) to say which covered a lost line."""
        return [path for path in paths if matches(path, JS_TESTS) and not path.endswith(".py")]


class Python:
    """coverage.py around the project's own pytest or unittest, in the project's own interpreter."""

    def __init__(self, args, repo, scratch):
        self.module, self.args, self.repo, self.scratch = args.runner, shlex.split(args.args), repo, scratch
        self.python, self.site, self.env, self.local = interpreter(args.python, repo, args.runner), None, None, args.local_env

    def boot(self):
        """The command that runs coverage.py; first, the environment its runs share (self.env)."""
        if self.site is None:
            tool = ensure_python_tool(PYTHON_TOOL, self.python)
            self.site = run_tool([tool, "-c", "import sysconfig; print(sysconfig.get_path('purelib'))"],
                                 None, 60, "python")[1].strip().splitlines()[-1]
            hook = tempfile.mkdtemp(prefix="site-", dir=self.scratch)
            with open(os.path.join(hook, "sitecustomize.py"), "w", encoding="utf-8") as handle:
                handle.write(SITECUSTOMIZE % self.site + HIDDEN)
            self.env = {key: value for key, value in ENV.items() if key not in COVERAGE_ENV}
            self.env["PYTHONPATH"] = os.pathsep.join(filter(None, [hook, ENV.get("PYTHONPATH")]))
        return [self.python, "-c", BOOT % self.site]

    def run(self, tree, project, deadline, budget, files=()):
        boot, cwd = self.boot(), os.path.join(tree, project)
        out = tempfile.mkdtemp(prefix="run-", dir=self.scratch)
        settings, report = os.path.join(out, "coveragerc"), os.path.join(out, "coverage.json")
        with open(settings, "w", encoding="utf-8") as handle:
            # parallel: a data file per process (patch = subprocess sets it too), combined below. The report reads
            # only the checkout and the working copy (an editable install, refused in parse), so a script a test ran
            # from a directory it then removed cannot stop it ("No source for code"). Not [run] include: that turns
            # off coverage.py's own skipping of installed packages, and a venv in the working copy would be refused.
            handle.write("[run]\ndata_file = %s\ndynamic_context = test_function\nparallel = true\npatch = subprocess\n"
                         "[report]\ninclude =\n    %s\n    %s\n" % (os.path.join(out, "data"), os.path.join(tree, "*"),
                                                                   os.path.join(self.repo, "*")))
        what = "the %s run in the %s checkout" % (self.module, os.path.basename(tree))
        with Guard("coverage-not-lost", self.repo, self.local, more=(tree,)) as guard:
            code, output = run_tool(boot + ["run", "--rcfile=" + settings, "-m", self.module] + self.args, cwd,
                                    time_left(deadline), self.module, env=guard.env(what, base=self.env), budget=budget, advice=ADVICE)
            empty = code == 5 or re.search(r"^Ran 0 tests", output, re.MULTILINE)  # pytest's and unittest's "no test ran"
            guard.judge("the suite (%s exited %d)" % (self.module, code) if code and not empty else None, what, output[-3000:])
        if empty:
            return Coverage({}, False)
        if code != 0:
            raise Red("%s exited %d" % (self.module, code), output[-3000:])
        for step in (["combine", "--quiet"], ["json", "--show-contexts", "-o", report]):  # one data file per process
            code, output = run_tool(boot + step + ["--rcfile=" + settings], cwd, time_left(deadline), "coverage.py",
                                    env=self.env, budget=budget, advice=ADVICE)
            if code != 0:
                raise CannotRun("coverage.py %s exited %d: %s" % (step[0], code, output.strip()[-500:]))
        data = read_json(report)
        if data is None:
            raise CannotRun("coverage.py wrote no report: %s" % output.strip()[-500:])
        return self.parse(data, cwd, tree)

    def parse(self, data, cwd, tree):
        """A Coverage from coverage.py's JSON report, with each line's test functions."""
        lines, contexts = {}, {}
        for name, entry in data.get("files", {}).items():
            path = repository_path(name, cwd, tree, self.repo)
            if path is None or matches(path, NOT_SOURCE):
                continue
            lines[path] = {line: False for line in entry.get("missing_lines", [])}
            lines[path].update((line, True) for line in entry.get("executed_lines", []))
            contexts[path] = {int(line): sorted(n for n in names if n) for line, names in entry.get("contexts", {}).items()}
        return Coverage(lines, True, contexts)

    @staticmethod
    def test_files(paths, tree):
        return []  # each line's tests come from its contexts, so nothing is run again


def interpreter(python, repo, module):
    """The project's interpreter, able to import `module` (pytest or unittest); after gates/_mutation_python.py."""
    if os.sep in python and not os.path.isabs(python):
        python = os.path.join(repo, python)
    found = shutil.which(python)
    if not found:
        raise CannotRun("--python %s is not an interpreter on PATH or in the repository" % python)
    found = os.path.abspath(found)
    code, output = run_tool([found, "-c", "import sys, %s; print('%%d.%%d' %% sys.version_info[:2])" % module], None, 60, "python")
    if code != 0:
        raise CannotRun("%s cannot import %s: install the project's dependencies before this gate, which runs the "
                        "project's own tests" % (python, module))
    version = output.strip().splitlines()[-1]
    if tuple(int(part) for part in version.split(".")) < (3, 10):
        raise CannotRun("%s is Python %s; the pinned coverage.py needs 3.10 or later" % (python, version))
    return found


RUNNERS = {"vitest": Vitest, "pytest": Python, "unittest": Python}
