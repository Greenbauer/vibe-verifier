"""The feature map: one Markdown file per user-facing feature, read the same way by every gate.

A map is a directory (default `docs/features/`) holding a `README.md` index, which nothing here reads,
and one `<id>.md` per feature. The file name is the feature's id. A feature file has these sections,
in any order; others (a `## Sub-features` list, say) are allowed and not read:

    ## Surfaces   what the feature owns in code, one bullet per line:
                    - source: `glob`     files that implement it: `*` within one path segment, `**`
                                         across segments, relative to the repository root
                    - <kind>: `value`    a surface the code declares, such as `- route: `/login``,
                                         checked against what the gate's `--surface <kind>` finds
    ## Reach      how a user gets there, in the product's own words (prose, not read here)
    ## Verify     `- `<path>::<exact test title>`` bullets (or `<path>:<line>` / `<path>:<start>-<end>`),
                  each resolving in the tree, and optionally numbered browser steps (`1. ...`)
    ## Gotchas    traps a change or a verification run tends to hit (prose, not read here)

In Surfaces and Verify every top-level `- ` bullet must have one of the shapes above: a bullet that
does not is malformed, never skipped, because a typo would otherwise drop a claim silently.

`select` picks the features a pull request's changed files touch, for a QAE re-walk: the features
whose own source globs match a changed file, ignoring a file so many features list that it says
nothing about which one changed, best first, capped. The rule reads the map and nothing else (no
history, no outcomes), and a feature file the base has selects by its base Surfaces, so a pull
request cannot narrow its own re-walk by editing the map.

`missing_map` judges a head with no feature file, for the drift gate and the re-walk alike, from the
commits alone: the branch removed the map, or it predates a map the base has since gained, or nothing
tells which and the caller cannot run.
"""
import os
import posixpath
import re
import subprocess
from typing import NamedTuple

from _acceptance import parse_anchor
from _contract import CannotRun, glob_to_regex, resolve_base

SECTION = re.compile(r"^##[ \t]+(?P<name>\S.*?)[ \t]*$")
SURFACE = re.compile(r"^- (?P<kind>[a-z][a-z0-9-]*): `(?P<value>[^`]+)`[ \t]*$")
VERIFY_BULLET = re.compile(r"^- `(?P<anchor>[^`]+)`[ \t]*$")
STEP = re.compile(r"^[0-9]+[.)][ \t]+\S")
# The id is the token a QAE verdict line names the feature by, so it is one word.
FEATURE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
INDEX = "README.md"


class Feature:
    """One parsed feature file. Lines are 1-based line numbers in the file."""

    def __init__(self, fid, path):
        self.id = fid
        self.path = path
        self.sections = set()
        self.sources = []    # (glob, line)
        self.surfaces = []   # (kind, value, line)
        self.anchors = []    # (Anchor, line)
        self.steps = 0       # numbered browser steps under Verify
        self.malformed = []  # (line, message)


def parse(text, fid, path):
    feature = Feature(fid, path)
    section = None
    for number, line in enumerate(text.splitlines(), 1):
        heading = SECTION.match(line)
        if heading:
            section = heading.group("name").casefold()
            feature.sections.add(section)
        elif section == "surfaces" and line.startswith("- "):
            read_surface(feature, number, line)
        elif section == "verify":
            read_verify(feature, number, line)
    return feature


def read_surface(feature, number, line):
    bullet = SURFACE.match(line)
    if not bullet:
        feature.malformed.append((number, "malformed Surfaces bullet (write `- source: `glob`` or "
                                          "`- <kind>: `value``): %s" % line))
    elif bullet.group("kind") == "source":
        feature.sources.append((bullet.group("value"), number))
    else:
        feature.surfaces.append((bullet.group("kind"), bullet.group("value"), number))


def read_verify(feature, number, line):
    if STEP.match(line):
        feature.steps += 1
    elif line.startswith("- "):
        bullet = VERIFY_BULLET.match(line)
        anchor = parse_anchor(bullet.group("anchor")) if bullet else None
        if anchor:
            feature.anchors.append((anchor, number))
        else:
            feature.malformed.append((number, "malformed Verify bullet (write `- `<path>::<exact test title>`` "
                                              "with a title of 8 or more characters, or `- `<path>:<line>``; "
                                              "write browser steps as a numbered list): %s" % line))


def map_directory(directory):
    """The map's directory as a repository-relative path, or exit 2 for one outside the repository."""
    normal = posixpath.normpath(directory.replace(os.sep, "/"))
    if normal.startswith(("/", "../")) or normal in ("..", "."):
        raise CannotRun("the feature map directory must be a path inside the repository: %s" % directory)
    return normal


def feature_paths(directory, tracked):
    """The tracked feature files directly in `directory`, sorted: every `*.md` but the index."""
    prefix = map_directory(directory) + "/"
    return sorted(path for path in tracked if path.startswith(prefix) and path.endswith(".md")
                  and "/" not in path[len(prefix):] and path[len(prefix):] != INDEX)


def head_map(repo, directory, tracked):
    """[Feature] for the map as the working tree has it; empty when it has no feature file, which
    `missing_map` then judges."""
    features = []
    for path in feature_paths(directory, tracked):
        with open(os.path.join(repo, path), encoding="utf-8", errors="replace") as handle:
            features.append(parse(handle.read(), posixpath.basename(path)[:-3], path))
    return features


def paths_at(repo, directory, commit):
    """The map's feature files as `commit` has them, sorted; empty when it has none."""
    prefix = map_directory(directory) + "/"
    listed = subprocess.run(["git", "-C", repo, "ls-tree", "-z", "--name-only", commit, "--", prefix],
                            capture_output=True, text=True)
    if listed.returncode != 0:
        raise CannotRun("git ls-tree %s %s failed: %s" % (commit, prefix, listed.stderr.strip()))
    return feature_paths(directory, [p for p in listed.stdout.split("\0") if p])


class Missing(NamedTuple):
    """Why the head's map has no feature file, as the base tells it."""
    removed: bool   # the merge base has the map, so this branch removed it; else the branch predates the map
    directory: str  # the map's directory
    base: str       # the base ref
    point: str      # the merge base of the base and HEAD, abbreviated
    count: int      # the feature files at the merge base (removed) or at the base (predates)

    def why(self):
        """The fact, in the words the drift gate and the re-walk both print."""
        if self.removed:
            return ("the feature map was removed: %s/ has no feature file at the head, and the merge base %s has %d"
                    % (self.directory, self.point, self.count))
        return ("this branch has no feature map because it predates it (%s/ has no feature file here or at the merge "
                "base %s, and %s has %d)" % (self.directory, self.point, self.base, self.count))


def missing_map(repo, directory, base_ref=None):
    """What the base says of a head whose map has no feature file, or exit 2 when it says nothing.

    The merge base of the base and HEAD has feature files: this branch removed the map. It has none and
    the base has some: the map landed on the base after the branch left it, so the branch predates the
    map and gets it by taking the base. Anything else is the exit 2 a missing map always was, because
    nothing then tells a branch that predates a map from a subscription that has none: no base resolves,
    the two share no merge base (a shallow clone), or the base has no map either.
    """
    where = map_directory(directory)
    absent = "no feature files in %s/ (one <id>.md per feature, besides the %s index)" % (where, INDEX)
    try:
        base = resolve_base(repo, base_ref)
    except CannotRun as reason:
        raise CannotRun("%s, and no base to tell whether the branch predates the map: %s" % (absent, reason))
    found = subprocess.run(["git", "-C", repo, "merge-base", base, "HEAD"], capture_output=True, text=True)
    point = found.stdout.strip()
    if found.returncode != 0 or not point:
        raise CannotRun("%s, and %s and HEAD have no merge base to tell whether the branch predates the map "
                        "(fetch history: fetch-depth: 0)" % (absent, base))
    at_point = paths_at(repo, directory, point)
    if at_point:
        return Missing(True, where, base, point[:12], len(at_point))
    at_base = paths_at(repo, directory, base)
    if not at_base:
        raise CannotRun(absent)
    return Missing(False, where, base, point[:12], len(at_base))


def base_map(repo, directory, base):
    """{id: Feature} for the map at the base commit; empty when the base has none."""
    features = {}
    for path in paths_at(repo, directory, base):
        shown = subprocess.run(["git", "-C", repo, "show", "%s:%s" % (base, path)], capture_output=True, text=True)
        if shown.returncode != 0:
            raise CannotRun("git show %s:%s failed: %s" % (base, path, shown.stderr.strip()))
        fid = posixpath.basename(path)[:-3]
        features[fid] = parse(shown.stdout, fid, path)
    return features


class Selection(NamedTuple):
    selected: list  # [(feature id, [changed paths it owns])], best first, at most the cap
    dropped: list   # the same, for features over the cap
    shared: list    # [(changed path, [ids of the features whose globs match it])], not counted
    predates: Missing = None  # set when the branch predates the map: it has no feature to select


def select(head, base, changed, cap, shared_over):
    """The features `changed` touches. `head` is the map at the head ([Feature]); `base` the map at the
    base ({id: Feature}), whose version of a feature, when it has one, supplies that feature's globs.

    A changed path counts for every feature whose source globs match it, unless more than `shared_over`
    features match it: then it is shared (a stylesheet, a router) and counts for none. Features rank by
    how many counted paths they own, then by id, and the first `cap` are selected.
    """
    globs = {feature.id: [glob_to_regex(glob) for glob, _ in (base.get(feature.id) or feature).sources]
             for feature in head}
    owned, shared = {}, []
    for path in dict.fromkeys(changed):
        ids = listers(path, globs)
        if len(ids) > shared_over:
            shared.append((path, ids))
            continue
        for fid in ids:
            owned.setdefault(fid, []).append(path)
    ranked = sorted(owned, key=lambda fid: (-len(owned[fid]), fid))
    return Selection([(fid, owned[fid]) for fid in ranked[:cap]], [(fid, owned[fid]) for fid in ranked[cap:]], shared)


def listers(path, globs):
    """The ids, sorted, of the features whose compiled globs match `path`."""
    return [fid for fid in sorted(globs) if any(regex.match(path) for regex in globs[fid])]


def add_selection_arguments(parser):
    """The selection options, on acceptance-verdict's manifest line and read from it by
    `vibe-verifier features`, so both apply one rule with one set of defaults."""
    parser.add_argument("--features", metavar="DIR", default=None,
                        help="the feature map; turns on the re-walk check (default: off)")
    parser.add_argument("--changed-files", metavar="FILE", default=None,
                        help="the pull request's changed paths, one per line (required with --features)")
    parser.add_argument("--max-features", metavar="N", type=int, default=3,
                        help="re-walk at most this many features (default: 3)")
    parser.add_argument("--shared-over", metavar="N", type=int, default=2,
                        help="a changed file more than N features list is shared and selects none (default: 2)")
    parser.add_argument("--max-states", metavar="N", type=int, default=3,
                        help="the explorer walks at most this many of each feature's Verify states (default: 3)")


def without_map(repo, directory, base_ref):
    """What `selection` returns for a head with no feature file, as `missing_map` judges it: nothing
    selected, with the reason, on a branch that predates the map. A branch that removed the map is
    exit 2, since there is then no feature to select and none of its files for an explorer to read."""
    missing = missing_map(repo, directory, base_ref)
    if missing.removed:
        raise CannotRun("%s, so no feature can be selected for the re-walk. Restore it" % missing.why())
    return Selection([], [], [], missing), {}


def selection(repo, args, base_ref=None):
    """(Selection, {id: Feature at the head}) for the selection options in `args`, or exit 2 when the
    map, the changed paths or the base cannot be read. With no base to resolve (no history, no
    default branch), the head's map stands alone, as the runner's manifest does; an explicit
    `base_ref` must resolve. A head with no feature file selects as `without_map` says."""
    if not args.changed_files or not os.path.isfile(args.changed_files):
        raise CannotRun("--features needs --changed-files, a readable file of changed paths: %s" % (args.changed_files or "(none given)"))
    if min(args.max_features, args.shared_over, args.max_states) < 1:
        raise CannotRun("--max-features, --shared-over and --max-states must be 1 or more")
    with open(args.changed_files, encoding="utf-8", errors="replace") as handle:
        changed = [line.strip() for line in handle if line.strip()]
    tracked = subprocess.run(["git", "-C", repo, "ls-files", "-z"], capture_output=True, text=True)
    if tracked.returncode != 0:
        raise CannotRun("git ls-files failed: %s" % tracked.stderr.strip())
    head = head_map(repo, args.features, [p for p in tracked.stdout.split("\0") if p])
    if not head:
        return without_map(repo, args.features, base_ref)
    try:
        base_commit = resolve_base(repo, base_ref)
    except CannotRun:
        if base_ref:
            raise
        base_commit = None
    base = base_map(repo, args.features, base_commit) if base_commit else {}
    return select(head, base, changed, args.max_features, args.shared_over), {feature.id: feature for feature in head}


def describe(chosen):
    """One line per decision, for a log a person reads."""
    def owns(paths):
        return "%d changed file%s" % (len(paths), "" if len(paths) == 1 else "s")
    if chosen.predates:
        why = chosen.predates
        return ["not re-walked: %s. Features are re-walked once the branch takes %s" % (why.why(), why.base)]
    lines = ["re-walk: %s (%s)" % (fid, owns(paths)) for fid, paths in chosen.selected]
    lines += ["shared, not counted: %s (listed by %d features)" % (path, len(ids)) for path, ids in chosen.shared]
    lines += ["over the cap, not re-walked: %s (%s)" % (fid, owns(paths)) for fid, paths in chosen.dropped]
    return lines
