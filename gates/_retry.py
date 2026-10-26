"""A step the QAE explorer took once more because the environment failed it, as the gates read it.

A walk can fail on something that is not the pull request's doing: a 5xx from an endpoint it does not change,
Vercel's deployment protection in place of the site, a websocket or network connection that drops. On the same
head a second walk then passes, but a blind second walk also passes when it skips the step that failed (a
2026-10-26 backtest of 89 repeated walks found 4 real bugs that way). So the explorer takes only that step once
more, and the qae-artifacts gate counts the retry only on structural evidence, never on the explorer's words.

The explorer (harnesses/qae/prompt.md) saves the moment's records by filename, right after step k's screenshot,
then logs the retry as a step of its own, naming step k and repeating its action word for word:

    - step 5: as admin: opened organization search -> the console reports a dashboard POST 500
    - step 6: as admin: retry of step 5: opened organization search -> the search is active

    qae/AC1-step-5-network.txt    browser_network_requests, filename given
    qae/AC1-step-5-console.txt    browser_console_messages, filename given

A retry counts when all of these hold:

- it is the step right after the one it names, in the same criterion's log, which is not a retry itself and no other
  retry names;
- its action, the text before `->` with the marker taken out (an `as <role>:` prefix stays), is step k's;
- a record of step k is in the evidence, and the session log shows the tool saved it after step k's screenshot and
  before the retry's;
- those records show an environment error, and no 5xx or failed connection on a URL the pull request changes:
    5xx                     a request answered 500 or above,
    network                 a request, or Chromium's console line for a resource, failed with a connection error (net::ERR_CONNECTION_*, ERR_TIMED_OUT,
                            ERR_NAME_NOT_RESOLVED, ...), never ERR_ABORTED, which is the page's own cancellation,
    websocket               a console line `WebSocket connection to '<url>' failed`,
  each on a URL the pull request's changed files do not name (below), or
    deployment protection   a request to Vercel's login for a protected deployment: https://vercel.com/sso-api, or
                            https://vercel.com/login whose query names /sso-api (checked on a protected preview,
                            2026-10-26: 302 to the first, which redirects to the second).

A changed file names a URL when one of the URL path's words (3 characters or more, not `api`, `rest`, `rpc`, `v1`
and the like, not a record id or a number) is part of a word of the file's path (a directory, or the file name
before its first dot), letters and digits only, case ignored: `/api/dashboard` is named by
`app/(main)/dashboard/page.tsx`, `/rest/v1/journeys` by `supabase/migrations/2026_journeys.sql`. A URL with no
such word is taken as named, and with no --changed-files every URL is: the rule only ever errs toward no retry.

A retry that does not count is a finding, so its criterion cannot pass on it. One that counts excuses its errors
in the qae-artifacts gate: a 5xx or failed request only when the same request (method and URL) was answered below
400 somewhere in the run's evidence, its network records or the page record, and a websocket failure on the retry
alone, since no record holds a websocket's answer. A repeat of the same error, by status and URL anywhere in the run, is
excused with it: whether the retried step held is the explorer's verdict, which acceptance-verdict judges, and a FAIL
there stays a FAIL.
"""
import glob
import os
import re
import urllib.parse

from _contract import Finding
from _page_record import digest, page_record

STEP = re.compile(r"^[ \t]*-[ \t]+step[ \t]+(?P<k>[0-9]+):(?P<text>.*)$", re.IGNORECASE)
RETRY = re.compile(r"^(?P<role>[ \t]*(?:as[ \t]+[^:>]+:)?)[ \t]*retry[ \t]+of[ \t]+step[ \t]+(?P<k>[0-9]+)[ \t]*:",
                   re.IGNORECASE)
TOOL_CALL = re.compile(r"^### Tool call: (?P<name>\S+)\s*$", re.MULTILINE)
# What a tool call was told to save its result to, in the call's Args block.
SAVED_TO = re.compile(r'"filename":\s*"(?P<name>[^"\\]+)"')
# `3. [GET] http://host/path => [404] Not Found` or `=> [FAILED] net::ERR_...`, as the tool renders it;
# inside a JSON result the newline is escaped, so the line is matched without anchors.
REQUEST = re.compile(r"[0-9]+\. \[(?P<method>[A-Z]+)\] (?P<url>\S+) => \[(?P<status>[0-9]{3}|FAILED)\](?: (?P<reason>net::[A-Z0-9_]+))?")
CONNECTION = re.compile(r"net::ERR_(?:CONNECTION_[A-Z_]+|TIMED_OUT|NAME_NOT_RESOLVED|NETWORK_CHANGED|INTERNET_DISCONNECTED"
                        r"|EMPTY_RESPONSE|ADDRESS_UNREACHABLE)")
WEBSOCKET = re.compile(r"WebSocket connection to '(?P<url>[^']+)' failed")
RESOURCE = re.compile(r"Failed to load resource: (?P<reason>net::ERR_[A-Z_]+) @ (?P<url>\S+):[0-9]+$", re.MULTILINE)
GENERIC = re.compile(r"(?:api|rest|rpc|graphql|v[0-9]+|next)")
RECORD_ID = re.compile(r"[0-9]+|[0-9a-f-]{16,}|[0-9a-hjkmnp-tv-z]{26}", re.IGNORECASE)
RECORDS = (("browser_network_requests", "network"), ("browser_console_messages", "console"))


def exact(url):
    return urllib.parse.urldefrag(url)[0]


def word(text):
    return re.sub(r"[^a-z0-9]", "", text.lower())


def changed_paths(path):
    """The changed paths the manifest line names (--changed-files), or None when it names none."""
    if not path or not os.path.isfile(path):
        return None
    with open(path, encoding="utf-8", errors="replace") as handle:
        return [line.strip() for line in handle if line.strip()]


def named(url, changed):
    """True when a changed path names `url` (see the module docstring), or when that cannot be told."""
    segments = [urllib.parse.unquote(part) for part in urllib.parse.urlsplit(url).path.split("/")]
    words = [word(part) for part in segments if part and not RECORD_ID.fullmatch(part)]
    words = [w for w in words if len(w) >= 3 and not GENERIC.fullmatch(w)]
    if not words or changed is None:
        return True
    parts = {word(part.split(".")[0]) for path in changed for part in path.split("/")}
    return any(w in part for w in words for part in parts if part)


def protected(url):
    parts = urllib.parse.urlsplit(url)
    return parts.hostname == "vercel.com" and (parts.path == "/sso-api" or (
        parts.path == "/login" and "/sso-api" in urllib.parse.unquote(parts.query)))


def shown(url):
    """A URL as a review line names it: its path, never its query, which may carry a key."""
    parts = urllib.parse.urlsplit(url)
    return (parts.netloc if parts.hostname and parts.path in ("", "/") else "") + (parts.path or "/")


def environment(texts, changed):
    """([description of each environment error], [(status, URL, method) to excuse], [what the pull request's own
    URLs answered]) in the records `texts` of one step."""
    errors, excuses, own = [], [], []

    def found(said, url, excuse):
        if named(url, changed):
            own.append(said)
        else:
            errors.append(said)
            excuses.append(excuse)
    for text in texts:
        for request in REQUEST.finditer(text):
            method, url, status = request.group("method", "url", "status")
            if protected(url):
                errors.append("the site answered with Vercel's deployment protection login")
                continue
            failed = status == "FAILED" and CONNECTION.fullmatch(request.group("reason") or "")
            if not (failed or (status != "FAILED" and int(status) >= 500)):
                continue
            said = "%s %s %s" % (method, shown(url), "failed (%s)" % request.group("reason") if failed else "answered " + status)
            found(said, url, (status, exact(url), method))
        # A connection the console reports and the network record leaves out (a static chunk): a GET, by Chromium.
        for resource in RESOURCE.finditer(text):
            url = resource.group("url")
            if CONNECTION.fullmatch(resource.group("reason")):
                found("GET %s failed (%s)" % (shown(url), resource.group("reason")), url, ("FAILED", exact(url), "GET"))
        for socket in WEBSOCKET.finditer(text):
            url = socket.group("url")
            found("the websocket to %s failed" % shown(url), url, ("WEBSOCKET", exact(url), None))
    return list(dict.fromkeys(errors)), excuses, list(dict.fromkeys(own))


def read(path):
    with open(path, encoding="utf-8", errors="replace") as handle:
        return handle.read()


def tool_calls(root):
    """[(tool name, the file its Args saved to or '')] per session log, in the order called."""
    sessions = []
    for session in sorted(glob.glob(os.path.join(root, "session-*", "session.md"))):
        calls, listed = TOOL_CALL.split(read(session)), []
        for name, body in zip(calls[1::2], calls[2::2]):
            saved = SAVED_TO.search(body.partition("- Result")[0])
            listed.append((name, saved.group("name") if saved else ""))
        sessions.append(listed)
    return sessions


def records(root, calls, item, k, m):
    """The texts of step k's records the session log shows saved between step k's screenshot and step m's."""
    shot = "qae/%s-step-%%s.png" % item
    texts = []
    for session in calls:
        shots = [i for i, (name, saved) in enumerate(session) if name == "browser_take_screenshot"]
        after = [i for i in shots if session[i][1].endswith(shot % m)]
        before = [i for i in shots if session[i][1].endswith(shot % k) and after and i < after[0]]
        if not before:
            continue
        for name, saved in session[before[-1] + 1:after[0]]:
            for tool, kind in RECORDS:
                own = "qae/%s-step-%s-%s.txt" % (item, k, kind)
                if name == tool and saved.endswith(own) and os.path.isfile(os.path.join(root, own)):
                    texts.append(read(os.path.join(root, own)))
    return texts


class Retry:
    """Step k of `item` taken again as step m: the environment errors its records show and whether it counts."""

    def __init__(self, item, k, m, errors=(), excuses=(), problem=None):
        self.item, self.k, self.m = item, k, m
        self.errors, self.excuses, self.problem = list(errors), list(excuses), problem

    def describe(self):
        if self.problem:
            return "step %s retried as step %s, which does not count: %s" % (self.k, self.m, self.problem)
        return "step %s retried once as step %s after an environment error: %s" % (self.k, self.m, "; ".join(self.errors))


def action(text):
    return " ".join(text.split("->")[0].split()).casefold().rstrip(".,;:")


def log_retries(root, calls, item, text, changed):
    steps, retried, found = {}, {}, []
    for line in text.splitlines():
        step = STEP.match(line)
        if step:
            steps.setdefault(int(step.group("k")), step.group("text"))
    for m, said in sorted(steps.items()):
        marker = RETRY.match(said)
        if marker:
            retried.setdefault(int(marker.group("k")), []).append((m, marker.group("role") + said[marker.end():]))
    for k, again in sorted(retried.items()):
        for m, repeated in again:
            found.append(judge(root, calls, item, steps, k, m, repeated, len(again), changed))
    return found


def judge(root, calls, item, steps, k, m, repeated, times, changed):
    if k not in steps or m != k + 1:
        return Retry(item, k, m, problem="it is not the step right after step %s: a retry takes the failed step again at "
                                         "once, before the criterion is walked any further" % k)
    if RETRY.match(steps[k]):
        return Retry(item, k, m, problem="step %s is a retry itself, and a step is retried once" % k)
    if times > 1:
        return Retry(item, k, m, problem="step %s is retried %d times, and a step is retried once" % (k, times))
    if action(repeated) != action(steps[k]):
        return Retry(item, k, m, problem="its action is not step %s's word for word (%r, step %s: %r), and a retry takes "
                                         "the same step again, never another way" % (k, action(repeated)[:120], k,
                                                                                     action(steps[k])[:120]))
    texts = records(root, calls, item, k, m)
    if not texts:
        return Retry(item, k, m, problem="no record of step %s (qae/%s-step-%s-network.txt or -console.txt) was saved "
                                         "by its tool between step %s's screenshot and step %s's" % (k, item, k, k, m))
    errors, excuses, own = environment(texts, changed)
    if own and changed is None:
        return Retry(item, k, m, errors, problem="step %s's records show %s, and without the pull request's changed paths "
                                                 "no URL can be told apart from its own: give the qae-artifacts line "
                                                 "--changed-files qae-inputs/changed-files" % (k, "; ".join(own)))
    if own:
        return Retry(item, k, m, errors, problem="step %s's records show %s, on a URL the pull request's changed files "
                                                 "name, so it may be the pull request's doing" % (k, "; ".join(own)))
    if not errors:
        return Retry(item, k, m, problem="step %s's records show no environment error (a 5xx, a failed connection or "
                                         "websocket, Vercel's deployment protection) on a URL the pull request does not "
                                         "change" % k)
    return Retry(item, k, m, errors, excuses)


def retries(root, changed_files):
    """Every retry in the criteria's step logs (qae/ACn.md, qae/TCn.md), judged."""
    if not root or not os.path.isdir(root):
        return []
    logs = sorted(glob.glob(os.path.join(root, "qae", "AC*.md")) + glob.glob(os.path.join(root, "qae", "TC*.md")))
    texts = {os.path.basename(log)[:-3]: read(log) for log in logs}
    if not any(RETRY.match(step.group("text")) for text in texts.values()
               for step in map(STEP.match, text.splitlines()) if step):
        return []
    calls, changed = tool_calls(root), changed_paths(changed_files)
    return [retry for item, text in texts.items() for retry in log_retries(root, calls, item, text, changed)]


class _Urls:
    def __init__(self, urls):
        self.urls = frozenset(urls)

    def fullmatch(self, url):
        return exact(url) in self.urls


def answered(root):
    """The requests (method, URL) answered below 400 anywhere in the evidence, and the page record's answers."""
    seen = set()
    for path in glob.glob(os.path.join(root, "**", "*"), recursive=True):
        if path.endswith((".txt", ".log", ".md")) and os.path.isfile(path):
            seen.update((r.group("method"), exact(r.group("url"))) for r in REQUEST.finditer(read(path))
                        if r.group("status") != "FAILED" and int(r.group("status")) < 400)
    return seen, page_record(root)[1]


def retry_checks(root, changed_files):
    """(findings, expected) for the qae-artifacts gate: a finding per retry that does not count, and the
    (status, URL pattern) pairs a counted one excuses, as the gate's expected refusals are written. Each counted
    retry is printed, so the log says what was retried and why."""
    found = retries(root, changed_files)
    findings = [Finding(retry.describe(), "qae/%s.md" % retry.item) for retry in found if retry.problem]
    counted = [retry for retry in found if not retry.problem]
    if not counted:
        return findings, []
    seen, recorded = answered(root)
    excused = {}
    for retry in counted:
        print("qae-artifacts: %s: %s" % (retry.item, retry.describe()))
        for status, url, method in retry.excuses:
            if method is None or (method, url) in seen or digest("%s %s" % (method, url)) in recorded:
                excused.setdefault(status, set()).add(url)
            else:
                print("qae-artifacts: %s: %s %s is not excused: it was never answered below 400 in this run"
                      % (retry.item, method, shown(url)))
    return findings, [(status, _Urls(urls)) for status, urls in excused.items()]


def console_excused(message, expected):
    """True for Chromium's console line about a websocket or a connection a counted retry excused (a 5xx's own line
    is excused as an expected refusal is)."""
    first = message.split("\n")[0]
    socket, resource = WEBSOCKET.search(first), RESOURCE.search(first)
    status, url = ("WEBSOCKET", socket.group("url")) if socket else ("FAILED", resource.group("url")) if resource else (None, "")
    return any(status == want and pattern.fullmatch(url) for want, pattern in expected)
