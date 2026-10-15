#!/usr/bin/env python3
"""Hold the proof auditor's grades to the repository: every requirement a linked ticket section defines is
PROVEN by quotes that are really there, EXCLUDED to an owner that took it, or a finding.

    # the QAE verify job, after the proof-audit jobs graded the requirements (harnesses/proof-audit)
    proof-audit --body qae-inputs/pr-body.md --grades proof-audit-grades

The requirements are the `[req: ID]` items of each section a `Ticket: <plan>#<anchor>` line of the body
links, read at the merge base as ticket-coverage reads them. --grades is a directory of the grader's
`<K>.json` files (`bin/vibe-verifier proof-audit`): between them they must grade exactly those IDs, or the
gate cannot run. Per ID, the model's grade is checked, never trusted:

- PROVEN needs at least one case, each with at least one assertion. Every assertion names a file that
  exists at HEAD, a test title that occurs in that file at or above the assertion, and a line number whose
  line holds the quoted text verbatim (at least 6 characters). One quote that is not there makes the
  requirement UNPROVEN: a fabricated quote is not proof.
- EXCLUDED needs a plan line, quoted verbatim at its line number at the merge base, and an owner section
  (`path#anchor`) that carries `[takes: ID]` at the merge base and is not one this body links.
- UNPROVEN, a missing grade or a doubled one is a finding.

A shard the grader could not grade (the Codex CLI's own error: a usage limit, a timeout, output that is not
JSON) is exit 2, never a pass, and so are grades that do not match harnesses/proof-audit/schema.json. No
Ticket line, or linked sections with no IDs, is a qualified pass. --table FILE gets the per-ID table.
"""
import json
import os
import re
import subprocess

from _contract import CannotRun, Finding, git, qualify, run_gate
from _proof_audit import SCHEMA, merge_base, requirements
from _ticket import LINK, links
import ticket_coverage

GATE = "proof-audit"
MIN_QUOTE = 6
# A model usage limit as the Codex CLI words it ("You've hit your usage limit ... try again at 3:05 PM."):
# the same pattern bin/vibe-verifier's USAGE_LIMIT reads from an explorer's failure annotation.
USAGE_LIMIT = re.compile(r"You(?:'|’)ve (?:hit|reached) your (?:[\w-]+ ){0,3}limit\b", re.I)


KINDS = {"object": dict, "array": list, "string": str, "null": type(None)}


def kind_matches(value, kind):
    if kind == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    return isinstance(value, KINDS[kind])


def schema_problem(value, schema, where="grades"):
    """The first way `value` breaks the subset of JSON Schema schema.json uses, or None."""
    kinds = schema.get("type")
    kinds = [kinds] if isinstance(kinds, str) else kinds or []
    if kinds and not any(kind_matches(value, kind) for kind in kinds):
        return "%s is not %s" % (where, " or ".join(kinds))
    if "enum" in schema and value not in schema["enum"]:
        return "%s is %r, not one of %s" % (where, value, ", ".join(schema["enum"]))
    if isinstance(value, dict):
        return object_problem(value, schema, where)
    if isinstance(value, list):
        problems = (schema_problem(item, schema.get("items", {}), "%s[%d]" % (where, index)) for index, item in enumerate(value))
        return next((problem for problem in problems if problem), None)
    return None


def object_problem(value, schema, where):
    properties = schema.get("properties", {})
    missing = [key for key in schema.get("required", []) if key not in value]
    if missing:
        return "%s lacks %s" % (where, ", ".join(missing))
    extra = [key for key in value if key not in properties] if schema.get("additionalProperties") is False else []
    if extra:
        return "%s has %s" % (where, ", ".join(extra))
    problems = (schema_problem(value[key], sub, "%s.%s" % (where, key)) for key, sub in properties.items() if key in value)
    return next((problem for problem in problems if problem), None)


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
                try:
                    with open(path, encoding="utf-8") as handle:
                        found.append((path, json.load(handle)))
                except (OSError, ValueError) as error:
                    raise CannotRun("%s cannot be read: %s" % (path, error))
    if not found:
        raise CannotRun("no grades in %s: the proof-audit job graded nothing (did it run?)" % directory)
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


def quote_problem(lines, path, number, quote):
    text = quote.strip()
    if lines is None:
        return "%s is not a file in the repository" % path
    if len(text) < MIN_QUOTE:
        return "the quote %r at %s:%s is too short to check" % (quote, path, number)
    if not 1 <= number <= len(lines):
        return "%s has no line %d" % (path, number)
    if text not in lines[number - 1]:
        return "%s:%d does not hold %r (a fabricated quote)" % (path, number, text)
    return None


def proof_problem(files, proof):
    lines = files.lines("HEAD", proof["file"])
    problem = quote_problem(lines, proof["file"], proof["line"], proof["assertion"])
    if problem:
        return problem
    title = proof["test"].strip()
    above = [index for index, line in enumerate(lines[:proof["line"]], 1) if title and title in line]
    if not title or not any(title in line for line in lines):
        return "%s has no test titled %r" % (proof["file"], proof["test"])
    if not above:
        return "%s:%d comes before its test %r" % (proof["file"], proof["line"], title)
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
    problem = quote_problem(files.lines(files.base, exclusion["plan"]), exclusion["plan"], exclusion["line"], exclusion["quote"])
    if problem:
        return problem + " at the merge base"
    link = LINK.fullmatch(exclusion["owner"].strip())
    if not link or not ticket_coverage.repo_path(link.group("path")):
        return "the owner %r is not a path#anchor link" % exclusion["owner"]
    if (link.group("path"), link.group("anchor")) in linked:
        return "the owner %s is a section this pull request links, so the item is its own" % exclusion["owner"]
    try:
        lines, start, level = ticket_coverage.linked_section(files.repo, files.base, *link.groups())
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


def proof_summary(grade):
    return "; ".join("%s: %s" % (case["case"], ", ".join("`%s:%d` (%s)" % (proof["file"], proof["line"], proof["test"].strip())
                                                        for proof in case["proof"])) for case in grade["cases"])


def cell(text, limit=600):
    text = " ".join(str(text).split()).replace("|", "\\|")
    return text if len(text) <= limit else text[:limit - 1] + "…"


def table(rows, note=None):
    proven = sum(1 for _, status, _ in rows if status != "UNPROVEN")
    lines = ["## Proof audit: %s" % (note or "%d of %d requirements proven or excluded" % (proven, len(rows))), ""]
    if rows:
        lines += ["| ID | Verdict | Proof, or what is missing |", "|---|---|---|"]
        lines += ["| %s | %s | %s |" % (rid, status, cell(detail)) for rid, status, detail in rows]
    return "\n".join(lines) + "\n"


def write_table(path, text):
    if path:
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(text)


def check(args):
    try:
        with open(args.body, encoding="utf-8") as handle:
            body = handle.read()
    except OSError as error:
        raise CannotRun("--body cannot be read: %s" % error)
    items, notes = requirements(args.repo, args.base_ref, body)
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


if __name__ == "__main__":
    run_gate(GATE, __doc__, check, add_arguments)
