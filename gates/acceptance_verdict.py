#!/usr/bin/env python3
"""Require one evidenced PASS per acceptance criterion, anchored to something at the head.

A wired gate: the caller supplies two declared inputs and the working tree is the head.

    --criteria FILE   the PR body (its `## Acceptance criteria` list) or a plain list, one per line
    --verdict FILE    the verdict text, usually the QAE's PR comment
    --artifacts DIR   a second root anchors may resolve in: the run's evidence (step logs, traces)
    --token TOKEN     the marker each check line starts with (default: acceptance-check)

Every criterion ACn needs exactly one line `<token>: ACn -- PASS -- <evidence>`, and the evidence
must carry at least one anchor that resolves in the working tree, or under --artifacts when given:
`<path>::<test title>` with the title verbatim in that file, or `<path>:<line>` /
`<path>:<start>-<end>` inside the file's length. Browser evidence is never a file in the tree, so an
explorer writes a step log per criterion into the artifact directory and anchors to a line of it.
The gate never judges whether the evidence covers the criterion's meaning; that is not structurally
checkable. It refuses a PASS that points at nothing, which is the floor.

A missing input file is exit 2: nothing was graded. A criteria file with no criteria in it is a
finding, because a repository that subscribes to this gate has decided its PRs state them.
"""
import os

from _acceptance import ANCHOR_FORMS, PASS_RE, anchors, check_lines, criteria, declares_none
from _contract import CannotRun, Finding, run_gate, tracked_files

GATE = "acceptance-verdict"


def read_input(path, label):
    if not path or not os.path.isfile(path):
        raise CannotRun("%s file not found: %s" % (label, path or "(none given)"))
    with open(path, encoding="utf-8", errors="replace") as handle:
        return handle.read()


def locate(anchor, repo, tracked, artifacts):
    """The file an anchor names: a tracked path at the head, else a file under --artifacts."""
    if anchor.path in tracked:
        return os.path.join(repo, anchor.path)
    if artifacts:
        candidate = os.path.normpath(os.path.join(artifacts, anchor.path))
        if candidate.startswith(os.path.normpath(artifacts) + os.sep) and os.path.isfile(candidate):
            return candidate
    return None


def resolves(anchor, repo, tracked, artifacts):
    """Why the anchor does not resolve, or None when it does."""
    located = locate(anchor, repo, tracked, artifacts)
    if located is None:
        return "%s is not in the tree%s" % (anchor.path, " or the artifacts" if artifacts else "")
    with open(located, encoding="utf-8", errors="replace") as handle:
        body = handle.read()
    if anchor.title:
        return None if anchor.title in body else 'the title "%s" is not in %s' % (anchor.title, anchor.path)
    length = body.count("\n") + (0 if body.endswith("\n") or not body else 1)
    if anchor.start < 1 or anchor.end < anchor.start or anchor.end > length:
        return "%s has %d lines, so %s is outside it" % (anchor.path, length, anchor.raw)
    return None


def check(args):
    criteria_text = read_input(args.criteria, "criteria")
    reason = declares_none(criteria_text)
    if reason:
        print("%s: nothing to verify, declared: %s" % (GATE, reason))
        return []
    wanted = criteria(criteria_text)
    verdict = read_input(args.verdict, "verdict")
    if not wanted:
        return [Finding("no acceptance criteria found (a `## Acceptance criteria` list, or `- None: <why>`)", args.criteria)]
    tracked = set(tracked_files(args.repo))
    lines = check_lines(verdict, args.token)
    findings = []
    for item, wording in wanted:
        carried = lines.get(item.casefold(), [])
        if not carried:
            findings.append(Finding("%s has no `%s: %s` line (%s)" % (item, args.token, item, wording)))
            continue
        if len(carried) != 1:
            findings.append(Finding("%s has %d check lines; exactly one is allowed" % (item, len(carried))))
            continue
        line = carried[0]
        if not PASS_RE.search(line):
            findings.append(Finding("%s is not a PASS: %s" % (item, line.strip())))
            continue
        found = anchors(line)
        if not found:
            findings.append(Finding("%s has no anchor; cite %s" % (item, ANCHOR_FORMS)))
            continue
        reasons = [resolves(anchor, args.repo, tracked, args.artifacts) for anchor in found]
        if all(reasons):
            findings.append(Finding("%s: no anchor resolves at the head: %s" % (item, "; ".join(reasons))))
    return findings


def add_arguments(parser):
    parser.add_argument("--criteria", metavar="FILE", required=True)
    parser.add_argument("--verdict", metavar="FILE", required=True)
    parser.add_argument("--artifacts", metavar="DIR", default=None)
    parser.add_argument("--token", default="acceptance-check")


if __name__ == "__main__":
    run_gate(GATE, __doc__, check, add_arguments)
