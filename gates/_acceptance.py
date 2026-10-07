"""The acceptance-verdict grammar: criteria, their annotations, check lines, and evidence anchors.

Adapted from the grammar a private predecessor's QAE bots write verdicts in (2026-09). Kept: the PASS token, the marker-line form, and
the two anchor forms. Dropped: the looser "substantive" and "resolvable token" bars that the
anchor bar superseded there on 2026-09-14.

A verdict line reads

    acceptance-check: AC2 -- PASS -- signup rejects an expired invite (e2e/signup.spec.ts::rejects an expired invite)

and an anchor is either `<path>::<test title>`, whose title must appear verbatim in that file, or
`<path>:<line>` / `<path>:<start>-<end>`, which must lie inside the file. An anchor names WHERE the
proof is; a bare file name does not, because a file exists whether or not the assertion cited
exists inside it.

A criterion's own text may carry annotations a QAE run is held to (harnesses/qae/README.md):
`[ref: <key>]` names a design reference the workflow supplies, and `[as: <role>, <role>]` the roles
to walk the criterion as.
"""
import os
import re
import struct
from typing import NamedTuple

FILE_EXT = r"[cm]?tsx?|[cm]?jsx?|css|scss|html|svelte|vue|astro|md|ya?ml|sql|json|sh|py"
# `- PASS -`, `-- PASS --`, or the en/em dash forms the fleet bots write.
PASS_RE = re.compile(r"(?:—|–|-{1,2})[ \t]*PASS\b", re.IGNORECASE)
ANCHOR_HEAD_RE = re.compile(
    r"(?P<path>\.?[A-Za-z0-9_][A-Za-z0-9_./-]*\.(?:" + FILE_EXT + r"))"
    r"(?:::|:(?P<start>[0-9]+)(?:-(?P<end>[0-9]+))?(?![0-9A-Za-z]))"
)
# Resolution is a substring test, so a title this short would match almost any file.
MIN_ANCHOR_TITLE = 8
ANCHOR_FORMS = ("`<path>::<test title>` (for example `e2e/signup.spec.ts::rejects an expired invite`) "
                "or `<path>:<line>` / `<path>:<start>-<end>` (for example `src/lib/invites.ts:118-134`)")
CRITERIA_HEADING = re.compile(r"^#{1,6}[ \t]+acceptance criteria[ \t]*$", re.IGNORECASE)
LIST_ITEM = re.compile(r"^[ \t]*(?:[-*+]|[0-9]+[.)])[ \t]+(?:\[[ xX]\][ \t]+)?(?P<text>\S.*)$")
# `- None: this changes one pinned SHA` declares that there is nothing for a browser to check. The
# reason is required: a bare "None" is a shrug, and the point of the declaration is that a reviewer
# can weigh the claim against the diff. Silence is still a finding; only the declaration passes.
NONE_ITEM = re.compile(r"^none\b[ \t]*[:—-]?[ \t]*(?P<reason>\S.*)$", re.IGNORECASE)


class Anchor(NamedTuple):
    path: str
    title: str  # empty for a line anchor
    start: int  # 0 for a title anchor
    end: int
    raw: str


def criteria(text):
    """The acceptance criteria in `text`, as (id, wording) pairs numbered AC1, AC2, ...

    Two shapes are read. A Markdown document (anything with a `##` or deeper heading, which is
    how a PR body is sectioned): only the list items under its `## Acceptance criteria` heading
    count, up to the next heading, and a document without that heading has none. A plain list (no
    such headings): every list item, or every non-empty line that is not a `#` comment, is one
    criterion.
    """
    lines = text.splitlines()
    heading = re.compile(r"^#{2,6}[ \t]+")
    if any(heading.match(line) for line in lines):
        section, inside = [], False
        for line in lines:
            if CRITERIA_HEADING.match(line):
                inside = True
            elif inside and heading.match(line):
                break
            elif inside:
                section.append(line)
        items = [m.group("text").strip() for m in map(LIST_ITEM.match, section) if m]
    else:
        items = []
        for line in lines:
            match = LIST_ITEM.match(line)
            if match:
                items.append(match.group("text").strip())
            elif line.strip() and not line.lstrip().startswith("#"):
                items.append(line.strip())
    return [("AC%d" % (n + 1), wording) for n, wording in enumerate(items)]


def declares_none(text):
    """The reason given for having no acceptance criteria, or None when none is declared.

    A document declares none by writing exactly one criterion, `None: <why>`. That is a claim a
    reviewer can weigh against the diff, unlike an absent section, which is indistinguishable
    from forgetting.
    """
    items = criteria(text)
    if len(items) != 1:
        return None
    match = NONE_ITEM.match(items[0][1])
    return match.group("reason") if match else None


TICKET_NONE = ("the ticket in %s lists no acceptance criteria: its criteria go under a `## Acceptance criteria` "
               "heading (or the file is a plain list), or it declares `- None: <why>`; an empty file links no ticket")


def ticket_items(text, path="qae-inputs/ticket.md"):
    """(the ticket's criteria as (id, wording) pairs numbered TC1, TC2, ..., why they cannot be read or None).

    A consumer's workflow writes the acceptance criteria of the ticket a pull request implements to a file,
    read with the same grammar as a PR body. They are graded in addition to the pull request's own, so a
    pull request cannot drop one from its list. An empty file links no ticket; a ticket that declares
    `- None: <why>` has none. A non-empty ticket in which no criteria can be found is a problem, never no
    criteria: a section the reader missed must not drop the ticket's requirements silently.
    """
    if not text.strip() or declares_none(text):
        return [], None
    items = criteria(text)
    if not items:
        return [], TICKET_NONE % path
    return [("TC%d" % (n + 1), wording) for n, (_, wording) in enumerate(items)], None


# `[ref: home-desktop]` and `[as: admin, read-only]` inside a criterion's text, each a comma list. A
# reference key is the file stem of qae-inputs/references/<key>.png. A role is what a step line names
# after "as" and before a colon, so it holds no colon, comma or bracket. A bracket that starts like an
# annotation and does not parse is malformed, never skipped: a typo must not drop a requirement silently.
ANNOTATION = re.compile(r"\[[ \t]*(?P<kind>ref|as)[ \t]*:(?P<value>[^\]]*)\]", re.IGNORECASE)
REFERENCE_KEY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$")
ROLE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ._-]*$")


class Annotations(NamedTuple):
    references: list  # reference keys, in written order, deduplicated
    roles: list       # role names, the same
    malformed: list   # each bracket that starts like an annotation and does not parse, as written


def annotations(wording):
    """The annotations in one criterion's text."""
    found = Annotations([], [], [])
    for match in ANNOTATION.finditer(wording):
        kind = match.group("kind").casefold()
        grammar, into = (REFERENCE_KEY, found.references) if kind == "ref" else (ROLE_NAME, found.roles)
        items = [item.strip() for item in match.group("value").split(",")]
        if not all(grammar.match(item) for item in items):
            found.malformed.append(match.group(0))
            continue
        into.extend(item for item in dict.fromkeys(items) if item not in into)
    return found


# A step line that names `reference <key>` is a comparison with that design reference
# (harnesses/qae/prompt.md): its screenshot is what was compared, at the reference's width.
REFERENCE_NAMED = re.compile(r"\breference[ \t]+`?(?P<key>[A-Za-z0-9][A-Za-z0-9_-]*)", re.IGNORECASE)


def named_references(line):
    """The reference keys one step line names, as written, in written order."""
    return [match.group("key") for match in REFERENCE_NAMED.finditer(line)]


def png_size(path):
    """(width, height) from a PNG file's header, or None when the file is not a PNG."""
    with open(path, "rb") as handle:
        head = handle.read(24)
    if len(head) < 24 or head[:8] != b"\x89PNG\r\n\x1a\n" or head[12:16] != b"IHDR":
        return None
    return struct.unpack(">II", head[16:24])


# Paths no browser can see: CI configuration, this catalog's manifests, and documentation a site does
# not render. Adapted from the no-plan allowlist of a private predecessor's QAE (2026-10), which also
# exempts every `*.md`; that is narrowed here to the names below, because a `.md` elsewhere can be a
# page the site renders.
UNRENDERED_DIRS = (".github", "docs")
UNRENDERED_NAMES = {"README.md", "CLAUDE.md", "AGENTS.md", "CHANGELOG.md", "CONTRIBUTING.md", "SECURITY.md",
                    "CODEOWNERS", ".gitignore", ".gitattributes", ".editorconfig"}


def unrendered(path):
    name = path.rsplit("/", 1)[-1]
    return (path.split("/", 1)[0] in UNRENDERED_DIRS or name in UNRENDERED_NAMES
            or name.startswith((".vibe-verifier", "LICENSE")))


def not_required(text, changed, ticket=""):
    """Why a pull request needs no browser check, or None when it does.

    `text` is its body, `changed` its changed paths (a rename lists both names) and `ticket` the text of
    its ticket's criteria file, when the workflow supplies one. It needs none when it declares
    `- None: <why>`, or when it lists no criteria and every changed path is one no browser can see. A
    ticket that lists criteria, or that is not empty and lists none a reader can find, overrides both:
    the pull request cannot declare its ticket's requirements away. Criteria win over paths: a pull
    request that lists one is explored whatever it touches.
    An empty path list proves nothing, so it never exempts. A body lists criteria only under an
    `Acceptance criteria` heading: without one, the plain-list reading counts every line of prose as a
    criterion, and a short body with no headings would never be exempt (found on the first consumer
    PR, 2026-10-05).
    """
    if any(ticket_items(ticket)):
        return None
    declared = declares_none(text)
    headed = any(CRITERIA_HEADING.match(line) for line in text.splitlines())
    if declared or (headed and criteria(text)) or not changed or not all(map(unrendered, changed)):
        return declared
    return "every changed file (%d) is CI configuration or documentation no site renders" % len(changed)


def marker_re(token):
    """`<token>: <id> ...`, optionally as a list item with a CHECKED box. An unchecked box
    beside a PASS contradicts itself and is left unmatched, so it reads as missing."""
    return re.compile(r"^[ \t]*(?:[-*+][ \t]+(?:\[[xX]\][ \t]+)?)?" + re.escape(token) + r":[ \t]*(\S+)(?:[ \t]|$)",
                      re.IGNORECASE)


def check_lines(verdict, token):
    """Map casefolded criterion ids to the verdict lines that carry them."""
    pattern = marker_re(token)
    found = {}
    for line in verdict.splitlines():
        match = pattern.match(line)
        if match:
            found.setdefault(match.group(1).casefold(), []).append(line)
    return found


def evidence_text(line):
    match = PASS_RE.search(line)
    return line[match.end():].lstrip(" \t:-–—") if match else ""


def _title(text, start):
    """The `::<title>` half: runs to a backtick or an unmatched closer, else to the end. Ending
    early is safe (a prefix of the real title is still a substring); ending late is the failure."""
    depth, end = 0, start
    for index in range(start, len(text)):
        char = text[index]
        if char == "`":
            break
        if char in "([{":
            depth += 1
        elif char in ")]}":
            if depth == 0:
                break
            depth -= 1
        end = index + 1
    return text[start:end].strip().rstrip(".,;:").strip()


def anchors(line):
    """Every anchor in one check line's evidence, in written order, deduplicated."""
    text = evidence_text(line)
    found, seen, position = [], set(), 0
    while True:
        match = ANCHOR_HEAD_RE.search(text, position)
        if not match:
            return found
        position = match.end()  # never consume the title: a later `path:line` on the line still counts
        path = match.group("path")
        if match.group("start") is None:
            title = _title(text, match.end())
            if len(title) < MIN_ANCHOR_TITLE:
                continue
            anchor = Anchor(path, title, 0, 0, "%s::%s" % (path, title))
        else:
            first = int(match.group("start"))
            last = int(match.group("end")) if match.group("end") else first
            anchor = Anchor(path, "", first, last, match.group(0))
        key = (anchor.path, anchor.title, anchor.start, anchor.end)
        if key not in seen:
            seen.add(key)
            found.append(anchor)


# One whole anchor, as a feature map's Verify bullet holds it between backticks. The backticks bound
# it, so the path needs no known extension (the extension list above exists to find an anchor inside
# free prose) and the title runs to the end.
WHOLE_ANCHOR = re.compile(r"^(?P<path>[^\s`:]+)(?:::(?P<title>.+)|:(?P<start>[0-9]+)(?:-(?P<end>[0-9]+))?)$")


def parse_anchor(text):
    """The one anchor `text` is, or None: `<path>::<title>` (a title of MIN_ANCHOR_TITLE characters
    or more) or `<path>:<line>` / `<path>:<start>-<end>`."""
    match = WHOLE_ANCHOR.match(text.strip())
    if not match:
        return None
    if match.group("title") is not None:
        title = match.group("title").strip()
        return Anchor(match.group("path"), title, 0, 0, text.strip()) if len(title) >= MIN_ANCHOR_TITLE else None
    first = int(match.group("start"))
    last = int(match.group("end")) if match.group("end") else first
    return Anchor(match.group("path"), "", first, last, text.strip())


def locate(anchor, repo, tracked, artifacts):
    """The file an anchor names: a tracked path at the head, else a file under `artifacts`."""
    if anchor.path in tracked:
        return os.path.join(repo, anchor.path)
    if artifacts:
        candidate = os.path.normpath(os.path.join(artifacts, anchor.path))
        if candidate.startswith(os.path.normpath(artifacts) + os.sep) and os.path.isfile(candidate):
            return candidate
    return None


def resolves(anchor, repo, tracked, artifacts=None):
    """Why the anchor does not resolve, or None when it does: a tracked path (or one under
    `artifacts`) whose text holds the title, or whose length holds the line range."""
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
