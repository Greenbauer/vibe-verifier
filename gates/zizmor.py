#!/usr/bin/env python3
"""Fail when a workflow this pull request added or changed has a zizmor finding.

A tool-backed gate (zizmor is pinned in gates/_tools.py). zizmor audits GitHub Actions workflows
for security problems: template injection, unpinned actions, persisted credentials, excessive
permissions. Only the workflows changed since the base ref are judged; `--all` judges every
tracked workflow. It runs `--offline`, so the audits that need GitHub are skipped and the verdict
is the same on a laptop and on a runner. A finding the repository has ignored the way zizmor
documents (a `# zizmor: ignore[...]` comment or `zizmor.yml`) is not a finding here.
"""
import json
import subprocess

from _contract import CannotRun, Finding, resolve_base, run_gate
from _tools import ensure
from _workflows import workflow_files

GATE = "zizmor"


def check(args):
    files = workflow_files(args.repo, None if args.all else resolve_base(args.repo, args.base_ref), args.all)
    if not files:
        return []
    command = [ensure("zizmor"), "--format", "json", "--offline", "--no-exit-codes", *files]
    if args.min_severity:
        command += ["--min-severity", args.min_severity]
    if args.min_confidence:
        command += ["--min-confidence", args.min_confidence]
    result = subprocess.run(command, cwd=args.repo, capture_output=True, text=True)
    if result.returncode != 0:
        raise CannotRun("zizmor exited %d: %s" % (result.returncode, (result.stderr.strip() or "no output").splitlines()[-1]))
    try:
        report = json.loads(result.stdout or "[]")
    except ValueError as error:
        raise CannotRun("zizmor's JSON report could not be read: %s" % error)
    findings = []
    for item in report:
        if item.get("ignored"):
            continue
        locations = item.get("locations") or [{}]
        primary = next((loc for loc in locations if loc.get("symbolic", {}).get("kind") == "Primary"), locations[0])
        symbolic, concrete = primary.get("symbolic", {}), primary.get("concrete", {})
        path = symbolic.get("key", {}).get("Local", {}).get("verbatim_path")
        row = concrete.get("location", {}).get("start_point", {}).get("row")
        judged = item.get("determinations", {})
        message = "%s: %s (%s severity, %s confidence) %s" % (
            item.get("ident", "?"), symbolic.get("annotation") or item.get("desc", ""),
            str(judged.get("severity", "?")).lower(), str(judged.get("confidence", "?")).lower(), item.get("url", ""))
        findings.append(Finding(message.rstrip(), path or None, row + 1 if isinstance(row, int) else None))
    return findings


def add_arguments(parser):
    parser.add_argument("--all", action="store_true", help="audit every tracked workflow, not only those changed since the base")
    parser.add_argument("--min-severity", metavar="LEVEL", default=None, help="passed to zizmor (unknown, informational, low, medium, high)")
    parser.add_argument("--min-confidence", metavar="LEVEL", default=None, help="passed to zizmor (unknown, low, medium, high)")


if __name__ == "__main__":
    run_gate(GATE, __doc__, check, add_arguments)
