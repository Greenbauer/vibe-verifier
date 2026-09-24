#!/usr/bin/env python3
"""Adjudicate a QAE run from its artifacts, never from the model's prose.

A wired gate over the directory the explore job uploads (see harnesses/qae/). It reads what
playwright-mcp and the explorer wrote and refuses on structural facts:

    --artifacts DIR          the run's artifact directory (required)
    --criteria FILE          the PR body; when it declares `- None: <why>` there was nothing to
                             explore, so an empty run is correct and this gate passes
    --allow-console REGEX    console errors matching this are expected (repeatable)
    --allow-request REGEX    requests whose URL matches this are not judged (repeatable)
    --site URL-PREFIX        judge only requests to this origin (default: every request)
    --site-file FILE         the same, read from a file the workflow wrote: one http(s) URL, the
                             one the explore job declared, so a preview's URL reaches the gate
                             without a manifest edit; a missing, empty or malformed file cannot run

1. Every step in every step log has its screenshot: `qae/ACn.md` line `- step k:` needs a
   non-empty `qae/ACn-step-k.png`. A step without a picture is a claim, not evidence.
2. The console holds no error outside the allowlist: every `[ERROR]` line in `console-*.log`.
3. The session log exists (`session-*/session.md`, written by `--save-session`) and shows the
   browser was driven: at least one `browser_navigate` call.
4. The network record exists and is clean: at least one `browser_network_requests` result in the
   session log (the explorer is told to call it after each criterion), and no request in any
   such result, or in a `network-*.log` file, answered 400 or worse or failed, outside the
   allowlist. This encodes the recorded false-PASS lesson: a PASS obtained while the real
   endpoint failed is refused here whatever the verdict says.

Exit 2 when the artifact directory is missing, or a --site-file is missing or holds anything but
one http(s) URL: nothing was adjudicated.
"""
import glob
import os
import re
import urllib.parse

from _acceptance import declares_none
from _contract import CannotRun, Finding, run_gate

GATE = "qae-artifacts"
STEP_LINE = re.compile(r"^[ \t]*-[ \t]+step[ \t]+(?P<k>[0-9]+):", re.IGNORECASE)
CONSOLE_ERROR = re.compile(r"^\[\s*[0-9]+ms\]\s+\[ERROR\]\s+(?P<message>.*)$")
TOOL_CALL = re.compile(r"^### Tool call: (?P<name>\S+)\s*$", re.MULTILINE)
# `3. [GET] http://host/path => [404] Not Found` or `=> [FAILED] net::ERR_...`, as the tool renders it;
# inside a JSON result the newline is escaped, so the line is matched without anchors.
REQUEST = re.compile(r"[0-9]+\. \[(?P<method>[A-Z]+)\] (?P<url>\S+) => \[(?P<status>[0-9]{3}|FAILED)\]")


def read(path):
    with open(path, encoding="utf-8", errors="replace") as handle:
        return handle.read()


def relative(path, root):
    return os.path.relpath(path, root).replace(os.sep, "/")


def steps_have_screenshots(root):
    findings = []
    logs = sorted(glob.glob(os.path.join(root, "qae", "AC*.md")))
    if not logs:
        return [Finding("no step logs under qae/ (the explorer writes one qae/ACn.md per criterion)")]
    for log in logs:
        stem = os.path.basename(log)[:-3]
        for line in read(log).splitlines():
            match = STEP_LINE.match(line)
            if not match:
                continue
            shot = os.path.join(root, "qae", "%s-step-%s.png" % (stem, match.group("k")))
            if not os.path.isfile(shot) or os.path.getsize(shot) == 0:
                findings.append(Finding("step %s has no screenshot (expected %s)" % (match.group("k"), relative(shot, root)), relative(log, root)))
    return findings


def console_is_clean(root, allowed):
    findings, seen = [], set()
    for log in sorted(glob.glob(os.path.join(root, "console-*.log"))):
        for number, line in enumerate(read(log).splitlines(), 1):
            match = CONSOLE_ERROR.match(line)
            if not match:
                continue
            message = match.group("message")
            if any(pattern.search(message) for pattern in allowed) or message in seen:
                continue
            seen.add(message)
            findings.append(Finding("console error outside the allowlist: %s" % message[:200], relative(log, root), number))
    return findings


def session_and_network(root, allowed, site):
    sessions = sorted(glob.glob(os.path.join(root, "session-*", "session.md")))
    if not sessions:
        return [Finding("no session log (run playwright-mcp with --save-session so every tool call is on record)")]
    calls = []
    for session in sessions:
        calls += [name for name in TOOL_CALL.findall(read(session))]
    findings = []
    if "browser_navigate" not in calls:
        findings.append(Finding("the browser was never navigated: no browser_navigate call in the session log"))
    if "browser_network_requests" not in calls:
        findings.append(Finding("no network record: the explorer must call browser_network_requests after each criterion"))
    sources = sessions + sorted(glob.glob(os.path.join(root, "network-*.log")))
    seen = set()
    for source in sources:
        for match in REQUEST.finditer(read(source)):
            url, status = match.group("url"), match.group("status")
            if site and not url.startswith(site):
                continue
            if any(pattern.search(url) for pattern in allowed):
                continue
            if status != "FAILED" and int(status) < 400:
                continue
            key = (match.group("method"), url, status)
            if key in seen:
                continue
            seen.add(key)
            findings.append(Finding("request answered %s outside the allowlist: [%s] %s" % (status, match.group("method"), url[:200]), relative(source, root)))
    return findings


def declared_site(path):
    """The one http(s) URL the workflow wrote to `path`. Anything else means the declared input is
    wrong, and judging every request instead would pass a run against the wrong site."""
    if not os.path.isfile(path):
        raise CannotRun("site file not found: %s" % path)
    words = read(path).split()
    parts = urllib.parse.urlsplit(words[0]) if len(words) == 1 else None
    if not parts or parts.scheme not in ("http", "https") or not parts.netloc:
        raise CannotRun("site file %s does not hold one http(s) URL: %r" % (path, " ".join(words)[:200]))
    return words[0]


def check(args):
    root = args.artifacts
    if not root or not os.path.isdir(root):
        raise CannotRun("artifact directory not found: %s" % (root or "(none given)"))
    if args.criteria:
        if not os.path.isfile(args.criteria):
            raise CannotRun("criteria file not found: %s" % args.criteria)
        with open(args.criteria, encoding="utf-8", errors="replace") as handle:
            reason = declares_none(handle.read())
        if reason:
            print("%s: nothing was required, declared: %s" % (GATE, reason))
            return []
    # Resolved only once something is being judged: a pull request that declares None runs no
    # explorer, so its site step may never have declared a URL, and that must not fail it.
    site = declared_site(args.site_file) if args.site_file else args.site
    console_allow = [re.compile(pattern) for pattern in (args.allow_console or [])]
    request_allow = [re.compile(pattern) for pattern in (args.allow_request or [])]
    return (steps_have_screenshots(root)
            + console_is_clean(root, console_allow)
            + session_and_network(root, request_allow, site))


def add_arguments(parser):
    parser.add_argument("--artifacts", metavar="DIR", required=True)
    parser.add_argument("--criteria", metavar="FILE", default=None)
    parser.add_argument("--allow-console", action="append", metavar="REGEX")
    parser.add_argument("--allow-request", action="append", metavar="REGEX")
    site = parser.add_mutually_exclusive_group()
    site.add_argument("--site", metavar="URL-PREFIX", default=None)
    site.add_argument("--site-file", metavar="FILE", default=None)


if __name__ == "__main__":
    run_gate(GATE, __doc__, check, add_arguments)
