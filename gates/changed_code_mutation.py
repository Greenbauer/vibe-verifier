#!/usr/bin/env python3
"""Fail when the project's tests do not notice the lines a pull request changed being broken.

StrykerJS, pinned in tools/changed-code-mutation with its Vitest runner, makes small changes
(mutants) to the lines the pull request added or modified in non-test JS and TS source files,
and runs the project's own Vitest suite against each one. A mutant every test still passes on
survived: that line can be wrong and nothing notices, which is what a test that copies the code
it covers, asserts nothing, or reads the source as text looks like. Only the changed line ranges
are mutated (Stryker's `file:start-end` mutate ranges), never whole files.

The score is killed / (killed + survived + not covered) over those mutants. Below --break the
gate fails and lists every surviving mutant at its line with the code it replaced. Mutants that
timed out, did not compile, crashed the runner or carry a `// Stryker disable` comment are left
out of the score and reported. No mutant on the changed lines is a pass.

The project's own Vitest (2.x to 4.x) and dependencies must be installed (`npm ci`) before the
gate runs. Without them, with failing tests, past --max-files or past --timeout, it cannot run
(exit 2): never a pass, and never a verdict on part of the change.

    --project DIR       directory with the Vitest config and package.json (default: the repository root)
    --break N           lowest passing score, 0-100 (default 60)
    --max-files N       more changed source files than this cannot run (default 20)
    --timeout SECONDS   wall-clock limit for the whole Stryker run (default 900)
    --source GLOB       what counts as source (repeatable; default: JS and TS files)
    --exclude GLOB      extra paths to ignore (repeatable; tests, *.d.ts, config, stories and node_modules always are)
"""
import json
import os
import posixpath
import re
import shutil
import signal
import subprocess
import sys
import tempfile

from _contract import CannotRun, Finding, changed_files, git, matches, renamed, resolve_base, run_gate, tracked_files
from _tools import ensure_node_tool

GATE = "changed-code-mutation"
DEFAULT_SOURCE = ["**/*." + ext for ext in ("ts", "tsx", "js", "jsx", "mjs", "cjs", "mts", "cts")]
ALWAYS_EXCLUDED = ["**/*.test.*", "**/*.spec.*", "**/__tests__/**", "**/*.d.ts", "**/*.config.*", "**/*.stories.*",
                   "**/node_modules/**"]
HUNK = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")
# Under Vitest 5 the pinned Stryker 10.0.0 kills no mutant at all (measured 2026-10-05: 8 of 8 survived
# a test that kills all 8 under Vitest 2.1.9, 3.2.7 and 4.1.10), so outside this range there is no verdict.
VITEST_MAJORS = (2, 3, 4)
UNDETECTED = {"Survived": "survived: every test still passed", "NoCoverage": "not covered: no test runs this code"}
EXCLUDED = {"Timeout": "timed out", "CompileError": "did not compile", "RuntimeError": "crashed the test runner",
            "Ignored": "disabled by a comment"}
# Stryker's own count of the files its mutate patterns matched. A file whose changed lines hold no mutant
# is absent from the report, so only this line shows that every requested file was found.
FOUND = re.compile(r"Found (\d+) of \d+ file\(s\) to be mutated")


def note(text):
    print("%s: %s" % (GATE, text), file=sys.stderr)


def changed_ranges(repo, base, path, origin):
    """[(first, last)] lines of `path` at HEAD that the diff since the base added or modified."""
    diff = git(repo, "diff", "-U0", "--no-color", "--no-ext-diff", "-M", base + "...HEAD", "--", *sorted({origin, path}))
    ranges = []
    for line in diff.splitlines():
        hunk = HUNK.match(line)
        if hunk:
            start, count = int(hunk.group(1)), int(hunk.group(2) or 1)
            if count:  # 0 is a pure deletion: nothing at HEAD to mutate
                ranges.append((start, start + count - 1))
    return ranges


def project_vitest(directory):
    """The version of the vitest Node would resolve from `directory`, or None."""
    directory = os.path.abspath(directory)
    while True:
        manifest = os.path.join(directory, "node_modules", "vitest", "package.json")
        if os.path.isfile(manifest):
            with open(manifest, encoding="utf-8") as handle:
                return str(json.load(handle).get("version", ""))
        parent = os.path.dirname(directory)
        if parent == directory:
            return None
        directory = parent


def stryker(tool, project_dir, ranges, timeout):
    """Run Stryker in project_dir on {path relative to it: [(first, last)]}; its JSON report."""
    with tempfile.TemporaryDirectory(prefix="vv-mutation-") as scratch:
        report_path = os.path.join(scratch, "mutation.json")
        config_path = os.path.join(scratch, "stryker.config.json")
        # Stryker copies the project into a sandbox under this directory (never into itself), inside the
        # project so dependencies hoisted above it still resolve. Removed below, even after a kill.
        sandbox = tempfile.mkdtemp(prefix=".vibe-verifier-mutation-", dir=project_dir)
        config = {
            "testRunner": "vitest",
            # Off, so the dry run runs every test: with it on, a test that never imports the changed file is
            # not run at all, and when no test does Stryker writes no report.
            "vitest": {"related": False},
            "coverageAnalysis": "perTest",
            "mutate": ["%s:%d-%d" % (path, first, last) for path, spans in sorted(ranges.items()) for first, last in spans],
            "reporters": ["json"],
            "jsonReporter": {"fileName": report_path},
            "tempDirName": os.path.basename(sandbox),
            "cleanTempDir": "always",
            "dryRunTimeoutMinutes": timeout / 60,
            "fileLogLevel": "off",
            "allowConsoleColors": False,
        }
        with open(config_path, "w", encoding="utf-8") as handle:
            json.dump(config, handle)
        command = ["node", os.path.join(tool, "node_modules", "@stryker-mutator", "core", "bin", "stryker.js"), "run", config_path]
        try:
            process = subprocess.Popen(command, cwd=project_dir, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                       text=True, encoding="utf-8", errors="replace", start_new_session=True)
            output, _ = process.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(process.pid, signal.SIGKILL)  # Stryker's test runner workers are in its process group
            except ProcessLookupError:  # the group ended between the timeout and the kill
                pass
            process.communicate()
            raise CannotRun("StrykerJS did not finish within --timeout %ds; no verdict on any file. Raise --timeout or "
                            "--max-files on the base manifest, or split the pull request" % timeout)
        finally:
            shutil.rmtree(sandbox, ignore_errors=True)
        if process.returncode != 0:
            print(output, file=sys.stderr)
            raise CannotRun("StrykerJS exited %d (its output is above)" % process.returncode)
        found = FOUND.search(output)
        if not found or int(found.group(1)) != len(ranges):
            print(output, file=sys.stderr)
            raise CannotRun("StrykerJS matched %s of the %d changed file(s) it was asked to mutate (its output is above); "
                            "a file it cannot match by path cannot be judged by line range" % (found.group(1) if found else "none", len(ranges)))
        try:
            with open(report_path, encoding="utf-8") as handle:
                return json.load(handle)
        except (OSError, ValueError) as error:
            raise CannotRun("StrykerJS's JSON report could not be read: %s" % error)


def clip(text):
    text = " ".join(str(text).split())
    return text if len(text) <= 60 else text[:57] + "..."


def describe(mutant, lines):
    """`original` -> `replacement` (Mutator), the original only when the mutant sits on one line."""
    start, end = mutant["location"]["start"], mutant["location"]["end"]
    replaced = "`%s`" % clip(mutant.get("replacement", ""))
    if start["line"] == end["line"] and 0 < start["line"] <= len(lines):
        replaced = "`%s` -> %s" % (clip(lines[start["line"] - 1][start["column"] - 1:end["column"] - 1]), replaced)
    return "%s (%s)" % (replaced, mutant.get("mutatorName", "?"))


def verdict(report, prefix, threshold):
    undetected, excluded, killed = [], [], 0
    for name, entry in sorted(report.get("files", {}).items()):
        lines = entry.get("source", "").splitlines()
        for mutant in sorted(entry.get("mutants", []), key=lambda m: (m["location"]["start"]["line"], m["location"]["start"]["column"])):
            status, where = mutant.get("status"), (prefix + name, mutant["location"]["start"]["line"])
            if status == "Killed":
                killed += 1
            elif status in UNDETECTED:
                undetected.append((where, "mutant %s: %s" % (UNDETECTED[status], describe(mutant, lines))))
            elif status in EXCLUDED:
                excluded.append((where, "mutant %s, left out of the score: %s" % (EXCLUDED[status], describe(mutant, lines))))
            else:
                raise CannotRun("StrykerJS left mutant %s in %s with status %r: the run did not finish" % (mutant.get("id"), name, status))
    for (path, line), text in excluded:
        note("%s:%d: %s" % (path, line, text))
    scored = killed + len(undetected)
    if not scored:
        note("no mutant on the changed lines to score (%d left out)" % len(excluded))
        return []
    score = 100.0 * killed / scored
    note("%d of %d mutant(s) on the changed lines killed: score %.0f%%, --break %g" % (killed, scored, score, threshold))
    if score >= threshold:
        for (path, line), text in undetected:
            note("%s:%d: %s" % (path, line, text))
        return []
    summary = ("mutation score on the changed lines is %.0f%% (%d of %d mutants killed), below --break %g. Each mutant below "
               "is a change to the code that no test failed on: test that behavior through the real code, or mark a mutant "
               "no test can tell apart with `// Stryker disable next-line <Mutator>: <reason>`" % (score, killed, scored, threshold))
    return [Finding(summary)] + [Finding(text, path, line) for (path, line), text in undetected]


def check(args):
    if not 0 <= args.break_score <= 100:
        raise CannotRun("--break must be between 0 and 100")
    project = posixpath.normpath(args.project.replace(os.sep, "/"))
    if project.startswith("../") or project == ".." or posixpath.isabs(project):
        raise CannotRun("--project %s is outside the repository" % args.project)
    project_dir = os.path.join(args.repo, project)
    if not os.path.isdir(project_dir):
        raise CannotRun("--project %s is not a directory" % args.project)
    prefix = "" if project == "." else project + "/"
    base = resolve_base(args.repo, args.base_ref)
    tracked = set(tracked_files(args.repo))
    excluded = ALWAYS_EXCLUDED + (args.exclude or [])
    files = [p for p in changed_files(args.repo, base)
             if p in tracked and matches(p, args.source or DEFAULT_SOURCE) and not matches(p, excluded)]
    outside = [p for p in files if not p.startswith(prefix)]
    if outside:
        note("%d changed source file(s) outside --project %s are not judged: %s" % (len(outside), project, ", ".join(outside)))
    moved = renamed(args.repo, base)
    ranges = {}
    for path in files:
        if path.startswith(prefix):
            spans = changed_ranges(args.repo, base, path, moved.get(path, path))
            if spans:
                ranges[path[len(prefix):]] = spans
    if not ranges:
        return []
    if len(ranges) > args.max_files:
        raise CannotRun("%d changed source files, over --max-files %d; no verdict on any of them. Raise --max-files on the "
                        "base manifest, or split the pull request" % (len(ranges), args.max_files))
    version = project_vitest(project_dir)
    if version is None:
        raise CannotRun("no vitest is installed for %s: install the project's dependencies (npm ci) before this gate, "
                        "which runs the project's own Vitest" % project)
    major = re.match(r"(\d+)\.", version)
    if not major or int(major.group(1)) not in VITEST_MAJORS:
        raise CannotRun("vitest %s is not 2.x to 4.x, the versions the pinned StrykerJS kills mutants under" % version)
    tool = ensure_node_tool(GATE)
    return verdict(stryker(tool, project_dir, ranges, args.timeout), prefix, args.break_score)


def add_arguments(parser):
    parser.add_argument("--project", default=".", help="directory with the Vitest config, relative to the repository (default: its root)")
    parser.add_argument("--break", dest="break_score", type=float, default=60, metavar="N",
                        help="lowest passing mutation score over the changed lines, 0-100 (default 60)")
    parser.add_argument("--max-files", type=int, default=20, help="more changed source files than this cannot run (default 20)")
    parser.add_argument("--timeout", type=int, default=900, help="seconds the whole Stryker run may take (default 900)")
    parser.add_argument("--source", action="append", metavar="GLOB")
    parser.add_argument("--exclude", action="append", metavar="GLOB")


if __name__ == "__main__":
    run_gate(GATE, __doc__, check, add_arguments)
