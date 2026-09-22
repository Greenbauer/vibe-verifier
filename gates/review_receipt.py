#!/usr/bin/env python3
"""Fail unless the review completed on this exact head, and its threads are resolved.

A wired gate for the review harness. Its inputs are declared files: the newest receipt the
review workflow posted (`review-receipt: <sha> -- <mode> -- run <id>`), the head SHA the pull
request is at, and optionally the count of unresolved review threads. A receipt for an older
commit is not a receipt for this one: a push after the review is a change nobody reviewed. A
missing receipt means the review did not complete (quota, a model error, a broken step), which
is red, never a quiet pass.
"""
import json
import os
import re

from _contract import CannotRun, Finding, run_gate

GATE = "review-receipt"
RECEIPT = re.compile(r"review-receipt: ([0-9a-f]{40})")


def read(path, label):
    if not path or not os.path.isfile(path):
        raise CannotRun("%s file not found: %s" % (label, path or "(none given)"))
    with open(path, encoding="utf-8") as handle:
        return handle.read()


def check(args):
    head = read(args.head, "--head").strip()
    if not re.fullmatch(r"[0-9a-f]{40}", head):
        raise CannotRun("--head must hold a 40-character commit SHA, got %r" % head[:60])
    reviewed = RECEIPT.findall(read(args.receipt, "--receipt"))
    findings = []
    if head not in reviewed:
        findings.append(Finding("no review receipt for %s: the review did not complete on this head%s (a re-run posts one)"
                                % (head[:10], "; the newest receipt is for %s" % reviewed[-1][:10] if reviewed else "")))
    if args.threads:
        try:
            unresolved = int(json.loads(read(args.threads, "--threads"))["unresolved"])
        except (ValueError, KeyError, TypeError) as error:
            raise CannotRun("--threads must be JSON with an integer \"unresolved\": %s" % error)
        if unresolved:
            findings.append(Finding("%d unresolved review thread%s: reply to and resolve each, then re-run the review"
                                    % (unresolved, "" if unresolved == 1 else "s")))
    return findings


def add_arguments(parser):
    parser.add_argument("--receipt", metavar="FILE", required=True, help="the newest receipt comment the review workflow posted")
    parser.add_argument("--head", metavar="FILE", required=True, help="a file holding the pull request's head SHA")
    parser.add_argument("--threads", metavar="FILE", default=None, help='JSON {"unresolved": N}; when given, N must be 0')


if __name__ == "__main__":
    run_gate(GATE, __doc__, check, add_arguments)
