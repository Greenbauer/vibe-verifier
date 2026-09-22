#!/usr/bin/env python3
"""Fail when the branch name is too long for the host that derives a name from it.

Preview hosts build a DNS label from the branch. Past the label budget they
truncate and append a hash, and links built from the branch name stop resolving.
The slug is the branch with "/" and "_" replaced by "-".

    --max N          required: the slug budget for this repository
    --branch NAME    default: $GITHUB_HEAD_REF, a branch $GITHUB_REF_NAME, else the checked-out branch
"""
import os
import subprocess

from _contract import CannotRun, Finding, run_gate

GATE = "branch-name-length"


def current_branch(repo):
    if os.environ.get("GITHUB_HEAD_REF"):
        return os.environ["GITHUB_HEAD_REF"]
    if os.environ.get("GITHUB_REF_TYPE") == "branch" and os.environ.get("GITHUB_REF_NAME"):
        return os.environ["GITHUB_REF_NAME"]
    probe = subprocess.run(["git", "-C", repo, "symbolic-ref", "--short", "-q", "HEAD"], capture_output=True, text=True)
    if probe.returncode != 0 or not probe.stdout.strip():
        raise CannotRun("no branch to measure: HEAD is detached, pass --branch")
    return probe.stdout.strip()


def check(args):
    branch = args.branch or current_branch(args.repo)
    slug = branch.replace("/", "-").replace("_", "-")
    if len(slug) <= args.max:
        return []
    return [Finding('branch slug "%s" is %d characters, budget is %d: shorten the branch name' % (slug, len(slug), args.max))]


def add_arguments(parser):
    parser.add_argument("--max", type=int, required=True, metavar="N")
    parser.add_argument("--branch", default=None)


if __name__ == "__main__":
    run_gate(GATE, __doc__, check, add_arguments)
