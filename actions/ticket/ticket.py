#!/usr/bin/env python3
"""Write the acceptance criteria of the plan section a pull request names, for actions/criteria's `ticket`.

A pull request body links its spec with one line per section:

    Ticket: docs/plans/continuation.md#b-notification-selection-and-responses

The path is a file in the repository and the anchor is the slug GitHub gives one of its Markdown headings.
The file is read at the base commit, never the head, so a pull request cannot rewrite the spec it is graded
against. Every list item of that section, up to the next heading of the same or a higher level and
including its subsections, becomes one criterion; nested items and continuation lines join their parent. A
section with an `Acceptance criteria` heading of its own, at any depth below it, gives that subsection's items
only, so a plan section can hold its sources, data model and tests beside the criteria a browser checks.

    ticket.py --body pr-body.md --repo owner/name --base <sha> --out qae-inputs/ticket.md

No `Ticket:` line writes an empty file, which links no ticket. A link that cannot be read (a malformed line,
a file absent at the base, an anchor no heading has, a section with no list items) exits 2 naming it, so a
broken link never reads as "no ticket". Reads files with `gh api` (GH_TOKEN, contents: read).

    --names REGEX   the spec files a `Ticket:` line can point at (`docs/plans/continuation\.md`, say). A body that
                    names one outside a code fence and has no `Ticket:` line exits 2: it names requirements the
                    explorer would otherwise never be given. Match only what a `Ticket:` line can link, never a
                    tracker key or a file bodies cite for other reasons. Empty (the default) asks for none.
"""
import argparse
import base64
import re
import subprocess
import sys

TICKET_LINE = re.compile(r"^[ \t]*ticket:[ \t]*(?P<value>\S.*?)[ \t]*$", re.IGNORECASE)
LINK = re.compile(r"^(?P<path>[A-Za-z0-9._][A-Za-z0-9._/-]*)#(?P<anchor>[^\s#]+)$")
FENCE = re.compile(r"^[ \t]*(```|~~~)")
HEADING = re.compile(r"^(?P<marks>#{1,6})[ \t]+(?P<text>.*?)[ \t]*#*[ \t]*$")
CRITERIA = re.compile(r"^acceptance criteria$", re.IGNORECASE)  # gates/_acceptance.py CRITERIA_HEADING's words
ITEM = re.compile(r"^(?P<indent>[ \t]*)(?:[-*+]|[0-9]+[.)])[ \t]+(?:\[[ xX]\][ \t]+)?(?P<text>\S.*)$")


class Unreadable(Exception):
    pass


def unfenced(lines):
    """The lines outside fenced code blocks, as (index, line)."""
    inside = False
    for index, line in enumerate(lines):
        if FENCE.match(line):
            inside = not inside
        elif not inside:
            yield index, line


def links(body):
    """Each `Ticket:` link in the body, as (path, anchor), in written order."""
    found = []
    for _, line in unfenced(body.splitlines()):
        match = TICKET_LINE.match(line)
        if not match:
            continue
        value = match.group("value").strip("`")
        link = LINK.match(value)
        if not link or ".." in link.group("path").split("/"):
            raise Unreadable("`Ticket: %s` is not a link: write `Ticket: <path in this repository>#<heading anchor>`" % value)
        found.append((link.group("path"), link.group("anchor")))
    return found


def slug(text):
    """GitHub's anchor for a heading: lower case, punctuation dropped, each space a hyphen."""
    text = re.sub(r"`|\*\*|__", "", text).strip().lower()
    return re.sub(r"[^\w\- ]", "", text).replace(" ", "-")


def headings(lines):
    """Each heading outside a code fence, as (index, level, text)."""
    for index, line in unfenced(lines):
        heading = HEADING.match(line)
        if heading:
            yield index, len(heading.group("marks")), heading.group("text")


def find_section(lines, anchor):
    """(index, level) of the heading whose GitHub anchor is `anchor`; a repeated heading's is numbered -1, -2."""
    seen = {}
    for index, level, text in headings(lines):
        base = slug(text)
        count = seen.get(base, 0)
        seen[base] = count + 1
        if (base if count == 0 else "%s-%d" % (base, count)) == anchor:
            return index, level
    raise Unreadable("no heading has the anchor #%s" % anchor)


def criteria_subsection(lines, start, level):
    """The section's own `Acceptance criteria` subsection, when it has one: the part a browser checks."""
    for index, sub, text in headings(lines):
        if index <= start:
            continue
        if sub <= level:
            break
        if CRITERIA.match(text):
            return index, sub
    return start, level


def body_lines(lines, start, level):
    """The lines of the section that starts at `start`, None standing for each subsection's heading."""
    for index, line in unfenced(lines):
        if index <= start:
            continue
        heading = HEADING.match(line)
        if heading and len(heading.group("marks")) <= level:
            return
        yield None if heading else line


def indent_of(line, item):
    return len((item.group("indent") if item else re.match(r"[ \t]*", line).group(0)).expandtabs(4))


def list_items(section):
    """Each list item of the section's lines, nested items and continuation lines joined to their parent."""
    # `top` is the indent of the list being read (None outside one). After a blank line, only an item or an
    # indented line continues it; an unindented paragraph ends it. A subsection's heading ends it too.
    items, top, blank = [], None, False
    for line in section:
        if line is None or not line.strip():
            top, blank = (None, blank) if line is None else (top, True)
            continue
        item = ITEM.match(line)
        indent = indent_of(line, item)
        if item and (top is None or indent <= top):
            top = indent
            items.append(item.group("text").strip())
        elif top is not None and (not blank or indent > top):
            items[-1] += " " + (item.group("text") if item else line).strip()
        else:
            top = None
        blank = False
    return items


def section_items(document, anchor):
    """The list items of the section whose heading's anchor is `anchor` (of its own Acceptance criteria
    subsection, when it has one), one string each."""
    lines = document.splitlines()
    start, level = criteria_subsection(lines, *find_section(lines, anchor))
    items = list_items(body_lines(lines, start, level))
    if not items:
        raise Unreadable("the section #%s lists no items" % anchor)
    return items


def fetch(repo, path, ref):
    result = subprocess.run(["gh", "api", "repos/%s/contents/%s?ref=%s" % (repo, path, ref), "--jq", ".content"],
                            capture_output=True, text=True)
    if result.returncode != 0:
        raise Unreadable("%s cannot be read at the base commit %s: %s" % (path, ref, result.stderr.strip()))
    return base64.b64decode(result.stdout).decode("utf-8")


def named(body, names):
    """The first text outside a code fence that matches `names`, or None (always None for an empty pattern)."""
    if not names:
        return None
    pattern = re.compile(names)
    for _, line in unfenced(body.splitlines()):
        match = pattern.search(line)
        if match:
            return match.group(0)
    return None


def ticket(body, repo, base, names=""):
    """(the ticket file's text, how many criteria it lists): ("", 0) without a link."""
    found = links(body)
    if not found:
        name = named(body, names)
        if name:
            raise Unreadable("the body names `%s` but links no section of it: add a line `Ticket: <path>#<heading anchor>` "
                             "for each section this pull request implements" % name)
        return "", 0
    criteria = []
    for path, anchor in found:
        try:
            criteria += section_items(fetch(repo, path, base), anchor)
        except Unreadable as error:
            raise Unreadable("%s#%s: %s" % (path, anchor, error))
    sources = ["- %s#%s" % link for link in found]
    text = "\n".join(["Read from the base commit %s:" % base, *sources, "", "## Acceptance criteria", "",
                      *("- " + item for item in criteria)]) + "\n"
    return text, len(criteria)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--body", required=True)
    parser.add_argument("--repo", required=True)
    parser.add_argument("--base", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--names", default="")
    args = parser.parse_args(argv)
    try:
        text, count = ticket(open(args.body, encoding="utf-8").read(), args.repo, args.base, args.names)
    except Unreadable as error:
        print("::error::the pull request's ticket cannot be read: %s" % error, file=sys.stderr)
        return 2
    with open(args.out, "w", encoding="utf-8") as handle:
        handle.write(text)
    print("ticket: %d criteria" % count if text else "ticket: none linked")
    return 0


if __name__ == "__main__":
    sys.exit(main())
