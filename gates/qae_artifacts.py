#!/usr/bin/env python3
"""Adjudicate a QAE run from its artifacts, never from the model's prose.

A wired gate over the directory the explore job uploads (see harnesses/qae/). It reads what
playwright-mcp and the explorer wrote and refuses on structural facts:

    --artifacts DIR          the run's artifact directory (required)
    --criteria FILE          the PR body; when it declares `- None: <why>` there was nothing to
                             explore, so an empty run is correct and this gate passes. A criterion
                             whose line carries `expected-refusal: <status> <path-or-URL>` makes
                             exactly that status at exactly that URL expected for this run (see 5)
    --allow-console REGEX    console errors matching this are expected (repeatable)
    --allow-request REGEX    requests whose URL matches this are not judged (repeatable)
    --site URL-PREFIX        judge only this origin: a request elsewhere is skipped, and so is
                             Chromium's "Failed to load resource" line for that other host, and
                             a console error a page elsewhere logged (see 2)
                             (default: every request and every console error)
    --site-file FILE         the same, read from a file the workflow wrote: the http(s) URL the
                             explore job declared, so a preview's URL reaches the gate without a
                             manifest edit; a missing, empty or malformed file cannot run. A file
                             may list further URLs after the first, one per line: other origins
                             the site itself is served from (its API on another host), judged
                             exactly as the site is, base path included (an API stage)
    --widths N[,N...]        viewport widths in pixels (opt-in, the repository's choice): each
                             criterion's step screenshots must include one of each width (see 6)
    --ticket FILE            the criteria of the ticket the pull request implements (TC1, TC2, ...):
                             their step logs `qae/TCn.md` are held to the rules a criterion's are,
                             their `expected-refusal:` declarations count, and a pull request that
                             declares `- None:` still needs a run when its ticket lists criteria

1. Every step in every step log has its screenshot: `qae/ACn.md` line `- step k:` needs a
   non-empty `qae/ACn-step-k.png`. A step without a picture is a claim, not evidence. A feature
   re-walk's log, `qae/features/<id>.md`, is held to the same rule (`qae/features/<id>-step-k.png`).
   A step line naming `reference <key>` is a comparison with that design reference, and its finding
   says so: the explorer once logged one without saving its screenshot.
2. The console holds no error outside the allowlist: every `[ERROR]` line in `console-*.log`.
   A `Failed to load resource` line for a host other than the declared site and its further
   declared origins is not judged, the same way a third-party request is not. Any other console
   error still is. A site whose API answers on another host declares that host too, or a 401 or 500
   from it reaches neither this check nor the network one (a signed-out page was once caught only by
   this line).
   An error a page outside the site logged is not judged either: a hosted page a link opened that
   fails to reach its own analytics is not the site's failure. A console line names the script or
   resource the error is about, never the page that logged it, so the page is read from the record
   the harness keeps beside the console logs (`console-pages.jsonl`, written by
   actions/qae-browser/console-pages.js: the page's URL and the digest of the error's line). Every
   page that logged the line must be an http(s) page outside the declared site and its further
   origins, and the line must not be about a URL of the site. An error with no record is judged, so
   a run without the record is judged as before, and so is one a page of the site logged, whatever
   it is about: the site's page failing to load a third-party script may be the site's own doing
   (its content security policy, a wrong URL).
3. The session log exists (`session-*/session.md`, written by `--save-session`) and shows the
   browser was driven: at least one `browser_navigate` call.
4. The network record exists and is clean: at least one `browser_network_requests` result in the
   session log (the explorer is told to call it after each criterion), and no request in any
   such result, or in a `network-*.log` file, answered 400 or worse or failed, outside the
   allowlist. A call may save its result to a file (its `filename` argument); the session log then
   holds a link and no requests, so that file is read as the result. One the evidence does not hold
   is a finding: a record nobody can read is not a record. A request the page cancelled itself
   (`[FAILED] net::ERR_ABORTED`) was never answered by the site. The record cannot say why it was
   cancelled: a page that drops a fetch when the user moves on, and one that gives up on an endpoint
   that never answers, write the same line. So it is not judged only when the record also holds that
   request (same method, same URL, fragment dropped) answered below 400 somewhere in the run: the
   endpoint is shown to work, and the cancellation was the page moving on. Cancelled and never
   answered is a finding, and every other failed request always is. This encodes the recorded
   false-PASS lesson: a PASS obtained while the real endpoint failed is refused here whatever the
   verdict says.
5. A refusal is the correct outcome of some criteria (an auth gate answering 401 signed out, a
   server answering 422 to the invalid input the explorer is told to try), so a criterion declares
   it: `- Signed out, the quotes API refuses (expected-refusal: 401 /api/quotes)`.
   The declaration is read from the criteria file the workflow fetched, never from the explorer,
   and only from a criterion's own line (an HTML comment does not count). It excuses that status at
   that URL and nothing else: the request in the network record, and Chromium's `Failed to load
   resource: ... status of 401` console line for it. A path resolves against the site, and is
   appended to each further declared origin (whose host a pull request cannot know when every
   preview gets its own), a URL is taken as written, and either must match the request's URL exactly (query included, fragment
   dropped). A 5xx, another status, another URL, and any other console error still fail, and a
   declaration of any status but a handled refusal (400, 401, 403, 404, 409 or 422), or a path with
   no site to resolve it, is a finding.
6. With --widths, each criterion was checked at every declared viewport width: among the step
   screenshots of each `qae/ACn.md`, one is that many pixels wide, read from the PNG's header (a
   browser screenshot is as wide as its viewport). A step screenshot that is not a PNG is a finding.
   The explore job writes the same widths to qae-inputs/widths for the explorer (actions/qae-inputs),
   from this line as the base has it. Feature re-walk logs are not held to it.

Exit 2 when the artifact directory is missing, or a --site-file is missing or holds anything but
http(s) URLs: nothing was adjudicated.
"""
import argparse
import glob
import hashlib
import json
import os
import re
import urllib.parse

from _acceptance import body_criteria, declares_none, named_references, png_size, ticket_items
from _contract import CannotRun, Finding, run_gate

GATE = "qae-artifacts"
STEP_LINE = re.compile(r"^[ \t]*-[ \t]+step[ \t]+(?P<k>[0-9]+):", re.IGNORECASE)
# The offset is negative for what a tab logged before playwright-mcp opened its log: a tab a link
# opened is already loading by then (`[      -4ms] [ERROR] ...`, from the pinned build, 2026-10-08).
CONSOLE_ERROR = re.compile(r"^\[\s*-?[0-9]+ms\]\s+\[ERROR\]\s+(?P<message>.*)$")
TOOL_CALL = re.compile(r"^### Tool call: (?P<name>\S+)\s*$", re.MULTILINE)
# `3. [GET] http://host/path => [404] Not Found` or `=> [FAILED] net::ERR_...`, as the tool renders it;
# inside a JSON result the newline is escaped, so the line is matched without anchors.
REQUEST = re.compile(r"[0-9]+\. \[(?P<method>[A-Z]+)\] (?P<url>\S+) => \[(?P<status>[0-9]{3}|FAILED)\](?: (?P<reason>net::[A-Z0-9_]+))?")
# The page's own cancellation of a request: it aborted its fetch, for whatever reason. A request
# still pending when a document unloads is recorded with no failure at all, so this is never that.
CANCELLED = "net::ERR_ABORTED"
# What a browser_network_requests call was told to save its result to, in the call's Args block.
SAVED_TO = re.compile(r'"filename":\s*"(?P<name>[^"\\]+)"')
REFUSAL = re.compile(r"expected-refusal:[ \t]*(?P<status>[^\s`]+)[ \t]+(?P<target>[^\s`)]+)", re.IGNORECASE)
REFUSABLE = ("400", "401", "403", "404", "409", "422")
HTML_COMMENT = re.compile(r"<!--.*?-->", re.DOTALL)
# Chromium's line for a response it refused to load, as playwright-mcp saves it: `<text> @ <url>:<line>`.
RESOURCE_ERROR = re.compile(r"^Failed to load resource: the server responded with a status of (?P<status>[0-9]{3})\b.* @ (?P<url>\S+):[0-9]+$")
# Where any console line says its error is: the script or resource, as ` @ <url>:<line>` at its end.
LOCATION = re.compile(r" @ (?P<url>\S*):[0-9]+$")


def read(path):
    with open(path, encoding="utf-8", errors="replace") as handle:
        return handle.read()


def relative(path, root):
    return os.path.relpath(path, root).replace(os.sep, "/")


def criterion_logs(root):
    """The step logs of the criteria: `qae/ACn.md` for the pull request's, `qae/TCn.md` for its ticket's."""
    return sorted(glob.glob(os.path.join(root, "qae", "AC*.md")) + glob.glob(os.path.join(root, "qae", "TC*.md")))


def steps_have_screenshots(root):
    logs = criterion_logs(root)
    if not logs:
        return [Finding("no step logs under qae/ (the explorer writes one qae/ACn.md, or qae/TCn.md for a ticket's, "
                        "per criterion)")]
    return screenshot_findings(root, logs + sorted(glob.glob(os.path.join(root, "qae", "features", "*.md"))))


def screenshot_findings(root, logs):
    """A finding per `- step k:` line whose `<log stem>-step-k.png` beside the log is missing or empty."""
    findings = []
    for log in logs:
        stem = os.path.basename(log)[:-3]
        for line in read(log).splitlines():
            match = STEP_LINE.match(line)
            if not match:
                continue
            shot = os.path.join(os.path.dirname(log), "%s-step-%s.png" % (stem, match.group("k")))
            if not os.path.isfile(shot) or os.path.getsize(shot) == 0:
                findings.append(Finding(missing_screenshot(match.group("k"), line, relative(shot, root)), relative(log, root)))
    return findings


def missing_screenshot(k, line, shot):
    """The finding for step k, whose screenshot `shot` is missing; a comparison with a reference says so."""
    named = named_references(line)
    if not named:
        return "step %s has no screenshot (expected %s)" % (k, shot)
    return ("step %s, the comparison with reference %s, has no screenshot (expected %s): a comparison is a step, so "
            "save its screenshot, at the reference's width, before writing its line" % (k, ", ".join(named), shot))


def screenshot_widths(root, log):
    """({widths of the step screenshots of one log}, [findings for the ones that are not PNGs])."""
    stem, seen, findings = os.path.basename(log)[:-3], set(), []
    for k in (m.group("k") for m in map(STEP_LINE.match, read(log).splitlines()) if m):
        shot = os.path.join(root, "qae", "%s-step-%s.png" % (stem, k))
        if not os.path.isfile(shot) or os.path.getsize(shot) == 0:
            continue  # already a finding: the step has no screenshot
        size = png_size(shot)
        if size is None:
            findings.append(Finding("step %s's screenshot is not a PNG, so its width cannot be read" % k, relative(log, root)))
        else:
            seen.add(size[0])
    return seen, findings


def widths_findings(root, widths):
    """A finding per criterion step log whose step screenshots miss a declared width."""
    findings = []
    for log in criterion_logs(root):
        seen, unreadable = screenshot_widths(root, log)
        findings += unreadable
        missing = [width for width in widths if width not in seen]
        if missing:
            findings.append(Finding("no step screenshot %s pixels wide (its widths: %s): check the criterion's end state "
                                    "at each width in qae-inputs/widths" % (" or ".join(map(str, missing)),
                                                                            ", ".join(map(str, sorted(seen))) or "none"),
                                    relative(log, root)))
    return findings


def width_list(text):
    """`1280,375` as [1280, 375]: what --widths takes, also read by actions/qae-inputs from the manifest line."""
    try:
        widths = [int(part) for part in text.split(",")]
    except ValueError:
        widths = []
    if not widths or any(width < 1 for width in widths):
        raise argparse.ArgumentTypeError("widths are positive whole numbers of pixels, comma-separated: %r" % text)
    return list(dict.fromkeys(widths))


def exact(url):
    return urllib.parse.urldefrag(url)[0]


def expected_refusals(items, sites):
    """The (status, URL) pairs the criteria `items` ((id, wording) pairs) declare expected, and findings
    for any declaration that cannot be honoured as written."""
    expected, findings = set(), []
    for ac, wording in items:
        for match in REFUSAL.finditer(wording):
            status, target = match.group("status"), match.group("target").rstrip(".,;:")
            if status not in REFUSABLE:
                findings.append(Finding("%s declares expected-refusal %s: only a handled refusal (%s) can be expected"
                                        % (ac, status, ", ".join(REFUSABLE))))
            elif urllib.parse.urlsplit(target).scheme in ("http", "https"):
                expected.add((status, exact(target)))
            elif target.startswith("/") and sites:
                expected.add((status, exact(urllib.parse.urljoin(sites[0], target))))
                expected.update((status, exact(origin.rstrip("/") + target)) for origin in sites[1:])
            else:
                findings.append(Finding("%s declares expected-refusal at %s: write a path starting with / (resolved against "
                                        "the site under test, which needs --site or --site-file) or an http(s) URL" % (ac, target)))
    return expected, findings


def judged(url, sites):
    """True when `url` is on an origin this run judges: the declared site or a further origin it
    declared, and every URL when none is declared."""
    return not sites or any(url.startswith(site) for site in sites)


def resource_error_is_excused(message, expected, sites):
    """True for Chromium's `Failed to load resource` line when its URL is on a host this run does
    not judge (like the request it reports), or is a refusal this run declared."""
    refused = RESOURCE_ERROR.match(message)
    if not refused:
        return False
    url = refused.group("url")
    return not judged(url, sites) or (refused.group("status"), exact(url)) in expected


def logging_pages(root):
    """{digest of a console error's line: the URLs of the pages that logged it}, from the record the
    harness keeps (actions/qae-browser/console-pages.js). A run without the record names no page."""
    pages, path = {}, os.path.join(root, "console-pages.jsonl")
    for line in read(path).splitlines() if os.path.isfile(path) else ():
        try:
            entry = json.loads(line)
            pages.setdefault(entry["sha256"], []).append(str(entry["page"]))
        except (ValueError, KeyError, TypeError):
            continue  # a line cut short names no page, so its error is judged
    return pages


def logged_off_site(message, pages, sites):
    """True when every page that logged this console error is an http(s) page outside the origins
    this run judges, and the error is not about a URL on one of them either."""
    logged_on = pages.get(hashlib.sha256(message.encode("utf-8")).hexdigest())
    about = LOCATION.search(message)
    if not logged_on or (about and judged(about.group("url"), sites)):
        return False
    return all(is_origin(page) and not judged(page, sites) for page in logged_on)


def console_is_clean(root, allowed, expected, sites):
    findings, seen, pages = [], set(), logging_pages(root)
    for log in sorted(glob.glob(os.path.join(root, "console-*.log"))):
        for number, line in enumerate(read(log).splitlines(), 1):
            match = CONSOLE_ERROR.match(line)
            if not match:
                continue
            message = match.group("message")
            if any(pattern.search(message) for pattern in allowed) or message in seen:
                continue
            if resource_error_is_excused(message, expected, sites) or logged_off_site(message, pages, sites):
                continue
            seen.add(message)
            findings.append(Finding("console error outside the allowlist: %s" % message[:200], relative(log, root), number))
    return findings


def site_failed(request):
    """True when the request the REQUEST match holds answered 400 or worse, or failed outright."""
    status = request.group("status")
    return status == "FAILED" or int(status) >= 400


def cancelled(request):
    """True when the page cancelled the request the REQUEST match holds before any answer."""
    return request.group("status") == "FAILED" and request.group("reason") == CANCELLED


def same_request(request):
    return request.group("method"), exact(request.group("url"))


def saved_network_records(root, sessions):
    """The file each browser_network_requests call saved its result to, when it named one. The name
    is as the explorer gave it, from the workspace (`qae-artifacts/qae/AC1-network.txt`), and is
    looked up inside the evidence directory, which is all the verify job holds."""
    files, findings = set(), []
    evidence = os.path.basename(os.path.normpath(root)) + "/"
    for session in sessions:
        calls = TOOL_CALL.split(read(session))
        for name, body in zip(calls[1::2], calls[2::2]):
            saved = SAVED_TO.search(body.partition("- Result")[0]) if name == "browser_network_requests" else None
            if not saved:
                continue
            path = os.path.normpath(os.path.join(root, saved.group("name").rpartition(evidence)[2]))
            if path.startswith(os.path.join(os.path.normpath(root), "")) and os.path.isfile(path):
                files.add(path)
            else:
                findings.append(Finding("network record saved to %s, which the evidence does not hold: save it under the "
                                        "evidence directory, or call browser_network_requests without a filename"
                                        % saved.group("name")[:200], relative(session, root)))
    return sorted(files), findings


def session_and_network(root, allowed, sites, expected):
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
    saved, unreadable = saved_network_records(root, sessions)
    findings += unreadable
    return findings + request_findings(root, sessions + saved + sorted(glob.glob(os.path.join(root, "network-*.log"))),
                                       allowed, sites, expected)


def request_findings(root, sources, allowed, sites, expected):
    """A finding per request to a judged origin that answered 400 or worse or failed, across every
    network record of the run. What excuses a cancelled one, beyond the allowlist, is that same
    request answered below 400 anywhere in those records."""
    records = [(source, match) for source in sources for match in REQUEST.finditer(read(source))]
    answered = {same_request(match) for _, match in records if not site_failed(match)}
    findings, seen = [], set()
    for source, match in records:
        url, status = match.group("url"), match.group("status")
        if not site_failed(match) or not judged(url, sites) or any(pattern.search(url) for pattern in allowed):
            continue
        if (status, exact(url)) in expected or (cancelled(match) and same_request(match) in answered):
            continue
        key = (match.group("method"), url, status)
        if key in seen:
            continue
        seen.add(key)
        findings.append(Finding(request_failure(match), relative(source, root)))
    return findings


def request_failure(request):
    """The finding for a failed request: one the page cancelled says what would have excused it."""
    line = "[%s] %s" % (request.group("method"), request.group("url")[:200])
    if cancelled(request):
        return ("request cancelled and never answered in this run, outside the allowlist: %s (%s is the page's own "
                "cancellation, excused only when the record also shows this request answered below 400)" % (line, CANCELLED))
    return "request answered %s outside the allowlist: %s" % (request.group("status"), line)


def is_origin(word):
    parts = urllib.parse.urlsplit(word)
    return parts.scheme in ("http", "https") and bool(parts.netloc)


def declared_sites(path):
    """The http(s) URLs the workflow wrote to `path`: the site under test first, then any further
    origin it is served from. Anything else means the declared input is wrong, and judging every
    request instead would pass a run against the wrong site."""
    if not os.path.isfile(path):
        raise CannotRun("site file not found: %s" % path)
    words = read(path).split()
    if not words or not all(is_origin(word) for word in words):
        raise CannotRun("site file %s does not hold http(s) URLs only: %r" % (path, " ".join(words)[:200]))
    return words


def read_declared(path, label):
    """The text of an input file the manifest line names, "" when it names none."""
    if not path:
        return ""
    if not os.path.isfile(path):
        raise CannotRun("%s file not found: %s" % (label, path))
    return read(path)


def check(args):
    root = args.artifacts
    if not root or not os.path.isdir(root):
        raise CannotRun("artifact directory not found: %s" % (root or "(none given)"))
    body = read_declared(args.criteria, "criteria")
    ticket = ticket_items(HTML_COMMENT.sub("", read_declared(args.ticket, "ticket")))[0]
    reason = declares_none(body)
    if reason and not ticket:
        print("%s: nothing was required, declared: %s" % (GATE, reason))
        return []
    # Resolved only once something is being judged: a pull request that declares None runs no
    # explorer, so its site step may never have declared a URL, and that must not fail it.
    sites = declared_sites(args.site_file) if args.site_file else [args.site] if args.site else []
    console_allow = [re.compile(pattern) for pattern in (args.allow_console or [])]
    request_allow = [re.compile(pattern) for pattern in (args.allow_request or [])]
    items = ([] if reason else body_criteria(HTML_COMMENT.sub("", body))) + ticket
    expected, declared = expected_refusals(items, sites)
    return (declared
            + steps_have_screenshots(root)
            + (widths_findings(root, args.widths) if args.widths else [])
            + console_is_clean(root, console_allow, expected, sites)
            + session_and_network(root, request_allow, sites, expected))


def add_arguments(parser):
    parser.add_argument("--artifacts", metavar="DIR", required=True)
    parser.add_argument("--criteria", metavar="FILE", default=None)
    parser.add_argument("--allow-console", action="append", metavar="REGEX")
    parser.add_argument("--allow-request", action="append", metavar="REGEX")
    parser.add_argument("--widths", metavar="N[,N...]", type=width_list, default=None)
    parser.add_argument("--ticket", metavar="FILE", default=None)
    site = parser.add_mutually_exclusive_group()
    site.add_argument("--site", metavar="URL-PREFIX", default=None)
    site.add_argument("--site-file", metavar="FILE", default=None)


if __name__ == "__main__":
    run_gate(GATE, __doc__, check, add_arguments)
