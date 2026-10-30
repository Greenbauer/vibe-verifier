"""A plan kept in a plan store, outside the repository: the `[plan:<id>]` markers that name one, its read from the
store, and the requirements the proof audit cuts from its wording.

A store answers `GET <endpoint>/plans/<id>` with JSON `{planMarkdown, amendments: [{body}]}`, the amendments oldest
first, given a bearer token. actions/ticket makes a named plan's Acceptance items the ticket's criteria; the proof
audit (gates/_proof_requirements.py) grades its whole wording, cut here into requirements:

- every list item (nested items and continuation lines joined to their parent, as a `Ticket:` section's items are)
  and every paragraph of planMarkdown, outside code fences, in the order written: `PL-1`, `PL-2`, ...;
- then each amendment's, oldest first: `AM1-1`, `AM1-2`, ... for the first. An amendment with no list item and no
  paragraph at all (only a heading, or only a code block) is one requirement, its whole text.

Not requirements: a heading; a label, a line that is bold and nothing else and no sentence
(`**Added acceptance criteria**`, where `**Prices never show in dollars.**` is a paragraph); a closed HTML comment;
everything under an aside heading (its subsections with it), whose text is one of the phrases Why, Problem(s),
Background, Context, Repo baseline, Out of scope or Non-goals, in any case and emphasis or none (`## **Why**`), with
nothing after it but punctuation (a dash after a space) and what that opens (`## Why (verified on ...)`,
`## Repo baseline: ...`; never `## Context menu` or `## Context-aware search`), though never under the plan's title, a
`# ...` heading before any other; the one list, paragraph or code block directly under an aside label
(`**Background**`); and an item or paragraph that opens with the label `Out of scope:` or a bold `**Out of scope`
(never `Out-of-scope requests return 403.`). A second plan one pull request names numbers its IDs `P2PL-1`,
`P2AM1-1`, a third `P3PL-1`, so no two plans share an ID. Every ID fits ticket-coverage's grammar (a letter,
letters or digits, a hyphen, digits).
"""
import json
import re
import urllib.error
import urllib.parse
import urllib.request

from _acceptance import HTML_COMMENT
from _ticket import HEADING, ITEM, Unreadable, headings, indexed_list_items, unfenced

PLAN_MARKER = re.compile(r"\[plan:[ \t]*(?P<id>[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12})[ \t]*\]")
# A line in bold and nothing else: `**Added acceptance criteria**`. An amendment marks its criteria with one for
# actions/ticket; the proof audit reads one that is no sentence as a label (label()).
BOLD_LINE = re.compile(r"^[ \t]*(\*\*|__)(?P<text>[^*_].*?)\1[ \t]*:?[ \t]*$")
# A body's `Split off: <IDs> -> <owner>` line (gates/_proof_requirements.py), as a list item or not: a `[plan:<id>]`
# on it names the plan that takes those requirements, never one this pull request implements. A `Split off:` line
# with no `->` is prose. SPLIT_START is shared with the line's full grammar there.
SPLIT_START = r"^[ \t]*(?:(?:[-*+]|[0-9]+[.)])[ \t]+)?(?i:split[ \t]+off):"
SPLIT_LINE = re.compile(SPLIT_START + r".*->")
# An aside heading or label: the whole phrase, then nothing but punctuation (a bracket, a colon, a dash after a
# space) and what it opens. A hyphen against the word joins it to another (`Context-aware search`): no aside.
ASIDE = re.compile(r"^(?:why|problems?|background|context|repo(?:sitory)? baseline|out[- ]of[- ]scope|non[- ]goals?)"
                   r"(?:[ \t]*(?:$|[(\[:?!.,;])|[ \t]+[\u2013\u2014-])", re.IGNORECASE)
# An item or paragraph that opens with the label `Out of scope:`, or a bold `**Out of scope**` / `**Out of scope:**`.
OUT_OF_SCOPE = re.compile(r"^(?:out[- ]of[- ]scope[ \t]*:|(\*\*|__)out[- ]of[- ]scope[ \t]*(?::|\1))", re.IGNORECASE)
# A closed HTML comment in a plan's text, read across code fences too. One left open is text, so a fenced example's
# `<!--` keeps what follows it unless a `-->` comes later in the plan.
PLAN_COMMENT = re.compile(r"<!--.*?-->", re.DOTALL)


def plan_ids(*texts):
    """The plan ids `[plan:<id>]` markers name in `texts`, outside code fences, HTML comments and split-off lines
    (SPLIT_LINE), in order, once each."""
    found = []
    for text in texts:
        for _, line in unfenced(HTML_COMMENT.sub("", text).splitlines()):
            if not SPLIT_LINE.match(line):
                found += [match.group("id").lower() for match in PLAN_MARKER.finditer(line)]
    return list(dict.fromkeys(found))


class NoRedirect(urllib.request.HTTPRedirectHandler):
    """Follow no redirect: urllib would send the store's bearer token on to wherever one points, another host too."""

    def redirect_request(self, *args):
        return None


def fetch_plan(endpoint, plan_id, token):
    """The plan store's JSON for one plan, over http(s) only, from the endpoint itself: a redirect is Unreadable."""
    if urllib.parse.urlsplit(endpoint).scheme not in ("https", "http"):
        raise Unreadable("the plan store endpoint is not an http(s) URL: %s" % endpoint)
    request = urllib.request.Request("%s/plans/%s" % (endpoint.rstrip("/"), plan_id),
                                     headers={"Authorization": "Bearer " + token, "Accept": "application/json"})
    try:
        with urllib.request.build_opener(NoRedirect).open(request, timeout=30) as response:
            plan = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        moved = " (a redirect, which is not followed: give the store's own address)" if 300 <= error.code < 400 else ""
        raise Unreadable("the plan store answered %d%s" % (error.code, moved))
    except (urllib.error.URLError, OSError, ValueError) as error:
        raise Unreadable("the plan store could not be read: %s" % error)
    if not isinstance(plan.get("planMarkdown"), str) or not isinstance(plan.get("amendments", []), list):
        raise Unreadable("the plan store's answer has no planMarkdown or amendments list")
    return plan


def stored_plans(title, body, endpoint, token):
    """[(plan id, the store's JSON)] of each plan the title or body names, or [] when no store is named. A marker
    with no token, or a plan the store does not give, is Unreadable: a plan never read never reads as none."""
    found = plan_ids(title, body) if endpoint else []
    if found and not token:
        raise Unreadable("the pull request names plan %s, and the step has no token for the plan store "
                         "($VV_PLANS_TOKEN)" % found[0])
    read = []
    for plan_id in found:
        try:
            read.append((plan_id, fetch_plan(endpoint, plan_id, token)))
        except Unreadable as error:
            raise Unreadable("plan %s: %s" % (plan_id, error))
    return read


def lead(text):
    """The text from its first letter on, without emphasis: `**Why**` and `_Background_` read as the words, and a
    heading's number or other punctuation before them is dropped."""
    return re.sub(r"^[^A-Za-z]+", "", re.sub(r"[*_]+", "", text))


def label(line):
    """The text of a line that is bold and nothing else and no sentence (it ends in no `.`, `!` or `?`), else None."""
    bold = BOLD_LINE.match(line)
    return bold.group("text") if bold and not re.search(r"[.!?]$", bold.group("text").strip()) else None


def opened(stack, heading, first):
    """The (level, aside) headings a line sits under once `heading` opens. The plan's title, its `# ...` heading when
    that is the `first` it has, is never an aside."""
    level = len(heading.group("marks"))
    aside = not (first and level == 1) and bool(ASIDE.match(lead(heading.group("text"))))
    return [entry for entry in stack if entry[0] < level] + [(level, aside)]


def aside_label(marked):
    """"label" when `marked`, a label's text or None, is an aside, so the one block under it is left out; else None."""
    return "label" if marked is not None and ASIDE.match(lead(marked)) else None


def under_label(block, blank, line):
    """The block directly under an aside label that `line` is in: "list" or "paragraph", the first it opens (`block`
    "label"), or None once it is past it. A paragraph ends at a blank line or an item; a list, loose or not, at a
    line after a blank line that is no item and not indented."""
    if not line.strip() or block is None:
        return block
    if block == "label":
        return "list" if ITEM.match(line) else "paragraph"
    if block == "paragraph":
        return None if blank or ITEM.match(line) else block
    return None if blank and not ITEM.match(line) and line == line.lstrip() else block


def past_fence(block, fence):
    """The block under an aside label once a code fence opening `fence` is skipped: the fence is the block directly
    under the label, and it ends a block of one, unless it is indented inside the list there."""
    return block if block == "list" and fence != fence.lstrip() else None


def outline(lines, titled):
    """The (index, line) pairs of `lines` outside code fences that indexed_list_items reads: a heading or a label as
    None, a skipped code fence as a blank line, and none under an aside heading, or in the one block directly under
    an aside label. With `titled`, a first heading of `# ...` is the plan's title, never an aside."""
    stack, under, last, blank = [], None, -1, False
    for index, line in unfenced(lines):
        if index != last + 1:
            blank, under = True, past_fence(under, lines[last + 1])
            yield index - 1, ""  # a code fence ends a paragraph, as a blank line does
        last, heading, marked = index, HEADING.match(line), label(line)
        under = under_label(under, blank, line)
        if heading:
            stack = opened(stack, heading, titled and not stack)
        if heading or marked is not None:
            under = aside_label(marked)
            yield index, None
        elif not line.strip() or not (under or any(aside for _, aside in stack)):
            yield index, line
        blank = not line.strip()


def wording(lines, titled=False):
    """(line number, text) of each list item and paragraph of `lines` that is a requirement (the module's rules).
    With `titled`, a first heading of `# ...` is the plan's title."""
    return [(number, text) for number, text in indexed_list_items(list(outline(lines, titled)), paragraphs=True)
            if not OUT_OF_SCOPE.match(text)]


def has_wording(lines):
    """Whether `lines` hold a list item or a paragraph at all, before any rule leaves one out."""
    return any(line.strip() and not HEADING.match(line) and label(line) is None for _, line in unfenced(lines))


def heading_above(lines, number, default):
    """The text of the nearest heading above line `number`, or `default` when there is none."""
    found = default
    for index, _, text in headings(lines):
        if index >= number - 1:
            break
        found = text
    return found


def cut(prefix, source, section, text, default, whole=False):
    """The requirements of one text (a plan's Markdown, or with `whole` an amendment's), numbered `<prefix>-<n>`.
    An HTML comment is blanked first, its lines kept, so every line number stays the text's own."""
    text = PLAN_COMMENT.sub(lambda comment: "\n" * comment.group(0).count("\n"), text)
    lines = text.splitlines()
    pieces = wording(lines, titled=not whole)
    if whole and text.strip() and not has_wording(lines):
        pieces = [(1, " ".join(text.split()))]
    return [{"id": "%s-%d" % (prefix, number), "text": words, "plan": source, "line": line, "section": section,
             "heading": heading_above(lines, line, default)} for number, (line, words) in enumerate(pieces, 1)]


def plan_requirements(plans):
    """Each requirement, {id, text, plan, line, section, heading}, of the plans read from the store, [(plan id, its
    JSON)] in the order named, cut by the module's rules. A plan that yields none is Unreadable: a plan with
    nothing to grade never reads as graded."""
    found = []
    for number, (plan_id, plan) in enumerate(plans, 1):
        prefix, source = "" if number == 1 else "P%d" % number, "plan:" + plan_id
        items = cut(prefix + "PL", source, source, plan["planMarkdown"], "the top of the plan")
        for count, amendment in enumerate(plan.get("amendments") or [], 1):
            items += cut("%sAM%d" % (prefix, count), source, "%s, amendment %d" % (source, count),
                         str(amendment.get("body") or ""), "Amendment %d" % count, whole=True)
        if not items:
            raise Unreadable("plan %s has no list item or paragraph to grade" % plan_id)
        found += items
    return found
