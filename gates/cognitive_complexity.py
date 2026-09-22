#!/usr/bin/env python3
"""Fail when a file this pull request changed has more over-limit functions than it had at the base.

The metric is SonarSource's cognitive complexity, computed by eslint-plugin-sonarjs through the
pinned toolchain in tools/cognitive-complexity (eslint, the sonarjs plugin, the TypeScript parser
and typescript, every version and tarball integrity from the committed lockfile). A ratchet, not
a backlog: every source file changed since the base ref is measured at HEAD and at the base, and
a file is a finding only when it has more functions over the limit than it had, or is new and
has any. Functions nobody touched never block, and a refactor that removes one is never undone
by the count. --all measures every tracked source file against the limit instead, for an audit.

    --max N          the limit (default 15)
    --source GLOB    what counts as source (repeatable; default: JS and TS files)
    --exclude GLOB   extra paths to ignore (repeatable; tests, node_modules and *.d.ts always are)
"""
import json
import os
import subprocess
import tempfile

from _contract import CannotRun, Finding, changed_files, git, matches, renamed, resolve_base, run_gate, tracked_files
from _tools import ensure_node_tool

GATE = "cognitive-complexity"
RULE = "sonarjs/cognitive-complexity"
DEFAULT_SOURCE = ["**/*." + ext for ext in ("ts", "tsx", "js", "jsx", "mjs", "cjs", "mts", "cts")]
ALWAYS_EXCLUDED = ["**/*.d.ts", "**/node_modules/**", "**/*.test.*", "**/*.spec.*", "**/__tests__/**"]


def measure(tool, root, files, limit):
    """{path: [(line, message)]} for every over-limit function eslint reports under root."""
    if not files:
        return {}
    result = subprocess.run([os.path.join(tool, "node_modules", ".bin", "eslint"), "--no-config-lookup", "--config",
                             os.path.join(tool, "eslint.config.mjs"), "--format", "json", *files],
                            cwd=root, capture_output=True, text=True, env=dict(os.environ, VV_COMPLEXITY_MAX=str(limit)))
    if result.returncode not in (0, 1):
        raise CannotRun("eslint exited %d: %s" % (result.returncode, (result.stderr.strip() or "no output").splitlines()[-1]))
    try:
        report = json.loads(result.stdout or "[]")
    except ValueError as error:
        raise CannotRun("eslint's JSON report could not be read: %s" % error)
    found = {}
    real_root = os.path.realpath(root)  # eslint reports resolved paths; a temp dir may be reached through a symlink
    for entry in report:
        path = os.path.relpath(os.path.realpath(entry.get("filePath", "")), real_root)
        for message in entry.get("messages", []):
            if message.get("fatal"):
                raise CannotRun("could not parse %s: %s" % (path, message.get("message", "").split("\n")[0]))
            if message.get("ruleId") == RULE:
                found.setdefault(path, []).append((message.get("line"), message.get("message", "")))
    return found


def check(args):
    source = args.source or DEFAULT_SOURCE
    excluded = ALWAYS_EXCLUDED + (args.exclude or [])
    tracked = set(tracked_files(args.repo))
    if args.all:
        files = sorted(p for p in tracked if matches(p, source) and not matches(p, excluded))
        tool = ensure_node_tool("cognitive-complexity")
        return [Finding("%s (this file has it at the base too; --all reports everything)" % text, path, line)
                for path, hits in sorted(measure(tool, args.repo, files, args.max).items()) for line, text in hits]
    base = resolve_base(args.repo, args.base_ref)
    files = sorted(p for p in changed_files(args.repo, base) if p in tracked and matches(p, source) and not matches(p, excluded))
    if not files:
        return []
    tool = ensure_node_tool("cognitive-complexity")
    head = measure(tool, args.repo, files, args.max)
    moved = renamed(args.repo, base)
    with tempfile.TemporaryDirectory(prefix="vv-base-") as snapshot:
        at_base = []
        for path in files:
            origin = moved.get(path, path)  # a renamed file is compared with itself at its old path
            probe = subprocess.run(["git", "-C", args.repo, "cat-file", "-e", "%s:%s" % (base, origin)], capture_output=True)
            if probe.returncode != 0:
                continue  # new in this pull request: any over-limit function is a finding
            target = os.path.join(snapshot, path)
            os.makedirs(os.path.dirname(target), exist_ok=True)
            with open(target, "w", encoding="utf-8") as handle:
                handle.write(git(args.repo, "show", "%s:%s" % (base, origin)))
            at_base.append(path)
        before = measure(tool, snapshot, at_base, args.max)
    findings = []
    for path, hits in sorted(head.items()):
        had = len(before.get(path, []))
        if len(hits) <= had:
            continue
        for line, text in hits:
            findings.append(Finding("%s (over-limit functions in this file: %d at the base, %d now)" % (text, had, len(hits)), path, line))
    return findings


def add_arguments(parser):
    parser.add_argument("--max", type=int, default=15, help="cognitive complexity a function may reach (default 15)")
    parser.add_argument("--source", action="append", metavar="GLOB")
    parser.add_argument("--exclude", action="append", metavar="GLOB")
    parser.add_argument("--all", action="store_true", help="measure every tracked source file, not only those changed since the base")


if __name__ == "__main__":
    run_gate(GATE, __doc__, check, add_arguments)
