#!/usr/bin/env python3
"""Decide whether a pull request's diff can change what a slow suite verifies, post the suite's
statuses as not applicable when it cannot, and check the list the decision reads.

A browser suite that tests a deployed preview takes tens of minutes and queues for a runner, and a
pull request that changes one file under docs/ waits for it and runs it like any other. A workflow
can skip it for a diff the suite cannot see, but only when every doubt runs the suite. Two
repositories each wrote that rule on their own, and the one that listed the paths that run its
suite published "not applicable" for a pull request that added only a new spec, and for one of
298 files when the shell pipeline that tested its file list died early (docs/suite-scope.md).

    decide --repo <owner/name> --list <path> [--pr <n> --sha <commit>] [--also-run <reason>]

The suite is not applicable only when --pr names a pull request whose head is --sha, it targets
the repository's default branch, and every path it changes (both names of a rename) is inert by
the list as the default branch has it. Everything else runs the suite, with the reason: no pull
request, a head that moved, another base branch, a default branch with no list or a malformed one,
a path the list does not hold, a file list GitHub cut short or did not return, a read that failed,
and any --also-run reason (a consumer's own rule to run it anyway). The list is read from GitHub on
the default branch, never from a checkout, so a pull request cannot widen it to skip its own suite.

The list is one pattern per line, `#` for comments. `**/` crosses directories, `*` stays inside one
path segment, `?` is one character, and a pattern covers the whole path from the repository root.
A line that starts with `!` names paths that are never inert, whatever the other lines say. A path
with a segment named `e2e`, at any depth, is never inert whatever the list says: Playwright matches
`testMatch` at any depth, so a file under such a directory can be a spec, and no list can forget
that. A changed path named `e2e` itself counts too: GitHub's file list does not say whether it is a
submodule or a link to a directory.

It writes two step outputs, `not-applicable` (true or false) and `reason`, and one annotation, a
warning when the suite runs because something could not be read. Exit 0 with either verdict.

    post --repo <owner/name> --sha <commit> --reason <text> [--url <link>] <context> ...

writes each context on the commit as a commit status: state success, the reason as its description.
Exit 1 when GitHub refuses one three times.

    check --list <file> --reads <file>

reads the list from the working tree, as a pull request would merge it, and the paths a suite (or
the pipeline that builds what it tests) reads, one per line. Exit 1 naming each of those the list
calls inert, exit 2 when the list is malformed or there is no path to check.

Reads and writes go through `gh` with the token in GH_TOKEN: decide needs `pull-requests: read`
and `contents: read`, post needs `statuses: write`, check needs neither.
"""
import argparse
import json
import os
import shlex
import subprocess
import sys
import time
from typing import NamedTuple
from urllib.parse import quote

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "gates"))
from _contract import glob_to_regex  # noqa: E402

TITLE = "Suite scope"
# GitHub lists at most this many of a pull request's files, a hundred to a page.
LISTED, PAGE = 3000, 100
# The longest description a commit status takes.
DESCRIPTION = 140
# The directory name no list can make inert: Playwright finds a spec under it at any depth.
E2E = "e2e"


class Malformed(Exception):
    """The inert list is not one pattern per line."""


class Unread(Exception):
    """GitHub did not answer a read, or did not answer what was asked."""


class Verdict(NamedTuple):
    not_applicable: bool
    reason: str
    unread: bool = False  # the suite runs because something could not be read: a warning, not a notice


def run(reason, unread=False):
    return Verdict(False, reason, unread)


def annotation(level, message):
    """A workflow command for one line of text. A changed path is the pull request's to name, and it is
    only ever printed inside one of these, never on a line of its own for the runner to read."""
    return "::%s title=%s::%s" % (level, TITLE, " ".join(message.split()).replace("%", "%25"))


def hash_inside_a_pattern(raw):
    """True when an unquoted `#` follows a pattern's own character. shlex reads it as a comment that ends the
    pattern there, so `!docs/#internal/**` would quietly mean `!docs/` and widen what is inert."""
    quote, previous, escaped = None, " ", False
    for char in raw:
        if escaped:
            escaped = False
        elif quote:
            quote = None if char == quote else quote
        elif char == "\\":
            escaped = True
        elif char in "'\"":
            quote = char
        elif char == "#":
            return not previous.isspace()
        previous = char
    return False


def parse_list(text):
    """(inert, never): the compiled patterns of an inert list, the `!` lines apart."""
    inert, never = [], []
    for number, raw in enumerate(text.splitlines(), 1):
        if hash_inside_a_pattern(raw):
            raise Malformed("line %d has a `#` inside a pattern, which would start a comment there (quote the pattern, "
                            "or put a space before the comment): %s" % (number, raw.strip()))
        try:
            words = shlex.split(raw, comments=True)
        except ValueError as error:
            raise Malformed("line %d: %s" % (number, error))
        if not words:
            continue
        if len(words) > 1 or words[0] in ("", "!"):
            raise Malformed("line %d is not one pattern (quote a path that holds a space): %s" % (number, raw.strip()))
        if words[0].startswith("!"):
            never.append(glob_to_regex(words[0][1:]))
        else:
            inert.append(glob_to_regex(words[0]))
    return inert, never


def under_e2e(path):
    """True for a path with a segment named e2e, at any depth: a file under such a directory, or the
    directory itself as a submodule or a link, which GitHub lists as one path. A name that only holds
    e2e (preview-e2e.md) is not one."""
    return E2E in path.split("/")


def is_inert(path, patterns):
    inert, never = patterns
    if under_e2e(path):
        return False
    return any(regex.match(path) for regex in inert) and not any(regex.match(path) for regex in never)


def gh(endpoint, *args, payload=None):
    """The output of one `gh api` call; Unread, naming the endpoint, when it fails."""
    try:
        result = subprocess.run(["gh", "api", endpoint, *args], capture_output=True, text=True, input=payload)
    except OSError as error:
        raise Unread("gh could not be run: %s" % error)
    if result.returncode != 0:
        raise Unread("%s: %s" % (endpoint.split("?")[0], (result.stderr.strip() or "gh api failed").splitlines()[-1]))
    return result.stdout


def base_list(repo, path, branch):
    """The inert list as `branch` has it, parsed; None when the branch has no such file."""
    try:
        text = gh("repos/%s/contents/%s?ref=%s" % (repo, quote(path), quote(branch, safe="")),
                  "-H", "Accept: application/vnd.github.raw")
    except Unread as error:
        if "HTTP 404" in str(error):
            return None
        raise
    return parse_list(text)


def changed_paths(repo, number, count):
    """Every path the pull request changes, both names of a rename: moving a file out of the app takes
    it off the site. Unread unless GitHub listed exactly the `count` files the pull request reports."""
    files = []
    for page in range(1, -(-count // PAGE) + 1):
        files += json.loads(gh("repos/%s/pulls/%s/files?per_page=%d&page=%d" % (repo, number, PAGE, page)))
    paths, names = [], set()
    for file in files:
        # A file the count includes and the list cannot name is a path nobody judged.
        if not isinstance(file.get("filename"), str) or not file["filename"]:
            raise Unread("GitHub listed a changed file with no name")
        names.add(file["filename"])
        paths += [file["filename"]] + ([file["previous_filename"]] if file.get("previous_filename") else [])
    # Distinct names: a file listed twice (pages read while the list changed) would hide one never listed.
    if len(names) != count:
        raise Unread("GitHub listed %d of the pull request's %d changed files" % (len(names), count))
    return paths


def judge(args, pull):
    """The verdict for a pull request GitHub answered with. Each check that fails runs the suite."""
    head, base = pull["head"]["sha"], pull["base"]
    if head != args.sha:
        return run("this run is for %s and the pull request's head is %s" % (args.sha[:8] or "no commit", head[:8]))
    default = base["repo"]["default_branch"]
    if base["ref"] != default:
        # Nothing has verified the tree under a stacked pull request, and retargeting it to the
        # default branch later changes its diff with no new head.
        return run("the pull request targets %s, not the default branch %s" % (base["ref"], default))
    count = pull["changed_files"]
    if not isinstance(count, int) or count < 1:
        return run("the pull request reports no changed file")
    if count > LISTED:
        return run("the pull request changes %d files and GitHub lists only %d of them" % (count, LISTED))
    where = "%s on %s" % (args.list, default)
    patterns = base_list(args.repo, args.list, default)
    if not patterns or not patterns[0]:
        return run("%s names no inert path, so nothing is inert until a list merges" % where)
    paths = changed_paths(args.repo, args.pr, count)
    active = [path for path in paths if not is_inert(path, patterns)]
    if active:
        first, more = active[0], len(active) - 1
        why = "has a path segment named e2e, which is never inert" if under_e2e(first) else "is not on the inert list (%s)" % where
        also = ", and %d more changed path%s not inert" % (more, " is" if more == 1 else "s are") if more else ""
        return run("%s %s%s" % (first, why, also))
    return Verdict(True, "Not applicable: every changed path (%d) is on the inert list (%s)" % (len(paths), where))


def decide(args):
    """This run's verdict. It never fails on what it cannot read: it says run, and why."""
    if args.also_run.strip():
        return run(args.also_run)
    if not args.pr:
        return run("no pull request is named, and only a pull request's diff can be inert")
    try:
        return judge(args, json.loads(gh("repos/%s/pulls/%s" % (args.repo, args.pr))))
    except Malformed as error:
        return run("the default branch's inert list %s is malformed (%s)" % (args.list, error), unread=True)
    except Unread as error:
        return run(str(error), unread=True)
    except (ValueError, LookupError, TypeError, AttributeError) as error:
        return run("GitHub's answer could not be read (%s: %s)" % (type(error).__name__, error), unread=True)


def report(verdict):
    """One annotation and the two step outputs. The reason can hold a changed path, so it is flattened
    to one line first: a line break in it would otherwise write an output of its own."""
    reason = " ".join(verdict.reason.split())
    print(annotation("warning" if verdict.unread else "notice", reason if verdict.not_applicable else "The suite runs: " + reason))
    out = os.environ.get("GITHUB_OUTPUT")
    if out:
        with open(out, "a", encoding="utf-8") as handle:
            handle.write("not-applicable=%s\nreason=%s\n" % (str(verdict.not_applicable).lower(), reason))
    return 0


def send(args, context, status):
    """True once GitHub took the status, after at most three attempts."""
    payload = json.dumps(dict(status, context=context))
    for attempt in range(1, 4):
        try:
            gh("repos/%s/statuses/%s" % (args.repo, args.sha), "-X", "POST", "--input", "-", payload=payload)
            return True
        except Unread as error:
            sys.stderr.write("posting %s failed (attempt %d of 3): %s\n" % (context, attempt, error))
            if attempt < 3:
                time.sleep(args.interval)
    return False


def post(args):
    """Each context as a success status on the commit, with the reason. A status owed and not written
    leaves the head waiting on a suite that never runs, so a refusal fails the step."""
    status = {"state": "success", "description": " ".join(args.reason.split())[:DESCRIPTION]}
    if args.url:
        status["target_url"] = args.url
    for context in args.contexts:
        if not send(args, context, status):
            print(annotation("error", "could not post the `%s` status on %s; the job needs `statuses: write`" % (context, args.sha)))
            return 1
        print("posted %s on %s: %s" % (context, args.sha[:8], status["description"]))
    return 0


def check(args):
    """Exit 1 when the working tree's list calls a path the suite reads inert."""
    try:
        with open(args.list, encoding="utf-8") as handle:
            patterns = parse_list(handle.read())
        with open(args.reads, encoding="utf-8") as handle:
            reads = [line.strip() for line in handle if line.strip()]
    except (OSError, Malformed) as error:
        print(annotation("error", "the inert list %s cannot be checked: %s" % (args.list, error)))
        return 2
    if not reads:
        print(annotation("error", "%s names no path, so nothing was checked" % args.reads))
        return 2
    inert = [path for path in reads if is_inert(path, patterns)]
    for path in inert:
        print(annotation("error", "%s is on the inert list %s and the suite reads it: a pull request that changes only "
                                  "that path would skip the suite. Take it off the list, or add `!%s`." % (path, args.list, path)))
    print("%d path%s the suite reads, %d on the inert list %s" % (len(reads), "" if len(reads) == 1 else "s", len(inert), args.list))
    return 1 if inert else 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    commands = parser.add_subparsers(dest="command", required=True)
    deciding = commands.add_parser("decide")
    deciding.add_argument("--repo", required=True)
    deciding.add_argument("--list", required=True, help="the inert list's path in the repository")
    deciding.add_argument("--pr", default="", help="the pull request this run is about; none runs the suite")
    deciding.add_argument("--sha", default="", help="the commit this run is about, which must be the pull request's head")
    deciding.add_argument("--also-run", default="", help="a reason of the caller's own to run the suite whatever the diff")
    posting = commands.add_parser("post")
    posting.add_argument("--repo", required=True)
    posting.add_argument("--sha", required=True)
    posting.add_argument("--reason", required=True)
    posting.add_argument("--url", default="", help="what the status links to, the workflow run")
    posting.add_argument("--interval", type=float, default=2, help="seconds between attempts")
    posting.add_argument("contexts", nargs="+", metavar="context")
    checking = commands.add_parser("check")
    checking.add_argument("--list", required=True, help="the inert list, a file in the working tree")
    checking.add_argument("--reads", required=True, help="a file of the paths the suite reads, one per line")
    args = parser.parse_args(argv)
    if args.command == "decide":
        return report(decide(args))
    return post(args) if args.command == "post" else check(args)


if __name__ == "__main__":
    sys.exit(main())
