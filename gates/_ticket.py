"""Shared Markdown grammar for Ticket links, heading anchors and section list items."""
import re

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


def indexed_body_lines(lines, start, level):
    """The (index, line) pairs of a section, None standing for each subsection's heading."""
    for index, line in unfenced(lines):
        if index <= start:
            continue
        heading = HEADING.match(line)
        if heading and len(heading.group("marks")) <= level:
            return
        yield index, None if heading else line


def body_lines(lines, start, level):
    return (line for _, line in indexed_body_lines(lines, start, level))


def indent_of(line, item):
    return len((item.group("indent") if item else re.match(r"[ \t]*", line).group(0)).expandtabs(4))


def indexed_list_items(section):
    """Each (line number, item), nested items and continuation lines joined to their parent."""
    # `top` is the indent of the list being read (None outside one). After a blank line, only an item or an
    # indented line continues it; an unindented paragraph ends it. A subsection's heading ends it too.
    items, top, blank = [], None, False
    for index, line in section:
        if line is None or not line.strip():
            top, blank = (None, blank) if line is None else (top, True)
            continue
        item = ITEM.match(line)
        indent = indent_of(line, item)
        if item and (top is None or indent <= top):
            top = indent
            items.append((index + 1, item.group("text").strip()))
        elif top is not None and (not blank or indent > top):
            number, text = items[-1]
            items[-1] = (number, text + " " + (item.group("text") if item else line).strip())
        else:
            top = None
        blank = False
    return items


def list_items(section):
    return [text for _, text in indexed_list_items(enumerate(section))]


def section_items(document, anchor):
    """The list items of the section whose heading's anchor is `anchor` (of its own Acceptance criteria
    subsection, when it has one), one string each."""
    lines = document.splitlines()
    start, level = criteria_subsection(lines, *find_section(lines, anchor))
    items = list_items(body_lines(lines, start, level))
    if not items:
        raise Unreadable("the section #%s lists no items" % anchor)
    return items


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

