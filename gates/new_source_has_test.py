#!/usr/bin/env python3
"""Fail when a PR adds a source file that no test covers.

Differential: only files ADDED since the base ref. Editing an existing file never
trips it. A test counts if it sits next to the source (foo.test.ts, foo.spec.ts),
in a sibling __tests__ directory, or anywhere in the repo as long as it imports the
source by relative path (`from '../email/templates'`). The test may predate the PR.
Imports through a path alias (`@/lib/x`) are not resolved and do not count.

    --source GLOB    what counts as source (repeatable; default: JS and TS files)
    --exclude GLOB   extra paths to ignore (repeatable)
"""
import os
import posixpath
import re

from _contract import Finding, added_files, matches, resolve_base, run_gate, tracked_files

GATE = "new-source-has-test"

EXTENSIONS = ("ts", "tsx", "js", "jsx", "mjs", "cjs")
DEFAULT_SOURCE = ["**/*." + ext for ext in EXTENSIONS]
TEST_FILES = ["**/*.test.*", "**/*.spec.*", "**/__tests__/**"]
ALWAYS_EXCLUDED = TEST_FILES + [
    "**/*.d.ts", "**/*.types.ts", "**/index.*", "**/*.stories.*", "**/*.config.*",
    "**/node_modules/**",
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


def check(args):
    base = resolve_base(args.repo, args.base_ref)
    tracked = set(tracked_files(args.repo))
    source = args.source or DEFAULT_SOURCE
    excluded = ALWAYS_EXCLUDED + (args.exclude or [])
    imported = None  # read test files only once, and only if a naming lookup misses
    findings = []
    for path in added_files(args.repo, base):
        if path not in tracked or not matches(path, source) or matches(path, excluded):
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
