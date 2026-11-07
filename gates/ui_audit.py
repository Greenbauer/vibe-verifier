#!/usr/bin/env python3
"""Fail on layout and copy defects the QAE explorer's browser measured on the site under test.

A wired gate over the directory the explore job uploads (harnesses/qae/). The harness's layout hook
(actions/qae-browser/ui-audit.js, loaded into playwright-mcp with --init-page) measures every page the
explorer opens, at every width it uses, and writes ui-audit.jsonl there; the explorer never sees it.
Its rules are listed in actions/qae-browser/ui-audit-page.js: controls in one row that do not line
up, bordered controls touching each other or their container's edge, a spinner off its button's
middle, a badge's count, initials or check off its circle's middle, walls of text, raw values such as `in_review` or `undefined`,
text or values cut off, a short label in a pill, badge, button or tab that takes two lines, a table that scrolls sideways beside room for it, page bands whose content starts different distances from the edge, a field too small for what it asks (free text in a narrow box, or a value wider than its field), an icon or spinner whose ink sits off its button's middle, and two buttons in one dialog or form a person cannot tell apart by name.

    --artifacts DIR     the run's artifact directory (required)
    --site-file FILE    judge only pages under the URLs the explore job's site step declared (one a line)
    --site URL-PREFIX   the same, one URL given here
    --rules R[,R...]    judge only these rules (default: all)
    --allow REGEX       skip a finding whose element path or text matches (repeatable): for a design the
                        repository chose, named in its own QAE manifest
    --all               every finding blocks, not only those on copy the pull request adds

A finding blocks only when the pull request adds the text of the element it names, or of the neighbour it was
measured against, as copy-audit decides it (gates/_added_copy.py): a defect on a screen the base already had, or on
an element that shows no text, is listed as advisory, so a cleanup pull request can work from the list.

A run with step logs and no ui-audit.jsonl cannot run: the hook was not loaded, and a silent pass would
look like a clean page. Rows of one table repeat a defect, so findings that differ only in which row
(nth-of-type) are reported once, with how many more there are.
"""
import glob
import json
import os
import re

from _added_copy import added_by_pull_request, added_text
from _contract import CannotRun, Finding, run_gate
from qae_artifacts import declared_sites

GATE = "ui-audit"
RECORD = "ui-audit.jsonl"
NTH = re.compile(r":nth-of-type\(\d+\)")


def record_paths(root, name=RECORD):
    """Each copy of one of the hook's records: at the root for one explorer, under shard-<n>/ for each of several,
    where the workflow step that merges their evidence moves it (harnesses/qae/README.md). Step logs and no record
    cannot run: the hook was not loaded. The copy-audit gate reads its record (copy.jsonl) the same way."""
    paths = sorted(glob.glob(os.path.join(root, name)) + glob.glob(os.path.join(root, "shard-*", name)))
    if not paths:
        steps = os.path.join(root, "qae")
        if os.path.isdir(steps) and any(entry.endswith(".md") for entry in os.listdir(steps)):
            raise CannotRun("%s holds step logs and no %s: load actions/qae-browser/ui-audit.js into playwright-mcp "
                            "with --init-page (harnesses/qae/README.md)" % (root, name))
    return paths


def read_lines(path, root):
    """The JSON lines of one record; a line that is not JSON cannot run."""
    lines = []
    with open(path, encoding="utf-8") as handle:
        for number, line in enumerate(handle, 1):
            try:
                lines.append(json.loads(line))
            except ValueError:
                raise CannotRun("%s line %d is not JSON" % (os.path.relpath(path, root), number))
    return lines


def read_record(root):
    """Every line of the layout record, from every explorer."""
    return [line for path in record_paths(root) for line in read_lines(path, root)]


def on_site(page, site):
    """Whether `page` is under the declared `site`: the URL itself or a path below it, never a longer host name."""
    site = site.rstrip("/")
    return page == site or page.startswith(site + "/")


def judged(line, sites, rules, allow):
    """Whether a record line counts: on the declared site, one of the rules asked for, and not allowed."""
    if sites and not any(on_site(str(line.get("page", "")), site) for site in sites):
        return False
    if rules and line.get("rule") not in rules:
        return False
    return not any(pattern.search(str(line.get(key) or "")) for pattern in allow for key in ("at", "text"))


def finding(lines, added, blocking_all):
    """One finding for lines that differ only in which table row (nth-of-type) they are on: blocking when the pull
    request adds the text of the element or of its neighbour, or with --all."""
    first = lines[0]
    what = (first.get("text") or first.get("at")) + (" / " + first["other"] if first.get("other") else "")
    more = " (and %d more like it)" % (len(lines) - 1) if len(lines) > 1 else ""
    new = any(text and added_by_pull_request(str(text), added) for text in (first.get("text"), first.get("other")))
    return Finding("%s%s on %s at %spx: %s: %s%s (%s)" % (
        first.get("rule"), "" if new else ", existing", first.get("page"), first.get("width"), what, first.get("detail"),
        more, first.get("at")), advisory=not (new or blocking_all))


def check(args):
    if not os.path.isdir(args.artifacts):
        raise CannotRun("artifact directory not found: %s" % args.artifacts)
    # Resolved only once there is a record to judge: a pull request that declares None runs no explorer, so its site
    # file is empty, and that must not fail it (qae_artifacts.check does the same).
    if not record_paths(args.artifacts):
        return []
    record = read_record(args.artifacts)
    sites = declared_sites(args.site_file) if args.site_file else [args.site] if args.site else []
    rules = set(args.rules.split(",")) if args.rules else None
    allow = [re.compile(pattern) for pattern in args.allow or []]
    groups = {}
    for line in record:
        if judged(line, sites, rules, allow):
            key = (line.get("page"), line.get("width"), line.get("rule"), NTH.sub("", str(line.get("at"))))
            groups.setdefault(key, []).append(line)
    added = added_text(args) if groups and not args.all else ""
    return [finding(lines, added, args.all) for lines in groups.values()]


def add_arguments(parser):
    parser.add_argument("--artifacts", metavar="DIR", required=True)
    site = parser.add_mutually_exclusive_group()
    site.add_argument("--site", metavar="URL-PREFIX", default=None)
    site.add_argument("--site-file", metavar="FILE", default=None)
    parser.add_argument("--rules", metavar="R[,R...]", default=None)
    parser.add_argument("--allow", action="append", metavar="REGEX")
    parser.add_argument("--all", action="store_true")


if __name__ == "__main__":
    run_gate(GATE, __doc__, check, add_arguments)
