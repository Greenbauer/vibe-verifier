"""Which runner a changed test file of a fix belongs to, for regression-test-fails-on-base. A JS or TS file's is read
from the file's imports and from how its repository runs it, never from the calls alone. A file that only calls describe, it or
test is read the same by Vitest with `globals: true`, Jest and bun test, so the nearest directory, from the file's up
to the root, holding a Vitest config, a Jest config or a package.json that names its runner decides.
"""
import json
import os
import re
import sys

from _contract import PY_TESTS, CannotRun, matches
from _regression_node import plan as node_plan
from _regression_playwright import collected

JS = tuple("." + ext for ext in ("ts", "tsx", "js", "jsx", "mjs", "cjs", "mts", "cts"))
PLAYWRIGHT = re.compile(r"""['"]@playwright/test['"]""")
NODE_TEST = re.compile(r"""['"]node:test['"]""")
VITEST = re.compile(r"""['"]vitest['"]""")
BUN_TEST = re.compile(r"""['"]bun:test['"]""")
JEST_GLOBALS = re.compile(r"""['"]@jest/globals['"]""")
# Test calls with no import: Vitest with `globals: true`, Jest and bun test all read them, so the package decides.
TEST_CALLS = re.compile(r"""^\s*(?:describe|it|test)(?:\.\w+)*\s*\(""", re.MULTILINE)
# The runners a repository may run its tests with that this gate does not run yet, as its messages name them.
RUNNER_NAMES = {"jest": "Jest", "bun": "bun"}
UNSUPPORTED_NAMES = {"jest": "Jest", "bun": "bun test"}
CONFIG_RUNNERS = ((("vitest.config.", "vitest.workspace."), "vitest"), (("jest.config.",), "jest"))
RUNNERS = "this gate runs Vitest, node:test, pytest and unittest, and Playwright specs with --playwright"


def declared_runner(package_json):
    """'vitest', 'jest' or 'bun' when a package.json says it runs its tests so: a vitest dependency, a jest
    dependency or `jest` key, or a script that runs `bun test`; else None."""
    try:
        with open(package_json, encoding="utf-8") as handle:
            package = json.load(handle)
    except (OSError, ValueError):
        return None
    if not isinstance(package, dict):
        return None
    dependencies = {}
    for field in ("dependencies", "devDependencies"):
        dependencies.update(package.get(field) if isinstance(package.get(field), dict) else {})
    scripts = package.get("scripts") if isinstance(package.get("scripts"), dict) else {}
    if "vitest" in dependencies:
        return "vitest"
    if "jest" in dependencies or "jest" in package:
        return "jest"
    if any(isinstance(script, str) and re.search(r"\bbun\s+test\b", script) for script in scripts.values()):
        return "bun"
    return None


def directory_runner(repo, directory):
    """(the runner, the file that says so) when `directory` holds a Vitest config, a Jest config or a package.json
    that names its runner, else (None, None)."""
    names = sorted(os.listdir(os.path.join(repo, directory)))
    for prefixes, runner in CONFIG_RUNNERS:
        configs = [name for name in names if name.startswith(prefixes)]
        if configs:
            return runner, os.path.join(directory, configs[0])
    runner = declared_runner(os.path.join(repo, directory, "package.json")) if "package.json" in names else None
    return (runner, os.path.join(directory, "package.json")) if runner else (None, None)


def package_runner(repo, path):
    """(the runner, the file that says so) for a test file that calls describe, it or test without importing a
    framework: the nearest directory, from the file's up to the root, that names one. A package.json that names
    none (a workspace package that runs its tests with the root's Vitest) passes the question up. (None, None)
    when nothing does."""
    directory = os.path.dirname(path)
    while True:
        found = directory_runner(repo, directory)
        if found[0] or not directory:
            return found
        directory = os.path.dirname(directory)


def framework(repo, path, text):
    """('vitest', None) for a JS or TS test that is neither Playwright's nor node:test's and runs under Vitest;
    ('jest' or 'bun', why) for one its package runs with a runner this gate does not run yet; else (None, why)."""
    if BUN_TEST.search(text):
        return "bun", "a bun test (it imports bun:test), which this gate does not run; " + RUNNERS
    if JEST_GLOBALS.search(text):
        return "jest", "a Jest test (it imports @jest/globals), which this gate does not run; " + RUNNERS
    if VITEST.search(text):
        return "vitest", None
    if not TEST_CALLS.search(text):
        return None, "a script with no test framework, judged by its exit code; " + RUNNERS
    runner, where = package_runner(repo, path)
    if runner == "vitest":
        return "vitest", None
    if runner:
        return runner, "a %s test (%s), which this gate does not run; %s" % (RUNNER_NAMES[runner], where, RUNNERS)
    return None, ("it calls describe, it or test without importing a framework, and no Vitest config or package.json "
                  "that names vitest holds it; " + RUNNERS)


def node_test(args, path):
    """How the repository runs a JS or TS file with `node --test` (gates/_regression_node.py), or None."""
    return node_plan(args.repo, path, tuple(args.node_test or ())) if path.endswith(JS) else None


def kind(args, path, listed):
    """(its runner, None) for a test this gate runs ('vitest', 'node' or 'python'); ('jest' or 'bun', why) for a test
    of a runner it does not run yet; else (None, why). `listed` is ({path: config}, {path: why it does not load}) of
    the --playwright configs at HEAD: a spec one collects runs, even when --tests also names it."""
    if path in listed[0]:
        return "playwright", None
    if matches(path, args.tests or []):
        return None, "declared test code (--tests), which this gate does not run"
    if path.endswith(".py") and matches(path, PY_TESTS):
        return "python", None
    if not path.endswith(JS):
        return None, "a %s test; %s" % (os.path.splitext(path)[1] or "file", RUNNERS)
    with open(os.path.join(args.repo, path), encoding="utf-8", errors="replace") as handle:
        text = handle.read()
    if PLAYWRIGHT.search(text):
        return None, playwright_why(args, path, listed[1])
    if node_test(args, path):
        return "node", None
    if NODE_TEST.search(text):
        return None, "a node:test file that no package.json script runs with `node --test` and no --node-test declares"
    return framework(args.repo, path, text)


def maybe_playwright(args, path):
    """Whether a changed test could be a Playwright spec, so it is listed under the --playwright configs: a JS or TS
    file that imports none of vitest, @jest/globals or bun:test and that no package.json script runs with
    `node --test`. The others never start Playwright, so a fix with only those needs nothing a browser run does (a file
    that imports another runner, such as mocha, is listed, and not collected)."""
    if not path.endswith(JS) or node_test(args, path):
        return False
    return not any(found.search(text_of(args, path)) for found in (VITEST, NODE_TEST, BUN_TEST, JEST_GLOBALS))


def text_of(args, path):
    with open(os.path.join(args.repo, path), encoding="utf-8", errors="replace") as handle:
        return handle.read()


def listed_specs(args, tests, deadline):
    """({path: config}, {path: why it does not load}) of the changed tests the --playwright configs collect at HEAD. A
    test that imports @playwright/test is a spec, so a listing that cannot run (a credential in the step, no
    Playwright) is exit 2. When every candidate only might be one (it calls test() and imports no framework, as a
    Vitest or Jest file with globals does), such a listing means none is taken for a spec, and they are classified
    as before, so a fix with no spec never depends on a browser run."""
    if not args.playwright:
        return {}, {}
    candidates = [path for path in tests if maybe_playwright(args, path)]
    try:
        return collected(args.repo, args.playwright, candidates, set(args.local_env or ()), deadline, args.timeout)
    except CannotRun as error:
        if any(PLAYWRIGHT.search(text_of(args, path)) for path in candidates):
            raise
        print("regression-test-fails-on-base: no changed test imports @playwright/test, and listing the others under "
              "--playwright could not run, so none is taken for a spec: %s" % error, file=sys.stderr, flush=True)
        return {}, {}


def playwright_why(args, path, unloaded):
    """Why a Playwright spec is not run: no --playwright config collects it, or it does not load under one."""
    if path in unloaded:
        return "a Playwright test that does not load at HEAD: " + unloaded[path]
    if args.playwright:
        return "a Playwright test that no --playwright config (%s) collects" % ", ".join(args.playwright)
    return ("a Playwright test, which needs a browser and a running site; name the config that runs it with --playwright, "
            "and this gate runs it against the site that config's webServer starts on this machine")


def classify(args, tests, listed):
    """({path: runner} of the tests this gate runs, [(path, why)] of the others, {path: runner} of those whose runner
    this gate does not run yet)."""
    judged, not_judged, unsupported = {}, [], {}
    for path in tests:
        what, why = kind(args, path, listed)
        if what and what not in UNSUPPORTED_NAMES:
            judged[path] = what
        else:
            not_judged.append((path, why))
            unsupported.update({path: what} if what else {})
    return judged, not_judged, unsupported
