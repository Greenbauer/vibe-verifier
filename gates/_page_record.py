"""The record the QAE harness keeps beside its evidence, console-pages.jsonl, as the qae-artifacts gate
reads it. actions/qae-browser/console-pages.js writes it through playwright-mcp's --init-page, one JSON
object a line, digests and never text, so no credential lands in it:

    {"page": "<URL, no query>", "sha256": "<digest of a console error's first line>"}
        a page of the run logged that console error or uncaught exception
    {"answered": "<digest of `<METHOD> <URL, fragment dropped>`>"}
        a page of the run got an answer below 400 for that request, static requests included

and, for one explorer of several (its isolation lines, which page_record skips and isolation_record reads):

    {"isolation": "<tag>"}                                    the explorer's browser guard ran
    {"created": "<record id>", "explorer": "<tag>"}           a POST of the explorer's was answered 201 for it
    {"opened": "<record id>", "explorer": "<tag>", "request": "<METHOD> <URL, no query>"}
    {"refused": "<digest of METHOD URL>", "location": "<digest of URL>", "explorer": "<tag>",
     "request": "<METHOD> <URL, no query>", "why": "<reason>"}  the harness answered 409 itself
"""
import hashlib
import json
import os

from _contract import Finding

NAME = "console-pages.jsonl"
ISOLATION_KEYS = ("isolation", "created", "opened", "refused")


def digest(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def page_record(root):
    """({digest of a console error's line: the URLs of the pages that logged it}, {digest of each
    request a page got an answer below 400 for}). A run without the record names no page and no
    answer, and a line cut short names neither."""
    pages, answers, path = {}, set(), os.path.join(root, NAME)
    if not os.path.isfile(path):
        return pages, answers
    with open(path, encoding="utf-8", errors="replace") as handle:
        for line in handle:
            try:
                entry = json.loads(line)
                if any(key in entry for key in ISOLATION_KEYS):
                    continue
                if "answered" in entry:
                    answers.add(str(entry["answered"]))
                else:
                    page = str(entry["page"])
                    pages.setdefault(entry["sha256"], []).append(page)
            except (ValueError, KeyError, TypeError):
                continue
    return pages, answers


def isolation_record(root):
    """The isolation lines of the record, each a dict with string values, in the order written. A run of one
    explorer, or one without the record, has none, and a line cut short or of another shape is skipped."""
    lines, path = [], os.path.join(root, NAME)
    if not os.path.isfile(path):
        return lines
    with open(path, encoding="utf-8", errors="replace") as handle:
        for line in handle:
            try:
                entry = json.loads(line)
            except ValueError:
                continue
            if isinstance(entry, dict) and any(key in entry for key in ISOLATION_KEYS):
                lines.append({key: str(value) for key, value in entry.items()})
    return lines


class _Refused:
    """The URLs the harness answered 409 itself, as an expected refusal's URL pattern: `fullmatch` is true for a URL
    (fragment dropped) whose digest a refusal line names."""

    def __init__(self, locations):
        self.locations = locations

    def fullmatch(self, url):
        return digest(url) in self.locations


def harness_refusals(root):
    """("409", pattern) for the qae-artifacts gate's expected refusals: a request the harness refused was never sent,
    so its 409, in the network record and as Chromium's console line, is the harness's and not judged again."""
    refused = isolation_record(root)
    return "409", _Refused(frozenset(line["location"] for line in refused if "refused" in line and "location" in line))


def isolation_findings(root):
    """Explorers of one run keep off each other's records. A finding per request an explorer's browser refused (a
    record another explorer's tag marks, a fixture a criterion outside its share names), and one per record an
    explorer's request named that exactly one other explorer created (a POST of its answered 201): a walk on a record
    another explorer is changing judges that explorer's state, not the site's (a consumer's run once failed a
    correct criterion on a survey another explorer had deleted). A record two explorers' answers named, such as a
    sign-in's user, is nobody's."""
    lines = isolation_record(root)
    creators = {}
    for line in lines:
        if "created" in line:
            creators.setdefault(line["created"], set()).add(line.get("explorer", ""))
    findings, seen = [], set()
    for line in lines:
        explorer, request = line.get("explorer", "?"), line.get("request", "?")[:200]
        if "refused" in line:
            findings.append(Finding("explorer %s's browser refused %s: %s (explorers keep off each other's records, "
                                    "harnesses/qae/README.md)" % (explorer, request, line.get("why", "?")), NAME))
            continue
        made = creators.get(line.get("opened"), set())
        if len(made) != 1 or explorer in made or (explorer, line["opened"]) in seen:
            continue
        seen.add((explorer, line["opened"]))
        findings.append(Finding("explorer %s's %s worked on record %s, which explorer %s created in this run: an "
                                "explorer opens only records it created or that existed before the run, so this walk "
                                "judged the other explorer's state (harnesses/qae/README.md)"
                                % (explorer, request, line["opened"], next(iter(made))), NAME))
    return findings
