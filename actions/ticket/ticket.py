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

A `[plan:<id>]` marker in the title or body (outside code fences, HTML comments and `Split off: ... -> ...`
lines, a list item or not, whose plan is another owner's) names a plan; gates/_plan_store.py reads it from the
store, following no redirect. Its criteria
are the list items under each plan heading that starts "Acceptance", then, for each amendment in order, the
items under its own "Acceptance criteria" heading or bold line, or, when it has none, the whole amendment as
one criterion: an amendment is a requirement however it is written. They follow any `Ticket:` section's
criteria. A plan with no Acceptance section, a store that does not answer 200, or a marker with no token
exits 2: a plan the step could not read never reads as "no ticket". The store is read when the step runs, so
a plan amended later is graded on the next run.
"""
import argparse
import base64
import os
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "gates"))
from _plan_store import BOLD_LINE, stored_plans
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
    """The list items under each heading of the plan that starts "Acceptance", subsections included."""
    lines = markdown.splitlines()
    items = []
    for start, level, title in headings(lines):
        if PLAN_SECTION.match(title):
            items += [text for _, text in indexed_list_items(indexed_body_lines(lines, start, level))]
    return items


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


def amendment_items(number, body):
    """An amendment's criteria: the items its marker introduces, else the whole amendment as one criterion."""
    block = marked_block(body.splitlines())
    items = [text for _, text in indexed_list_items(block)] if block else []
    return items or ["Amendment %d: %s" % (number, " ".join(body.split()))]


def plan_items(plan_id, plan):
    """A plan's criteria: its Acceptance sections' items, then each amendment's, oldest first."""
    items = plan_body_items(plan["planMarkdown"])
    if not items:
        raise Unreadable("plan %s has no list under an Acceptance heading" % plan_id)
    for number, amendment in enumerate(plan.get("amendments") or [], 1):
        items += amendment_items(number, str(amendment.get("body", "")))
    return items


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
