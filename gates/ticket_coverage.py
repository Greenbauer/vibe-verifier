#!/usr/bin/env python3
"""Trace plan requirements to criteria, and ticket requirements to PR proof or an accepted owner.

    # .vibe-verifier (plan-only, read at HEAD; --plans is repeatable)
    ticket-coverage --plans 'docs/plans/*.md'
    # .vibe-verifier-qae (wired: the workflow writes the PR body)
    ticket-coverage --body qae-inputs/pr-body.md

A ticket section has a direct `Acceptance criteria` subsection. Its requirement items are the
list items before its first subheading. Tag each with `[req: P1-4]` or `[req: P1-4, G2-3a]`.
An ID starts with a letter, then letters/digits, a hyphen, digits and an optional lowercase letter.
Every ID is defined once across --plans files, and cited by an item in its own criteria, anywhere
in its direct `Not checked in a browser...` subsection, or in an own-body paragraph opening with
those words. Unknown citations and `[takes: ID, ...]`
tags fail. With `--keyed-column NAME` (repeatable), every row of a table whose header has that
column (a ledger's owner column, say) must carry a `[req: ID]` that a requirement item defines, so
a ledger row no bullet carries is a finding, not a silent gap. Globs use fnmatch over tracked HEAD paths (`*` crosses `/`); fences do not count.

A body links sections with `Ticket: docs/plans/plan.md#heading-anchor`, as actions/ticket does.
Read requirements at the merge base of the contract's base ref and HEAD. For each ID, write a
body list item with `[req: ID]` and `[proof: path]` (exists at HEAD), `[proof: path::text]` (literal
text in that file at HEAD), or `[deferred: path#heading-anchor]`. The deferred section must carry
`[takes: ID]` at the merge base: land its ownership first. Criteria alone no longer define coverage.
Proof checks existence and text, never whether a test ran or proved the requirement's meaning.

Either option may be omitted to skip that check; both can run together. No ticket or no IDs is a
qualified pass. Unreadable links, files or anchors exit 2, even with --soak. No network or model.
"""
import fnmatch
import re
import subprocess

from _contract import CannotRun, Finding, git, qualify, resolve_base, run_gate
from _ticket import (HEADING, LINK, Unreadable, find_section, headings, indexed_body_lines,
                     indexed_list_items, links, unfenced)

GATE = "ticket-coverage"
ID = r"[A-Za-z][A-Za-z0-9]*-[0-9]+[a-z]?"
TAG = re.compile(r"\[(req|takes):\s*(" + ID + r"(?:\s*,\s*" + ID + r")*)\s*\]")
CLAIM = re.compile(r"\[(proof|deferred):\s*([^\]\n]+)\]")
NOT_CHECKED = re.compile(r"^[*_ \t]*not checked in a browser", re.IGNORECASE)
ROW = re.compile(r"^[ \t]*\|")
RULE = re.compile(r"^[ \t]*\|?[ \t]*:?-+:?[ \t]*(\|[ \t]*:?-+:?[ \t]*)*\|?[ \t]*$")


def ids(text, kind="req"):
    return [value.strip() for tag, values in TAG.findall(text) if tag == kind for value in values.split(",")]


def children(lines, start, level):
    """Direct subsections, allowing skipped heading levels without adopting a grandchild's criteria."""
    previous = None
    for index, sub, title in headings(lines):
        if index <= start:
            continue
        if sub <= level:
            break
        if previous is None or sub <= previous:
            yield index, sub, title
            previous = sub


def section_text(lines, start, level):
    """Text anywhere in a section, including subsection headings, outside fences."""
    yield start, lines[start]
    for index, _ in indexed_body_lines(lines, start, level):
        yield index, lines[index]


def own_items(lines, start, level):
    section = []
    for index, line in indexed_body_lines(lines, start, level):
        if line is None:
            break
        section.append((index, line))
    return indexed_list_items(section)


def section_ids(lines, start, level):
    return [(rid, number, text) for number, text in own_items(lines, start, level) for rid in ids(text)]


def inline_not_checked(lines, start, level):
    """A paragraph of the section's own body that opens "Not checked in a browser", up to a blank line."""
    inside = False
    for index, line in indexed_body_lines(lines, start, level):
        if line is None:
            return
        inside = bool(NOT_CHECKED.match(line)) or (inside and bool(line.strip()))
        if inside:
            yield index + 1, line


def coverage_entries(lines, start, level):
    """(line number, text) of what may cite a requirement: its own criteria items, its own
    not-checked subsection, and an own-body paragraph that opens "Not checked in a browser"."""
    yield from inline_not_checked(lines, start, level)
    for index, sub, title in children(lines, start, level):
        if title.casefold() == "acceptance criteria":
            yield from indexed_list_items(indexed_body_lines(lines, index, sub))
        elif title.casefold().startswith("not checked in a browser"):
            yield from ((number + 1, text) for number, text in section_text(lines, index, sub))


def section_citations(lines, start, level):
    """Requirement citations in a section's own criteria and not-checked entries."""
    return [(rid, number) for number, text in coverage_entries(lines, start, level) for rid in ids(text)]


def requirement_findings(path, lines, start, level, cited, definitions):
    """Check tags, unique definitions and coverage for every requirement item in one ticket section."""
    findings = []
    for number, text in own_items(lines, start, level):
        required = ids(text)
        if not required:
            findings.append(Finding("requirement has no [req: ID] tag: %s" % text[:80], path, number))
        for rid in required:
            if rid in definitions:
                old_path, old_line = definitions[rid]
                findings.append(Finding("%s is defined more than once (first at %s:%d)" %
                                        (rid, old_path, old_line), path, number))
            else:
                definitions[rid] = path, number
            if rid not in cited:
                findings.append(Finding("%s has no criterion and no not-checked entry" % rid, path, number))
    return findings


def ticket_sections(lines):
    """Sections with their own direct Acceptance criteria subsection."""
    for start, level, _ in headings(lines):
        if any(title.casefold() == "acceptance criteria" for _, _, title in children(lines, start, level)):
            yield start, level


def keyed_rows(lines, columns):
    """(line number, row) of each data row of a table whose header names one of `columns`."""
    wanted = {column.strip().casefold() for column in columns}
    previous, keyed = None, False
    for index, line in unfenced(lines):
        if not ROW.match(line):
            previous, keyed = None, False
            continue
        if previous is not None and RULE.match(line):
            keyed = bool(wanted & {cell.strip().casefold() for cell in previous.strip().strip("|").split("|")})
        elif keyed:
            yield index + 1, line
        previous = line


def ledger_findings(path, lines, columns, citations):
    """A ledger row a plan assigns must name the requirement bullet that carries it, so a row with no
    bullet cannot drop out of every check. The row's tag is a citation: it must be defined."""
    findings = []
    for number, row in keyed_rows(lines, columns):
        tagged = ids(row)
        if not tagged:
            findings.append(Finding("table row has no [req: ID] tag: %s" % row.strip()[:80], path, number))
        citations.extend((rid, path, number) for rid in tagged)
    return findings


def plan_findings(documents, columns=()):
    definitions, citations, findings = {}, [], []
    for path, document in documents:
        lines = document.splitlines()
        findings += ledger_findings(path, lines, columns, citations)
        for start, level in ticket_sections(lines):
            own_citations = section_citations(lines, start, level)
            findings += requirement_findings(path, lines, start, level, {rid for rid, _ in own_citations}, definitions)
            citations.extend((rid, path, number) for rid, number in own_citations)
        # An owner may be any section, including one without browser criteria.
        citations.extend((rid, path, number + 1) for number, text in unfenced(lines) for rid in ids(text, "takes"))
    findings.extend(Finding("%s is defined nowhere in the matched plans" % rid, path, number)
                    for rid, path, number in citations if rid not in definitions)
    return findings


def read_at(repo, ref, path):
    return git(repo, "show", "%s:%s" % (ref, path))


def linked_section(repo, base, path, anchor):
    lines = read_at(repo, base, path).splitlines()
    try:
        start, level = find_section(lines, anchor)
    except Unreadable as error:
        raise CannotRun("%s#%s: %s" % (path, anchor, error))
    return lines, start, level


def repo_path(path):
    return bool(path) and not path.startswith("/") and ".." not in path.split("/")


def proof_problem(repo, value):
    path, separator, text = value.partition("::")
    if not repo_path(path):
        return "proof must name a repository path"
    result = subprocess.run(["git", "-C", repo, "cat-file", "-e", "HEAD:" + path], capture_output=True)
    if result.returncode:
        return "proof path %s does not exist at HEAD" % path
    if separator and git(repo, "cat-file", "-t", "HEAD:" + path).strip() != "blob":
        return "proof text needs a file: %s" % path
    if separator and (not text or text not in read_at(repo, "HEAD", path)):
        return "proof text %r is missing from %s at HEAD" % (text, path)
    return None


def claim_problem(repo, base, rid, kind, value):
    if kind == "proof":
        return proof_problem(repo, value)
    link = LINK.fullmatch(value)
    if not link or not repo_path(link.group("path")):
        raise CannotRun("unreadable deferred link %r: write path#heading-anchor" % value)
    lines, start, level = linked_section(repo, base, *link.groups())
    owned = [rid for _, line in section_text(lines, start, level) for rid in ids(line, "takes")]
    return None if rid in owned else "%s has no [takes: %s] at the merge base" % (value, rid)


def linked_requirements(repo, base, found):
    """The IDs owed by the linked sections, plus a qualifier for each legacy section with no IDs."""
    required, empty = {}, []
    for path, anchor in found:
        lines, start, level = linked_section(repo, base, path, anchor)
        defined = section_ids(lines, start, level)
        if not defined:
            empty.append("%s#%s: ticket section has no requirement IDs; nothing traced" % (path, anchor))
        for rid, number, text in defined:
            required[rid] = path, number, text
    return required, empty


def body_claims(body, path, required):
    """Claims in list items, and findings for unknown IDs anywhere outside fenced examples."""
    claims, findings = {}, []
    lines = list(unfenced(body.splitlines()))
    for number, text in lines:
        findings.extend(Finding("%s in the body is not defined in any linked section" % rid, path, number + 1)
                        for rid in ids(text) if rid not in required)
    section = [(number, None if HEADING.match(line) else line) for number, line in lines]
    for _, text in indexed_list_items(section):
        for rid in ids(text):
            claims.setdefault(rid, []).extend(CLAIM.findall(text))
    return claims, findings


def body_findings(args, body):
    try:
        found = links(body)
    except Unreadable as error:
        raise CannotRun(str(error))
    if not found:
        note = "no ticket linked"
        qualify(note)
        return [Finding(note, advisory=True)]
    base = git(args.repo, "merge-base", resolve_base(args.repo, args.base_ref), "HEAD").strip()
    required, empty = linked_requirements(args.repo, base, found)
    claims, findings = body_claims(body, args.body, required)
    findings += [Finding(note, advisory=True) for note in empty]
    if empty:
        qualify("; ".join(empty))
    for rid, (path, number, text) in required.items():
        problems = [claim_problem(args.repo, base, rid, kind, value.strip()) for kind, value in claims.get(rid, [])]
        if not problems or all(problem is not None for problem in problems):
            why = "; ".join(problems) if problems else "no proof or accepted deferral in a body list item"
            findings.append(Finding("%s: %s. %s; claim with [req: %s] and [proof: path] / [proof: path::text], "
                                    "or [deferred: path#anchor] whose section already [takes: %s] at the merge base"
                                    % (rid, text, why, rid, rid), path, number))
    return findings


def check(args):
    findings = []
    if args.plans:
        paths = [path for path in git(args.repo, "ls-tree", "-r", "--name-only", "-z", "HEAD").split("\0")
                 if path and any(fnmatch.fnmatchcase(path, glob) for glob in args.plans)]
        findings += plan_findings([(path, read_at(args.repo, "HEAD", path)) for path in paths], args.keyed_column or ())
    if args.body:
        try:
            with open(args.body, encoding="utf-8") as handle:
                findings += body_findings(args, handle.read())
        except OSError as error:
            raise CannotRun("--body cannot be read: %s" % error)
    return findings


def add_arguments(parser):
    parser.add_argument("--plans", metavar="GLOB", action="append", help="tracked plan paths at HEAD (fnmatch; repeatable)")
    parser.add_argument("--keyed-column", metavar="NAME", action="append", help="with --plans, every row of a table "
                        "whose header has this column must carry a [req: ID] defined by a requirement (repeatable)")
    parser.add_argument("--body", metavar="FILE", help="the PR body: linked requirements need proof or an accepted owner")


if __name__ == "__main__":
    run_gate(GATE, __doc__, check, add_arguments)
