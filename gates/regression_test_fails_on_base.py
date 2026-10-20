#!/usr/bin/env python3
"""Fail a fix pull request unless one of its new or changed tests fails on the code before the fix.

A wired gate: the pull request's title and body are files the workflow writes before it runs, the way
ticket-coverage reads the body (`gh pr view <n> --json title --jq .title`, then `--json body --jq .body`).

    regression-test-fails-on-base --title pr-inputs/title.txt --body pr-inputs/body.md

A fix is a pull request whose title has the conventional-commit type `fix` (`fix:`, `fix(scope):`,
`fix!:`). Anything else passes without running a test, and so does a fix whose every change is to
tests, documentation (.md, .mdx, .rst, .adoc) or comments and whitespace. A fix that cannot carry a
test says why on a body line `regression-test: none -- <reason>`: it passes, and the reason is printed.

Otherwise the fix must add or change a test. The gate runs the test files it changed twice, under the
project's own runner: at HEAD, where every one of their tests must pass (a red suite is exit 2), and
on the code before the fix, the merge base with only the pull request's test code laid over it. It
passes when a test that passes at HEAD fails there, or when the file's own top-level code fails an
assertion there before its tests run (any other error that stops it there means it did not load); it
fails when they all pass there, which does not
reproduce the bug, and when the only failures there are a module, export, file or script the test
needs not existing yet, which does not show the bug either. Each runner's structured report decides
(Vitest's JSON reporter, node:test's TAP reporter, pytest's reports, unittest's results, a Playwright
reporter of the gate's), never an exit code.

Test code is a file named as a test (`*.test.*`, `*.spec.*`, `__tests__/`, and test_*.py, *_test.py,
test-*.py, *-test.py, *.test.py), conftest.py, anything under a `test`, `tests`, `__tests__`,
`__mocks__`, `__snapshots__` or `__fixtures__` directory, --tests and --node-test, and a file the
repository runs with `node --test`. Of it, a JS or TS file that a package.json script's
`node ... --test ...` command selects runs that way, from that package with the command's options,
and one --node-test declares runs as `node --test <file>` from the root (gates/_regression_node.py);
a JS or TS file that imports vitest, or calls describe, it or test in a package that runs Vitest (the
nearest Vitest config, Jest config or package.json naming its runner, from the file's directory up, is a
Vitest config or a package.json with a vitest dependency; gates/_regression_frameworks.py), runs under Vitest (from the nearest directory
with a Vitest or Vite config, else with a package.json). Python tests run under --python, with
pytest when it is installed there and unittest when not or when the file's name has a hyphen or a dot
(a test run by path, which pytest cannot import). A Playwright spec that a --playwright config collects runs
under that config against the site its webServer starts on this machine, each run refused (exit 2) first when a
target is not this machine or what the run inherits holds a credential for anywhere else
(gates/_regression_playwright.py, gates/_local_only.py), and kept from reaching anywhere else during it
(gates/_local_network.py). Another Playwright test, a Jest test (it imports
@jest/globals, or its package has a Jest config, a jest dependency or a `jest` key), a bun test (it
imports bun:test, or its package's scripts run `bun test`), a node:test file the repository does not
run with `node --test`, a shell or other test, and a --tests path no --playwright config collects are
reported as not judged, naming
the runner, and a fix whose only tests are such files cannot be judged (exit 2), except that a fix whose
only such files are Jest or bun tests (the repository's own runner, which this gate does not run yet)
passes qualified, each file an advisory, so a soak in a Jest or bun repository is not exit 2.

The project's dependencies must be installed before the gate runs. It swaps the files the fix changed
back to the merge base in the working tree for the second run and always puts them back, so those
files must not differ from HEAD when it starts.

    --title FILE       the pull request's title (required)
    --body FILE        the pull request's body (required)
    --python PATH      the project's interpreter (default: python3 on PATH; a relative path is the repository's)
    --tests GLOB       more paths that are test code, never run (repeatable; an e2e directory, say)
    --node-test GLOB   node:test files the repository runs as `node --test <file>` some way no package.json
                       script says (a shell script, a workflow step): run that way (repeatable)
    --playwright CONFIG  a Playwright config whose specs run against the site its webServer starts (repeatable)
    --local-env NAME   a variable named like a credential that holds the job's own local stack's secret (repeatable)
    --timeout SECONDS  wall-clock limit for both runs (default 600)
"""
import io
import os
import re
import signal
import sys
import time
import tokenize
from contextlib import contextmanager

from _acceptance import DASH
from _contract import PY_TESTS, CannotRun, Finding, git, matches, qualify, resolve_base, run_gate
from _regression_frameworks import JS, UNSUPPORTED_NAMES, classify, listed_specs, node_test
from _regression_node import run_node
from _regression_playwright import run_playwright
from _regression_runners import missing, run_python, run_vitest
from _ticket import unfenced
from universal_checks import lex

GATE = "regression-test-fails-on-base"
FIX = re.compile(r"^\s*fix(?:\([^)]*\))?!?:", re.IGNORECASE)
REASON = re.compile(r"^[ \t]*(?:[-*+][ \t]+)?regression-test:[ \t]*none[ \t]*" + DASH + r"[ \t]*(?P<reason>\S.*?)[ \t]*$",
                    re.IGNORECASE)
TEST_NAMES = ["**/*.test.*", "**/*.spec.*", "**/__tests__/**"] + PY_TESTS  # the names new-source-has-test reads
TEST_CODE = TEST_NAMES + ["**/conftest.py"] + ["**/%s/**" % name for name in
                                               ("test", "tests", "__tests__", "__mocks__", "__snapshots__", "__fixtures__")]
DOCS = ["**/*.md", "**/*.mdx", "**/*.rst", "**/*.adoc"]
ASSERTION = re.compile(r"AssertionError\b")
HOW = ("make one fail there through code that exists before the fix, or say why the fix cannot carry one "
       "with a body line `regression-test: none -- <reason>`")


def note(text):
    print("%s: %s" % (GATE, text), file=sys.stderr, flush=True)


def read(path, flag):
    try:
        with open(path, encoding="utf-8") as handle:
            return handle.read()
    except OSError as error:
        raise CannotRun("%s cannot be read: %s" % (flag, error))


def declared_reason(body):
    """The reason on the body's `regression-test: none -- <reason>` line, outside code fences, or None."""
    for _, line in unfenced(body.splitlines()):
        found = REASON.match(line)
        if found:
            return found.group("reason")
    return None


def changes(repo, base):
    """[(status, path)] from the merge base to HEAD, a move being a deletion and an addition."""
    fields = git(repo, "diff", "-z", "--name-status", "--no-renames", base, "HEAD").split("\0")
    return [(fields[i][0], fields[i + 1]) for i in range(0, len(fields) - 1, 2)]


def code_of(path, text):
    """A JS, TS or Python file's code without its comments and whitespace, in a form two versions compare in."""
    if path.endswith(JS):
        return [line.strip() for line in lex(text)[0].splitlines() if line.strip()]
    try:
        return [(token.type, "" if token.type in (tokenize.INDENT, tokenize.NEWLINE) else token.string)
                for token in tokenize.generate_tokens(io.StringIO(text).readline)
                if token.type not in (tokenize.COMMENT, tokenize.NL)]
    except (tokenize.TokenError, SyntaxError):
        return text


def comments_only(repo, base, path):
    """Whether the change to a JS, TS or Python file touches only comments and whitespace. Any change to
    another file is a change."""
    if not path.endswith(JS + (".py",)):
        return False
    before, after = (git(repo, "show", "%s:%s" % (rev, path)) for rev in (base, "HEAD"))
    return code_of(path, before) == code_of(path, after)


def run_tests(args, judged, deadline, plans):
    """{path: Run} of the judged files; `plans` is how the repository runs each node:test one, and the config that
    runs each Playwright one, read at HEAD."""
    runs = {}
    vitest = [path for path, what in judged.items() if what == "vitest"]
    python = [path for path, what in judged.items() if what == "python"]
    node = {path: plans[path] for path, what in judged.items() if what == "node"}
    if vitest:
        runs.update(run_vitest(args.repo, vitest, deadline, args.timeout))
    if node:
        runs.update(run_node(args.repo, node, deadline, args.timeout))
    if python:
        runs.update(run_python(args.repo, args.python, python, deadline, args.timeout))
    browser = {path: plans[path] for path, what in judged.items() if what == "playwright"}
    if browser:
        runs.update(run_playwright(args.repo, browser, set(args.local_env or ()), deadline, args.timeout))
    return runs


def interrupted(signum, _frame):
    raise CannotRun("stopped by %s; no verdict" % signal.Signals(signum).name)


@contextmanager
def code_before_the_fix(repo, base, swapped):
    """The working tree with every non-test path the fix changed back at the merge base; HEAD's on exit."""
    signal.signal(signal.SIGTERM, interrupted)
    signal.signal(signal.SIGINT, interrupted)
    try:
        git(repo, "--literal-pathspecs", "restore", "--source=" + base, "--worktree", "--", *[path for _, path in swapped])
        yield
    finally:
        at_head = [path for status, path in swapped if status != "D"]
        try:
            if at_head:
                git(repo, "--literal-pathspecs", "restore", "--source=HEAD", "--worktree", "--", *at_head)
        finally:
            for status, path in swapped:
                if status == "D" and os.path.lexists(os.path.join(repo, path)):
                    os.remove(os.path.join(repo, path))


def at_head(run):
    """(the ids of a file's tests that passed at HEAD, why it is not judged or None, its failing tests)."""
    if run is None:
        return set(), "the project's Vitest does not collect it (its config's include globs)", []
    if run.load_error:
        return set(), "it does not load at HEAD: %s" % run.load_error, []
    failed = ["%s: %s" % (name, error) for name, (status, error) in run.tests.items() if status == "failed"]
    ran = {name for name, (status, _) in run.tests.items() if status == "passed"}
    return ran, None if ran else "no test in it ran at HEAD (a script judged by its exit code is not a test this gate runs)", failed


def passed_at_head(runs, judged, not_judged):
    """{path: the ids of its tests that passed at HEAD}; a file that did not run there is not judged.
    A test that failed at HEAD is the pull request's red suite: there is nothing to compare."""
    passed, red = {}, []
    for path in judged:
        ran, why, failed = at_head(runs.get(path))
        red += ["%s: %s" % (path, line) for line in failed]
        if why:
            not_judged.append((path, why))
        else:
            passed[path] = ran
    if red:
        raise CannotRun("%d changed test(s) fail at HEAD, so the pull request's suite is red and there is nothing to compare "
                        "the code before the fix with: %s" % (len(red), "; ".join(red[:5])))
    return passed


def only_missing(what, error, repo):
    """The finding for a test that failed before the fix only because `what` did not exist yet."""
    for root in {os.path.realpath(repo), os.path.abspath(repo)}:
        what = what[len(root) + 1:] if what.startswith(root + os.sep) else what
    return "fails on the code before the fix only because `%s` is missing (%s), which does not show the bug" % (what, error)


def before_the_fix(repo, names, run):
    """(the tests of a file that fail on the code before the fix and show the bug, the finding when none does)."""
    if run is None:
        return [], "the project's Vitest does not collect it before the fix, so it shows nothing about the bug"
    if run.load_error:
        if ASSERTION.match(run.load_error):  # the file's own top-level check failed there: an assertion, not a load error
            return [("its top-level code", run.load_error)], None
        what = missing(run.load_error)
        return [], (only_missing(what, run.load_error, repo) if what else
                    "does not load on the code before the fix (%s), so none of its tests ran there" % run.load_error)
    failed = [(name, run.tests[name][1]) for name in sorted(names) if run.tests.get(name, ("",))[0] == "failed"]
    shown = [(name, error) for name, error in failed if not missing(error)]
    if shown or not failed:
        return shown, None if shown else ("its %d test(s) all pass on the code before the fix, so it does not reproduce "
                                          "the bug" % len(names))
    name, error = failed[0]
    return [], only_missing(missing(error), "%s, in %s" % (error, name), repo)


def verdict(repo, passed, base_runs):
    """[] when a test that passed at HEAD fails before the fix for a reason other than something missing."""
    findings = []
    for path, names in sorted(passed.items()):
        shown, problem = before_the_fix(repo, names, base_runs.get(path))
        for name, error in shown[:3]:
            note("%s: %s fails on the code before the fix: %s" % (path, name, error))
        if shown:
            return []
        findings.append(Finding(problem, path))
    return [Finding("no new or changed test fails on the code before the fix, so none shows the bug it fixes: " + HOW)] + findings


def none_ran(not_judged, unsupported):
    """The verdict of a fix none of whose changed tests this gate ran: when every one is a test of a runner it does
    not run yet ({path: runner} in `unsupported`), the repository's own, nothing in the job is wrong, so a qualified
    pass with each file an advisory; else exit 2."""
    if not_judged and all(path in unsupported for path, _ in not_judged):
        qualify("not judged: the changed tests run under %s, which this gate does not run yet"
                % " and ".join(sorted({UNSUPPORTED_NAMES[runner] for runner in unsupported.values()})))
        return [Finding("not judged: " + why, path, advisory=True) for path, why in not_judged]
    raise CannotRun("no changed test is one this gate can run (%s); a fix whose only test needs what this gate does not "
                    "have can say so with a body line `regression-test: none -- <reason>`"
                    % "; ".join("%s: %s" % item for item in not_judged))


def runners(args, tests, deadline):
    """classify()'s three answers, and `plans`: how the repository runs each node:test file, and the config that runs
    each Playwright spec, read at HEAD."""
    listed = listed_specs(args, tests, deadline)
    judged, not_judged, unsupported = classify(args, tests, listed)
    plans = {path: node_test(args, path) if what == "node" else listed[0][path]
             for path, what in judged.items() if what in ("node", "playwright")}
    return judged, not_judged, unsupported, plans


def compare(args, base, changed, tests, is_test_code):
    """Run the changed tests at HEAD and on the code before the fix, and judge them."""
    dirty = git(args.repo, "--literal-pathspecs", "status", "--porcelain", "-z", "--untracked-files=all", "--",
                *[path for _, path in changed])
    if dirty:
        raise CannotRun("the working tree differs from HEAD at a path the pull request changed (%s): this gate swaps those "
                        "files and puts them back, so commit or stash first" % dirty.split("\0")[0][3:])
    deadline = time.monotonic() + args.timeout
    judged, not_judged, unsupported, plans = runners(args, tests, deadline)
    passed = passed_at_head(run_tests(args, judged, deadline, plans) if judged else {}, judged, not_judged)
    for path, why in not_judged:
        note("%s: not judged: %s" % (path, why))
    if not passed:
        return none_ran(not_judged, unsupported)
    with code_before_the_fix(args.repo, base, [(status, path) for status, path in changed if not is_test_code(path)]):
        base_runs = run_tests(args, {path: judged[path] for path in passed}, deadline, plans)
    return verdict(args.repo, passed, base_runs)


def check(args):
    title, body = read(args.title, "--title"), read(args.body, "--body")
    if not FIX.match(title):
        note("not a fix pull request (its title's type is not `fix`): nothing to check")
        return []
    reason = declared_reason(body)
    if reason:
        qualify("no regression test, as the pull request body declares")
        return [Finding("no regression test, as the pull request body declares: " + reason, advisory=True)]
    if args.timeout <= 0:
        raise CannotRun("--timeout must be a positive number of seconds")
    base = git(args.repo, "merge-base", resolve_base(args.repo, args.base_ref), "HEAD").strip()
    changed = changes(args.repo, base)
    declared = (args.tests or []) + (args.node_test or [])

    def is_test_code(path):
        return matches(path, TEST_CODE + declared) or node_test(args, path) is not None

    real = [(status, path) for status, path in changed if status != "M" or not comments_only(args.repo, base, path)]
    if all(is_test_code(path) or matches(path, DOCS) for _, path in real):
        note("every change is to tests, documentation or comments: nothing to check")
        return []
    tests = [path for status, path in real if status != "D" and (matches(path, TEST_NAMES + declared) or node_test(args, path))]
    if not tests:
        return [Finding("a fix pull request must add or change a test that fails on the code before the fix, and this one "
                        "changes none (a file named as a test, `*.test.*`, `*.spec.*` or test_*.py, a path the manifest "
                        "declares with --tests or --node-test, or a file a package.json script runs with `node --test`; a "
                        "change to comments only does not count): add one, or say why the fix cannot carry one with a body "
                        "line `regression-test: none -- <reason>`")]
    return compare(args, base, changed, tests, is_test_code)


def add_arguments(parser):
    parser.add_argument("--title", metavar="FILE", required=True, help="a file holding the pull request's title")
    parser.add_argument("--body", metavar="FILE", required=True, help="a file holding the pull request's body")
    parser.add_argument("--python", default="python3", help="the project's interpreter (default: python3)")
    parser.add_argument("--tests", action="append", metavar="GLOB", help="more paths that are test code, never run (repeatable)")
    parser.add_argument("--node-test", action="append", metavar="GLOB",
                        help="node:test files run as `node --test <file>` from the repository root (repeatable)")
    parser.add_argument("--playwright", action="append", metavar="CONFIG",
                        help="a Playwright config whose specs this gate runs, against the site its webServer starts here (repeatable)")
    parser.add_argument("--local-env", action="append", metavar="NAME",
                        help="a variable named like a credential that holds the job's own local stack's secret (repeatable)")
    parser.add_argument("--timeout", type=int, default=600, help="seconds both test runs may take together (default 600)")


if __name__ == "__main__":
    run_gate(GATE, __doc__, check, add_arguments)
