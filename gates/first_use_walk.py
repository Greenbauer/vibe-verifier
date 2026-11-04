#!/usr/bin/env python3
"""Fail a pull request whose site a person new to it could not use: the first-use walk got stuck, could not get
back to where it started, or tripped on something on the way.

The criteria walk is handed the pull request's acceptance criteria and checks each one, so a defect no criterion
names (no way back to a list, two buttons of one name, an error that does not say why) passes it. The first-use walk
(harnesses/first-use/) is a second QAE session handed only the task the body's `## First-use task` section states
and the site: it does the task, then retraces its path to the page it started on, and writes walk.md, a screenshot
per step and verdict.md. Its prose is checked, never trusted:

- A pull request with acceptance criteria and no `## First-use task` section is a finding: nothing walked it cold.
  One with none to walk (`- None: <why>`, or no criteria) needs no walk.
- A task and no walk.md, verdict.md or session log cannot run: the walk did not run, or did not finish.
- Every step line has its screenshot; step 1 is the `start:` and the last step the `back:`.
- The session log holds each step's screenshot in walk.md's order. From step 1's screenshot to the log's end it
  holds no call a person cannot make (a typed address, Back or Forward, a script, a tab opened by hand), and the page
  at the last step's screenshot, and at the log's last call, is the page at step 1's.
- verdict.md has one `first-use:` and one `first-use-back:` line, DONE or STUCK, each anchored to a step line that is
  in walk.md word for word. A STUCK line is a finding, and so is every `first-use-snag:` line.

    --artifacts DIR     the run's artifact directory (required); the walk is DIR/first-use/
    --body FILE         the pull request body the task was read from (required)
"""
import os
import re

from _acceptance import body_criteria, declares_none
from _contract import CannotRun, Finding, run_gate
from _first_use import task
from _session_log import page_urls, session_calls, saved_to

GATE = "first-use-walk"
STEP = re.compile(r"^[ \t]*-[ \t]+(?P<line>step (?P<k>[0-9]+):.*?)[ \t]*$", re.MULTILINE)
OUTCOME = re.compile(r"^[ \t]*first-use(?P<back>-back)?:[ \t]*(?P<state>DONE|STUCK)[ \t]*-{1,2}[ \t]*(?P<said>.*?)"
                     r"[ \t]*\(walk\.md::(?P<step>step [0-9]+:.*)\)[ \t]*$", re.IGNORECASE)
SNAG = re.compile(r"^[ \t]*first-use-snag:[ \t]*(?P<said>.*?)[ \t]*\(walk\.md::(?P<step>step [0-9]+:.*)\)[ \t]*$",
                  re.IGNORECASE)
# What a person cannot do with the page in front of them. browser_tabs is one only when it opens a tab: switching to
# a tab a link opened is a person's click.
NOT_A_PERSON = frozenset("""browser_navigate browser_navigate_back browser_navigate_forward browser_evaluate
    browser_run_code_unsafe""".split())


def read(path):
    try:
        with open(path, encoding="utf-8", errors="replace") as handle:
            return handle.read()
    except OSError:
        return None


def page(url):
    """A page as a person sees which page they are on: the address without its query and fragment."""
    return re.split(r"[?#]", url, maxsplit=1)[0].rstrip("/")


def needs_walk(body):
    return bool(body_criteria(body)) and declares_none(body) is None


def step_findings(walk, steps):
    found = [Finding("the first-use walk's step %s has no screenshot (first-use/step-%s.png)" % (k, k))
             for k in steps if not os.path.isfile(os.path.join(walk, "step-%s.png" % k))]
    lines = list(steps.values())
    if not lines[0].split(":", 1)[1].strip().lower().startswith("start:"):
        found.append(Finding("the first-use walk's step 1 is not its start (`step 1: start: ...`)"))
    if not lines[-1].split(":", 1)[1].strip().lower().startswith("back:"):
        found.append(Finding("the first-use walk's last step is not its way back (`step k: back: ...`)"))
    return found


def outcome_findings(verdict, lines):
    found, seen = [], {"first-use": [], "first-use-back": []}
    for text in verdict.splitlines():
        outcome, snag = OUTCOME.match(text), SNAG.match(text)
        match = outcome or snag
        if match and match.group("step") not in lines:
            found.append(Finding("the first-use verdict cites a step walk.md does not have: %s" % text.strip()))
        elif outcome:
            seen["first-use-back" if outcome.group("back") else "first-use"].append(outcome)
        elif snag:
            found.append(Finding("a person new to the site tripped: %s (%s)" % (snag.group("said"), snag.group("step"))))
    for name, matches in seen.items():
        if len(matches) != 1:
            found.append(Finding("the first-use verdict has %d `%s:` lines anchored to the walk, not one" % (len(matches), name)))
        elif matches[0].group("state").upper() == "STUCK":
            what = "could not get back to where it started" if name == "first-use-back" else "could not finish the task"
            found.append(Finding("a person new to the site %s: %s (%s)" % (what, matches[0].group("said"), matches[0].group("step"))))
    return found


def shots(calls, k):
    """The indexes of the calls that saved step k's screenshot."""
    return [i for i, (name, args, _) in enumerate(calls)
            if name == "browser_take_screenshot" and re.search(r"(^|/)step-%s\.png$" % k, saved_to(args))]


def not_a_person(name, args):
    return name in NOT_A_PERSON or (name == "browser_tabs" and '"new"' in args)


def page_at(calls, index):
    """The page the browser was on at call `index`: the last Page URL a call up to it reported."""
    urls = [url for _, _, result in calls[:index + 1] for url in page_urls(result)]
    return page(urls[-1]) if urls else ""


def out_of_order(calls, steps):
    """The first step whose screenshot the session log does not hold after the screenshot of the step before it."""
    after = -1
    for k in steps:
        later = [i for i in shots(calls, k) if i > after]
        if not later:
            return k
        after = later[0]
    return None


def session_findings(walk, steps):
    """What the session log shows from step 1's screenshot to its end: each step's screenshot in the order walk.md
    gives, no call a person cannot make, and the walk ending, at its last screenshot and at its last call, on the page
    it started on."""
    calls = [call for session in session_calls(walk) for call in session]
    if not calls:
        raise CannotRun("first-use/ holds no session log (session-*/session.md): playwright-mcp ran without --save-session")
    missing = out_of_order(calls, steps)
    if missing:
        return [Finding("the first-use walk's session log has no screenshot of step %s after the step before it" % missing)]
    start, last = shots(calls, steps[0])[0], shots(calls, steps[-1])[-1]
    found = [Finding("after it started, the first-use walk called %s, which no person can do with the page in front "
                     "of them" % name) for name, args, _ in calls[start:] if not_a_person(name, args)]
    for end in sorted({last, len(calls) - 1}):
        if page_at(calls, start) != page_at(calls, end):
            found.append(Finding("the first-use walk ended on %s, not where it started (%s)" % (
                page_at(calls, end) or "no page", page_at(calls, start))))
    return found


def check(args):
    body = read(args.body)
    if body is None:
        raise CannotRun("body file not found: %s" % args.body)
    if not task(body):
        return [Finding("the pull request lists acceptance criteria and no `## First-use task`: say in the requester's "
                        "words what a person uses this change to get done, and a QAE session walks it cold "
                        "(harnesses/first-use/README.md)")] if needs_walk(body) else []
    walk = os.path.join(args.artifacts, "first-use")
    log, verdict = read(os.path.join(walk, "walk.md")), read(os.path.join(walk, "verdict.md"))
    if log is None or verdict is None:
        raise CannotRun("the body states a first-use task and first-use/ has no %s: the walk did not run or did not "
                        "finish" % ("walk.md" if log is None else "verdict.md"))
    steps = {match.group("k"): match.group("line") for match in STEP.finditer(log)}
    if not steps:
        return [Finding("the first-use walk logged no step")]
    return (step_findings(walk, steps) + outcome_findings(verdict, set(steps.values()))
            + session_findings(walk, list(steps)))


def add_arguments(parser):
    parser.add_argument("--artifacts", metavar="DIR", required=True)
    parser.add_argument("--body", metavar="FILE", required=True)


if __name__ == "__main__":
    run_gate(GATE, __doc__, check, add_arguments)
