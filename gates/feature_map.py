#!/usr/bin/env python3
"""Keep the feature map true against the code: no surface unowned, no listing stale, no anchor dangling.

The map is a directory of one Markdown file per user-facing feature (format: gates/_features.py and
docs/feature-map.md). This gate reads it at the head, with the code beside it:

    --dir DIR                   the map (default: docs/features)
    --surface KIND GLOB REGEX   the code's surfaces of one kind: every match of REGEX (one capture
                                group, `^` and `$` per line) in every tracked file GLOB matches. A
                                feature owns one by listing `- KIND: `value`` under Surfaces.
    --surface KIND GLOB         the same, with each tracked file GLOB matches as a surface (its path)

Both forms are repeatable, and two for one kind add up. A finding is

1. a surface the code declares that no feature lists (it is owned by no feature),
2. a listed surface the code no longer declares, or a listed kind no --surface declares,
3. a `- source:` glob that matches no tracked file, or a feature with no source glob at all,
4. a Verify anchor that does not resolve in the tree (`path::title` with the title verbatim in that
   file, or `path:line` inside its length), or a Verify section with neither an anchor nor a step,
5. a malformed Surfaces or Verify bullet, a missing `## Surfaces` or `## Verify` section, or a file
   name that is not one word (it is the feature's id).

The whole map is judged on every run, not only what the pull request changed: a pull request that
removes a route must update the map in the same change. So a repository subscribes clean.

With no --surface, the gate checks anchors and source globs only and says so: the pass is labelled
`completeness not checked`, because a surface no feature owns would pass unseen.

Exit 2 when a --surface is malformed, matches no tracked file, or finds no surface: an extractor
that reads nothing checks nothing, and a pattern that stopped matching after a reformat would
otherwise pass every map.

A head whose map has no feature file is judged by what the base says of it (--base-ref, resolved
as every gate resolves it), and by nothing else:

- the merge base of the base and the head has feature files: the branch removed the map, which is
  a finding;
- the merge base has none and the base has some: the branch predates the map, which landed on the
  base after the branch left it. That is no fault of the branch, so the gate passes, checks
  nothing, and says so: an advisory finding, and a pass labelled `not checked: the branch predates
  the feature map`. The map is checked once the branch takes the base;
- anything else is exit 2, as a missing map always was: no base resolves, the two share no merge
  base (a shallow clone), or the base has no map either, so the subscription has none.
"""
import os
import re

from _acceptance import resolves
from _contract import CannotRun, Finding, glob_to_regex, qualify, run_gate, tracked_files
from _features import FEATURE_ID, head_map, map_directory, missing_map

GATE = "feature-map"
KIND = re.compile(r"^[a-z][a-z0-9-]*$")


def extractors(declared):
    """[(kind, glob, compiled regex or None)] from the --surface arguments, or exit 2."""
    found = []
    for words in declared:
        if len(words) not in (2, 3):
            raise CannotRun("--surface takes KIND GLOB [REGEX], got %d argument(s): %s" % (len(words), " ".join(words)))
        kind, glob = words[0], words[1]
        if not KIND.match(kind) or kind == "source":
            raise CannotRun("--surface kind %r must be lowercase letters, digits and dashes, and not `source`" % kind)
        pattern = None
        if len(words) == 3:
            try:
                pattern = re.compile(words[2], re.MULTILINE)
            except re.error as error:
                raise CannotRun("--surface %s regex %r does not compile: %s" % (kind, words[2], error))
            if pattern.groups != 1:
                raise CannotRun("--surface %s regex %r must have exactly one capture group, the surface; it has %d"
                                % (kind, words[2], pattern.groups))
        found.append((kind, glob, pattern))
    return found


def extract(repo, tracked, kind, glob, pattern):
    """{surface: (path, line or None)}: what one --surface finds, or exit 2 when it finds nothing."""
    regex = glob_to_regex(glob)
    files = [path for path in tracked if regex.match(path)]
    if not files:
        raise CannotRun("--surface %s %s matches no tracked file" % (kind, glob))
    if pattern is None:
        return {path: (path, None) for path in files}
    found = {}
    for path in files:
        with open(os.path.join(repo, path), encoding="utf-8", errors="replace") as handle:
            text = handle.read()
        for match in pattern.finditer(text):
            value = (match.group(1) or "").strip()
            if value:
                found.setdefault(value, (path, text.count("\n", 0, match.start(1)) + 1))
    if not found:
        raise CannotRun("--surface %s regex %r found no surface in the %d file(s) %s matches"
                        % (kind, pattern.pattern, len(files), glob))
    return found


def structure_findings(feature):
    """Malformed bullets, missing sections, an id that is not one word, nothing to select or verify by."""
    findings = [Finding(message, feature.path, line) for line, message in feature.malformed]
    if not FEATURE_ID.match(feature.id):
        findings.append(Finding("the file name is the feature's id, so it must be one word "
                                "(letters, digits, `.`, `_`, `-`)", feature.path))
    for section in ("surfaces", "verify"):
        if section not in feature.sections:
            findings.append(Finding("no `## %s` section" % section.capitalize(), feature.path))
    if "surfaces" in feature.sections and not feature.sources:
        findings.append(Finding("lists no `- source:` glob, so no change can select this feature", feature.path))
    if "verify" in feature.sections and not feature.anchors and not feature.steps:
        findings.append(Finding("Verify lists no anchor and no step", feature.path))
    return findings


def source_findings(feature, tracked):
    findings = []
    for glob, line in feature.sources:
        regex = glob_to_regex(glob)
        if not any(regex.match(path) for path in tracked):
            findings.append(Finding("source `%s` matches no tracked file" % glob, feature.path, line))
    return findings


def surface_findings(feature, declared, owned):
    """Listings of an undeclared kind or of a surface the code no longer declares; records the rest in `owned`."""
    findings = []
    for kind, value, line in feature.surfaces:
        if kind not in declared:
            findings.append(Finding("lists a %s, but no `--surface %s` is declared, so nothing checks it" % (kind, kind),
                                    feature.path, line))
        elif value not in declared[kind]:
            findings.append(Finding("lists %s `%s`, which the code no longer declares" % (kind, value), feature.path, line))
        else:
            owned.add((kind, value))
    return findings


def anchor_findings(feature, repo, tracked_set):
    findings = []
    for anchor, line in feature.anchors:
        why = resolves(anchor, repo, tracked_set)
        if why:
            findings.append(Finding("Verify anchor `%s` does not resolve: %s" % (anchor.raw, why), feature.path, line))
    return findings


def missing_findings(args):
    """A head with no feature file, judged by what the base says of it (`missing_map` is exit 2 when it
    says nothing). Removing the map is a finding, not an exit 2: the gate read the history it needs
    and has a verdict on the change. Predating it is a pass that says nothing was checked."""
    missing = missing_map(args.repo, args.dir, args.base_ref)
    if missing.removed:
        return [Finding("%s. Restore it, or unsubscribe first: remove the feature-map line and merge, then delete "
                        "the map" % missing.why())]
    qualify("not checked: the branch predates the feature map")
    return [Finding("not checked: %s. The map is checked once the branch takes %s" % (missing.why(), missing.base),
                    advisory=True)]


def check(args):
    declared_extractors = extractors(args.surface or [])
    tracked = tracked_files(args.repo)
    features = head_map(args.repo, args.dir, tracked)
    if not features:
        return missing_findings(args)
    declared = {}
    for kind, glob, pattern in declared_extractors:
        for value, where in extract(args.repo, tracked, kind, glob, pattern).items():
            declared.setdefault(kind, {}).setdefault(value, where)
    owned, tracked_set, findings = set(), set(tracked), []
    for feature in features:
        findings += (structure_findings(feature) + source_findings(feature, tracked)
                     + surface_findings(feature, declared, owned) + anchor_findings(feature, args.repo, tracked_set))
    for kind in sorted(declared):
        for value, (path, line) in sorted(declared[kind].items()):
            if (kind, value) not in owned:
                findings.append(Finding("%s `%s` is owned by no feature: list `- %s: `%s`` under the Surfaces of the "
                                        "feature that serves it" % (kind, value, kind, value), path, line))
    counts = ", ".join("%d %s" % (len(values), kind) for kind, values in sorted(declared.items()))
    print("%s: %d feature(s) in %s/; surfaces checked: %s" % (GATE, len(features), map_directory(args.dir), counts or "none"))
    if not declared_extractors:
        qualify("completeness not checked")
        findings.append(Finding("completeness was not checked: no --surface says where the code declares its surfaces, "
                                "so one that no feature owns passes unseen; only anchors and source globs were checked",
                                advisory=True))
    return findings


def add_arguments(parser):
    parser.add_argument("--dir", default="docs/features", help="the feature map directory (default: docs/features)")
    parser.add_argument("--surface", action="append", nargs="+", metavar="ARG",
                        help="KIND GLOB [REGEX]: the code's surfaces of one kind (repeatable)")


if __name__ == "__main__":
    run_gate(GATE, __doc__, check, add_arguments)
