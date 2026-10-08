#!/usr/bin/env python3
"""Fail when a PR adds a source file that no test covers.

Differential: only files ADDED since the base ref. Editing an existing file never
trips it. The test may predate the PR.

JS and TS: a test counts if it sits next to the source (foo.test.ts, foo.spec.ts),
in a sibling __tests__ directory, or anywhere in the repo as long as it imports the
source by relative path (`from '../email/templates'`). Imports through a path alias
(`@/lib/x`) are not resolved and do not count.

Python: a test is a test_*.py, *_test.py, test-*.py, *-test.py or *.test.py file. One
counts if it is named for the source (test_foo.py, foo_test.py, test-foo.py, foo-test.py
or foo.test.py) beside it or under a tests/ or test/ directory, if it imports the source
(`import pkg.foo`, `from pkg import foo`, `from .foo import x`; a dotted name counts when
it is the end of the source's path, so `pkg.foo` and `foo` both cover src/pkg/foo.py), or
if it runs the source as a script: a string that is its file name, ends in /<file name>,
or is its command name (foo_bar.py as `foo-bar`).

    --source GLOB    what counts as source (repeatable; default: JS, TS and Python files)
    --exclude GLOB   extra paths to ignore (repeatable)
"""
import ast
import os
import posixpath
import re

from _contract import Finding, added_files, matches, resolve_base, run_gate, tracked_files

GATE = "new-source-has-test"

EXTENSIONS = ("ts", "tsx", "js", "jsx", "mjs", "cjs")
DEFAULT_SOURCE = ["**/*." + ext for ext in EXTENSIONS] + ["**/*.py"]
# What a Python test file is called, around the stem of what it tests: pytest's two names, and the
# hyphen and dot forms of test files that are run by path.
PY_TEST_NAMES = ("test_%s.py", "%s_test.py", "test-%s.py", "%s-test.py", "%s.test.py")
PY_TESTS = ["**/" + name % "*" for name in PY_TEST_NAMES]
TEST_FILES = ["**/*.test.*", "**/*.spec.*", "**/__tests__/**"] + PY_TESTS
ALWAYS_EXCLUDED = TEST_FILES + [
    "**/*.d.ts", "**/*.types.ts", "**/index.*", "**/*.stories.*", "**/*.config.*",
    "**/node_modules/**", "**/__init__.py", "**/conftest.py", "**/setup.py",
]
# `from './x'`, `import './x'`, `import('./x')`, `require('./x')`: relative specifiers only.
RELATIVE_IMPORT = re.compile(r"""(?:\bfrom\s*|\bimport\s*\(?\s*|\brequire\s*\(\s*)['"](\.\.?/[^'"]+)['"]""")


def test_candidates(path):
    directory, _, name = path.rpartition("/")
    stem = name.rsplit(".", 1)[0]
    prefix = directory + "/" if directory else ""
    for ext in EXTENSIONS:
        for kind in ("test", "spec"):
            yield "%s%s.%s.%s" % (prefix, stem, kind, ext)
            yield "%s__tests__/%s.%s.%s" % (prefix, stem, kind, ext)


def module_key(path):
    """A path without its source extension, so `../a`, `../a.js` and `a.ts` all agree."""
    stem, dot, ext = path.rpartition(".")
    return stem if dot and ext in EXTENSIONS else path


def imported_by_tests(repo, tracked):
    """Every module some test file imports by relative path, as module keys."""
    keys = set()
    for test in tracked:
        if not matches(test, TEST_FILES) or not test.endswith(EXTENSIONS):
            continue
        try:
            with open(os.path.join(repo, test), encoding="utf-8", errors="replace") as handle:
                text = handle.read()
        except OSError:
            continue
        for spec in RELATIVE_IMPORT.findall(text):
            keys.add(module_key(posixpath.normpath(posixpath.join(posixpath.dirname(test), spec))))
    return keys


def named_python_test(path, tracked):
    """A tracked test named for the source (PY_TEST_NAMES) beside it or under a tests/ or test/ directory."""
    stem = posixpath.splitext(posixpath.basename(path))[0]
    names = {name % stem for name in PY_TEST_NAMES}
    for test in tracked:
        directory, name = posixpath.split(test)
        if name in names and (directory == posixpath.dirname(path) or {"tests", "test"} & set(directory.split("/"))):
            return True
    return False


def python_references(repo, tracked):
    """(dotted names Python test files import, as tuples; every string literal they hold)."""
    modules, strings = set(), set()
    for test in tracked:
        if not matches(test, PY_TESTS):
            continue
        try:
            with open(os.path.join(repo, test), encoding="utf-8", errors="replace") as handle:
                tree = ast.parse(handle.read())
        except (OSError, SyntaxError, ValueError):
            continue
        package = posixpath.dirname(test).split("/") if posixpath.dirname(test) else []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                modules.update(tuple(alias.name.split(".")) for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                base = tuple(node.module.split(".")) if node.module else ()
                if node.level:  # relative: resolved against the test file's own directory
                    base = tuple(package[:len(package) - node.level + 1]) + base
                if base:
                    modules.add(base)
                modules.update(base + (alias.name,) for alias in node.names if alias.name != "*")
            elif isinstance(node, ast.Constant) and isinstance(node.value, str):
                strings.add(node.value)
    return modules, strings


def python_test_refers_to(path, references):
    """Whether a Python test imports the source or names it as a script."""
    modules, strings = references
    parts = tuple(posixpath.splitext(path)[0].split("/"))
    if any(parts[-n:] in modules for n in range(1, len(parts) + 1)):
        return True
    name = posixpath.basename(path)
    stem = posixpath.splitext(name)[0]
    command = {stem.replace("_", "-")} if "_" in stem else set()
    return any(s == name or s.endswith("/" + name) or s in command for s in strings)


def check(args):
    base = resolve_base(args.repo, args.base_ref)
    tracked = set(tracked_files(args.repo))
    source = args.source or DEFAULT_SOURCE
    excluded = ALWAYS_EXCLUDED + (args.exclude or [])
    imported = None  # read test files only once, and only if a naming lookup misses
    references = None
    findings = []
    for path in added_files(args.repo, base):
        if path not in tracked or not matches(path, source) or matches(path, excluded):
            continue
        if path.endswith(".py"):
            if named_python_test(path, tracked):
                continue
            if references is None:
                references = python_references(args.repo, tracked)
            if not python_test_refers_to(path, references):
                findings.append(Finding("new source file has no test (no test_*.py, *_test.py, test-*.py, *-test.py or "
                                        "*.test.py is named for it, imports it, or runs it by name)", path))
            continue
        if any(candidate in tracked for candidate in test_candidates(path)):
            continue
        if imported is None:
            imported = imported_by_tests(args.repo, tracked)
        if module_key(path) not in imported:
            findings.append(Finding("new source file has no test (none beside it, in __tests__/, or importing it by relative path)", path))
    return findings


def add_arguments(parser):
    parser.add_argument("--source", action="append", metavar="GLOB")
    parser.add_argument("--exclude", action="append", metavar="GLOB")


if __name__ == "__main__":
    run_gate(GATE, __doc__, check, add_arguments)
