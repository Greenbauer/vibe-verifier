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

The feature re-walk (opt-in, docs/feature-map.md):

    --features DIR        the feature map: every feature the pull request's changes touch also needs
                          exactly one `regression-check: <id> -- PASS -- <evidence>` line, held to
                          the same anchor rule
    --changed-files FILE  the pull request's changed paths, one per line (required with --features)
    --max-features N      at most N features are re-walked (default 3)
    --shared-over N       a changed file more than N features list selects none (default 2)

The gate selects those features itself, from these arguments (judged from the base like any
manifest line) and the map, never from what the explorer says it re-walked: a feature the explorer
skipped has no line and is refused.

A missing input file is exit 2: nothing was graded. A criteria file with no criteria in it is a
finding, because a repository that subscribes to this gate has decided its PRs state them.
"""
import os

from _acceptance import ANCHOR_FORMS, PASS_RE, anchors, check_lines, criteria, declares_none, resolves
from _contract import CannotRun, Finding, run_gate, tracked_files
from _features import add_selection_arguments, describe, selection

GATE = "acceptance-verdict"
REGRESSION_TOKEN = "regression-check"


def read_input(path, label):
    if not path or not os.path.isfile(path):
        raise CannotRun("%s file not found: %s" % (label, path or "(none given)"))
    with open(path, encoding="utf-8", errors="replace") as handle:
        return handle.read()


def line_finding(item, wording, carried, token, repo, tracked, artifacts):
    """Why the lines carried for `item` are not exactly one PASS with a resolving anchor, or None."""
    if not carried:
        return "%s has no `%s: %s` line (%s)" % (item, token, item, wording)
    if len(carried) != 1:
        return "%s has %d check lines; exactly one is allowed" % (item, len(carried))
    line = carried[0]
    if not PASS_RE.search(line):
        return "%s is not a PASS: %s" % (item, line.strip())
    found = anchors(line)
    if not found:
        return "%s has no anchor; cite %s" % (item, ANCHOR_FORMS)
    reasons = [resolves(anchor, repo, tracked, artifacts) for anchor in found]
    if all(reasons):
        return "%s: no anchor resolves at the head: %s" % (item, "; ".join(reasons))
    return None


def regression_findings(args, verdict, tracked):
    """One finding per feature the changes touch whose regression-check line is missing or not an anchored PASS."""
    chosen, features = selection(args.repo, args, args.base_ref)
    for line in describe(chosen) or ["re-walk: no feature's source globs match a changed file"]:
        print("%s: %s" % (GATE, line))
    lines = check_lines(verdict, REGRESSION_TOKEN)
    findings = []
    for fid, _ in chosen.selected:
        why = line_finding(fid, features[fid].path, lines.get(fid.casefold(), []), REGRESSION_TOKEN,
                           args.repo, tracked, args.artifacts)
        if why:
            findings.append(Finding(why))
    return findings


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
        why = line_finding(item, wording, lines.get(item.casefold(), []), args.token, args.repo, tracked, args.artifacts)
        if why:
            findings.append(Finding(why))
    if args.features:
        findings += regression_findings(args, verdict, tracked)
    return findings


def add_arguments(parser):
    parser.add_argument("--criteria", metavar="FILE", required=True)
    parser.add_argument("--verdict", metavar="FILE", required=True)
    parser.add_argument("--artifacts", metavar="DIR", default=None)
    parser.add_argument("--token", default="acceptance-check")
    add_selection_arguments(parser)


if __name__ == "__main__":
    run_gate(GATE, __doc__, check, add_arguments)
