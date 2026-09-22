"""The acceptance-verdict grammar: criteria, check lines, and evidence anchors.

Adapted from the grammar a private predecessor's QAE bots write verdicts in (2026-09). Kept: the PASS token, the marker-line form, and
the two anchor forms. Dropped: the looser "substantive" and "resolvable token" bars that the
anchor bar superseded there on 2026-09-14.

A verdict line reads

    acceptance-check: AC2 -- PASS -- signup rejects an expired invite (e2e/signup.spec.ts::rejects an expired invite)

and an anchor is either `<path>::<test title>`, whose title must appear verbatim in that file, or
`<path>:<line>` / `<path>:<start>-<end>`, which must lie inside the file. An anchor names WHERE the
proof is; a bare file name does not, because a file exists whether or not the assertion cited
exists inside it.
"""
import re
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
