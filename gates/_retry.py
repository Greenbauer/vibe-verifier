"""A step the QAE explorer took once more because the environment failed it, as the qae-artifacts gate reads it.

Why, and the explorer's side: harnesses/qae/README.md, "A step retried after an environment error". The explorer
saves the failed step's records by filename after its screenshot (qae/ACn-step-k-network.txt, -console.txt), then
logs `- step k+1: [as <role>:] retry of step k: <step k's action> -> <what it saw>`. The retry counts when:

- it is step k+1 of a log that numbers no step twice, step k is not a retry, nothing else retries it, and no other step takes step k's action;
- its action (text before `->`, marker removed) is step k's word for word;
- its page-changing calls in the session log (every argument but element refs and descriptions; reads left out) are the last of step k's, which may follow a sign-in or the criterion's opening navigation;
- a record of step k saved by its tool after step k's screenshot, before the retry's first page-changing call, shows an environment error, and
  none on a URL the pull request's changed files name: a request answered 500 or above, a request or Chromium's
  console line for a resource failed with a connection error (CONNECTION, never ERR_ABORTED), a console line
  `WebSocket connection to '<url>' failed` (each on an unnamed URL), or Vercel's protection login
  (https://vercel.com/sso-api, or https://vercel.com/login whose query names /sso-api) as a request or as the page
  URL a call of step k reports; a step may be shot more than once if nothing changed the page between.

A changed path names a URL when a word of the URL's path (3 characters or more, not GENERIC, not a record id or
number) is part of a word of the path, letters and digits only. A URL with no such word is named, and with no
--changed-files every URL is. The match cannot see a change reached through a shared module, and no check can tell
which error made a step fail: a real bug passes only when it fails once, works on the retry, and its request was
answered below 400 in the run.

A retry that does not count is a finding. One that counts excuses its errors by status and URL across the run: a
request only when the same request was answered below 400 in a browser_network_requests result, a file one saved,
network-*.log or the page record (never text the explorer chose: a step log, a verdict, another call); a websocket on
the retry alone. Whether the retried step held is the explorer's verdict, judged by acceptance-verdict.
"""
import glob
import os
import re
import urllib.parse

from _contract import Finding
from _page_record import digest, page_record
from _session_log import acts, page_urls, read, result_field, screenshots, tool_calls

STEP = re.compile(r"^[ \t]*-[ \t]+step[ \t]+(?P<k>[0-9]+):(?P<text>.*)$", re.IGNORECASE)
RETRY = re.compile(r"^(?P<role>[ \t]*(?:as[ \t]+[^:>]+:)?)[ \t]*retry[ \t]+of[ \t]+step[ \t]+(?P<k>[0-9]+)[ \t]*:",
                   re.IGNORECASE)
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


def request_errors(text):
    """(description, URL, excuse) per error a network record's text shows: a 5xx or a connection failure. Vercel's
    protection login is one too, with no URL to judge and nothing to excuse (it is not on the site's origin)."""
    for request in REQUEST.finditer(text):
        method, url, status = request.group("method", "url", "status")
        failed = status == "FAILED" and CONNECTION.fullmatch(request.group("reason") or "")
        if protected(url) and status != "FAILED":
            yield "the site answered with Vercel's deployment protection login", None, None
        elif failed or (status != "FAILED" and int(status) >= 500):
            said = "failed (%s)" % request.group("reason") if failed else "answered " + status
            yield "%s %s %s" % (method, shown(url), said), url, (status, exact(url), method)


def console_errors(text):
    """(description, URL, excuse) per error a console record's text shows: a connection failure Chromium reports for
    a resource the network record leaves out (a static chunk, always a GET), and a failed websocket."""
    for resource in RESOURCE.finditer(text):
        url, reason = resource.group("url", "reason")
        if CONNECTION.fullmatch(reason):
            yield "GET %s failed (%s)" % (shown(url), reason), url, ("FAILED", exact(url), "GET")
    for socket in WEBSOCKET.finditer(text):
        url = socket.group("url")
        yield "the websocket to %s failed" % shown(url), url, ("WEBSOCKET", exact(url), None)


def environment(texts, changed, pages=()):
    """([description of each environment error], [(status, URL, method) to excuse], [what the pull request's own
    URLs answered]) in the records `texts` of one step and the page URLs its calls report. The network record leaves
    out a document answered below 400, so Vercel's protection login shows only as the page's own URL."""
    errors = ["the site answered with Vercel's deployment protection login"] if any(map(protected, pages)) else []
    excuses, own = [], []
    for said, url, excuse in (error for text in texts for error in (*request_errors(text), *console_errors(text))):
        if url is not None and named(url, changed):
            own.append(said)
            continue
        errors.append(said)
        if excuse:
            excuses.append(excuse)
    return list(dict.fromkeys(errors)), excuses, list(dict.fromkeys(own))


def step_records(root, calls, item, k):
    """The texts of step k's records (qae/<item>-step-k-network.txt, -console.txt) that one of `calls` saved."""
    texts = []
    for (tool, kind), (name, saved, _, _) in ((record, call) for call in calls for record in RECORDS):
        own = "qae/%s-step-%s-%s.txt" % (item, k, kind)
        if name == tool and saved.endswith(own) and os.path.isfile(os.path.join(root, own)):
            texts.append(read(os.path.join(root, own)))
    return texts


def window(root, calls, item, k, m):
    """(the texts of step k's records saved after its screenshot and before the retry changed the page, what step k
    did, what it did between two screenshots of itself, what step m did, the page URLs step k's calls report), or
    None. Step k's calls run from the screenshot before it (of any step) to its own."""
    for session in calls:
        found = screenshots(session, item, k, m)
        if found:
            start, first, last, retry, retried = found
            pages = [url for call in session[start + 1:last] for url in page_urls(call[3])]
            redo = next((i for i in range(last + 1, retry) if acts(session[i:i + 1])), retry)
            return (step_records(root, session[first + 1:redo], item, k), acts(session[start + 1:first]),
                    acts(session[first + 1:last]) + acts(session[retry + 1:retried]), acts(session[last + 1:retry]), pages)
    return None


class Retry:
    """Step k of `item` taken again as step m: the environment errors its records show and whether it counts."""

    def __init__(self, item, k, m, errors=(), excuses=(), problem=None):
        self.item, self.k, self.m = item, k, m
        self.errors, self.excuses, self.problem, self.unexcused = list(errors), list(excuses), problem, []

    def describe(self):
        if self.problem:
            return "step %s retried as step %s, which does not count: %s" % (self.k, self.m, self.problem)
        unexcused = "; not excused, never answered below 400 in this run: %s" % ", ".join(self.unexcused)
        return "step %s retried once as step %s after an environment error: %s%s" % (
            self.k, self.m, "; ".join(self.errors), unexcused if self.unexcused else "")


def shown_acts(done):
    return "(%s)" % ", ".join(name + (" " + shown(url) if url else "") for name, (url, _) in done) if done else "(none)"


def action(text):
    return " ".join(text.split("->")[0].split()).casefold().rstrip(".,;:")


def log_retries(root, calls, item, text, changed):
    lines = [(int(step.group("k")), step.group("text")) for step in map(STEP.match, text.splitlines()) if step]
    steps, retried = dict(reversed(lines)), {}
    for m, said in lines:
        marker = RETRY.match(said)
        if marker:
            retried.setdefault(int(marker.group("k")), []).append((m, marker.group("role") + said[marker.end():]))
    twice = sorted(n for n in steps if [k for k, _ in lines].count(n) > 1)
    if twice:  # a step number used twice hides a step from every rule below
        return [Retry(item, k, m, problem="the log numbers step %s more than once, so its steps cannot be told apart"
                      % twice[0]) for k, again in sorted(retried.items()) for m, _ in again]
    return [judge(root, calls, item, steps, k, m, repeated, len(again), changed)
            for k, again in sorted(retried.items()) for m, repeated in again]


def log_problem(steps, k, m, repeated, times):
    """Why the step log alone rules the retry of step k as step m out (its words `repeated`), or None."""
    if k not in steps or m != k + 1:
        return ("it is not the step right after step %s: a retry takes the failed step again at once, before the "
                "criterion is walked any further" % k)
    if RETRY.match(steps[k]):
        return "step %s is a retry itself, and a step is retried once" % k
    if times > 1:
        return "step %s is retried %d times, and a step is retried once" % (k, times)
    again = [n for n, said in steps.items() if n not in (k, m) and action(said) == action(steps[k])]
    if again:
        return "step %s takes step %s's action too, and a step is taken once and retried once" % (again[0], k)
    if action(repeated) != action(steps[k]):
        return ("its action is not step %s's word for word (%r, step %s: %r), and a retry takes the same step again, "
                "never another way" % (k, action(repeated)[:120], k, action(steps[k])[:120]))
    return None


def record_problem(item, k, m, texts, did, between, redid):
    """Why the session log rules the retry out: no record of step k saved in its window, or calls that are not the
    end of step k's (before them may come the sign-in or navigation that opened the criterion). None when neither."""
    if not texts:
        return ("no record of step %s (qae/%s-step-%s-network.txt or -console.txt) was saved by its tool after step "
                "%s's screenshot and before the retry acted" % (k, item, k, k))
    if between:
        return ("the session log shows calls %s between two screenshots of step %s or of the retry: a step that changed "
                "the page after its screenshot is not one step" % (shown_acts(between), k))
    if (did and not redid) or redid != did[len(did) - len(redid):]:
        return ("the session log shows step %s's calls %s and the retry's %s: a retry makes the last of the step's "
                "calls again, with the same URLs and values, never another way" % (k, shown_acts(did), shown_acts(redid)))
    return None


def judge(root, calls, item, steps, k, m, repeated, times, changed):
    problem = log_problem(steps, k, m, repeated, times)
    texts, did, between, redid, pages = window(root, calls, item, k, m) or ([], [], [], [], [])
    problem = problem or record_problem(item, k, m, texts, did, between, redid)
    if problem:
        return Retry(item, k, m, problem=problem)
    errors, excuses, own = environment(texts, changed, pages)
    if own:
        why = (", and without the pull request's changed paths no URL can be told apart from its own: give the "
               "qae-artifacts line --changed-files qae-inputs/changed-files" if changed is None else
               ", on a URL the pull request's changed files name, so it may be the pull request's doing")
        return Retry(item, k, m, errors, problem="step %s's records show %s%s" % (k, "; ".join(own), why))
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
    found = [retry for item, text in texts.items() for retry in log_retries(root, calls, item, text, changed)]
    answers = answered(root, calls)
    for retry in (retry for retry in found if not retry.problem):
        excuse_answered(retry, *answers)
    return found


def excuse_answered(retry, seen, recorded):
    """Keep of a counted retry's excuses the ones the run shows answered below 400 (a websocket's has no method and
    stands on the retry); the rest are its `unexcused`, still findings of the gate."""
    kept = [(status, url, method) for status, url, method in retry.excuses
            if method is None or (method, url) in seen or digest("%s %s" % (method, url)) in recorded]
    retry.unexcused = ["%s %s" % (excuse[2], shown(excuse[1])) for excuse in retry.excuses if excuse not in kept]
    retry.excuses = kept


class _Urls:
    def __init__(self, urls):
        self.urls = frozenset(urls)

    def fullmatch(self, url):
        return exact(url) in self.urls


def answered(root, calls):
    """The requests (method, URL) answered below 400 in the run's network records: the result of each
    browser_network_requests call in the session logs (`calls`, from tool_calls), or the file it saved, and
    network-*.log; and the page record's answers. Never another call's arguments or result, a step log or a verdict,
    which hold text the explorer chose."""
    texts = [read(path) for path in glob.glob(os.path.join(root, "network-*.log"))]
    evidence = os.path.basename(os.path.normpath(root)) + "/"
    for name, saved, _, result in (call for session in calls for call in session):
        path = os.path.normpath(os.path.join(root, saved.rpartition(evidence)[2]))
        if name != "browser_network_requests":
            continue
        texts.append(result_field(result, "result"))
        if saved and path.startswith(os.path.join(os.path.normpath(root), "")) and os.path.isfile(path):
            texts.append(read(path))
    seen = {(r.group("method"), exact(r.group("url"))) for text in texts for r in REQUEST.finditer(text)
            if r.group("status") != "FAILED" and int(r.group("status")) < 400}
    return seen, page_record(root)[1]


def retry_checks(root, changed_files):
    """(findings, expected) for the qae-artifacts gate: a finding per retry that does not count, and the
    (status, URL pattern) pairs a counted one excuses, as the gate's expected refusals are written. Each counted
    retry is printed, so the log says what was retried and why, and which of its errors stay findings."""
    found = retries(root, changed_files)
    findings = [Finding(retry.describe(), "qae/%s.md" % retry.item) for retry in found if retry.problem]
    excused = {}
    for retry in (retry for retry in found if not retry.problem):
        print("qae-artifacts: %s: %s" % (retry.item, retry.describe()))
        for status, url, _ in retry.excuses:
            excused.setdefault(status, set()).add(url)
    return findings, [(status, _Urls(urls)) for status, urls in excused.items()]


def console_excused(message, expected):
    """True for Chromium's console line about a websocket or a connection a counted retry excused (a 5xx's own line
    is excused as an expected refusal is)."""
    first = message.split("\n")[0]
    socket, resource = WEBSOCKET.search(first), RESOURCE.search(first)
    status, url = ("WEBSOCKET", socket.group("url")) if socket else ("FAILED", resource.group("url")) if resource else (None, "")
    return any(status == want and pattern.fullmatch(url) for want, pattern in expected)
