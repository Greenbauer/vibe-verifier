#!/usr/bin/env python3
"""Fail on UI copy a pull request adds that is not plain English, as a model graded it and the record shows it.

A wired gate over the directory the QAE explore job uploads (harnesses/qae/). The harness's browser hook
(actions/qae-browser/ui-audit.js) records every sentence and label each page the explorer opened shows, to
copy.jsonl; `bin/vibe-verifier copy-audit --grade-only` has a model grade every block against the rules in
harnesses/copy-audit/prompt.md (answer first, short, everyday words, no system terms) and writes copy-grades.json
beside it (gates/_copy_audit.py). The model's grade is checked, never trusted:

- Every block on the declared site is graded, in a shard that ran: a block with no grade, a grade of a block its
  shard was not given, or a shard the grader could not run (the Codex CLI's own error: a usage limit, a timeout,
  output that is not JSON or not the schema) cannot run, never a pass. A block graded more than once fails when
  any of its grades is a counted FAIL.
- A FAIL counts only when its quote is in that block's text as the page showed it. A FAIL whose quote is not
  there is dropped, and said so: a fabricated quote is not evidence.
- A counted FAIL blocks only when the pull request adds that copy: four consecutive whole words of the block are on
  a line `git diff <base>...HEAD` adds, or, for a shorter label, the whole label as a string, an element's text or a
  line of its own. A failing block the base already had is
  listed as advisory, so a cleanup pull request can work from the list; --all makes every one block.

    --artifacts DIR     the run's artifact directory (required)
    --site-file FILE    judge only pages under the URLs the explore job's site step declared (one a line)
    --site URL-PREFIX   the same, one URL given here
    --allow REGEX       skip a block whose text or element path matches (repeatable): for wording the repository
                        chose, named in its own QAE manifest
    --all               every failing block blocks, not only the copy the pull request adds

Step logs and no copy.jsonl cannot run (the hook was not loaded), and so does a record with no copy-grades.json
beside it (the grade step did not run). The same text on several pages is one finding.
"""
import html
import json
import os
import re

from _contract import CannotRun, Finding, git, resolve_base, run_gate
from _copy_audit import GRADES, RECORD, SCHEMA, blocks
from _schema_subset import schema_problem
from qae_artifacts import declared_sites
from ui_audit import on_site, read_lines, record_paths

GATE = "copy-audit"
RUN = 4  # consecutive words of a block on an added line that make it the pull request's copy
QUOTES = str.maketrans({"‘": "'", "’": "'", "“": '"', "”": '"', " ": " "})
ENVELOPE = {"type": "object", "required": ["shard", "ids", "error", "output", "seconds"],
            "properties": {"shard": {"type": "integer"}, "ids": {"type": "array", "items": {"type": "string"}},
                           "error": {"type": ["string", "null"]}, "output": {}, "seconds": {}}}


def normal(text):
    """Text as a diff line and a page both can hold it: entities decoded, curly quotes straight, one space, lower case."""
    return " ".join(html.unescape(text).translate(QUOTES).split()).lower()


def schema():
    with open(SCHEMA, encoding="utf-8") as handle:
        return json.load(handle)


def read_grades(folder, root):
    """copy-grades.json beside one record, and its path for messages."""
    path = os.path.join(folder, GRADES)
    try:
        with open(path, encoding="utf-8") as handle:
            return json.load(handle), os.path.relpath(path, root)
    except OSError:
        raise CannotRun("%s holds a copy record and no %s: grade it with bin/vibe-verifier copy-audit --grade-only "
                        "(harnesses/copy-audit/README.md)" % (os.path.relpath(folder, root) or ".", GRADES))
    except ValueError as error:
        raise CannotRun("%s is not JSON: %s" % (os.path.relpath(path, root), error))


def shard_grades(envelope, where, output_schema):
    """One shard's grades, when the shard ran and its envelope and output hold to their schemas."""
    problem = schema_problem(envelope, ENVELOPE, where + " shard")
    if problem:
        raise CannotRun(problem)
    if envelope["error"]:
        raise CannotRun("%s shard %d was not graded: %s" % (where, envelope["shard"], envelope["error"]))
    problem = schema_problem(envelope["output"], output_schema, "%s shard %d" % (where, envelope["shard"]))
    if problem:
        raise CannotRun(problem)
    stray = [grade["id"] for grade in envelope["output"]["blocks"] if grade["id"] not in envelope["ids"]]
    if stray:
        raise CannotRun("%s grades %s in a shard that was not given it" % (where, stray[0]))
    return envelope["output"]["blocks"]


def grades_of(folder, root, ids):
    """{id: [grade, ...]} for one record's blocks, from copy-grades.json beside it, held to the schema and to `ids`."""
    data, where = read_grades(folder, root)
    shards = data.get("shards") if isinstance(data, dict) else None
    output_schema, graded = schema(), {}
    for envelope in shards if isinstance(shards, list) else [None]:
        for grade in shard_grades(envelope, where, output_schema):
            graded.setdefault(grade["id"], []).append(grade)
    missing = sorted(set(ids) - set(graded))
    if missing:
        raise CannotRun("%s has no grade for %d block(s) of the record (%s)" % (where, len(missing), ", ".join(missing[:5])))
    return graded


def added_text(args):
    """Every line the pull request adds, normalized, one per line."""
    diff = git(args.repo, "diff", "--unified=0", "--no-color", "--no-ext-diff", resolve_base(args.repo, args.base_ref) + "...HEAD")
    return "\n".join(normal(line[1:]) for line in diff.splitlines() if line.startswith("+") and not line.startswith("+++"))


def added_by_pull_request(text, added):
    """Whether an added line holds four consecutive whole words of the block, or, for a shorter label, the whole label
    as a string, an element's text or a line of its own (`"Save"`, `>Save<`, JSX text on its own line): a label's
    word inside code (`unsaved`, `row.quote_status`) is not the label."""
    words = normal(text).split()
    if len(words) < RUN:
        label = re.escape(" ".join(words))
        return re.search(r"""(?:[>"'`]\s*|^[ \t]*)%s(?:\s*[<"'`]|[ \t]*$)""" % label, added, re.M) is not None
    return any(re.search(r"(?<!\w)%s(?!\w)" % re.escape(" ".join(words[at:at + RUN])), added)
               for at in range(len(words) - RUN + 1))


def on_declared(line, sites):
    return not sites or any(on_site(str(line.get("page", "")), site) for site in sites)


def counted(block, grades):
    """(the block's first FAIL whose quote is in its text or None, how many FAILs quoted text it does not hold)."""
    fails = [grade for grade in grades if grade["verdict"] == "FAIL"]
    held = [grade for grade in fails if grade["quote"].strip() and normal(grade["quote"]) in normal(block["text"])]
    return (held[0] if held else None), len(fails) - len(held)


def finding(text, grade, seen, added, blocking_all):
    new = added_by_pull_request(text, added)
    others = len({line.get("page") for line in seen}) - 1
    more = " (and on %d more page(s))" % others if others else ""
    return Finding("%s%s on %s%s: \"%s\". Try: \"%s\" (%s)" % (
        grade["rule"], "" if new else ", existing copy", seen[0].get("page"), more, grade["quote"], grade["rewrite"],
        seen[0].get("at")), advisory=not (new or blocking_all))


def judged(root, sites):
    """({text: its counted FAIL}, {text: the record lines that showed it}, how many FAILs were dropped), over every
    explorer's record, each held to its own grades."""
    failing, pages, dropped = {}, {}, 0
    for path in record_paths(root, RECORD):
        lines = read_lines(path, root)
        record = blocks(lines, sites)
        graded = grades_of(os.path.dirname(path), root, [block["id"] for block in record])
        for line in filter(lambda line: on_declared(line, sites), lines):
            pages.setdefault(str(line.get("text") or ""), []).append(line)
        for block in record:
            grade, unheld = counted(block, graded[block["id"]])
            dropped += unheld
            if grade:
                failing.setdefault(block["text"], grade)
    return failing, pages, dropped


def allowed(text, seen, allow):
    return any(pattern.search(text) or pattern.search(str(line.get("at") or "")) for pattern in allow for line in seen)


def check(args):
    if not os.path.isdir(args.artifacts):
        raise CannotRun("artifact directory not found: %s" % args.artifacts)
    sites = declared_sites(args.site_file) if args.site_file else [args.site] if args.site else []
    allow = [re.compile(pattern) for pattern in args.allow or []]
    failing, pages, dropped = judged(args.artifacts, sites)
    findings = [Finding("dropped %d FAIL grade(s) whose quote is not in the block's text" % dropped, advisory=True)] if dropped else []
    shown = {text: grade for text, grade in failing.items() if not allowed(text, pages[text], allow)}
    added = added_text(args) if shown else ""
    return findings + [finding(text, grade, pages[text], added, args.all) for text, grade in shown.items()]


def add_arguments(parser):
    parser.add_argument("--artifacts", metavar="DIR", required=True)
    site = parser.add_mutually_exclusive_group()
    site.add_argument("--site", metavar="URL-PREFIX", default=None)
    site.add_argument("--site-file", metavar="FILE", default=None)
    parser.add_argument("--allow", action="append", metavar="REGEX")
    parser.add_argument("--all", action="store_true")


if __name__ == "__main__":
    run_gate(GATE, __doc__, check, add_arguments)
