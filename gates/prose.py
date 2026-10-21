#!/usr/bin/env python3
"""Fail on an em dash a pull request adds to its Markdown, and report curly quotes.

A wired gate: it runs the pinned Vale (gates/_tools.py) with the catalog's own rules in
gates/_prose_styles/VibeVerifier/, so every repository is judged by the same rules and none brings
its own Vale config into the verdict. Vale reads the Markdown, so code spans, fenced code, HTML
comments, link targets, table cells and blockquotes are never judged.

    EmDash        blocking   an em dash
    EnDashAside   blocking   an en dash with a space on each side (a range such as 4-9 is fine)
    CurlyQuotes   advisory   a curly quote

Only the lines the pull request added count (git diff base...HEAD, renames followed), so text a
file already had never blocks; --all judges every line, for an audit. The generated rules block
(bin/vibe-verifier rules-doc) is skipped: it is written by a command, not a person.

The pull request description is judged too, advisory only: a workflow runs this gate on a push,
not on an edit of the description, so a blocking finding there could not be cleared by fixing it.
It is read from --body, else from the pull_request event GitHub hands the job ($GITHUB_EVENT_PATH);
outside a pull request there is none.
"""
import json
import os
import re
import subprocess
import tempfile

from _contract import CannotRun, Finding, changed_files, git, matches, resolve_base, run_gate, tracked_files
from _tools import ensure

GATE = "prose"
STYLES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_prose_styles")
DEFAULT_FILES = ["**/*.md", "**/*.mdx"]
BODY_NAME = "pull-request-description.md"
GENERATED_BEGIN = "<!-- BEGIN vibe-verifier rules-doc"
GENERATED_END = "<!-- END vibe-verifier rules-doc -->"
HUNK = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")
CONFIG = """StylesPath = {styles}
MinAlertLevel = warning
[formats]
mdx = md
[*.{{md,mdx}}]
BasedOnStyles = VibeVerifier
"""


def added_lines(repo, base, path):
    """The line numbers at HEAD that the diff from the base adds to path."""
    lines = set()
    for line in git(repo, "diff", "-M", "--unified=0", base + "...HEAD", "--", path).splitlines():
        hunk = HUNK.match(line)
        if hunk:
            start, count = int(hunk.group(1)), int(hunk.group(2) or "1")
            lines.update(range(start, start + count))
    return lines


def generated_lines(text):
    """Line numbers inside a generated rules block, its markers included."""
    lines, inside = set(), False
    for number, line in enumerate(text.splitlines(), 1):
        if line.startswith(GENERATED_BEGIN):
            inside = True
        if inside:
            lines.add(number)
        if line.strip() == GENERATED_END:
            inside = False
    return lines


def event_body():
    path = os.environ.get("GITHUB_EVENT_PATH")
    if not path or not os.path.isfile(path):
        return None
    try:
        with open(path) as handle:
            pull = json.load(handle).get("pull_request") or {}
    except ValueError as error:
        raise CannotRun("the GitHub event file %s is not JSON: %s" % (path, error))
    return pull.get("body") or None


def vale(binary, cwd, paths):
    """{path: [alert]} for paths relative to cwd, from Vale's JSON output."""
    with tempfile.TemporaryDirectory() as scratch:
        config = os.path.join(scratch, ".vale.ini")
        with open(config, "w") as handle:
            handle.write(CONFIG.format(styles=STYLES))
        result = subprocess.run([binary, "--no-global", "--config", config, "--output", "JSON", *paths],
                                cwd=cwd, capture_output=True, text=True)
    if result.returncode not in (0, 1):
        raise CannotRun("vale exited %d: %s" % (result.returncode, (result.stderr.strip() or result.stdout.strip() or "no output").splitlines()[-1]))
    try:
        return json.loads(result.stdout or "{}")
    except ValueError as error:
        raise CannotRun("vale's JSON report could not be read: %s" % error)


def findings_for(alerts, path, judged, all_advisory=False):
    """A Finding per alert on a judged line; judged(line) says which lines count."""
    return [Finding("%s: %s" % (alert["Check"].split(".")[-1], alert["Message"]), path, alert["Line"],
                    advisory=all_advisory or alert["Severity"] != "error")
            for alert in alerts if judged(alert["Line"])]


def check(args):
    files = args.files or DEFAULT_FILES
    if args.all:
        base, paths = None, [p for p in tracked_files(args.repo) if matches(p, files) and not matches(p, args.exclude)]
    else:
        base = resolve_base(args.repo, args.base_ref)
        paths = [p for p in changed_files(args.repo, base) if matches(p, files) and not matches(p, args.exclude)]
    body = open(args.body).read() if args.body else event_body()
    if not paths and not body:
        return []
    binary = ensure("vale")
    found = []
    if paths:
        report = vale(binary, args.repo, paths)
        for path in paths:
            with open(os.path.join(args.repo, path)) as handle:
                skip = generated_lines(handle.read())
            added = None if base is None else added_lines(args.repo, base, path)
            found += findings_for(report.get(path, []), path,
                                  lambda line: line not in skip and (added is None or line in added))
    if body:
        with tempfile.TemporaryDirectory() as scratch:
            with open(os.path.join(scratch, BODY_NAME), "w") as handle:
                handle.write(body)
            report = vale(binary, scratch, [BODY_NAME])
        found += findings_for(report.get(BODY_NAME, []), "pull request description", lambda line: True, all_advisory=True)
    return found


def add_arguments(parser):
    parser.add_argument("--files", metavar="GLOB", action="append", default=[],
                        help="Markdown files to judge, repeatable (default: %s)" % " ".join(DEFAULT_FILES))
    parser.add_argument("--exclude", metavar="GLOB", action="append", default=[],
                        help="files to leave out, repeatable, such as a vendored or generated document")
    parser.add_argument("--all", action="store_true", help="judge every line of every matching tracked file, not only added lines")
    parser.add_argument("--body", metavar="FILE", default=None,
                        help="a pull request description to judge, advisory (default: the pull_request event's body, if any)")


if __name__ == "__main__":
    run_gate(GATE, __doc__, check, add_arguments)
