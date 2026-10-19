#!/usr/bin/env python3
"""Fail when a file this pull request changed is over the size limit and got bigger.

A ratchet on file size, measured in lines and in tokens (bytes / 4, roughly what a model reads to
take the file in). A changed file is a finding when it exceeds a limit and is new or grew on that
measure since the merge base (what the pull request started from, so a file the base branch shrank
since is not held against it). A long file that shrank, or that nobody touched, never blocks.
--all reports every tracked source file over a limit instead, for an audit. --staged judges the
files staged for the next commit against HEAD instead, for a pre-commit check.

    --max N          lines a file may have (default 500)
    --max-tokens N   tokens (bytes / 4) a file may have (default 5000; 0 turns this limit off)
    --source GLOB    what counts as source (repeatable; default: JS, TS and Python files)
    --exclude GLOB   extra paths to ignore (repeatable; node_modules and *.d.ts always are)
"""
import os
import subprocess

from _contract import Finding, changed_files, git, matches, renamed, resolve_base, run_gate, staged_changes, tracked_files

GATE = "max-file-lines"
DEFAULT_SOURCE = ["**/*." + ext for ext in ("ts", "tsx", "js", "jsx", "mjs", "cjs", "mts", "cts", "py")]
ALWAYS_EXCLUDED = ["**/*.d.ts", "**/node_modules/**"]
# Every finding says how to fix it, because the reader is usually an agent that will do exactly what it is told.
SPLIT = ("Split it: move one self-contained part (a component, a class, a group of related functions or test cases) "
         "into a new file beside this one, import it back, and keep every name other files import from here working. "
         "Joining lines or shortening names does not help: tokens count every byte. A generated file or lockfile is "
         "not written by hand, so exclude it instead: add --exclude '<glob>' to this gate's line in .vibe-verifier.")


def line_count(text):
    return text.count("\n") + (1 if text and not text.endswith("\n") else 0)


def size(data):
    """(lines, tokens) of a file's bytes."""
    return line_count(data.decode("utf-8", errors="replace")), len(data) // 4


def blob(repo, spec):
    """The bytes git has at `<commit>:<path>` (or `:<path>`, the index), or None when there is no such file."""
    out = subprocess.run(["git", "-C", repo, "show", spec], capture_output=True)
    return out.stdout if out.returncode == 0 else None


def check(args):
    source = args.source or DEFAULT_SOURCE
    excluded = ALWAYS_EXCLUDED + (args.exclude or [])
    tracked = set(tracked_files(args.repo))
    if args.all:
        candidates, moved, start = tracked, {}, None
    elif args.staged:
        candidates, moved = staged_changes(args.repo)
        start = "HEAD"
    else:
        base = resolve_base(args.repo, args.base_ref)
        candidates, moved = changed_files(args.repo, base), renamed(args.repo, base)
        # The changed files are the changes since the merge base, so that is where each one's old size is read.
        start = git(args.repo, "merge-base", base, "HEAD").strip()
    files = sorted(p for p in set(candidates) if p in tracked and matches(p, source) and not matches(p, excluded))
    return [finding for finding in (judge(args, path, start, moved) for path in files) if finding]


def now_bytes(args, path):
    """The file as it will be judged: staged in the index for --staged, else in the working tree."""
    if args.staged:
        return blob(args.repo, ":" + path) or b""
    with open(os.path.join(args.repo, path), "rb") as handle:
        return handle.read()


def judge(args, path, start, moved):
    """The finding for one candidate file, or None when it is within the limits or did not grow past one."""
    now = size(now_bytes(args, path))
    limits = (args.max, args.max_tokens or None)
    if not any(limit is not None and value > limit for value, limit in zip(now, limits)):
        return None
    before = None
    if start is not None:
        old = blob(args.repo, "%s:%s" % (start, moved.get(path, path)))
        before = None if old is None else size(old)
    parts = []
    for unit, value, limit, was in zip(("lines", "tokens (bytes / 4)"), now, limits, before or (None, None)):
        if limit is None or value <= limit or (was is not None and value <= was):
            continue
        origin = "" if args.all else " (%s)" % ("new file" if before is None else "%d at the base" % was)
        parts.append("%d %s, over the %d allowed%s" % (value, unit, limit, origin))
    return Finding("; ".join(parts) + ". " + SPLIT, path) if parts else None


def add_arguments(parser):
    parser.add_argument("--max", type=int, default=500, help="lines a file may have (default 500)")
    parser.add_argument("--max-tokens", type=int, default=5000,
                        help="tokens (bytes / 4) a file may have (default 5000; 0 turns this limit off)")
    parser.add_argument("--source", action="append", metavar="GLOB")
    parser.add_argument("--exclude", action="append", metavar="GLOB")
    scope = parser.add_mutually_exclusive_group()
    scope.add_argument("--all", action="store_true", help="report every tracked source file over a limit")
    scope.add_argument("--staged", action="store_true", help="judge the files staged for the next commit against HEAD")


if __name__ == "__main__":
    run_gate(GATE, __doc__, check, add_arguments)
