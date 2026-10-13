#!/usr/bin/env python3
"""Fail when a Python file this pull request changed has a Bandit security finding it did not have at the base.

Bandit, Python's static security analyser, pinned in tools/bandit (every wheel digest in the committed
requirements file), reads each changed non-test Python file at HEAD and at the merge base. A ratchet
per check: a file is a finding when it has more results of one Bandit check (B602, subprocess with
shell=True, say) than it had at the base, or is new and has any; every result of that check in the file
is listed, because Bandit does not say which one is new. What nobody touched never blocks, and moving
or fixing code never does. --all reports every result in every tracked Python file, for an audit.

Only results at or above --severity and --confidence count (Bandit's own scale: low, medium, high;
default medium for both, which leaves out low-severity results such as importing subprocess). Tests
are not read: Bandit flags every `assert`. The repository's Bandit config files are not read; a line
the author judged safe is marked in the source with `# nosec <check id>`, which Bandit honours and
the review reads in the diff. A file Bandit cannot parse is exit 2, never a pass.

    --severity LEVEL     lowest severity that counts: low, medium or high (default medium)
    --confidence LEVEL   lowest confidence that counts: low, medium or high (default medium)
    --source GLOB        what counts as source (repeatable; default: Python files)
    --exclude GLOB       extra paths to ignore (repeatable; tests always are)
"""
import json
import os
import subprocess
import tempfile
from collections import Counter

from _contract import PY_TESTS, CannotRun, Finding, changed_files, git, matches, renamed, resolve_base, run_gate, tracked_files
from _tools import ensure_python_tool

GATE = "bandit"
LEVELS = ("low", "medium", "high")
DEFAULT_SOURCE = ["**/*.py"]
ALWAYS_EXCLUDED = ["**/conftest.py"] + PY_TESTS


def scan(interpreter, root, files, severity, confidence):
    """{path: [(line, check id, message)]} for every result Bandit reports at or above the levels."""
    if not files:
        return {}
    result = subprocess.run([interpreter, "-m", "bandit", "--format", "json", "--quiet", "--severity-level", severity,
                             "--confidence-level", confidence, "--", *files], cwd=root, capture_output=True, text=True)
    try:
        report = json.loads(result.stdout or "")
    except ValueError:
        raise CannotRun("bandit exited %d without a report: %s" % (result.returncode, (result.stderr.strip() or "no output").splitlines()[-1]))
    if report.get("errors"):
        first = report["errors"][0]
        raise CannotRun("bandit could not read %s: %s" % (os.path.normpath(first.get("filename", "?")), first.get("reason", "")))
    found = {}
    for item in report.get("results", []):
        message = "%s %s (severity %s, confidence %s)" % (item["test_id"], item["issue_text"], item["issue_severity"].lower(),
                                                          item["issue_confidence"].lower())
        found.setdefault(os.path.normpath(item["filename"]), []).append((item["line_number"], item["test_id"], message))
    return found


def regressions(path, hits, base_hits):
    """The findings for one changed file: every result of each check it has more of than at the base."""
    was, now = Counter(check for _, check, _ in base_hits), Counter(check for _, check, _ in hits)
    return [Finding("%s (%s results in this file: %d at the base, %d now)" % (message, check, was[check], now[check]), path, line)
            for line, check, message in sorted(hits) if now[check] > was[check]]


def sources(args, candidates):
    """The tracked, non-test Python source files among candidates."""
    tracked = set(tracked_files(args.repo))
    excluded = ALWAYS_EXCLUDED + (args.exclude or [])
    return sorted(p for p in candidates if p in tracked and matches(p, args.source or DEFAULT_SOURCE) and not matches(p, excluded))


def check(args):
    if args.all:
        files = sources(args, tracked_files(args.repo))
        found = scan(ensure_python_tool(GATE), args.repo, files, args.severity, args.confidence)
        return [Finding(message, path, line) for path, hits in sorted(found.items()) for line, _, message in sorted(hits)]
    base = resolve_base(args.repo, args.base_ref)
    files = sources(args, changed_files(args.repo, base))
    if not files:
        return []
    interpreter = ensure_python_tool(GATE)
    head = scan(interpreter, args.repo, files, args.severity, args.confidence)
    before = scan_at_base(interpreter, args, base, files)
    return [finding for path, hits in sorted(head.items()) for finding in regressions(path, hits, before.get(path, []))]


def scan_at_base(interpreter, args, base, files):
    """scan() of the files as they were at the merge base (what the pull request started from, as in the other
    ratchets), each renamed file at its old path; a file new in this pull request is absent, so any result counts."""
    moved = renamed(args.repo, base)
    start = git(args.repo, "merge-base", base, "HEAD").strip()
    with tempfile.TemporaryDirectory(prefix="vv-base-") as snapshot:
        at_base = []
        for path in files:
            origin = moved.get(path, path)
            if subprocess.run(["git", "-C", args.repo, "cat-file", "-e", "%s:%s" % (start, origin)], capture_output=True).returncode:
                continue
            target = os.path.join(snapshot, path)
            os.makedirs(os.path.dirname(target), exist_ok=True)
            with open(target, "w", encoding="utf-8") as handle:
                handle.write(git(args.repo, "show", "%s:%s" % (start, origin)))
            at_base.append(path)
        return scan(interpreter, snapshot, at_base, args.severity, args.confidence)


def add_arguments(parser):
    parser.add_argument("--severity", choices=LEVELS, default="medium", help="lowest severity that counts (default medium)")
    parser.add_argument("--confidence", choices=LEVELS, default="medium", help="lowest confidence that counts (default medium)")
    parser.add_argument("--source", action="append", metavar="GLOB")
    parser.add_argument("--exclude", action="append", metavar="GLOB")
    parser.add_argument("--all", action="store_true", help="report every result in every tracked Python file")


if __name__ == "__main__":
    run_gate(GATE, __doc__, check, add_arguments)
