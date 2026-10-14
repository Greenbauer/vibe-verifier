"""The record the QAE harness keeps beside its evidence, console-pages.jsonl, as the qae-artifacts gate
reads it. actions/qae-browser/console-pages.js writes it through playwright-mcp's --init-page, one JSON
object a line, digests and never text, so no credential lands in it:

    {"page": "<URL, no query>", "sha256": "<digest of a console error's first line>"}
        a page of the run logged that console error or uncaught exception
    {"answered": "<digest of `<METHOD> <URL, fragment dropped>`>"}
        a page of the run got an answer below 400 for that request, static requests included
"""
import hashlib
import json
import os

NAME = "console-pages.jsonl"


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
                if "answered" in entry:
                    answers.add(str(entry["answered"]))
                else:
                    page = str(entry["page"])
                    pages.setdefault(entry["sha256"], []).append(page)
            except (ValueError, KeyError, TypeError):
                continue
    return pages, answers
