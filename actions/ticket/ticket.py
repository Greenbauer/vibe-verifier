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
"""
import argparse
import base64
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "gates"))
from _ticket import Unreadable, links, named, section_items


def fetch(repo, path, ref):
    result = subprocess.run(["gh", "api", "repos/%s/contents/%s?ref=%s" % (repo, path, ref), "--jq", ".content"],
                            capture_output=True, text=True)
    if result.returncode != 0:
        raise Unreadable("%s cannot be read at the base commit %s: %s" % (path, ref, result.stderr.strip()))
    return base64.b64decode(result.stdout).decode("utf-8")


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
