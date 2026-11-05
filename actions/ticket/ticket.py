#!/usr/bin/env python3
r"""Write the acceptance criteria of the plan section a pull request names, for actions/criteria's `ticket`.

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

A spec kept in a plan store rather than the repository (a plan a requester submits, with amendments added
later) is read too when the consumer names the store:

    --plans-endpoint URL   a store that answers `GET <URL>/plans/<id>` with JSON `{planMarkdown, amendments:
                           [{body}]}`, the amendments oldest first, given the bearer token in $VV_PLANS_TOKEN
    --title TEXT           the pull request's title, which may carry the marker as the body may

A `[plan:<id>]` marker in the title or body (outside code fences, HTML comments and `Split off: ... -> ...` lines, a
list item or not, whose plan is another owner's) names a plan; gates/_plan_store.py reads it from the store,
following no redirect. Its criteria are the list items under each plan heading that starts "Acceptance" (with no
list item under such a heading, the plan's whole wording as the proof audit cuts it: every list item and paragraph
outside its title and asides), then, for each amendment in order, the items under its own "Acceptance criteria"
heading or bold line, or, when it has none, the whole amendment as one criterion: an amendment is a requirement
however it is written. An amendment that changes what the plan asked, rather than adding to it, says so on a line of
its own, `Replaces plan criterion 2.` (or `Replaces plan criteria 2 and 5.`; a list item, bold or `#2` read alike),
numbering the plan's own criteria as listed above, 1 first: its criteria then stand where the first criterion it still
holds stood, and every criterion it names is dropped, so the requester's newer wording is graded instead of the one it
overrides and the later criteria keep their numbers when it replaces one with one. An amendment of that line alone
retires them. The newest amendment to name a criterion wins it, and an amendment whose every named criterion a newer
one took is dropped with them. Only the plan store's amendments carry the line, never the pull request: the requester
changes the requirement, the author cannot. A number the plan has no criterion for, or a line that starts "Replaces
plan criteri" but does not parse, exits 2.
They follow any `Ticket:` section's criteria. A plan with nothing to walk at all, a store
that does not answer 200, or a marker with no token exits 2: a plan the step could not read never reads as "no
ticket". The store is read when the step runs, so a plan amended later is graded on the next run.
"""
import argparse
import base64
import os
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "gates"))
from _plan_store import BOLD_LINE, PLAN_COMMENT, stored_plans, wording
from _ticket import (HEADING, Unreadable, headings, indexed_body_lines, indexed_list_items, links, named,
                     section_items, unfenced)

PLAN_SECTION = re.compile(r"^acceptance\b", re.IGNORECASE)
# An amendment marks its criteria with a heading or a line in bold (BOLD_LINE): `**Added acceptance criteria**`.
CRITERIA_WORDS = re.compile(r"acceptance criteria", re.IGNORECASE)


def fetch(repo, path, ref):
    result = subprocess.run(["gh", "api", "repos/%s/contents/%s?ref=%s" % (repo, path, ref), "--jq", ".content"],
                            capture_output=True, text=True)
    if result.returncode != 0:
        raise Unreadable("%s cannot be read at the base commit %s: %s" % (path, ref, result.stderr.strip()))
    return base64.b64decode(result.stdout).decode("utf-8")


def plan_body_items(markdown):
    """The list items under each heading of the plan that starts "Acceptance", subsections included. A plan with no
    list item under such a heading gives its whole wording instead, cut as the proof audit cuts it
    (_plan_store.wording): every list item and paragraph outside its title and asides, so a plan written before it
    had an Acceptance section is walked on everything it asks for, never refused."""
    lines = markdown.splitlines()
    items = []
    for start, level, title in headings(lines):
        if PLAN_SECTION.match(title):
            items += [text for _, text in indexed_list_items(indexed_body_lines(lines, start, level))]
    if items:
        return items
    blanked = PLAN_COMMENT.sub(lambda comment: "\n" * comment.group(0).count("\n"), markdown)
    return [text for _, text in wording(blanked.splitlines(), titled=True)]


def marked_block(lines):
    """The (index, line) pairs after an amendment's "Acceptance criteria" heading or bold line, up to the next
    heading or bold line, or None when it has no such marker."""
    block = None
    for index, line in unfenced(lines):
        marker = HEADING.match(line) or BOLD_LINE.match(line)
        if marker and block is not None:
            break
        if marker and CRITERIA_WORDS.search(marker.group("text")):
            block = []
        elif block is not None:
            block.append((index, line))
    return block


# An amendment's line `Replaces plan criterion 2.` / `Replaces plan criteria 2, 3 and 5.` (see the docstring), as a
# list item, heading or quote or not, in bold or italics or not. A line that starts so but does not parse is
# Unreadable (STARTS), so a replacement written slightly differently never quietly becomes one more criterion.
REPLACES = re.compile(r"^[ \t]*(?:(?:[-*+]|[0-9]+[.)]|#{1,6}|>)[ \t]+)?(?P<em>\*\*|__|\*|_)?[ \t]*replaces[ \t]+plan[ \t]+"
                      r"criteri(?:on|a)[ \t]+(?P<numbers>#?[0-9]+(?:[ \t]*(?:,[ \t]*and|,|and|&)[ \t]*#?[0-9]+)*)"
                      r"[ \t]*[.:;]?[ \t]*(?:(?P=em))?[ \t]*[.:;]?[ \t]*$", re.IGNORECASE)
STARTS = re.compile(r"^[ \t]*(?:(?:[-*+]|[0-9]+[.)]|#{1,6}|>)[ \t]+)?(?:\*\*|__|\*|_)?[ \t]*replaces[ \t]+plan[ \t]+criteri",
                    re.IGNORECASE)


def replaced(body):
    """(the plan criteria, numbered from 1, an amendment's `Replaces plan criterion N.` lines name, its body without
    those lines). A line in a code fence is an example, not a replacement; one that starts like a replacement but does
    not parse is Unreadable."""
    lines = body.splitlines()
    outside = dict(unfenced(lines))
    numbers, kept = [], []
    for index, line in enumerate(lines):
        match = REPLACES.match(line) if index in outside else None
        if match:
            numbers += [int(number) for number in re.findall(r"[0-9]+", match.group("numbers"))]
        elif index in outside and STARTS.match(line):
            raise Unreadable("its line `%s` names no plan criterion this step can read: write "
                             "`Replaces plan criterion 2.` or `Replaces plan criteria 2 and 5.`" % line.strip())
        else:
            kept.append(line)
    return numbers, "\n".join(kept)


def amendment_items(number, body):
    """An amendment's criteria: the items its marker introduces, else the whole amendment as one criterion."""
    block = marked_block(body.splitlines())
    items = [text for _, text in indexed_list_items(block)] if block else []
    return items or ["Amendment %d: %s" % (number, " ".join(body.split()))]


def read_amendments(plan, count):
    """([(number, the plan criteria it names, its criteria)] of a plan's amendments, oldest first, {index of a plan
    criterion: the number of the newest amendment that replaces it}), for a plan with `count` criteria."""
    owners, amendments = {}, []
    for number, amendment in enumerate(plan.get("amendments") or [], 1):
        try:
            names, body = replaced(str(amendment.get("body", "")))
        except Unreadable as error:
            raise Unreadable("amendment %d: %s" % (number, error))
        for name in names:
            if not 1 <= name <= count:
                raise Unreadable("amendment %d replaces plan criterion %d, and the plan has %d" % (number, name, count))
            owners[name - 1] = number
        # The line alone retires the criteria it names: nothing stands in their place.
        amendments.append((number, names, amendment_items(number, body) if body.strip() or not names else []))
    return amendments, owners


def in_place(number, names, found, owners):
    """An amendment's criteria where it replaces the plan's, each saying which plan criteria it still holds."""
    held = [str(name) for name in sorted(set(names)) if owners[name - 1] == number]
    whole = "Amendment %d: " % number
    return ["Amendment %d, in place of plan %s %s: %s"
            % (number, "criteria" if len(held) > 1 else "criterion", " and ".join(held),
               text[len(whole):] if text.startswith(whole) else text) for text in found]


def plan_items(plan_id, plan):
    """A plan's criteria: its Acceptance sections' items, each in place of the plan criteria an amendment replaces,
    then each other amendment's, oldest first."""
    items = plan_body_items(plan["planMarkdown"])
    if not items:
        raise Unreadable("plan %s has no Acceptance item, list item or paragraph to walk" % plan_id)
    amendments, owners = read_amendments(plan, len(items))
    criteria, placed = [], set()
    for index, item in enumerate(items):
        owner = owners.get(index)
        if owner is None:
            criteria.append(item)
        elif owner not in placed:
            placed.add(owner)
            criteria += in_place(*amendments[owner - 1], owners)
    return criteria + [text for _, names, found in amendments if not names for text in found]


def plan_criteria(body, title, endpoint, token):
    """([(source line, criteria)] of each plan the title or body names), or [] when no store is named."""
    read = []
    for plan_id, plan in stored_plans(title, body, endpoint, token):
        try:
            read.append(("- plan %s, read from the plan store" % plan_id, plan_items(plan_id, plan)))
        except Unreadable as error:
            raise Unreadable("plan %s: %s" % (plan_id, error))
    return read


def ticket(body, repo, base, names="", title="", endpoint="", token=""):
    """(the ticket file's text, how many criteria it lists): ("", 0) without a link or a plan."""
    found = links(body)
    plans = plan_criteria(body, title, endpoint, token)
    if not found and not plans:
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
    sources = ["- %s#%s, read from the base commit %s" % (path, anchor, base) for path, anchor in found]
    for source, items in plans:
        sources.append(source)
        criteria += items
    text = "\n".join(["Read from:", *sources, "", "## Acceptance criteria", "",
                      *("- " + item for item in criteria)]) + "\n"
    return text, len(criteria)


def summarize(text, path):
    """Number the criteria for the job summary, so an author knows which TCn a `ticket-proof:` line names."""
    items = text.split("## Acceptance criteria\n\n", 1)[1].splitlines() if text else []
    with open(path, "a", encoding="utf-8") as handle:
        handle.write("### The ticket's criteria\n\n%s\n" % ("\n".join(
            "- TC%d: %s" % (number, item[2:]) for number, item in enumerate(items, 1)) or "None linked."))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--body", required=True)
    parser.add_argument("--repo", required=True)
    parser.add_argument("--base", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--names", default="")
    parser.add_argument("--title", default="")
    parser.add_argument("--plans-endpoint", default="")
    args = parser.parse_args(argv)
    try:
        text, count = ticket(open(args.body, encoding="utf-8").read(), args.repo, args.base, args.names, args.title,
                             args.plans_endpoint, os.environ.get("VV_PLANS_TOKEN", ""))
    except Unreadable as error:
        print("::error::the pull request's ticket cannot be read: %s" % error, file=sys.stderr)
        return 2
    with open(args.out, "w", encoding="utf-8") as handle:
        handle.write(text)
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        summarize(text, os.environ["GITHUB_STEP_SUMMARY"])
    print("ticket: %d criteria" % count if text else "ticket: none linked")
    return 0


if __name__ == "__main__":
    sys.exit(main())
