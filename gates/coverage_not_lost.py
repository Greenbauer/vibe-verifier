#!/usr/bin/env python3
"""Fail when a pull request leaves lines it did not change no longer run by the project's tests.

The project's whole unit suite runs with line coverage at the merge base and at HEAD, each in a checkout of
its own (a git worktree, with hooks off) that links the working copy's ignored files and mirrors its
node_modules, so both sides run with the dependencies the job installed. A finding is a source line the
pull request did not change, mapped from the merge base to HEAD through the diff so code that only moved
still matches, that the suite ran at the merge base and does not run at HEAD: the test that was its only
cover was deleted or weakened, or the code that reached it no longer does. A line counts only when two
runs at each side agree, because coverage can change from run to run: the second pair runs only when the
first finds a line. Lines in files the pull request deletes, and lines it changes or deletes, never count.
Each finding is reported at its line at HEAD, with what ran it at the merge base: the test functions
(pytest, unittest; none for a line only a child process ran), or whether the test files the pull request deleted or
changed did (Vitest, node:test). When lines were lost, the gate also lists the commits between the merge base and HEAD
(first parent, so a merged pull request is one, its number in its subject): run nightly with --base-ref set to the
last night's commit, that names the pull requests the loss came in with.

A pull request body (--body, a file the workflow writes) with a line `coverage-lost: <reason>` marks the
loss as intended: the findings are printed with the reason and do not fail the gate.

Runners (gates/_coverage_runs.py): the project's own Vitest 2 to 5, with @vitest/coverage-v8 at exactly its
version; or coverage.py 7.16.2 around the project's own pytest or unittest, measuring every Python process the
suite starts too; or Node's own coverage around the files the repository runs with `node --test`, Node 22 or later
(gates/_coverage_node.py), measuring the Node processes and worker threads a test starts too. Each run is guarded
(gates/_local_guard.py): a credential for anywhere else in what it inherits is exit 2, and a connection to anything
but this machine is refused. Install the project's dependencies first. A suite that fails at either side (or in a
run that refused a connection), no runner or provider, a Node before 22, no test run at either side, or a run past
--timeout cannot run (exit 2). Each run says how many lines ran, and how long it took.

    --runner NAME       vitest, node-test, pytest or unittest (default vitest)
    --project DIR       where the suite runs, relative to the repository (repeatable; default its root)
    --args TEXT         more arguments for the runner, split like a shell (after `vitest run`, `-m pytest` or
                        `-m unittest`, or before node's `--test`; such as --args 'discover -s tests')
    --node-test GLOB    node:test files the repository runs as `node --test <file>` some way no package.json script
                        says, run that way (repeatable)
    --python PATH       the project's interpreter (default python3; a relative path is the repository's)
    --body FILE         the pull request body, read for a `coverage-lost: <reason>` line
    --local-env NAME    a variable named like a credential that holds the job's own local stack's secret (repeatable)
    --timeout SECONDS   wall-clock limit for every run together (default 900)
    --max-files N       Vitest, node:test: run again at most this many deleted or changed test files to say which
                        ran a lost line (default 20); more are not run, and the findings stand without it
"""
import os
import posixpath
import re
import sys
import time

from _contract import CannotRun, Finding, git, renamed, resolve_base, run_gate
from _coverage_checkouts import checkouts
from _coverage_node import NodeTest
from _coverage_runs import RUNNERS, Coverage, Red
from _local_guard import add_local_env

GATE = "coverage-not-lost"
RUNNERS = dict(RUNNERS, **{"node-test": NodeTest})
HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@", re.MULTILINE)
REASON = re.compile(r"^[ \t]*(?:[-*+][ \t]+)?coverage-lost:[ \t]*(\S.*?)[ \t]*$", re.MULTILINE)


def note(text):
    print("%s: %s" % (GATE, text), file=sys.stderr)


def intended(body):
    """The reason a `coverage-lost:` line of the pull request body gives, or None."""
    if not body:
        return None
    try:
        with open(body, encoding="utf-8", errors="replace") as handle:
            found = REASON.search(handle.read())
    except OSError as error:
        raise CannotRun("--body cannot be read: %s" % error)
    return found.group(1) if found else None


def paths(repo, merge_base, kind):
    """The files of one kind of change since the merge base; with -M a moved file is neither deleted nor modified."""
    return [p for p in git(repo, "diff", "-z", "--name-only", "-M", "--diff-filter=" + kind, merge_base, "HEAD").split("\0") if p]


def count(group):
    return 1 if group is None else int(group)


class Diff:
    """Where each line of a file at the merge base is at HEAD."""

    def __init__(self, repo, merge_base):
        self.repo, self.merge_base, self.hunks = repo, merge_base, {}
        self.moved = {old: new for new, old in renamed(repo, merge_base).items()}
        self.deleted = set(paths(repo, merge_base, "D"))
        self.modified = set(paths(repo, merge_base, "MT"))

    def head_path(self, path):
        """The file's path at HEAD, or None when the pull request deleted it."""
        return None if path in self.deleted else self.moved.get(path, path)

    def line(self, path, number):
        """Line `number` of `path` at the merge base, as a line at HEAD; None when the pull request changed or deleted it."""
        if path not in self.modified and path not in self.moved:
            return number
        if path not in self.hunks:
            diff = git(self.repo, "diff", "-U0", "--no-color", "--no-ext-diff", "-M", self.merge_base, "HEAD", "--",
                       *sorted({path, self.moved.get(path, path)}))
            self.hunks[path] = [(int(m.group(1)), count(m.group(2)), count(m.group(4))) for m in HUNK.finditer(diff)]
        shift = 0
        for start, removed, added in self.hunks[path]:
            if removed and start <= number < start + removed:
                return None
            if (removed and start + removed <= number) or (not removed and start < number):
                shift += added - removed
        return number + shift

    def changed_tests(self, project):
        """{path at the merge base: what the pull request did to it} for the files it deleted, changed or moved under `project`."""
        prefix = "" if project == "." else project + "/"
        done = dict({p: "deleted" for p in self.deleted}, **{p: "changed" for p in self.modified})
        done.update({old: "moved to %s" % new for old, new in self.moved.items()})
        return {path: what for path, what in sorted(done.items()) if path.startswith(prefix)}


def lost_lines(base, head, diff):
    """{(path at HEAD, line at HEAD): (path, line) at the merge base} for each line that ran at the merge base, that
    the pull request did not change, and that does not run at HEAD. A file HEAD's run did not measure ran none."""
    found = {}
    for path, lines in base.lines.items():
        target = diff.head_path(path)
        if target is None:
            continue
        at_head = head.lines.get(target)
        for number, ran in lines.items():
            moved = diff.line(path, number) if ran else None
            if moved is not None and (at_head is None or at_head.get(moved) is False):
                found[(target, moved)] = (path, number)
    return found


def union(runs):
    """One Coverage of several projects' runs at one side: a line ran if it ran in any of them."""
    lines, contexts = {}, {}
    for run in runs:
        for path, ran in run.lines.items():
            merged = lines.setdefault(path, {})
            for number, hit in ran.items():
                merged[number] = merged.get(number, False) or hit
        for path, names in run.contexts.items():
            for number, tests in names.items():
                contexts.setdefault(path, {}).setdefault(number, set()).update(tests)
    return Coverage(lines, any(run.tested for run in runs), contexts)


def project_of(repo, project):
    project = posixpath.normpath(project.replace(os.sep, "/"))
    if project == ".." or project.startswith("../") or posixpath.isabs(project):
        raise CannotRun("--project %s is outside the repository" % project)
    if not os.path.isdir(os.path.join(repo, project)):
        raise CannotRun("--project %s is not a directory" % project)
    return project


class Suite:
    """The projects' suites at both sides, under one deadline."""

    def __init__(self, args, scratch, trees, merge_base):
        self.args, self.trees, self.projects = args, trees, args.projects
        self.runner = RUNNERS[args.runner](args, args.repo, scratch)
        self.deadline = time.monotonic() + args.timeout
        self.sides = {"base": "merge base %s" % merge_base[:12], "head": "head"}

    def run(self, side, project, files=()):
        if not os.path.isdir(os.path.join(self.trees[side], project)):
            return Coverage({}, False)  # the project is new at the head: nothing of it ran at the merge base
        try:
            return self.runner.run(self.trees[side], project, self.deadline, self.args.timeout, files)
        except Red as red:
            print(red.args[1], file=sys.stderr)
            hint =(". Both sides run with the dependencies installed for the head, so a merge base that imports one "
                    "this pull request removed fails here") if side == "base" else ""
            raise CannotRun("the suite of %s fails at the %s (%s; its failures are above), so there is no coverage to "
                            "compare%s" % (project, self.sides[side], red.args[0], hint))

    def measure(self):
        """(merge base, head): every project's coverage at each side, as one. Each side's run says what it measured
        and how long it took, which is what --timeout is set from."""
        sides = []
        for side in ("base", "head"):
            began = time.monotonic()
            sides.append(union([self.run(side, project) for project in self.projects]))
            note("the run at the %s: %s, in %d seconds" % (self.sides[side], ran_where(sides[-1]), time.monotonic() - began))
        return tuple(sides)


def ran_where(run):
    """'N line(s) ran (n in dir/, ...)': the lines a run ran, by the top directory of their file."""
    tops = {}
    for path, lines in run.lines.items():
        top = "in %s/" % path.split("/")[0] if "/" in path else "at the root"
        tops[top] = tops.get(top, 0) + sum(lines.values())
    where = ", ".join("%d %s" % (n, top) for top, n in sorted(tops.items(), key=lambda item: (-item[1], item[0])) if n)
    return "%d line(s) ran%s" % (sum(tops.values()), " (%s)" % where if where else "")


def commits_since(repo, merge_base, most=20):
    """'N commit(s) after the merge base <sha>: <sha> <subject>; ...', newest first, on the first-parent line."""
    commits = git(repo, "log", "--first-parent", "--format=%h %s", merge_base + "..HEAD").splitlines()
    more = " and %d more" % (len(commits) - most) if len(commits) > most else ""
    return "the lines were lost in the %d commit(s) after the merge base %s: %s%s" % (
        len(commits), merge_base[:12], "; ".join(commits[:most]), more)


def listed(names, most=5):
    return ", ".join(names[:most]) + (" and %d more" % (len(names) - most) if len(names) > most else "")


def tests_that_ran(confirmed, bases):
    """Python: each lost line's test functions at the merge base, from the runs' contexts."""
    contexts = union(bases).contexts
    said = {}
    for key, (path, number) in confirmed.items():
        names = sorted(contexts.get(path, {}).get(number, ()))
        said[key] = "Run at the merge base by %s." % listed(names) if names else ""
    return said


def changed_test_files(suite, diff):
    """{project: {test file at the merge base: what the pull request did to it}}."""
    base = suite.trees["base"]
    return {project: {p: what for p, what in diff.changed_tests(project).items() if suite.runner.test_files([p], base)}
            for project in suite.projects}


def test_files_that_ran(suite, diff, confirmed):
    """Vitest, node:test: whether the test files the pull request deleted or changed ran each lost line at the merge
    base, from one more run of just those files there."""
    changed = changed_test_files(suite, diff)
    every = dict(item for files in changed.values() for item in files.items())
    if not every:
        return dict.fromkeys(confirmed, "The pull request deleted or changed no test file: the code that ran it changed.")
    if len(every) > suite.args.max_files:
        note("%d test files deleted or changed, over --max-files %d: not run again to say which ran each lost line"
             % (len(every), suite.args.max_files))
        return {}
    # Real paths: Vitest matches a file filter against the real path, so one through a link (macOS's /var) matches nothing.
    ran = union([suite.run("base", project, [os.path.realpath(os.path.join(suite.trees["base"], p)) for p in files])
                 for project, files in changed.items() if files])
    files = listed(["%s (%s)" % item for item in sorted(every.items())], 10)
    return {key: ("Run at the merge base by the test files this pull request deleted or changed: %s." % files
                  if ran.lines.get(path, {}).get(number) else
                  "None of the test files this pull request deleted or changed ran it at the merge base (%s)." % files)
            for key, (path, number) in confirmed.items()}


def losses(suite, diff):
    """{(path, line) at HEAD: what ran it at the merge base} for the lines both pairs of runs agree no longer run."""
    first = suite.measure()
    if not first[0].tested and not first[1].tested:
        raise CannotRun("no test ran at the merge base or at the head: check --runner, --project and --args")
    ran = sum(sum(lines.values()) for lines in first[0].lines.values())
    candidates = lost_lines(first[0], first[1], diff)
    if not candidates:
        note("%d line(s) ran at the %s; every one the pull request left unchanged still runs at the head"
             % (ran, suite.sides["base"]))
        return {}
    second = suite.measure()
    again = lost_lines(second[0], second[1], diff)
    confirmed = {key: origin for key, origin in candidates.items() if again.get(key) == origin}
    note("%d line(s) ran at the %s; the first runs found %d that no longer run at the head, and the second runs agree "
         "on %d" % (ran, suite.sides["base"], len(candidates), len(confirmed)))
    dismissed = sorted(set(candidates) - set(confirmed))
    if dismissed:
        note("not reported, because the two runs disagree (coverage that changes from run to run): %s"
             % listed(["%s:%d" % key for key in dismissed], 20))
    if not confirmed:
        return {}
    if suite.args.runner in ("vitest", "node-test"):
        said = test_files_that_ran(suite, diff, confirmed)
    else:
        said = tests_that_ran(confirmed, [first[0], second[0]])
    return {key: said.get(key, "") for key in confirmed}


def spans(lost):
    """[path, first line, last line, what ran them] for each run of consecutive lost lines of a file with one cause."""
    groups = []
    for (path, number), said in sorted(lost.items()):
        last = groups[-1] if groups else None
        if last and last[0] == path and last[2] == number - 1 and last[3] == said:
            last[2] = number
        else:
            groups.append([path, number, number, said])
    return groups


def findings(repo, lost, reason):
    """One finding per run of consecutive lost lines of a file that the same tests ran at the merge base."""
    sources, result = {}, []
    for path, first, last, said in spans(lost):
        if path not in sources:
            sources[path] = git(repo, "show", "HEAD:" + path).splitlines()
        code = sources[path][first - 1].strip() if first <= len(sources[path]) else ""
        span = "line %d" % first if first == last else "lines %d-%d" % (first, last)
        text = ("%s (`%s`) ran under the project's tests at the merge base and does not at the head, and the pull request did not "
                "change it. %s" % (span[0].upper() + span[1:], code if len(code) <= 60 else code[:57] + "...", said)).strip()
        if reason:
            result.append(Finding("intended, the pull request body says `coverage-lost: %s`: %s" % (reason, text), path, first, advisory=True))
        else:
            result.append(Finding(text + " Test it again, or, if the loss is intended, say why in the pull request body: "
                                         "`coverage-lost: <reason>`.", path, first))
    return result


def check(args):
    if args.timeout < 1 or args.max_files < 0:
        raise CannotRun("--timeout must be at least 1 and --max-files at least 0")
    args.repo = os.path.abspath(args.repo)
    reason = intended(args.body)
    merge_base = git(args.repo, "merge-base", resolve_base(args.repo, args.base_ref), "HEAD").strip()
    args.projects = sorted({project_of(args.repo, project) for project in args.project or ["."]})
    if not git(args.repo, "diff", "--name-only", merge_base, "HEAD").strip():
        note("the head changes nothing since the merge base; nothing to compare")
        return []
    diff = Diff(args.repo, merge_base)
    with checkouts(args.repo, merge_base) as (scratch, trees):
        lost = losses(Suite(args, scratch, trees, merge_base), diff)
    if lost:
        note(commits_since(args.repo, merge_base))
    if reason and not lost:
        note("the pull request body says `coverage-lost: %s`, and no line was lost" % reason)
    return findings(args.repo, lost, reason)


def add_arguments(parser):
    parser.add_argument("--runner", choices=sorted(RUNNERS), default="vitest", help="the project's test runner (default vitest)")
    parser.add_argument("--project", action="append", metavar="DIR", help="where the suite runs (repeatable; default the repository root)")
    parser.add_argument("--args", default="", metavar="TEXT", help="more arguments for the runner, split like a shell")
    parser.add_argument("--python", default="python3", help="the project's interpreter (default python3)")
    parser.add_argument("--node-test", action="append", metavar="GLOB",
                        help="node:test files run as `node --test <file>` some way no package.json script says (repeatable)")
    parser.add_argument("--body", metavar="FILE", help="the pull request body, read for a `coverage-lost: <reason>` line")
    add_local_env(parser)
    parser.add_argument("--timeout", type=int, default=900, help="seconds every run together may take (default 900)")
    parser.add_argument("--max-files", type=int, default=20,
                        help="Vitest, node:test: the most deleted or changed test files run again to say which ran a lost "
                             "line (default 20)")


if __name__ == "__main__":
    run_gate(GATE, __doc__, check, add_arguments)
