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
- From step 1's screenshot on, the session log holds no call a person cannot make (a typed address, Back or
  Forward, a script, a tab opened by hand), and the page at the last step's screenshot is the page at step 1's.
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


def session_findings(walk, last):
    """What the session log shows from step 1's screenshot to the last step's: calls no person makes, and the page
    the walk ended on when it is not the page it started on."""
    calls = [call for session in session_calls(walk) for call in session]
    if not calls:
        raise CannotRun("first-use/ holds no session log (session-*/session.md): playwright-mcp ran without --save-session")
    shots = {k: [i for i, (name, args, _) in enumerate(calls)
                 if name == "browser_take_screenshot" and re.search(r"(^|/)step-%s\.png$" % k, saved_to(args))]
             for k in ("1", last)}
    if not shots["1"] or not shots[last]:
        return [Finding("the first-use walk's session log has no screenshot of step 1 and of step %s" % last)]
    start, end, found = shots["1"][0], shots[last][-1], []
    for name, args, _ in calls[start:end]:
        if name in NOT_A_PERSON or (name == "browser_tabs" and '"new"' in args):
            found.append(Finding("after it started, the first-use walk called %s, which no person can do with the page "
                                 "in front of them" % name))
    def at(index):
        urls = [url for _, _, result in calls[:index + 1] for url in page_urls(result)]
        return page(urls[-1]) if urls else ""
    if at(start) != at(end):
        found.append(Finding("the first-use walk ended on %s, not where it started (%s)" % (at(end) or "no page", at(start))))
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
            + session_findings(walk, list(steps)[-1]))


def add_arguments(parser):
    parser.add_argument("--artifacts", metavar="DIR", required=True)
    parser.add_argument("--body", metavar="FILE", required=True)


if __name__ == "__main__":
    run_gate(GATE, __doc__, check, add_arguments)
