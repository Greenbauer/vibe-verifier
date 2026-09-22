#!/usr/bin/env python3
"""Fail when any tracked package.json repeats a key.

JSON parsers keep the last duplicate and say nothing, so a merge that leaves two
"scripts" or two "dependencies" blocks silently drops one of them.
"""
import json
import re

from _contract import Finding, git, run_gate, tracked_files

GATE = "no-duplicate-package-json-keys"


def duplicate_keys(text):
    found = []

    def collect(pairs):
        seen = set()
        for key, _ in pairs:
            if key in seen and key not in found:
                found.append(key)
            seen.add(key)
        return dict(pairs)

    json.loads(text, object_pairs_hook=collect)
    return found


def line_of_last(text, key):
    pattern = re.compile(r'"%s"\s*:' % re.escape(key))
    hits = [i for i, line in enumerate(text.splitlines(), 1) if pattern.search(line)]
    return hits[-1] if hits else None


def check(args):
    findings = []
    for path in tracked_files(args.repo):
        if path.rsplit("/", 1)[-1] != "package.json" or "node_modules/" in path:
            continue
        text = git(args.repo, "show", "HEAD:" + path)
        try:
            keys = duplicate_keys(text)
        except ValueError as error:
            findings.append(Finding("not valid JSON: %s" % error, path))
            continue
        for key in keys:
            findings.append(Finding('duplicate key "%s": the parser keeps only the last one' % key, path, line_of_last(text, key)))
    return findings


if __name__ == "__main__":
    run_gate(GATE, __doc__, check)
