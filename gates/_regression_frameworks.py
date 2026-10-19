"""Which runner a JS or TS test file of a fix belongs to, for regression-test-fails-on-base: read from the file's
imports and from how its package runs its tests, never from the calls alone. A file that only calls describe, it or
test is read the same by Vitest with `globals: true`, Jest and bun test, so the nearest directory, from the file's up
to the root, holding a Vitest config, a Jest config or a package.json that names its runner decides.
"""
import json
import os
import re

VITEST = re.compile(r"""['"]vitest['"]""")
BUN_TEST = re.compile(r"""['"]bun:test['"]""")
JEST_GLOBALS = re.compile(r"""['"]@jest/globals['"]""")
# Test calls with no import: Vitest with `globals: true`, Jest and bun test all read them, so the package decides.
TEST_CALLS = re.compile(r"""^\s*(?:describe|it|test)(?:\.\w+)*\s*\(""", re.MULTILINE)
# The runners a repository may run its tests with that this gate does not run yet, as its messages name them.
RUNNER_NAMES = {"jest": "Jest", "bun": "bun"}
UNSUPPORTED_NAMES = {"jest": "Jest", "bun": "bun test"}
CONFIG_RUNNERS = ((("vitest.config.", "vitest.workspace."), "vitest"), (("jest.config.",), "jest"))
RUNNERS = "this gate runs Vitest, node:test, pytest and unittest"


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
