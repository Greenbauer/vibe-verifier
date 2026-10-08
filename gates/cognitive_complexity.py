#!/usr/bin/env python3
"""Fail when a file this pull request changed has more over-limit functions than at the base, or a worse one.

The metric is SonarSource's cognitive complexity. JS and TS files are measured by
eslint-plugin-sonarjs through the pinned toolchain in tools/cognitive-complexity (eslint, the sonarjs
plugin, the TypeScript parser and typescript, every version and tarball integrity from the committed
lockfile); Python files by complexipy through tools/cognitive-complexity-python (one wheel per
interpreter, every digest in the committed requirements file). Neither honors the repository's own
suppressions (eslint bulk suppressions, complexipy's ignore comments): the ratchet counts. A ratchet, not
a backlog: every source file changed since the base ref is measured at HEAD and at the base, and
a file is a finding when it has more functions over the limit than it had (or is new and has
any), or when one of them got worse: the file's over-limit costs, highest first, must each be no
higher than the base's at the same rank. So an over-limit function may be touched, moved, renamed
or improved, but it may not grow, and one function improving does not pay for another getting
worse. Functions nobody touched never block. --all measures every tracked source file against the
limit instead, for an audit.

    --max N          the limit (default 15)
    --source GLOB    what counts as source (repeatable; default: JS, TS and Python files)
    --exclude GLOB   extra paths to ignore (repeatable; tests, node_modules and *.d.ts always are)
"""
import json
import os
import re
import subprocess
import tempfile

from _contract import CannotRun, Finding, changed_files, git, matches, renamed, resolve_base, run_gate, tracked_files
from _tools import NODE_TOOLS, ensure_node_tool, ensure_python_tool

GATE = "cognitive-complexity"
RULE = "sonarjs/cognitive-complexity"
DEFAULT_SOURCE = ["**/*." + ext for ext in ("ts", "tsx", "js", "jsx", "mjs", "cjs", "mts", "cts", "py")]
ALWAYS_EXCLUDED = ["**/*.d.ts", "**/node_modules/**", "**/*.test.*", "**/*.spec.*", "**/__tests__/**",
                   "**/test_*.py", "**/*_test.py", "**/conftest.py"]
# eslint applies the eslint-suppressions.json in its working directory (bulk suppressions). The consumer's is
# for its own config: against this one rule its other entries look unused (eslint exits 2), and its count
# for this rule would hide what the ratchet counts. Read from the catalog, not the cache, whose key is the lockfile.
NO_SUPPRESSIONS = os.path.join(NODE_TOOLS, "cognitive-complexity", "no-suppressions.json")
# Both tools word a finding the same way; the cost is read out of it.
COST = re.compile(r"Cognitive Complexity from (\d+) to")


def cost(text):
    """A reported function's cost. A report this cannot read is cannot-run, never a function scored at zero."""
    found = COST.search(text)
    if not found:
        raise CannotRun("could not read a function's cost from the report: %s" % text.split("\n")[0][:120])
    return int(found.group(1))


def measure(root, files, limit):
    """{path: [(line, message)]} for every over-limit function in files under root, each language by its tool."""
    python = [p for p in files if p.endswith(".py")]
    found = measure_js(root, [p for p in files if not p.endswith(".py")], limit)
    if python:
        found.update(measure_python(ensure_python_tool("cognitive-complexity-python"), root, python, limit))
    return found


def measure_python(interpreter, root, files, limit):
    result = subprocess.run([interpreter, os.path.join(NODE_TOOLS, "cognitive-complexity-python", "measure.py"), root, str(limit), *files],
                            capture_output=True, text=True)
    if result.returncode != 0:
        raise CannotRun((result.stderr.strip() or "complexipy exited %d with no output" % result.returncode).splitlines()[-1])
    try:
        return {path: [tuple(hit) for hit in hits] for path, hits in json.loads(result.stdout).items()}
    except ValueError as error:
        raise CannotRun("complexipy's JSON report could not be read: %s" % error)


def measure_js(root, files, limit):
    """{path: [(line, message)]} for every over-limit function eslint reports under root."""
    if not files:
        return {}
    tool = ensure_node_tool("cognitive-complexity")
    result = subprocess.run([os.path.join(tool, "node_modules", ".bin", "eslint"), "--no-config-lookup", "--config",
                             os.path.join(tool, "eslint.config.mjs"), "--format", "json",
                             "--suppressions-location", NO_SUPPRESSIONS, *files],
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


def regressions(path, hits, base_hits):
    """The findings for one changed file: every over-limit function when it has more of them than at the base,
    otherwise each one whose cost is higher than the base's at the same rank (costs highest first)."""
    was = sorted((cost(text) for _, text in base_hits), reverse=True)
    if len(hits) > len(was):
        return [Finding("%s (over-limit functions in this file: %d at the base, %d now)" % (text, len(was), len(hits)), path, line)
                for line, text in hits]
    ranked = sorted(hits, key=lambda hit: cost(hit[1]), reverse=True)  # stable: equal costs keep file order
    now = [cost(text) for _, text in ranked]
    return [Finding("%s (it got worse: over-limit costs in this file, highest first, were %s at the base and are %s now)" % (text, was, now), path, line)
            for (line, text), current, allowed in zip(ranked, now, was) if current > allowed]


def check(args):
    source = args.source or DEFAULT_SOURCE
    excluded = ALWAYS_EXCLUDED + (args.exclude or [])
    tracked = set(tracked_files(args.repo))
    if args.all:
        files = sorted(p for p in tracked if matches(p, source) and not matches(p, excluded))
        return [Finding("%s (this file has it at the base too; --all reports everything)" % text, path, line)
                for path, hits in sorted(measure(args.repo, files, args.max).items()) for line, text in hits]
    base = resolve_base(args.repo, args.base_ref)
    files = sorted(p for p in changed_files(args.repo, base) if p in tracked and matches(p, source) and not matches(p, excluded))
    if not files:
        return []
    head = measure(args.repo, files, args.max)
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
        before = measure(snapshot, at_base, args.max)
    return [finding for path, hits in sorted(head.items()) for finding in regressions(path, hits, before.get(path, []))]


def add_arguments(parser):
    parser.add_argument("--max", type=int, default=15, help="cognitive complexity a function may reach (default 15)")
    parser.add_argument("--source", action="append", metavar="GLOB")
    parser.add_argument("--exclude", action="append", metavar="GLOB")
    parser.add_argument("--all", action="store_true", help="measure every tracked source file, not only those changed since the base")


if __name__ == "__main__":
    run_gate(GATE, __doc__, check, add_arguments)
