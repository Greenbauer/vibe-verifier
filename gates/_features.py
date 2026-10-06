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
"""
import os
import posixpath
import re

from _acceptance import parse_anchor
from _contract import CannotRun

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
    """[Feature] for the map as the working tree has it. No feature file is exit 2: nothing to read."""
    paths = feature_paths(directory, tracked)
    if not paths:
        raise CannotRun("no feature files in %s/ (one <id>.md per feature, besides the %s index)"
                        % (map_directory(directory), INDEX))
    features = []
    for path in paths:
        with open(os.path.join(repo, path), encoding="utf-8", errors="replace") as handle:
            features.append(parse(handle.read(), posixpath.basename(path)[:-3], path))
    return features
