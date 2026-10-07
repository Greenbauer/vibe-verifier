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
                          the same anchor rule, and at least one walked step in the run's evidence:
                          a `- step k:` line of `qae/features/<id>.md` under --artifacts (required
                          with --features) with its screenshot `qae/features/<id>-step-k.png`
    --changed-files FILE  the pull request's changed paths, one per line (required with --features)
    --max-features N      at most N features are re-walked (default 3)
    --shared-over N       a changed file more than N features list selects none (default 2)
    --max-states N        the explorer walks at most N of each feature's Verify states (default 3).
                          The explore job tells the explorer; the gate requires one walked step, not N

The gate selects those features itself, from these arguments (judged from the base like any
manifest line) and the map, never from what the explorer says it re-walked: a feature the explorer
skipped has no line and is refused. A re-walk is partial by design, so a state the explorer did not
walk is never a finding: it names them on a `regression-skip: <id> -- <states>` line, which the gate
prints and does not judge. A FAIL is a finding, and says a step that was walked no longer matches
the feature file: the pull request fixes the regression or, when it means the new behaviour, updates
the file. A feature with a line and no walked step was not re-walked, which is a finding too.

A criterion's annotations (harnesses/qae/README.md) are held to the run's evidence, its step log
`qae/ACn.md` under --artifacts:

    --references FILE     the design references the workflow supplied, as the JSON object
                          {key: sha256 of the image} it declared (an empty file is none)

    [ref: <key>]          refused unless the workflow supplied <key> and the evidence holds that very
                          image at references/<key>.png; a PASS also needs a step line naming
                          `reference <key>`, and every such line is a comparison whose screenshot
                          `qae/ACn-step-k.png` is as wide as the reference, both read from the PNG
                          header (a 1x reference is as wide as the viewport it shows). A reference is
                          never invented: one that was not supplied is the operator's to supply
                          (needs-operator-reference)
    [as: <role>, ...]     a PASS needs, for each role, a step line starting `as <role>:`
    a malformed bracket   refused, so a typo never drops a requirement

The ticket's criteria (opt-in, harnesses/qae/README.md):

    --ticket FILE         the acceptance criteria of the ticket the pull request implements, written by
                          the consumer's workflow: each is TC1, TC2, ... and needs its own anchored PASS,
                          in addition to the pull request's. A pull request that declares `- None: <why>`
                          or leaves one out cannot drop them. An empty file links no ticket; a non-empty
                          one in which no criteria can be found is a finding

A missing input file is exit 2: nothing was graded. A criteria file with no criteria in it is a
finding, because a repository that subscribes to this gate has decided its PRs state them.
"""
import hashlib
import json
import os
import re

from _acceptance import (ANCHOR_FORMS, FAIL_RE, PASS_RE, anchors, annotations, check_lines, criteria, declares_none,
                         named_references, png_size, resolves, said, ticket_items)
from _contract import CannotRun, Finding, run_gate, tracked_files
from _features import add_selection_arguments, describe, selection

GATE = "acceptance-verdict"
REGRESSION_TOKEN = "regression-check"
SKIP_TOKEN = "regression-skip"
STEP = r"^[ \t]*-[ \t]+step[ \t]+[0-9]+:"
STEP_LINE = re.compile(r"^[ \t]*-[ \t]+step[ \t]+(?P<k>[0-9]+):(?P<text>.*)$", re.IGNORECASE | re.MULTILINE)


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


def declared_references(path):
    """{key: sha256} from the file the workflow wrote, or None when the manifest line names none."""
    if path is None:
        return None
    text = read_input(path, "references").strip()
    try:
        declared = json.loads(text) if text else {}
    except ValueError as error:
        raise CannotRun("references file %s is not JSON: %s" % (path, error))
    if not isinstance(declared, dict) or not all(isinstance(v, str) for v in declared.values()):
        raise CannotRun("references file %s is not a JSON object of key to sha256" % path)
    return declared


def reference_finding(item, key, declared, artifacts):
    """Why the reference `key` that `item` names is not the image the workflow supplied, or None."""
    if declared is None:
        return ("%s names reference `%s`, but this QAE manifest declares no supplied references: add "
                "--references qae-inputs/references.json to its acceptance-verdict line" % (item, key))
    if key not in declared:
        return ("%s names reference `%s`, but the workflow supplied no qae-inputs/references/%s.png. Supply it from "
                "the site step (needs-operator-reference): a reference is never invented, so %s cannot pass "
                "without it" % (item, key, key, item))
    image = os.path.join(artifacts, "references", key + ".png")
    if not os.path.isfile(image):
        return "%s: the evidence holds no references/%s.png, the copy the workflow made" % (item, key)
    with open(image, "rb") as handle:
        if hashlib.sha256(handle.read()).hexdigest() != declared[key]:
            return "%s: references/%s.png in the evidence is not the image the workflow supplied" % (item, key)
    if png_size(image) is None:
        return "%s: references/%s.png is not a PNG, so the width it is compared at cannot be read" % (item, key)
    return None


def comparison_findings(item, key, text, artifacts, width):
    """Why the step log `text` of a PASSed `item` shows no comparison with reference `key` at the reference's
    `width` (None when the reference was not supplied, which is a finding of its own)."""
    steps = [m.group("k") for m in STEP_LINE.finditer(text)
             if key.casefold() in (named.casefold() for named in named_references(m.group("text")))]
    if not steps:
        return ["%s is a PASS, but no step line in qae/%s.md names `reference %s`" % (item, item, key)]
    if width is None:
        return []
    findings = []
    for k in steps:
        shot = os.path.join(artifacts, "qae", "%s-step-%s.png" % (item, k))
        size = png_size(shot) if os.path.isfile(shot) else None
        if size is None:
            findings.append("%s is a PASS, but step %s, its comparison with reference %s, has no PNG screenshot "
                            "qae/%s-step-%s.png to show it was compared at the reference's %d pixels"
                            % (item, k, key, item, k, width))
        elif size[0] != width:
            findings.append("%s is a PASS, but step %s compared with reference %s at %d pixels wide, and the reference "
                            "is %d: resize the browser to the reference's width before that step's screenshot"
                            % (item, k, key, size[0], width))
    return findings


def step_findings(item, notes, artifacts, widths):
    """What the step log of a PASSed `item` lacks: a comparison with each reference, at its width (`widths`,
    {key: pixels}, holds each reference the workflow supplied), and a step as each role."""
    log = os.path.join(artifacts, "qae", item + ".md")
    if not os.path.isfile(log):
        return ["%s carries annotations, but its step log qae/%s.md is not in the evidence" % (item, item)]
    with open(log, encoding="utf-8", errors="replace") as handle:
        text = handle.read()
    missing = []
    for key in notes.references:
        missing += comparison_findings(item, key, text, artifacts, widths.get(key))
    for role in notes.roles:
        if not re.search(STEP + r"[ \t]*as[ \t]+" + re.escape(role) + r"[ \t]*:", text, re.I | re.M):
            missing.append("%s is a PASS, but no step line in qae/%s.md starts `as %s:`" % (item, item, role))
    return missing


def annotation_findings(item, wording, passed, args, declared):
    """Findings for one criterion's annotations: malformed ones, references the workflow did not supply,
    and, on a PASS, the steps its log lacks."""
    notes = annotations(wording)
    why = ["%s has a malformed annotation %s (write `[ref: <key>]` or `[as: <role>, <role>]`)" % (item, bad)
           for bad in notes.malformed]
    if not (notes.references or notes.roles):
        return why
    if not args.artifacts:
        return why + ["%s carries annotations, which are checked in the run's evidence: give the gate --artifacts" % item]
    widths = {}
    for key in notes.references:
        problem = reference_finding(item, key, declared, args.artifacts)
        if problem:
            why.append(problem)
        else:
            widths[key] = png_size(os.path.join(args.artifacts, "references", key + ".png"))[0]
    return why + (step_findings(item, notes, args.artifacts, widths) if passed else [])


def walked_steps(artifacts, fid):
    """How many steps of feature `fid`'s re-walk are on record: the `- step k:` lines of its step log
    qae/features/<id>.md whose screenshot qae/features/<id>-step-k.png is a non-empty file."""
    directory = os.path.join(artifacts, "qae", "features")
    log = os.path.join(directory, fid + ".md")
    if not os.path.isfile(log):
        return 0
    with open(log, encoding="utf-8", errors="replace") as handle:
        shots = [os.path.join(directory, "%s-step-%s.png" % (fid, m.group("k"))) for m in STEP_LINE.finditer(handle.read())]
    return sum(1 for shot in shots if os.path.isfile(shot) and os.path.getsize(shot) > 0)


def regression_finding(fid, path, carried, walked, repo, tracked, artifacts):
    """Why the re-walk of feature `fid` (its file `path`) is not one anchored PASS over at least one walked step,
    or None. `carried` is its regression-check lines and `walked` how many of its steps are on record."""
    if len(carried) == 1 and not walked:
        return ("%s was not re-walked: its step log qae/features/%s.md holds no `- step k:` line with its screenshot "
                "qae/features/%s-step-k.png, so its line stands on nothing" % (fid, fid, fid))
    if len(carried) == 1 and FAIL_RE.search(carried[0]) and not PASS_RE.search(carried[0]):
        return ("%s is a FAIL: %s. A step the explorer walked no longer matches %s: fix the regression or, if this pull "
                "request means to change that behaviour, update %s in this pull request, since the re-walk reads the "
                "pull request's own copy of it" % (fid, carried[0].strip(), path, path))
    return line_finding(fid, path, carried, REGRESSION_TOKEN, repo, tracked, artifacts)


def regression_findings(args, verdict, tracked):
    """One finding per feature the changes touch whose re-walk is not an anchored PASS over a walked step. What the
    explorer left unwalked (its regression-skip lines) is printed and never judged."""
    if not args.artifacts:
        raise CannotRun("--features needs --artifacts, the run's evidence: each re-walk's step log is read there")
    chosen, features = selection(args.repo, args, args.base_ref)
    for line in describe(chosen) or ["re-walk: no feature's source globs match a changed file"]:
        print("%s: %s" % (GATE, line))
    lines, skipped = check_lines(verdict, REGRESSION_TOKEN), check_lines(verdict, SKIP_TOKEN)
    findings = []
    for fid, _ in chosen.selected:
        walked = walked_steps(args.artifacts, fid)
        print("%s: %s: %d step%s logged with a screenshot" % (GATE, fid, walked, "" if walked == 1 else "s"))
        for line in skipped.get(fid.casefold(), []):
            print("%s: %s: states not walked, as the explorer reports: %s" % (GATE, fid, said(line, SKIP_TOKEN)))
        why = regression_finding(fid, features[fid].path, lines.get(fid.casefold(), []), walked,
                                 args.repo, tracked, args.artifacts)
        if why:
            findings.append(Finding(why))
    return findings


def check(args):
    criteria_text = read_input(args.criteria, "criteria")
    ticket, problem = ticket_items(read_input(args.ticket, "ticket") if args.ticket else "", args.ticket)
    reason = declares_none(criteria_text)
    if reason and not (ticket or problem):
        print("%s: nothing to verify, declared: %s" % (GATE, reason))
        return []
    findings = [Finding(problem, args.ticket)] if problem else []
    wanted = ([] if reason else criteria(criteria_text)) + ticket
    verdict = read_input(args.verdict, "verdict")
    if not wanted:
        return findings or [Finding("no acceptance criteria found (a `## Acceptance criteria` list, or `- None: <why>`)",
                                    args.criteria)]
    tracked = set(tracked_files(args.repo))
    lines = check_lines(verdict, args.token)
    declared = declared_references(args.references)
    for item, wording in wanted:
        why = line_finding(item, wording, lines.get(item.casefold(), []), args.token, args.repo, tracked, args.artifacts)
        if why:
            findings.append(Finding(why))
        findings += [Finding(text) for text in annotation_findings(item, wording, why is None, args, declared)]
    if args.features:
        findings += regression_findings(args, verdict, tracked)
    return findings


def add_arguments(parser):
    parser.add_argument("--criteria", metavar="FILE", required=True)
    parser.add_argument("--verdict", metavar="FILE", required=True)
    parser.add_argument("--artifacts", metavar="DIR", default=None)
    parser.add_argument("--token", default="acceptance-check")
    parser.add_argument("--references", metavar="FILE", default=None)
    parser.add_argument("--ticket", metavar="FILE", default=None)
    add_selection_arguments(parser)


if __name__ == "__main__":
    run_gate(GATE, __doc__, check, add_arguments)
