#!/usr/bin/env python3
"""Fail when a file this pull request changed is over the line limit and got longer.

A ratchet on file length: a changed file is a finding when it exceeds --max lines and is new or
grew since the base ref. A long file that shrank, or that nobody touched, never blocks. --all
reports every tracked source file over the limit instead, for an audit.

    --max N          lines a file may have (default 500)
    --source GLOB    what counts as source (repeatable; default: JS and TS files)
    --exclude GLOB   extra paths to ignore (repeatable; node_modules and *.d.ts always are)
"""
import os

from _contract import CannotRun, Finding, changed_files, git, matches, renamed, resolve_base, run_gate, tracked_files

GATE = "max-file-lines"
DEFAULT_SOURCE = ["**/*." + ext for ext in ("ts", "tsx", "js", "jsx", "mjs", "cjs", "mts", "cts")]
ALWAYS_EXCLUDED = ["**/*.d.ts", "**/node_modules/**"]


def line_count(text):
    return text.count("\n") + (1 if text and not text.endswith("\n") else 0)


def check(args):
    source = args.source or DEFAULT_SOURCE
    excluded = ALWAYS_EXCLUDED + (args.exclude or [])
    tracked = set(tracked_files(args.repo))
    base = None if args.all else resolve_base(args.repo, args.base_ref)
    candidates = tracked if args.all else set(changed_files(args.repo, base))
    moved = {} if args.all else renamed(args.repo, base)
    findings = []
    for path in sorted(p for p in candidates if p in tracked and matches(p, source) and not matches(p, excluded)):
        with open(os.path.join(args.repo, path), encoding="utf-8", errors="replace") as handle:
            now = line_count(handle.read())
        if now <= args.max:
            continue
        if args.all:
            findings.append(Finding("%d lines, over the %d allowed" % (now, args.max), path))
            continue
        try:
            before = line_count(git(args.repo, "show", "%s:%s" % (base, moved.get(path, path))))
        except CannotRun:
            before = None  # new in this pull request
        if before is not None and now <= before:
            continue
        findings.append(Finding("%d lines, over the %d allowed (%s)" % (now, args.max, "new file" if before is None else "%d at the base" % before), path))
    return findings


def add_arguments(parser):
    parser.add_argument("--max", type=int, default=500, help="lines a file may have (default 500)")
    parser.add_argument("--source", action="append", metavar="GLOB")
    parser.add_argument("--exclude", action="append", metavar="GLOB")
    parser.add_argument("--all", action="store_true", help="report every tracked source file over the limit")


if __name__ == "__main__":
    run_gate(GATE, __doc__, check, add_arguments)
