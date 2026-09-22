#!/usr/bin/env python3
"""Fail when a workflow this pull request added or changed does not lint with actionlint.

A tool-backed gate (actionlint is pinned in gates/_tools.py). Only the workflows changed since
the base ref are judged, so a pre-existing problem in a workflow nobody touched is never this
PR's finding; `--all` judges every tracked workflow instead. actionlint's shellcheck and
pyflakes integrations are switched off: they switch themselves on wherever those tools happen
to be installed, and a gate must say the same thing on a laptop and on a runner.
"""
import json
import subprocess

from _contract import CannotRun, Finding, resolve_base, run_gate
from _tools import ensure
from _workflows import workflow_files

GATE = "actionlint"


def check(args):
    files = workflow_files(args.repo, None if args.all else resolve_base(args.repo, args.base_ref), args.all)
    if not files:
        return []
    result = subprocess.run([ensure("actionlint"), "-shellcheck=", "-pyflakes=", "-format", "{{json .}}", *files],
                            cwd=args.repo, capture_output=True, text=True)
    if result.returncode == 0:
        return []
    if result.returncode != 1:
        raise CannotRun("actionlint exited %d: %s" % (result.returncode, (result.stderr.strip() or "no output").splitlines()[-1]))
    try:
        errors = json.loads(result.stdout or "[]")
    except ValueError as error:
        raise CannotRun("actionlint reported problems but its JSON could not be read: %s" % error)
    if not errors:
        raise CannotRun("actionlint exited 1 with an empty report")
    return [Finding("%s [%s]" % (error.get("message", "?"), error.get("kind", "?")), error.get("filepath") or None, error.get("line") or None)
            for error in errors]


def add_arguments(parser):
    parser.add_argument("--all", action="store_true", help="lint every tracked workflow, not only those changed since the base")


if __name__ == "__main__":
    run_gate(GATE, __doc__, check, add_arguments)
