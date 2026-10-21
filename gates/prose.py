#!/usr/bin/env python3
"""Fail on an em dash a pull request adds to its Markdown, and report curly quotes.

A wired gate: it runs the pinned Vale (gates/_tools.py) with the catalog's own rules in
gates/_prose_styles/VibeVerifier/, so every repository is judged by the same rules and none brings
its own Vale config into the verdict. Vale reads the Markdown, so code spans, fenced code, HTML
comments and link targets are never judged; the dash rules also skip table cells, and all three
skip blockquotes.

    EmDash        blocking   an em dash
    EnDashAside   blocking   an en dash with a space on each side (a range such as 4-9 is fine)
    CurlyQuotes   advisory   a curly quote

Only the lines the pull request added count (git diff base...HEAD, renames followed), so text a
file already had never blocks; --all judges every line, for an audit. The generated rules block
(bin/vibe-verifier rules-doc) is skipped: it is written by a command, not a person. Vale reads a copy of
each file in which its own on-off switches are defused, so a document cannot turn the gate off; a binary
file or a symbolic link is skipped, and bytes that are not UTF-8 are replaced.

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

from _contract import CannotRun, Finding, _paths, matches, renamed, resolve_base, run_gate, tracked_files
from _tools import ensure

GATE = "prose"
STYLES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_prose_styles")
DEFAULT_FILES = ["**/*.md", "**/*.mdx"]
BODY_NAME = "pull-request-description.md"
GENERATED_BEGIN = "<!-- BEGIN vibe-verifier rules-doc"
GENERATED_END = "<!-- END vibe-verifier rules-doc -->"
HUNK = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")
# Vale's own switches inside a document (<!-- vale off -->, <!-- vale VibeVerifier.EmDash = NO -->) would let a
# file turn the gate off, so the copy Vale reads has them defused; an MDX {/* comment */} is blanked, since the
# Markdown parser that reads .mdx here does not know it is a comment. Both keep every line where it was.
VALE_SWITCH = re.compile(r"<!--(\s*)vale\b", re.IGNORECASE)
MDX_COMMENT = re.compile(r"\{/\*.*?\*/\}", re.DOTALL)
CONFIG = """StylesPath = {styles}
MinAlertLevel = warning
[formats]
mdx = md
[*.{{md,mdx}}]
BasedOnStyles = VibeVerifier
"""


def added_lines(repo, base, path, old=None):
    """The line numbers at HEAD that the diff from the base adds to path; old is its path at the base when the
    pull request renamed it, so the lines it carried over do not count as added."""
    lines = set()
    # Read as bytes: only the hunk headers are used, and a document that is not UTF-8 must not stop the gate.
    diff = subprocess.run(["git", "-C", repo, "diff", "-M", "--unified=0", base + "...HEAD", "--", *filter(None, (old, path))],
                          capture_output=True)
    if diff.returncode != 0:
        raise CannotRun("git diff of %s failed: %s" % (path, diff.stderr.decode("utf-8", "replace").strip()))
    for line in diff.stdout.decode("utf-8", errors="replace").splitlines():
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


def defused(text, path):
    text = VALE_SWITCH.sub(r"<!--\1(vale)", text)
    if path.endswith(".mdx"):
        text = MDX_COMMENT.sub(lambda comment: re.sub(r"[^\n]", " ", comment.group(0)), text)
    return text


def vale(binary, documents):
    """{path: [alert]} for {path: text}, from Vale's JSON output on defused copies of the texts."""
    with tempfile.TemporaryDirectory() as scratch:
        for path, text in documents.items():
            copy = os.path.join(scratch, "docs", path)
            os.makedirs(os.path.dirname(copy), exist_ok=True)
            with open(copy, "w", encoding="utf-8") as handle:
                handle.write(defused(text, path))
        config = os.path.join(scratch, ".vale.ini")
        with open(config, "w") as handle:
            handle.write(CONFIG.format(styles=STYLES))
        result = subprocess.run([binary, "--no-global", "--config", config, "--output", "JSON",
                                 *(os.path.join("docs", path) for path in documents)],
                                cwd=scratch, capture_output=True, text=True)
    if result.returncode not in (0, 1):
        raise CannotRun("vale exited %d: %s" % (result.returncode, (result.stderr.strip() or result.stdout.strip() or "no output").splitlines()[-1]))
    try:
        report = json.loads(result.stdout or "{}")
    except ValueError as error:
        raise CannotRun("vale's JSON report could not be read: %s" % error)
    return {os.path.relpath(path, "docs"): alerts for path, alerts in report.items()}


def read_text(repo, path):
    """The file's text, or None for a binary file or a symbolic link (a link has no prose of its own: its
    target, when the pull request changes it, is judged under its own path). Bytes that are not UTF-8 are
    replaced, so one badly encoded document cannot stop the gate from judging the rest."""
    if os.path.islink(os.path.join(repo, path)):
        return None
    with open(os.path.join(repo, path), "rb") as handle:
        data = handle.read()
    return None if b"\0" in data else data.decode("utf-8", errors="replace")


def findings_for(alerts, path, judged, all_advisory=False):
    """A Finding per alert on a judged line; judged(line) says which lines count."""
    return [Finding("%s: %s" % (alert["Check"].split(".")[-1], alert["Message"]), path, alert["Line"],
                    advisory=all_advisory or alert["Severity"] != "error")
            for alert in alerts if judged(alert["Line"])]


def file_findings(binary, repo, base, paths):
    """Findings on the judged lines of paths: those the diff from base adds (every line when base is None),
    less the generated rules block."""
    texts = {path: text for path, text in ((path, read_text(repo, path)) for path in paths) if text is not None}
    if not texts:
        return []
    report, found, moves = vale(binary, texts), [], {} if base is None else renamed(repo, base)
    for path, text in texts.items():
        skip = generated_lines(text)
        added = None if base is None else added_lines(repo, base, path, moves.get(path))
        found += findings_for(report.get(path, []), path, lambda line: line not in skip and (added is None or line in added))
    return found


def body_findings(binary, body):
    report = vale(binary, {BODY_NAME: body})
    return findings_for(report.get(BODY_NAME, []), "pull request description", lambda line: True, all_advisory=True)


def check(args):
    files = args.files or DEFAULT_FILES
    base = None if args.all else resolve_base(args.repo, args.base_ref)
    # T as well as ACMR: a symbolic link the pull request turns into a regular file is a type change, and every
    # line of the new file is its own.
    candidates = tracked_files(args.repo) if args.all else _paths(args.repo, "--name-only", "--diff-filter=ACMRT", base + "...HEAD")
    paths = [p for p in candidates if matches(p, files) and not matches(p, args.exclude)]
    body = open(args.body).read() if args.body else event_body()
    if not paths and not body:
        return []
    binary = ensure("vale")
    return (file_findings(binary, args.repo, base, paths) if paths else []) + (body_findings(binary, body) if body else [])


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
