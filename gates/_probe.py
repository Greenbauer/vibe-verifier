"""A 400 or 422 the site answered to the QAE explorer's invalid-input probe, as the qae-artifacts gate reads it.

Why, and the explorer's side: harnesses/qae/README.md, "An invalid input refused with a handled error". On a screen
with a form the explorer tries one invalid input as one step: it enters it, takes a whole-page browser_snapshot, takes
the action that sends it, takes a browser_snapshot once the page has answered, and logs the step
`- step k: [as <role>:] invalid input: <what it did> -> <what it saw>`. A 400 or 422 counts as a handled refusal of
that step when:

- the criterion's log marks step k, no other step of its role, and numbers no step twice;
- Chromium's console line for the answer (`Failed to load resource: the server responded with a status of 400`, at
  its URL) is one of the console entries playwright-mcp says a call of step k logged (the call's `events`, `New
  console entries: <file>#La-Lb`); step k's calls run from the screenshot before its first, of any step, to that one;
- it is the only 400 or 422 those calls logged, its call is step k's last page-changing call or a read after it,
  and none of them ran a script (browser_evaluate, browser_run_code): the answer to what the last action sent;
- the run's network records list that answer only to a write (POST, PUT, PATCH, DELETE), as a form sends;
- the snapshot of the whole page a call of step k wrote right before that action (no page-changing call between,
  never a browser_snapshot given a depth or an element, never one of an earlier step) and the last one written from the answer's call to step k's
  screenshot (a call's `snapshot` or `inlineSnapshot` field, never a script's result) show, as gates/_snapshot.py
  compares them, the page answering in place:
  - at the same page URL (the `page` field);
  - keeping KEPT of the elements before (same ref, role and name), and every control the step acted on, each by a
    snapshot ref (a control named by a selector cannot be checked, so the step is not excused);
  - with an element's own text it did not show before anywhere, holding no value typed in the session so far, under a ref it did
    not show, beside a control the step used (gates/_snapshot.py, new_text);
- and none of step k's console entries is an uncaught exception.

The excuse is by status and URL, as a declared refusal's is, so it holds only where it cannot reach another step:
every such console line of that status and URL in the run is one a counted probe logged, and the run's network
records list that answer under no more request numbers than there are of those lines. A 5xx, any other status, an
answer no call of the step logged, and a step the log does not mark are never excused. What the gate cannot read:
whether the new text is an error message, and which write the form sent, when the action set off another write that
the site refused in its place while the form showed text of its own.
"""
import glob
import json
import os
import re

from _retry import STEP, exact, shown
from _snapshot import KEPT, elements, kept, new_text
from _session_log import READS, REF_ID, TARGETS, console_lines, network_texts, page_urls, read, saved_to, session_calls, \
    snapshot, tool_calls

PROBE = re.compile(r"^(?P<role>(?:[ \t]*as[ \t]+[^:>]+:)?)[ \t]*invalid[ \t]+input[ \t]*:", re.IGNORECASE)
# A console log entry: its offset, then its text; an uncaught exception's has no level tag (gates/qae_artifacts.py).
ENTRY = re.compile(r"^\[\s*-?[0-9]+ms\]\s+(?P<text>.*)$")
LEVEL = re.compile(r"^\[[A-Z]+\]\s")
REFUSED = re.compile(r"^\[ERROR\] Failed to load resource: the server responded with a status of (?P<status>400|422)\b"
                     r".* @ (?P<url>\S+):[0-9]+$")
# An Args key that names the element a call acts on, at any depth: a snapshot ref (REF_ID), or else a selector.
TARGET = re.compile(r'"(?:%s)":\s*"(?P<target>(?:[^"\\]|\\.)*)"' % "|".join(TARGETS))
# What a call typed, filled or selected, at any depth of its Args: never a message the page put up.
TYPED = re.compile(r'"(?:value|text)":\s*"(?P<value>(?:[^"\\]|\\.)*)"')
# Tools that run the explorer's own script, which can send a request and change the page itself.
SCRIPTS = frozenset("browser_evaluate browser_run_code browser_run_code_unsafe".split())
# What makes browser_snapshot show part of the page: a depth, or an element to start from.
PARTIAL = re.compile(r'"(?:depth|ref|target|selector)"\s*:')
WRITES = frozenset("POST PUT PATCH DELETE".split())
NUMBERED = re.compile(r"(?P<n>[0-9]+)\. \[(?P<method>[A-Z]+)\] (?P<url>\S+) => \[(?P<status>[0-9]{3})\]")


class Probe:
    """Step k of `item`, the invalid-input probe: the 400 and 422 answers its calls logged and whether they count."""

    def __init__(self, item, k, refusals, problem=None):
        self.item, self.k, self.refusals, self.problem, self.withdrawn = item, k, refusals, problem, None

    def describe(self):
        said = ", ".join("%s %s" % (status, shown(url)) for status, url in sorted(self.refusals))
        why = self.problem or self.withdrawn
        if why:
            return "step %s, the invalid-input probe, was answered %s, not excused: %s" % (self.k, said, why)
        return ("step %s, the invalid-input probe, was answered %s while the page kept the controls it used and showed "
                "new text: excused as a handled refusal" % (self.k, said))


def is_act(name):
    return name.removeprefix("browser_") not in READS


def targets(args):
    """The elements a call's Args act on (a ref, a target, a form field's): snapshot refs, or selectors."""
    return [found.group("target") for found in TARGET.finditer(args)]


def window(session, item, k):
    """(index of the first call of step k, index of its first screenshot), or None when no call shot step k."""
    shots = [i for i, (name, args, _) in enumerate(session) if name == "browser_take_screenshot"]
    own = [i for i in shots if saved_to(session[i][1]).endswith("qae/%s-step-%s.png" % (item, k))]
    if not own:
        return None
    return max([i for i in shots if i < own[0]], default=-1) + 1, own[0]


class Console:
    """The run's console logs, line by line, as playwright-mcp wrote them (console-*.log in the evidence)."""

    def __init__(self, root):
        self.lines = {os.path.relpath(path, root): read(path).splitlines()
                      for path in glob.glob(os.path.join(root, "console-*.log"))}

    def entry(self, place):
        path, number = place
        lines = self.lines.get(path, [])
        match = ENTRY.match(lines[number - 1]) if 0 < number <= len(lines) else None
        return match.group("text") if match else None

    def refusals(self):
        """{(status, URL): {(log, line number)}} for every Chromium line of a 400 or 422 in the run."""
        found = {}
        for path, lines in self.lines.items():
            for number in range(1, len(lines) + 1):
                refused = REFUSED.match(self.entry((path, number)) or "")
                if refused:
                    found.setdefault((refused.group("status"), exact(refused.group("url"))), set()).add((path, number))
        return found


def logged(root, console, session, start, end):
    """({(status, URL): {(log, line)}} of the 400 and 422 answers the calls start..end logged, the index of the last call
    that logged one or -1, and whether one of their entries is an uncaught exception)."""
    refusals, last, crashed = {}, -1, False
    for i in range(start, end + 1):
        for place in console_lines(root, session[i][2]):
            text = console.entry(place)
            refused = REFUSED.match(text or "")
            crashed = crashed or (text is not None and not LEVEL.match(text))
            if refused:
                refusals.setdefault((refused.group("status"), exact(refused.group("url"))), set()).add(place)
                last = i
    return refusals, last, crashed


def typed(args):
    values = []
    for found in TYPED.finditer(args):
        try:
            values.append(" ".join(json.loads('"%s"' % found.group("value")).split()).casefold())
        except ValueError:
            continue
    return [value for value in values if value]


def whole(call):
    """The snapshot of the whole page a call wrote, or '': browser_snapshot given a depth or an element shows part."""
    name, args, result = call
    return "" if name == "browser_snapshot" and PARTIAL.search(args) else snapshot(result)


def page_problem(session, start, acted, last, end):
    """Why the snapshot a call of the step (from `start`) wrote right before its last action (`acted`, page-changing
    call indexes) and the last one from the answer's call `last` to the screenshot `end` do not show the page
    answering in place, or None."""
    back = next((i for i in range(acted[-1] - 1, start - 1, -1) if whole(session[i]) or is_act(session[i][0])), None)
    if back is None or not whole(session[back]):
        return ("no snapshot of the whole page was written in the step right before its last action, so what the action "
                "changed cannot be told")
    after = [i for i in range(last, end + 1) if snapshot(session[i][2])]
    if not after:
        return "no snapshot of the page was written after the answer, before the step's screenshot"
    used = [target for i in acted for target in targets(session[i][1])]
    values = [value for name, args, _ in session[:last + 1] if is_act(name) for value in typed(args)]
    return state_problem(session[back][2], session[after[-1]][2], used, values)


def state_problem(before, after, used, values):
    """Why the snapshots in the Results `before` and `after` the step's last action do not show the page at the same
    URL, keeping its elements and the controls `used` (targets) and putting up new text beside them, or None."""
    was, now = snapshot(before), snapshot(after)
    url = (page_urls(before) or [None])[-1]
    if url is None or url not in page_urls(after):
        return "the page is not at the URL it was at before the step's last action (%s)" % (shown(url) if url else "none shown")
    refs = [target for target in used if REF_ID.fullmatch(target)]
    if not used or len(refs) < len(used):
        return "the step acted on a control by a selector, or on none: only a snapshot ref names an element both snapshots show"
    gone = [ref for ref in refs if elements(was).get(ref) is None or elements(now).get(ref) != elements(was).get(ref)]
    if gone or kept(was, now) < KEPT:
        return ("the snapshot after the answer no longer holds the page's elements (%s of the step's controls lost, %d%% of "
                "the page kept), so the page was replaced or emptied" % (", ".join(gone) or "none", 100 * kept(was, now)))
    if not new_text(was, now, refs, values):
        return "the snapshot after the answer shows no new text beside the controls the step used"
    return None


def refusal_problem(session, refusals, last, start, end):
    """Why the step's 400 and 422 answers are not the one answer to its last action, or else why the page does not show
    it handled (page_problem), or None."""
    if sum(map(len, refusals.values())) > 1:
        return ("the step was answered 400 or 422 more than once, and a probe sends one invalid input: which answer was "
                "its own cannot be told")
    acted = [i for i in range(start, end + 1) if is_act(session[i][0])]
    if any(session[i][0] in SCRIPTS for i in acted):
        return "the step ran a script, which can send a request and change the page itself"
    if not acted or last < acted[-1]:
        return "the answer came before the step's last action, so it is not that action's"
    return page_problem(session, start, acted, last, end)


def judge(root, console, sessions, item, k, problem):
    for session in sessions:
        found = window(session, item, k)
        if not found:
            continue
        refusals, last, crashed = logged(root, console, session, *found)
        problem = problem or ("a console entry of the step is an uncaught exception" if crashed else
                              refusal_problem(session, refusals, last, *found) if refusals else None)
        return Probe(item, k, refusals, problem)
    return Probe(item, k, {})


def marked(text):
    """(the probe steps a step log marks, why none of them can count or None): a log that numbers a step twice cannot
    tell its steps apart, and the explorer tries one invalid input per criterion, once per role it walks it as."""
    lines = [(int(step.group("k")), step.group("text")) for step in map(STEP.match, text.splitlines()) if step]
    numbers = [k for k, _ in lines]
    twice = next((k for k in numbers if numbers.count(k) > 1), None)
    probed = [(k, " ".join(PROBE.match(said).group("role").replace(":", " ").split()).casefold())
              for k, said in lines if PROBE.match(said)]
    roles = [role for _, role in probed]
    again = next((role for role in roles if roles.count(role) > 1), None)
    problem = ("the log numbers step %s more than once, so its steps cannot be told apart" % twice if twice else
               "the log marks more than one invalid-input step%s, and a criterion tries one invalid input"
               % (" " + again.rstrip(":") if again else "") if again is not None else None)
    return sorted({k for k, _ in probed}), problem


def withdraw(root, found, console):
    """Withdraw a counted probe's excuse when its answer is not shown to be the form's own write, or could reach another
    step: a line of the same answer no counted probe logged, or more requests of it in the network records."""
    counted = [probe for probe in found if not probe.problem]
    tied = {place for probe in counted for places in probe.refusals.values() for place in places}
    everywhere, texts = console.refusals(), network_texts(root, tool_calls(root))
    for probe in counted:
        why = [answer_problem(status, url, everywhere.get((status, url), set()), tied, requests(texts, status, url))
               for status, url in sorted(probe.refusals)]
        probe.withdrawn = next((reason for reason in why if reason), None)


def requests(texts, status, url):
    """The (request number, method) pairs the network records list `url` answered `status` under."""
    return {(m.group("n"), m.group("method")) for text in texts for m in NUMBERED.finditer(text)
            if m.group("status") == status and exact(m.group("url")) == url}


def answer_problem(status, url, lines, tied, listed):
    """Why an answer's excuse does not hold: no write of the form's in the network records (`listed`), or it could
    reach another step (console `lines` no counted probe logged, or more requests than lines). None when it holds."""
    if not listed or any(method not in WRITES for _, method in listed):
        return ("the network records do not show %s %s answered to a write (%s) only, as a form sends its input"
                % (status, shown(url), ", ".join(sorted(WRITES))))
    if lines - tied or len(listed) > len(lines):
        return "the run shows %s %s answered to another step too, and an excuse by status and URL would cover it" % (
            status, shown(url))
    return None


def probes(root):
    """Every marked probe of the criteria's step logs (qae/ACn.md, qae/TCn.md) that a 400 or 422 answered, judged."""
    if not root or not os.path.isdir(root):
        return []
    logs = sorted(glob.glob(os.path.join(root, "qae", "AC*.md")) + glob.glob(os.path.join(root, "qae", "TC*.md")))
    steps = {os.path.basename(log)[:-3]: marked(read(log)) for log in logs}
    if not any(ks for ks, _ in steps.values()):
        return []
    console, sessions = Console(root), session_calls(root)
    found = [judge(root, console, sessions, item, k, problem) for item, (ks, problem) in steps.items() for k in ks]
    found = [probe for probe in found if probe.refusals]
    withdraw(root, found, console)
    return found


def probe_excuses(root):
    """The (status, URL pattern) pairs counted probes excuse, as the gate's expected refusals are written. Each probe a
    400 or 422 answered is printed, so the log says what was excused and why, or why not."""
    found = probes(root)
    for probe in found:
        print("qae-artifacts: %s: %s" % (probe.item, probe.describe()))
    return [(status, re.compile(re.escape(url))) for probe in found if not (probe.problem or probe.withdrawn)
            for status, url in probe.refusals]
