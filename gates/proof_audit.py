#!/usr/bin/env python3
"""Hold the proof auditor's grades to the repository: every requirement a linked ticket section defines is
PROVEN by quotes that are really there, EXCLUDED to an owner that took it, or a finding.

    # the QAE verify job, after the proof-audit jobs graded the requirements (harnesses/proof-audit)
    proof-audit --body qae-inputs/pr-body.md --grades "$RUNNER_TEMP/proof-audit-grades"

The requirements are the `[req: ID]` items of each section a `Ticket: <plan>#<anchor>` line of the body
links, read at the merge base as ticket-coverage reads them. --grades is a directory of the grader's
`<K>.json` files (`bin/vibe-verifier proof-audit`): between them they must grade exactly those IDs, or the
gate cannot run. Per ID, the model's grade is checked, never trusted:

- PROVEN needs at least one case, each with at least one assertion. Every assertion names a test file at
  HEAD (*.test.*, *.spec.*, __tests__/, node:test, or a Python test), a line whose whole text is the quote
  (at least 6 characters, or a shorter value line that continues an assert or expect call still open above it
  in the same test; never a line of closers such as `});`), and the title of the test that line sits in: the
  nearest test declaration above it (a JS test call's string literal, `void` first or not, or a Python test
  function's name). A quote at the wrong line number still counts when it is the whole text of exactly one line
  inside the test the grade names; the table gives that line, and the cited one beside it. One quote that is not
  there makes the requirement UNPROVEN: a fabricated quote is not proof.
- EXCLUDED needs a plan line, quoted verbatim (in part, 12 characters or more) at its line number at the merge base, and an owner section
  (`path#anchor`) that carries `[takes: ID]` at the merge base and is not one this body links.
- UNPROVEN, a missing grade or a doubled one is a finding.

A shard the grader could not grade (the Codex CLI's own error: a usage limit, a timeout, output that is not
JSON) is exit 2, never a pass, and so are grades that do not match harnesses/proof-audit/schema.json. No
Ticket line, or linked sections with no IDs, is a qualified pass, with two exceptions that are findings: a body
that claims `[req: ID]` items without the Ticket line that would define them, and, with --require-ticket GLOB, a
pull request that changes a matching file and neither links a section nor says why on a `No ticket: <why>`
line. --table FILE gets the per-ID table.

Grades the checkout supplied are never judged: a path the branch tracks at, under or on the way to --grades, or a
symbolic link in it, is a finding and nothing else is read (gates/_contract.py, planted). The workflow keeps the
grades in the runner's temporary directory, outside the checkout (harnesses/proof-audit/README.md).
"""
import json
import os
import posixpath
import re
import subprocess

from _acceptance import HTML_COMMENT
from _affected_graph import JS_EXTENSIONS
from _affected_tests import JS_TESTS, NODE_TEST
from _contract import PY_TESTS, CannotRun, Finding, git, matches, planted, qualify, run_gate
from _proof_audit import SCHEMA, merge_base, proof_summary, requirements, table, write_table
from _schema_subset import schema_problem
from _ticket import LINK, links, unfenced
import ticket_coverage

GATE = "proof-audit"
MIN_QUOTE = 6  # an assertion is quoted as its whole line; `});` is not one
# A line that only opens a call, `assert.equal(` or `await expect(`: the first line of a wrapped assertion,
# which names no value it checks.
OPENER = re.compile(r"^(?:await\s+)?[\w$.!?]+[(\[{]$")
# The line a wrapped assertion statement opens on: an assert or expect call, awaited or not. A value prettier
# leaves alone on a line inside one (`0,`, `[]`, `"named"`) is the line that holds the checked value, so it may be
# quoted however short; a line of closers (`});`) is never one.
ASSERTION_OPEN = re.compile(r"^(?:await\s+)?(?:assert\b|expect\s*[.(]|t\.assert|self\.assert)")
CLOSERS_ONLY = re.compile(r"^[)\]}>;,\s]*$")
STRING = re.compile(r"""(["'`])(?:\\.|(?!\1)[^\\])*\1""")
BLOCK_COMMENT = re.compile(r"/\*.*?\*/")
MIN_PART = 12  # a plan line, and a test title that is not a literal, may be quoted in part
# A test declaration: a JS test call (test(...), it(...), test.only(...), test.each(...)(...)), `void` first when a
# type-aware lint forbids a floating promise (`void it(...)`), or a Python test
# function. Its title is the call's first string literal, or the function's name. Only the modifiers that still
# register a test count: test.setTimeout(...), test.slow(), test.step(...), test.describe(...) and the hooks are
# calls inside or around tests, and taking one for the declaration would tie an assertion to no test.
DECLARATION = re.compile(r"^\s*(?:(?:void\s+)?(?:it|test|specify)(?:\.(?:only|skip|concurrent|serial|sequential|fixme|fail|failing|todo"
                         r"|each\([^)]*\)))*\s*\(|(?:async\s+)?def\s+test\w*\s*\()")
PYTHON_TEST = re.compile(r"^\s*(?:async\s+)?def\s+(test\w*)\s*\(")
TITLE = re.compile(r"""\(\s*(["'`])((?:\\.|(?!\1)[^\\])*)\1""")
# A model usage limit as the Codex CLI words it ("You've hit your usage limit ... try again at 3:05 PM."):
# the same pattern bin/vibe-verifier's USAGE_LIMIT reads from an explorer's failure annotation.
USAGE_LIMIT = re.compile(r"You(?:'|’)ve (?:hit|reached) your (?:[\w-]+ ){0,3}limit\b", re.I)
# A body's declared reason for linking no plan section, as `- None: <why>` declares no criteria.
NO_TICKET = re.compile(r"^\s*No ticket:\s*\w", re.I)  # a word, so a template's `No ticket: <why>` is no reason


ENVELOPE = {"type": "object", "required": ["shard", "ids", "error", "output", "seconds"],
            "properties": {"shard": {"type": "integer"}, "ids": {"type": "array", "items": {"type": "string"}},
                           "error": {"type": ["string", "null"]}, "output": {}, "seconds": {}}}


def envelopes(directory):
    """Every grade file under the directory (a download puts each artifact in a folder of its own)."""
    found = []
    for folder, _, names in sorted(os.walk(directory)) if os.path.isdir(directory) else []:
        for name in sorted(names):
            if re.fullmatch(r"[0-9]+\.json", name):
                path = os.path.join(folder, name)
                shown = os.path.relpath(path, directory)
                try:
                    with open(path, encoding="utf-8") as handle:
                        found.append((shown, json.load(handle)))
                except (OSError, ValueError) as error:
                    raise CannotRun("grade %s cannot be read: %s" % (shown, getattr(error, "strerror", None) or error))
    if not found:
        raise CannotRun("no grades: the proof-audit job graded nothing (did it run?)")
    return found


def checked(path, envelope, schema):
    """The envelope of a graded shard, else CannotRun: a file that is not one, or a shard the grader did not grade."""
    problem = schema_problem(envelope, ENVELOPE, path)
    if problem:
        raise CannotRun(problem)
    if envelope["error"]:
        limit = " (a model usage limit: nothing was graded; run it again after the reset)" if USAGE_LIMIT.search(envelope["error"]) else ""
        raise CannotRun("shard %d could not be graded%s: %s" % (envelope["shard"], limit, envelope["error"]))
    problem = schema_problem(envelope["output"], schema, "shard %d's output" % envelope["shard"])
    if problem:
        raise CannotRun("%s (harnesses/proof-audit/schema.json)" % problem)
    return envelope


def graded(found, wanted, schema):
    """{id: [grade, ...]} from the envelopes, which must grade exactly the wanted IDs, each in one shard."""
    owner, grades = {}, {}
    for path, envelope in found:
        checked(path, envelope, schema)
        for rid in envelope["ids"]:
            if rid in owner:
                raise CannotRun("%s is in shards %d and %d" % (rid, owner[rid], envelope["shard"]))
            owner[rid] = envelope["shard"]
        for grade in envelope["output"]["requirements"]:
            if grade["id"] in envelope["ids"]:
                grades.setdefault(grade["id"], []).append(grade)
    if set(owner) != set(wanted):
        raise CannotRun("the grades are for %s, the body's tickets define %s: grade this body again" % (
            ", ".join(sorted(owner)) or "nothing", ", ".join(wanted)))
    return grades


class Repository:
    """Files at HEAD and at the merge base, read once each, as lines numbered the way an editor numbers them."""

    def __init__(self, repo, base):
        self.repo, self.base, self.cache = repo, base, {}

    def lines(self, ref, path):
        """The file's lines at `ref`, or None when the path is not a file there."""
        key = (ref, path)
        if key not in self.cache:
            self.cache[key] = None
            if ticket_coverage.repo_path(path):
                kind = subprocess.run(["git", "-C", self.repo, "cat-file", "-t", "%s:%s" % (ref, path)],
                                      capture_output=True, text=True)
                if kind.returncode == 0 and kind.stdout.strip() == "blob":
                    self.cache[key] = git(self.repo, "show", "%s:%s" % (ref, path)).split("\n")
        return self.cache[key]


def quote_problem(lines, path, number, quote, whole):
    """Why `quote` is not at line `number` of the file (`whole`: the line's whole text, else a part of it of
    at least MIN_PART characters), or None."""
    text = quote.strip()
    if lines is None:
        return "%s is not a file in the repository" % path
    if len(text) < (MIN_QUOTE if whole else MIN_PART) and not (whole and continues_assertion(lines, number, text)):
        return "the quote %r at %s:%s is too short to check" % (quote, path, number)
    if not 1 <= number <= len(lines):
        return "%s has no line %d" % (path, number)
    if (lines[number - 1].strip() != text) if whole else (text not in lines[number - 1]):
        return "%s:%d does not hold %r%s (a fabricated quote)" % (path, number, text, " as its whole line" if whole else "")
    if whole and OPENER.match(text):
        return "%s:%d only opens a call (%r): quote the line that holds the value it checks" % (path, number, text)
    return None


def closes_on_its_line(line):
    """Whether a JS test declaration line also closes its call: a one-line test ends `)` or `);`, read with its
    block comments dropped, and again with its strings and a trailing `//` comment dropped too (a multi-line
    declaration ends with the `{`, `(` or `=>` that opens its body)."""
    raw = BLOCK_COMMENT.sub("", line).rstrip()
    bare = STRING.sub("", raw).split("//", 1)[0].rstrip()
    if bare.endswith(("{", "(", "=>", ",", "[")):
        return False  # it opens a body on the next lines, whatever a trailing comment ends with
    uncommented = raw.rsplit(" //", 1)[0].rstrip()
    return any(text.endswith((")", ");")) for text in (raw, uncommented, bare))


def bracket_depth(text):
    """Openers minus closers on a line, string literals and a trailing `//` comment left out."""
    text = STRING.sub("", text).split("//", 1)[0]
    return sum(text.count(c) for c in "([{") - sum(text.count(c) for c in ")]}")


def continues_assertion(lines, number, text):
    """Whether line `number`, whose whole text is `text`, is a value inside a wrapped assertion: walking up within
    the same test, the nearest statement opening above it is an assert or expect call, and that call stays open on
    every line from its own down to the one above `number` (a closed assert, with or without a `;`, ends there)."""
    if not 1 <= number <= len(lines) or lines[number - 1].strip() != text or CLOSERS_ONLY.match(text):
        return False
    for index in range(number - 2, -1, -1):
        line = lines[index].strip()
        if DECLARATION.match(lines[index]) or line.endswith(";"):
            return False
        if ASSERTION_OPEN.match(line):
            depth = 0
            for below in lines[index:number - 1]:
                depth += bracket_depth(below.strip())
                if depth <= 0:
                    return False
            return True
    return False


def is_test_file(path, lines):
    """A test as the affected-tests selection knows one: *.test.*, *.spec.*, __tests__/, a JS or TS file that
    imports node:test, or a Python test file."""
    if path.endswith(JS_EXTENSIONS):
        return matches(path, JS_TESTS) or any(NODE_TEST.search(line) for line in lines)
    return matches(path, PY_TESTS)


def enclosing_test(lines, number):
    """The nearest test declaration above line `number`, with the two lines after it (a long title is
    wrapped onto the next line), or None. An assertion belongs to the test it sits in."""
    for index in range(number - 2, -1, -1):
        if DECLARATION.match(lines[index]):
            return "\n".join(lines[index:index + 3])
    return None


def declared_title(declaration):
    """The title a declaration gives its test: a Python test function's name, or the string literal a JS
    test call opens with; None when the title is not a literal (a variable)."""
    python = PYTHON_TEST.search(declaration)
    if python:
        return python.group(1)
    literal = TITLE.search(declaration)
    return literal.group(2) if literal else None


def test_body(lines, title):
    """(first, last), the 1-based lines of the one test titled `title`: its declaration down to the line that closes
    it. None when no test, or more than one, carries that title. A body never reaches past the first non-blank line
    indented no deeper than the declaration (a JS test's closing `});`, or a Python test's dedent), so a bracket the
    count misreads in a comment, a regex or a template cannot stretch it over a following hook or helper; a JS
    body also ends where the declaration's call closes, on the declaration's own line for a one-line test."""
    starts = [index for index, line in enumerate(lines)
              if DECLARATION.match(line) and declared_title("\n".join(lines[index:index + 3])) == title]
    return (starts[0] + 1, body_end(lines, starts[0])) if len(starts) == 1 else None


def body_end(lines, start):
    """The 1-based last line of the test declared at index `start` (see test_body)."""
    indent = len(lines[start]) - len(lines[start].lstrip())
    dedent = next((index for index in range(start + 1, len(lines))
                   if lines[index].strip() and len(lines[index]) - len(lines[index].lstrip()) <= indent), len(lines))
    if PYTHON_TEST.match(lines[start]):
        return dedent
    if dedent < len(lines) and CLOSERS_ONLY.match(lines[dedent].strip()):
        dedent += 1
    if closes_on_its_line(lines[start]):
        return start + 1  # a one-line test, whatever a bracket inside a regex or comment says
    depth = 0
    for index in range(start, dedent):
        depth += bracket_depth(lines[index].strip())
        if depth <= 0:
            return index + 1
    return dedent


def relocated_line(lines, proof):
    """The line a quote cited at the wrong number really sits on: its one whole-line occurrence inside the test the
    grade names. None when it is not there, is there twice, or sits only outside that test (a hook, a helper,
    another test)."""
    body = test_body(lines, proof["test"].strip())
    text = proof["assertion"].strip()
    if body is None or not text:
        return None
    first, last = body
    hits = [number for number in range(first + 1, last + 1) if lines[number - 1].strip() == text]
    return hits[0] if len(hits) == 1 else None


def proof_problem(files, proof):
    path = posixpath.normpath(proof["file"].strip())
    lines = files.lines("HEAD", path)
    problem = quote_problem(lines, path, proof["line"], proof["assertion"], whole=True)
    if problem and lines is not None and is_test_file(path, lines):
        # The grader often has the right assertion at the wrong line number; the quote, not the number, is the proof.
        moved = relocated_line(lines, proof)
        if moved and moved != proof["line"] and not quote_problem(lines, path, moved, proof["assertion"], whole=True):
            proof["cited_line"], proof["line"], problem = proof["line"], moved, None
    if problem:
        return problem
    if not is_test_file(path, lines):
        return "%s is not a test file" % path
    declaration = enclosing_test(lines, proof["line"])
    if declaration is None:
        return "no test is declared above %s:%d" % (path, proof["line"])
    title, declared = proof["test"].strip(), declared_title(declaration)
    if declared is not None and title != declared:
        return "%s:%d is in the test %r, not %r" % (path, proof["line"], declared, title)
    if declared is None and (len(title) < MIN_PART or title not in declaration):
        return "the test around %s:%d is not titled %r" % (path, proof["line"], title)
    return None


def proven_problem(files, grade):
    if not grade["cases"]:
        return "graded PROVEN with no case"
    for case in grade["cases"]:
        if not case["proof"]:
            return "graded PROVEN, but the case %r has no assertion" % case["case"]
        for proof in case["proof"]:
            problem = proof_problem(files, proof)
            if problem:
                return problem
    return None


def excluded_problem(files, grade, linked):
    exclusion = grade["exclusion"]
    if not exclusion:
        return "graded EXCLUDED with no exclusion"
    plan = posixpath.normpath(exclusion["plan"].strip())
    problem = quote_problem(files.lines(files.base, plan), plan, exclusion["line"], exclusion["quote"], whole=False)
    if problem:
        return problem + " at the merge base"
    link = LINK.fullmatch(exclusion["owner"].strip())
    if not link or not ticket_coverage.repo_path(link.group("path")):
        return "the owner %r is not a path#anchor link" % exclusion["owner"]
    owner = (posixpath.normpath(link.group("path")), link.group("anchor"))
    if owner in {(posixpath.normpath(path), anchor) for path, anchor in linked}:
        return "the owner %s is a section this pull request links, so the item is its own" % exclusion["owner"]
    try:
        lines, start, level = ticket_coverage.linked_section(files.repo, files.base, *owner)
    except CannotRun as error:
        return "the owner cannot be read: %s" % error
    taken = [rid for _, line in ticket_coverage.section_text(lines, start, level) for rid in ticket_coverage.ids(line, "takes")]
    return None if grade["id"] in taken else "%s carries no [takes: %s] at the merge base" % (exclusion["owner"], grade["id"])


def verdict(files, grades, linked):
    """(status, detail) for one requirement: the grade's own, or UNPROVEN with the reason a check failed."""
    if not grades:
        return "UNPROVEN", "the grader returned no grade for it"
    if len(grades) > 1:
        return "UNPROVEN", "the grader graded it %d times" % len(grades)
    grade = grades[0]
    if grade["status"] == "PROVEN":
        problem = proven_problem(files, grade)
        return ("UNPROVEN", problem) if problem else ("PROVEN", proof_summary(grade))
    if grade["status"] == "EXCLUDED":
        problem = excluded_problem(files, grade, linked)
        return ("UNPROVEN", problem) if problem else ("EXCLUDED", "%s (%s:%d: %s)" % (
            grade["exclusion"]["owner"], grade["exclusion"]["plan"], grade["exclusion"]["line"], grade["exclusion"]["quote"].strip()))
    return "UNPROVEN", grade["missing"].strip() or "the grader named nothing missing"


def unlinked(args, body):
    """The finding for a body that links no Ticket section yet must: one that claims `[req: ID]` items anyway
    (the section that defines them is unlinked, so none would be audited), or, with --require-ticket, one whose
    pull request changes a matching file and gives no `No ticket: <why>` line."""
    if links(body):
        return []
    lines = [line for _, line in unfenced(HTML_COMMENT.sub("", body).splitlines())]  # a template's example claims nothing
    claimed = list(dict.fromkeys(rid for line in lines for rid in ticket_coverage.ids(line)))
    if claimed:
        return [Finding("the body claims %d requirement ID(s) (%s) but no `Ticket: <plan>#<anchor>` line links the "
                        "section that defines them, so none of them is audited: add that line"
                        % (len(claimed), ", ".join(claimed)))]
    if not args.require_ticket or any(NO_TICKET.match(line) for line in lines):
        return []
    base = merge_base(args.repo, args.base_ref)
    changed = [path for path in git(args.repo, "diff", "--name-only", "--no-renames", "-z", base, "HEAD").split("\0")
               if path and matches(path, args.require_ticket)]
    if not changed:
        return []
    return [Finding("the pull request changes %d file(s) under --require-ticket (%s) and links no plan section: add "
                    "`Ticket: <plan>#<anchor>` for the section it implements, or a line `No ticket: <why>` saying why "
                    "none applies" % (len(changed), ", ".join(changed)))]


def check(args):
    try:
        with open(args.body, encoding="utf-8") as handle:
            body = handle.read()
    except OSError as error:
        raise CannotRun("--body cannot be read: %s" % error)
    supplied = planted(args.repo, args.grades, "the grade jobs")
    if supplied:
        write_table(args.table, table([], "nothing judged: the pull request supplies grades of its own"))
        return supplied
    items, notes = requirements(args.repo, args.base_ref, body)
    unaudited = [] if items else unlinked(args, body)
    if unaudited:
        write_table(args.table, table([], "nothing audited: %s" % unaudited[0].message))
        return unaudited
    if not items:
        qualify("; ".join(notes))
        write_table(args.table, table([], "nothing to audit (%s)" % "; ".join(notes)))
        return [Finding(note, advisory=True) for note in notes]
    with open(SCHEMA, encoding="utf-8") as handle:
        schema = json.load(handle)
    try:
        grades = graded(envelopes(args.grades), [item["id"] for item in items], schema)
    except CannotRun as reason:
        write_table(args.table, table([], "could not run: %s" % reason))
        raise
    files = Repository(args.repo, merge_base(args.repo, args.base_ref))
    linked = set(links(body))
    rows, findings = [], [Finding(note, advisory=True) for note in notes]
    for item in items:
        status, detail = verdict(files, grades.get(item["id"], []), linked)
        rows.append((item["id"], status, detail))
        if status == "UNPROVEN":
            findings.append(Finding("%s UNPROVEN: %s. Requirement: %s" % (item["id"], detail, item["text"]), item["plan"], item["line"]))
    write_table(args.table, table(rows))
    return findings


def add_arguments(parser):
    parser.add_argument("--body", required=True, metavar="FILE", help="the pull request's body: its Ticket lines name the requirements")
    parser.add_argument("--grades", required=True, metavar="DIR", help="the grader's <K>.json files (bin/vibe-verifier proof-audit)")
    parser.add_argument("--table", metavar="FILE", help="write the per-ID table, in Markdown, here")
    parser.add_argument("--require-ticket", action="append", default=[], metavar="GLOB",
                        help="a pull request that changes a file matching GLOB (repeatable) must link a Ticket section or "
                        "say why none applies on a `No ticket: <why>` line")


if __name__ == "__main__":
    run_gate(GATE, __doc__, check, add_arguments)
