"""Affected-test selection, `bin/vibe-verifier affected-tests`: the test files a pull request must run.

Not a gate: it decides nothing about the code. It prints the test files the pull request's changes
can affect, one per line, or `ALL`, and the repository's own test job runs that list. The rule was
measured on a 277-pull-request history of a private TypeScript monorepo, where it picked every test
that failed on a deliberate break of each pull request's code (132 of 132) at 10.9% of its test files:

1. every test file whose imports reach a changed file, directly or through other files (the static
   graph in gates/_affected_graph.py; a changed test file selects itself). A test runner's config
   counts as imported by every test under its directory, and it imports each file it names (a setup
   file), so a change to what a setup file or a conftest.py imports selects the tests it runs before;
2. every test file that reads files, starts a process or uses a database, or imports a file that
   does (READS), always: the graph cannot see what it reads;
3. `ALL` instead when a lockfile, a tsconfig, a test runner's config (RUNNER_CONFIG, conftest.py
   included; a Playwright config only when a listed test imports @playwright/test) or a runtime version
   pin changed, when a package.json changed one of
   DEPENDENCY_FIELDS (its scripts do not count), when a path --all-on names changed (a repository's
   own test runner script, say), and when there is no base or it cannot be read.

A test file is a JS or TS file named *.test.* or *.spec.* or under __tests__/, a JS or TS file that
imports node:test, or a Python test (_contract.PY_TESTS); --tests replaces that definition.

With --report RESULTS the job has also run the full suite, and the tests that failed there but were not
selected (misses) are printed, as warnings under --format github: what selection alone would have let
through. RESULTS is a JUnit XML report (Vitest's, pytest's) or a list of failed test files, one per line
(actions/affected-tests/failed-files.mjs writes one for node:test). Exit 0 with a selection or a report,
2 when it cannot run.
"""
import json
import os
import posixpath
import re
import sys
import traceback
import xml.etree.ElementTree as ElementTree  # nosec B405: the job's own test report, not outside input

from _affected_graph import JS_EXTENSIONS, Graph, reach
from _contract import PY_TESTS, CannotRun, git, matches, tracked_files

NAME = "affected-tests"
JS_TESTS = ["**/*.test.*", "**/*.spec.*", "**/__tests__/**"]
NODE_TEST = re.compile(r"""['"]node:test['"]""")
LOCKFILES = {"package-lock.json", "npm-shrinkwrap.json", "pnpm-lock.yaml", "pnpm-workspace.yaml", "yarn.lock", "bun.lock",
             "bun.lockb", "poetry.lock", "uv.lock", "pdm.lock", "Pipfile", "Pipfile.lock"}
REQUIREMENTS = re.compile(r"^(?:requirements|constraints)[\w.-]*\.txt$")
TSCONFIG = re.compile(r"^(?:ts|js)config[^/]*\.json$")
RUNNER_CONFIG = re.compile(r"^(?:vitest|vite|jest|playwright)(?:\.[\w-]+)?\.(?:config|workspace|setup)\.[cm]?[jt]sx?$"
                           r"|^\.mocharc(?:\.\w+)?$|^bunfig\.toml$|^(?:pytest\.ini|tox\.ini|setup\.cfg|pyproject\.toml|conftest\.py)$")
# A Playwright config matters only to Playwright tests: a unit suite's selection ignores it.
PLAYWRIGHT_CONFIG = re.compile(r"^playwright(?:\.[\w-]+)?\.config\.")
PLAYWRIGHT_TEST = re.compile(r"""(?:\bfrom\s*|\brequire\s*\(\s*)['"]@playwright/test['"]""")  # a mock of it is not one
RUNTIME_PINS = {".nvmrc", ".node-version", ".python-version"}
DEPENDENCY_FIELDS = ("dependencies", "devDependencies", "peerDependencies", "optionalDependencies", "engines", "packageManager")
_DATABASE = r"DATABASE_URL|\b(?:postgres(?:ql)?|mysql|mongodb(?:\+srv)?|redis)://|\bsupabase start\b|\bdocker run\b|\bpsql\b"
_JS_MODULES = (r"(?:node:)?(?:fs|fs/promises|child_process)|fs-extra|glob|fast-glob|globby|tinyglobby"
               r"|pg|postgres|mysql2?|mongodb|ioredis|better-sqlite3|sqlite3|@prisma/client|knex")
_PY_MODULES = r"subprocess|shutil|glob|importlib|runpy|sqlite3|psycopg2?|asyncpg|sqlalchemy"
# What a file reads that the graph cannot follow: the file system, a process, a database, a computed import. Code
# shapes, so prose in a comment ("a type-only import (per ...") does not count; a vi.mock of a module is no import.
READS = {
    "js": re.compile(r"""(?:\bfrom\s*|\b(?:require|import)\(\s*)['"](?:""" + _JS_MODULES + r""")['"]"""
                     r"""|\b(?:readFileSync|readdirSync|globSync|execSync|execFileSync|spawnSync)\(|\bimport\.meta\.glob\b"""
                     r"""|\bBun\.file\(|\b(?:import|require)\(\s*[\w$`]|""" + _DATABASE),
    "py": re.compile(r"""\bopen\(|\.open\(|\.read_(?:text|bytes)\(|\bread_(?:csv|json|parquet|excel|table|pickle)\("""
                     r"""|\bos\.(?:listdir|scandir|walk|system|popen)\(|\.r?glob\(|__import__\("""
                     r"""|^[ \t]*(?:import|from)[ \t]+(?:""" + _PY_MODULES + r""")\b|""" + _DATABASE, re.M),
}


class Selection:
    def __init__(self, tests, reason=None, by_import=(), by_reads=(), changed=0):
        self.tests = tests                # every test file at the head
        self.reason = reason              # why every test must run, or None
        self.by_import = set(by_import)   # tests that are or import a changed file
        self.by_reads = set(by_reads)     # tests that always run
        self.changed = changed

    @property
    def selected(self):
        return sorted(self.tests) if self.reason else sorted(self.by_import | self.by_reads)

    def summary(self):
        if self.reason:
            return "%s: ALL %d test files: %s" % (NAME, len(self.tests), self.reason)
        return ("%s: selected %d of %d test files for %d changed files: %d are or import a changed file, %d always run "
                "(they or a file they import read files, start a process or use a database)"
                % (NAME, len(self.selected), len(self.tests), self.changed, len(self.by_import), len(self.by_reads)))


def merge_base(repo, explicit):
    """(the merge base with the pull request's base, None), or (None, why every test must run)."""
    name = explicit or os.environ.get("VIBE_VERIFIER_BASE_REF") or os.environ.get("GITHUB_BASE_REF")
    if not name:
        return None, "no base to compare with (not a pull request; pass --base-ref)"
    for ref in ("origin/" + name, name):
        try:
            return git(repo, "merge-base", ref, "HEAD").strip(), None
        except CannotRun:
            continue
    return None, "the base %s cannot be read (fetch the history: fetch-depth: 0)" % name


def changes(repo, base):
    """[(status letter, path)] from the merge base to HEAD, a rename as a deletion and an addition."""
    fields = git(repo, "diff", "-z", "--name-status", "--no-renames", base, "HEAD").split("\0")
    return [(fields[i][0], fields[i + 1]) for i in range(0, len(fields) - 1, 2)]


def trigger(path):
    """What kind of file `path` is when changing it means every test must run, else None."""
    name = posixpath.basename(path)
    if name in LOCKFILES or REQUIREMENTS.match(name):
        return "a lockfile"
    if TSCONFIG.match(name):
        return "a tsconfig"
    if RUNNER_CONFIG.match(name):
        return "a test runner's config"
    if name in RUNTIME_PINS:
        return "a runtime version pin"
    return None


def dependencies(text, where):
    try:
        manifest = json.loads(text) if text is not None else {}
    except ValueError:
        raise CannotRun("%s is not JSON" % where)
    return {field: manifest.get(field) for field in DEPENDENCY_FIELDS} if isinstance(manifest, dict) else {}


def changed_dependencies(repo, base, status, path):
    """Whether a changed package.json changed what it installs; a package.json that cannot be read did."""
    try:
        before = None if status == "A" else git(repo, "show", "%s:%s" % (base, path))
        after = None
        if status != "D":
            with open(os.path.join(repo, path), encoding="utf-8") as handle:
                after = handle.read()
        return dependencies(before, path + " at the base") != dependencies(after, path)
    except (CannotRun, OSError):
        return True


def runs_config(path, playwright):
    """Whether a test runner's config is one the listed tests run under (`playwright`: a test imports @playwright/test)."""
    return playwright or not PLAYWRIGHT_CONFIG.match(posixpath.basename(path))


def named_reason(repo, base, diff, all_on, playwright):
    """Why every test must run, judged by the changed paths alone, or None."""
    for _, path in diff:
        kind = trigger(path) if runs_config(path, playwright) else None
        kind = kind or ("named by --all-on" if matches(path, all_on) else None)
        if kind:
            return "%s changed (%s)" % (path, kind)
    for status, path in diff:
        if posixpath.basename(path) == "package.json" and changed_dependencies(repo, base, status, path):
            return "%s changed what it installs (%s)" % (path, ", ".join(DEPENDENCY_FIELDS))
    return None


def is_test(graph, path, globs):
    if globs:
        return matches(path, globs)
    if path.endswith(JS_EXTENSIONS):
        return matches(path, JS_TESTS) or bool(NODE_TEST.search(graph.text(path)))
    return matches(path, PY_TESTS)


def reads(graph, path):
    kind = "js" if path.endswith(JS_EXTENSIONS) else "py" if path.endswith(".py") else None
    return bool(kind and READS[kind].search(graph.text(path)))


def select(args):
    files = tracked_files(args.repo)
    base, reason = merge_base(args.repo, args.base_ref)
    diff = changes(args.repo, base) if base else []
    graph = Graph(args.repo, files, [path for status, path in diff if status == "D"])
    tests = [path for path in graph.files if is_test(graph, path, args.tests) and not matches(path, args.exclude or [])]
    playwright = any(PLAYWRIGHT_TEST.search(graph.text(test)) for test in tests)
    reason = reason or named_reason(args.repo, base, diff, args.all_on or [], playwright)
    if reason:
        return Selection(tests, reason)
    affected, always = reached(graph, tests, {path for _, path in diff}, playwright)
    always |= {test for test in tests if matches(test, args.always or [])}
    return Selection(tests, by_import=affected & set(tests), by_reads=always & set(tests), changed=len(diff))


def reached(graph, tests, changed, playwright):
    """(the files a change reaches, the files that reach a file that reads files) through imports."""
    configs = [path for path in graph.files if RUNNER_CONFIG.match(posixpath.basename(path)) and runs_config(path, playwright)]
    importers = graph.importers({path: graph.named_paths(path) for path in configs})
    always = reach(importers, {path for path in graph.files if reads(graph, path)})
    # A test runner's config, and each file it names (a setup file) or imports, runs before every test under its
    # directory: a change reaching it reaches those tests. What the config itself reads (a .env file) does not
    # make them read files.
    for config in configs:
        directory = posixpath.dirname(config)
        importers.setdefault(config, set()).update(test for test in tests if not directory or test.startswith(directory + "/"))
    return reach(importers, changed), always


# --report: what failed in the full run, by test file


def junit_failures(text, where):
    """[(the names a failed test case gives for its file)], and how many test cases the report holds."""
    try:
        root = ElementTree.fromstring(text)  # nosec B314: the job's own test report, not outside input
    except ElementTree.ParseError as error:
        raise CannotRun("%s is not a JUnit XML report: %s" % (where, error))
    suites = list(root.iter("testsuite")) + ([] if root.tag == "testsuite" else [root])
    cases = [(case, suite) for suite in suites for case in suite.findall("testcase")]
    return [_names(case, suite) for case, suite in cases if _failed(case)], len(cases)


def _failed(case):
    return case.find("failure") is not None or case.find("error") is not None


def _names(case, suite):
    """What a test case says its file is, most specific first: Vitest names it in classname and the suite, pytest a dotted classname."""
    names = [case.get("file"), case.get("classname"), suite.get("name") if suite.tag == "testsuite" else None, case.get("name")]
    return [name for name in names if name]


def locate(repo, name, tests):
    """The test files a report's name for one can only mean: a path (absolute, or relative to any directory a
    runner ran in) or a pytest dotted module name (`tests.unit.test_x.TestY`)."""
    if os.path.isabs(name):
        name = os.path.relpath(os.path.realpath(name), os.path.realpath(repo)).replace(os.sep, "/")
    name = posixpath.normpath(name)
    candidates = [name]
    if "/" not in name and not name.endswith((".py",) + JS_EXTENSIONS):
        parts = name.split(".")
        candidates = ["/".join(parts[:end]) + ".py" for end in range(len(parts), 0, -1)]
    for candidate in candidates:
        found = {test for test in tests if test == candidate or test.endswith("/" + candidate)}
        if found:
            return found
    return set()


def result_failures(path):
    """[(the names one failed test gives for its file)] in one results file: JUnit XML, or failed files one per line."""
    try:
        with open(path, encoding="utf-8", errors="replace") as handle:
            text = handle.read()
    except OSError as error:
        raise CannotRun("cannot read the results %s: %s" % (path, error.strerror))
    if not text.lstrip().startswith("<"):
        return [[line.strip()] for line in text.splitlines() if line.strip()]
    names, cases = junit_failures(text, path)
    if not cases:
        raise CannotRun("%s holds no test case: a full run that reported nothing is no evidence" % path)
    return names


def failed_tests(repo, results, tests):
    """(test files that failed in the full run, failures no test file could be found for)."""
    failed, unknown = set(), []
    for given in (names for path in results for names in result_failures(path)):
        # A case's names in order (file, classname, suite, name): the first that finds a test file decides.
        found = next((hit for hit in (locate(repo, name, tests) for name in given) if hit), set())
        failed |= found
        if not found:
            unknown.append(given[0])
    return failed, unknown


def report(args, selection):
    if selection.reason:
        return ["%s: report: the selection was ALL (%s), so no failure could be missed" % (NAME, selection.reason)], []
    failed, unknown = failed_tests(args.repo, args.report, selection.tests)
    chosen = set(selection.selected)
    misses = sorted(failed - chosen)
    lines = ["%s: report: %d test files failed in the full run, %d of them selected; %d missed"
             % (NAME, len(failed) + len(unknown), len(failed & chosen), len(misses) + len(unknown))]
    warnings = ["miss: %s failed in the full run and was not selected" % path for path in misses]
    warnings += ["miss: %r failed in the full run and names no test file this selection counts" % name for name in unknown]
    return lines, warnings


def command(args):
    try:
        selection = select(args)
        if args.report:
            lines, warnings = report(args, selection)
            print("\n".join(lines + [("::warning title=%s::%s" % (NAME, text)) if args.format == "github" else "%s: %s" % (NAME, text)
                                     for text in warnings]))
            return 0
    except CannotRun as reason:
        print("%s could not run: %s" % (NAME, reason), file=sys.stderr)
        return 2
    except Exception:
        traceback.print_exc()
        print("%s crashed (traceback above)" % NAME, file=sys.stderr)
        return 2
    print(selection.summary(), file=sys.stderr)
    if selection.reason or selection.selected:
        print("ALL" if selection.reason else "\n".join(selection.selected))
    return 0
