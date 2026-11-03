#!/usr/bin/env python3
"""Fail on layout and copy defects the QAE explorer's browser measured on the site under test.

A wired gate over the directory the explore job uploads (harnesses/qae/). The harness's layout hook
(actions/qae-browser/ui-audit.js, loaded into playwright-mcp with --init-page) measures every page the
explorer opens, at every width it uses, and writes ui-audit.jsonl there; the explorer never sees it.
Its rules are listed in actions/qae-browser/ui-audit-page.js: controls in one row that do not line
up, bordered controls touching each other or their container's edge, a spinner off its button's
middle, walls of text, raw values such as `in_review` or `undefined`, and text or values cut off.

    --artifacts DIR     the run's artifact directory (required)
    --site-file FILE    judge only pages under the URLs the explore job's site step declared (one a line)
    --site URL-PREFIX   the same, one URL given here
    --rules R[,R...]    judge only these rules (default: all)
    --allow REGEX       skip a finding whose element path or text matches (repeatable): for a design the
                        repository chose, named in its own QAE manifest

A run with step logs and no ui-audit.jsonl cannot run: the hook was not loaded, and a silent pass would
look like a clean page. Rows of one table repeat a defect, so findings that differ only in which row
(nth-of-type) are reported once, with how many more there are.
"""
import glob
import json
import os
import re

from _contract import CannotRun, Finding, run_gate
from qae_artifacts import declared_sites

GATE = "ui-audit"
RECORD = "ui-audit.jsonl"
NTH = re.compile(r":nth-of-type\(\d+\)")


def read_record(root):
    """Every line of the record: at the root for one explorer, under shard-<n>/ for each of several, where the
    workflow step that merges their evidence moves it (harnesses/qae/README.md)."""
    paths = sorted(glob.glob(os.path.join(root, RECORD)) + glob.glob(os.path.join(root, "shard-*", RECORD)))
    if not paths:
        steps = os.path.join(root, "qae")
        if os.path.isdir(steps) and any(name.endswith(".md") for name in os.listdir(steps)):
            raise CannotRun("%s holds step logs and no %s: load actions/qae-browser/ui-audit.js into playwright-mcp "
                            "with --init-page (harnesses/qae/README.md)" % (root, RECORD))
        return []
    lines = []
    for path in paths:
        with open(path, encoding="utf-8") as handle:
            for number, line in enumerate(handle, 1):
                try:
                    lines.append(json.loads(line))
                except ValueError:
                    raise CannotRun("%s line %d is not JSON" % (os.path.relpath(path, root), number))
    return lines


def on_site(page, site):
    """Whether `page` is under the declared `site`: the URL itself or a path below it, never a longer host name."""
    site = site.rstrip("/")
    return page == site or page.startswith(site + "/")


def check(args):
    if not os.path.isdir(args.artifacts):
        raise CannotRun("artifact directory not found: %s" % args.artifacts)
    sites = declared_sites(args.site_file) if args.site_file else [args.site] if args.site else []
    rules = set(args.rules.split(",")) if args.rules else None
    allow = [re.compile(pattern) for pattern in args.allow or []]
    groups = {}
    for line in read_record(args.artifacts):
        if sites and not any(on_site(str(line.get("page", "")), site) for site in sites):
            continue
        if rules and line.get("rule") not in rules:
            continue
        if any(pattern.search(str(line.get(key) or "")) for pattern in allow for key in ("at", "text")):
            continue
        key = (line.get("page"), line.get("width"), line.get("rule"), NTH.sub("", str(line.get("at"))))
        groups.setdefault(key, []).append(line)
    findings = []
    for (page, width, rule, _), lines in groups.items():
        first = lines[0]
        what = first.get("text") or first.get("at")
        if first.get("other"):
            what += " / " + first["other"]
        more = " (and %d more like it)" % (len(lines) - 1) if len(lines) > 1 else ""
        findings.append(Finding("%s on %s at %spx: %s: %s%s (%s)" % (rule, page, width, what, first.get("detail"), more, first.get("at"))))
    return findings


def add_arguments(parser):
    parser.add_argument("--artifacts", metavar="DIR", required=True)
    site = parser.add_mutually_exclusive_group()
    site.add_argument("--site", metavar="URL-PREFIX", default=None)
    site.add_argument("--site-file", metavar="FILE", default=None)
    parser.add_argument("--rules", metavar="R[,R...]", default=None)
    parser.add_argument("--allow", action="append", metavar="REGEX")


if __name__ == "__main__":
    run_gate(GATE, __doc__, check, add_arguments)
