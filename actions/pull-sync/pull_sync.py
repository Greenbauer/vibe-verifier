#!/usr/bin/env python3
"""Bring a repository's open pull requests up to date with its default branch, a few at a time.

After a merge every other open pull request is behind. Updating all of them at once starts every
pull request workflow on every one of them, and a new head voids whatever was verified on the old
one, so this updates only the pull requests it is worth updating now, and never more than the
runners can take:

  - never: a pull request with the `no-auto-sync` label, one from a fork, one into another branch
    than the default, one that is not behind, one whose checks are running (but see `replace`), or
    one that changed in the last --quiet-minutes (someone, or something, is working on it).
  - conflict: a pull request that conflicts with the default branch is not updated. It gets the
    `sync-conflict` label, which is taken off again once it merges cleanly, so whoever owns the pull
    request can find it by the label. Nothing here resolves a conflict.
  - replace: where the default branch merges only a head that is up to date with it (a protection
    rule or ruleset that requires branches to be up to date), checks running on a head that is behind
    can never be the ones it merges on, so waiting for them wastes the time they take. A pull request
    that is not a draft, has no failed check yet, is behind, and has checks running is updated at
    once, once its head was pushed more than --quiet-minutes ago (the checks' own comments keep the
    pull request itself changing, so the push is what counts here). Its new head starts its checks
    again, and workflows that cancel a superseded run free the runners the stale one held. It needs
    no free slot: it replaces checks already running instead of adding some. Where the setting is
    off, or this token cannot read it, a running check is never interrupted.
  - ready: not a draft, and every check on its head passed. Updated first, oldest first.
  - needs update: with --needs-update NAME, a commit status NAME that fails exactly when the pull
    request has to be updated before it may merge (a check that compares the pull request with the
    default branch, used in place of "require branches to be up to date"). Its failure is not held
    against the pull request: one whose only failed check is NAME counts as ready, and as not yet
    red while its other checks run, because the update this engine makes is what turns NAME green.
    Without the option, that failure makes the pull request red, and a red one waits a day.
  - stale: any other pull request, once its head commit is older than --stale-hours. An update is
    itself a head commit, so a pull request that is not ready is updated at most that often.
  - slots: at most --max-in-flight updates of a repository may be in flight at once. A pull request
    holds a slot when its head is an update (a merge commit GitHub made, which is what "update
    branch" makes, here or from the button) and any check on that head is unfinished, or it has no
    checks yet and is newer than --quiet-minutes (they may not have registered yet). A head whose
    checks have all finished holds none, however new it is, and a head someone pushed holds none:
    the cap bounds the load this adds, and a busy pull request never keeps another one stale.
    A candidate with no free slot waits for a later run.

Each update is GitHub's own "update branch" (a merge of the default branch into the head), sent
with the head this run judged, so a push that lands in between makes GitHub refuse it.

GitHub works out whether a pull request merges cleanly only when asked, so the first read after a
merge answers UNKNOWN for all of them. Asking is what starts the work: the run reads again, up to
three more times --settle-seconds apart, and leaves alone whatever is still unknown after that.

A dry run unless --act is given: it prints the plan and writes nothing. Reads and writes go through
`gh` with the token in GH_TOKEN. The token must not be a workflow's GITHUB_TOKEN: an update made
with that one starts no workflow on the new head.

Exit 0: the plan was printed and every write it called for succeeded. Exit 1: a write failed.
Exit 2: a repository could not be read, so it has no plan. With several --repo, each is planned
whatever happened to the others, and the worst of their outcomes is the exit code.
"""
import argparse
import json
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from typing import NamedTuple

OPT_OUT_LABEL = "no-auto-sync"
CONFLICT_LABEL = "sync-conflict"
# The states of a check run that passed, for the rollup.
PASSED = {"SUCCESS", "NEUTRAL", "SKIPPED"}
# The states of a check that has not finished. The rollup's own state cannot say this: it turns
# FAILURE at the first failed check, while the rest of that head's checks are still running.
UNFINISHED = {"IN_PROGRESS", "PENDING", "QUEUED", "WAITING", "EXPECTED"}
# How many more times a repository is read while GitHub has not said whether a pull request merges.
SETTLE_READS = 3

DEFAULT_BRANCH = "query($owner:String!,$name:String!){repository(owner:$owner,name:$name){defaultBranchRef{name}}}"
# The classic protection rule's "require branches to be up to date"; a ruleset's is read over REST.
STRICT = ("query($owner:String!,$name:String!){repository(owner:$owner,name:$name){"
          "defaultBranchRef{branchProtectionRule{requiresStrictStatusChecks}}}}")
# `headRef.compare(headRef: $base).aheadBy` is how many commits the base has that the head lacks.
# With --needs-update the head's commit status of that name is read too (NEEDS_UPDATE below); a token
# needs statuses read for it, so the field is asked for only then.
PULLS = """query($owner:String!,$name:String!,$base:String!%(variables)s){
  repository(owner:$owner,name:$name){
    pullRequests(states:OPEN,first:100,orderBy:{field:CREATED_AT,direction:ASC}){
      pageInfo{hasNextPage}
      nodes{
        number isDraft isCrossRepository mergeable updatedAt baseRefName headRefOid
        labels(first:100){nodes{name}}
        headRef{compare(headRef:$base){aheadBy}}
        commits(last:1){nodes{commit{committedDate committedViaWeb parents{totalCount} statusCheckRollup{state contexts(first:1){
          checkRunCountsByState{state count} statusContextCountsByState{state count}}}%(status)s}}}
      }
    }
  }
}"""


class CannotRead(Exception):
    """GitHub did not give an answer a plan can be built on."""


class Line(NamedTuple):
    """One pull request's row of the plan. `action` is sync, conflict, wait or skip."""
    number: int
    action: str
    reason: str
    head: str = ""
    label: str = ""   # "add" or "remove" the conflict label; empty leaves it alone
    ready: bool = False
    replaces: bool = False   # replaces checks running on a head that cannot merge: needs no slot


def gh(*args):
    return subprocess.run(["gh", "api", *args], capture_output=True, text=True)


def graphql(query, **variables):
    args = ["graphql", "-f", "query=" + query]
    for name, value in variables.items():
        args += ["-f", "%s=%s" % (name, value)]
    result = gh(*args)
    if result.returncode != 0:
        raise CannotRead((result.stderr or result.stdout).strip().split("\n")[0] or "gh failed")
    try:
        answer = json.loads(result.stdout)
    except json.JSONDecodeError:
        raise CannotRead("GitHub's answer was not JSON") from None
    if answer.get("errors") or not (answer.get("data") or {}).get("repository"):
        raise CannotRead("GitHub answered with errors or without the repository")
    return answer["data"]


NEEDS_UPDATE = {"variables": ",$context:String!", "status": "\n          status{context(name:$context){state}}"}
WITHOUT = {"variables": "", "status": ""}


def read(repo, context=""):
    """The default branch and every open pull request, each head with the state of the commit status
    `context` when one is named. Without one the query does not ask for statuses at all: GitHub
    answers "Resource not accessible by integration" to a token that cannot read them, even for an
    empty name (2026-10-14)."""
    owner, _, name = repo.partition("/")
    default = graphql(DEFAULT_BRANCH, owner=owner, name=name)["repository"]["defaultBranchRef"]
    if not default:
        raise CannotRead("%s has no default branch" % repo)
    if context:
        data = graphql(PULLS % NEEDS_UPDATE, owner=owner, name=name, base=default["name"], context=context)
    else:
        data = graphql(PULLS % WITHOUT, owner=owner, name=name, base=default["name"])
    pulls = data["repository"]["pullRequests"]
    if pulls["pageInfo"]["hasNextPage"]:
        raise CannotRead("%s has more than 100 open pull requests" % repo)
    return default["name"], pulls["nodes"]


def requires_up_to_date(repo, base):
    """Whether the base merges only a head that is up to date with it, from a classic protection rule
    or an active ruleset: True, False, or None when this token could read neither. None is treated as
    False by the plan, which keeps every running check: the side that wastes time, not work."""
    owner, _, name = repo.partition("/")
    answers = []
    try:
        rule = graphql(STRICT, owner=owner, name=name)["repository"]["defaultBranchRef"]["branchProtectionRule"]
        answers.append(bool(rule and rule["requiresStrictStatusChecks"]))
    except (CannotRead, TypeError, KeyError):
        pass
    result = gh("repos/%s/rules/branches/%s?per_page=100" % (repo, base))
    try:
        rules = json.loads(result.stdout) if result.returncode == 0 else None
        if isinstance(rules, list):
            answers.append(any(rule.get("type") == "required_status_checks"
                               and (rule.get("parameters") or {}).get("strict_required_status_checks_policy")
                               for rule in rules))
    except json.JSONDecodeError:
        pass
    return any(answers) if answers else None


def read_settled(repo, settle_seconds, context=""):
    """`read`, again while GitHub has not said whether a pull request this run would judge merges."""
    for reads_left in range(SETTLE_READS, -1, -1):
        base, pulls = read(repo, context)
        unknown = any(pull["mergeable"] == "UNKNOWN" and not out_of_scope(pull, base) for pull in pulls)
        if not unknown or not reads_left:
            return base, pulls
        time.sleep(settle_seconds)


def when(stamp):
    return datetime.fromisoformat(stamp.replace("Z", "+00:00"))


def head_commit(pull):
    return pull["commits"]["nodes"][0]["commit"]


def needs_update_failed(pull):
    """Whether the head's --needs-update status failed."""
    context = (head_commit(pull).get("status") or {}).get("context") or {}
    return context.get("state") in ("FAILURE", "ERROR")


def checks(pull, needs_update=""):
    """The rollup state of the head's checks. With `needs_update` (a commit status that fails exactly
    when the pull request needs an update), a failure of that status alone does not count: the state
    is then SUCCESS, or PENDING while another check runs."""
    rollup = head_commit(pull)["statusCheckRollup"] or {}
    state = rollup.get("state", "")
    if not needs_update or state not in ("FAILURE", "ERROR") or not needs_update_failed(pull):
        return state
    counts = rollup["contexts"]
    failed = sum(row["count"] for row in counts["checkRunCountsByState"] if row["state"] not in PASSED | UNFINISHED)
    failed += sum(row["count"] for row in counts["statusContextCountsByState"] if row["state"] in ("FAILURE", "ERROR"))
    if failed > 1:
        return state
    return "PENDING" if running(pull) else "SUCCESS"


def running(pull):
    """Whether any check on the head has not finished."""
    rollup = head_commit(pull)["statusCheckRollup"]
    if not rollup:
        return False
    counts = rollup["contexts"]["checkRunCountsByState"] + rollup["contexts"]["statusContextCountsByState"]
    return any(row["count"] for row in counts if row["state"] in UNFINISHED)


def is_update(pull):
    """Whether the head is a merge commit GitHub made: what "update branch" makes. A merge someone
    made and pushed is not one, and holds no slot: the author chose to start those checks."""
    commit = head_commit(pull)
    return commit["committedViaWeb"] and commit["parents"]["totalCount"] == 2


def holds_slot(pull, now, quiet):
    """An update whose checks are running, or that has none yet and is so new they may not have registered."""
    commit = head_commit(pull)
    return is_update(pull) and (
        running(pull) or (not commit["statusCheckRollup"] and now - when(commit["committedDate"]) < quiet))


def labels(pull):
    return {node["name"] for node in pull["labels"]["nodes"]}


def out_of_scope(pull, base):
    """Why this run leaves the pull request alone entirely, or an empty string."""
    if OPT_OUT_LABEL in labels(pull):
        return "label " + OPT_OUT_LABEL
    if pull["isCrossRepository"]:
        return "from a fork"
    if pull["baseRefName"] != base:
        return "into %s, not %s" % (pull["baseRefName"], base)
    if not pull["headRef"]:
        return "its head branch is gone"
    if not pull["headRef"]["compare"]:
        return "GitHub could not compare it with " + base
    return ""


def tier(pull, now, stale, needs_update=""):
    """ready, stale, or the reason a pull request that could be updated is not updated yet."""
    if not pull["isDraft"] and checks(pull, needs_update) == "SUCCESS":
        return "ready"
    if now - when(head_commit(pull)["committedDate"]) >= stale:
        return "stale"
    return "not ready, and its head is newer than %d hours" % (stale.total_seconds() // 3600)


def while_running(pull, base, now, quiet, strict, unlabel, needs_update=""):
    """The row of a pull request that is behind with checks running: left alone, unless the base
    merges only up-to-date heads, where those checks can never count and are replaced."""
    number = pull["number"]
    if not strict:
        return Line(number, "skip", "checks running", label=unlabel)
    if pull["isDraft"] or checks(pull, needs_update) in ("FAILURE", "ERROR"):
        return Line(number, "skip", "checks running, and it is %s" % (
            "a draft" if pull["isDraft"] else "already red"), label=unlabel)
    if now - when(head_commit(pull)["committedDate"]) < quiet:
        return Line(number, "skip", "checks running, and its head was pushed in the last %d minutes"
                    % (quiet.total_seconds() // 60), label=unlabel)
    return Line(number, "sync", "checks running on a head %d behind %s, which merges only an up-to-date "
                "head" % (pull["headRef"]["compare"]["aheadBy"], base), pull["headRefOid"], unlabel,
                replaces=True)


def judge(pull, base, now, quiet, stale, strict=False, needs_update=""):
    """This pull request's row before slots are given out: a candidate has the action `sync`. `strict`
    is whether the base merges only an up-to-date head; `needs_update` is --needs-update."""
    number, head, labelled = pull["number"], pull["headRefOid"], CONFLICT_LABEL in labels(pull)
    reason = out_of_scope(pull, base)
    if reason:
        return Line(number, "skip", reason)
    if pull["mergeable"] == "CONFLICTING":
        return Line(number, "conflict", "conflicts with " + base, label="" if labelled else "add")
    unlabel = "remove" if labelled and pull["mergeable"] == "MERGEABLE" else ""
    behind = pull["headRef"]["compare"]["aheadBy"]
    if behind == 0:
        return Line(number, "skip", "up to date", label=unlabel)
    if pull["mergeable"] != "MERGEABLE":
        return Line(number, "skip", "GitHub has not said whether it merges cleanly")
    if running(pull):
        return while_running(pull, base, now, quiet, strict, unlabel, needs_update)
    if now - when(pull["updatedAt"]) < quiet:
        return Line(number, "skip", "changed in the last %d minutes" % (quiet.total_seconds() // 60), label=unlabel)
    kind = tier(pull, now, stale, needs_update)
    if kind not in ("ready", "stale"):
        return Line(number, "skip", kind, label=unlabel)
    return Line(number, "sync", "%s, %d behind %s" % (kind, behind, base), head, unlabel, kind == "ready")


def plan(pulls, base, now, max_in_flight, quiet, stale, strict=False, needs_update=""):
    """Every pull request's row, how many hold a slot, and how many slots were free. A candidate that
    replaces running checks needs no slot. Ready candidates take slots first, then stale ones; within
    each, the order GitHub listed them in (oldest first)."""
    in_flight = sum(1 for pull in pulls if holds_slot(pull, now, quiet))
    free = max(0, max_in_flight - in_flight)
    lines = [judge(pull, base, now, quiet, stale, strict, needs_update) for pull in pulls]
    candidates = sorted((line for line in lines if line.action == "sync" and not line.replaces),
                        key=lambda line: not line.ready)
    waiting = {line.number for line in candidates[free:]}
    return [line._replace(action="wait", reason=line.reason + "; no free slot") if line.number in waiting else line
            for line in lines], in_flight, free


def write(repo, line):
    """Send what the row calls for. Returns the failures, one sentence each."""
    calls = []
    if line.action == "sync":
        calls.append(("update", ["-X", "PUT", "repos/%s/pulls/%d/update-branch" % (repo, line.number),
                                 "-f", "expected_head_sha=" + line.head]))
    if line.label == "add":
        calls.append(("label", ["-X", "POST", "repos/%s/issues/%d/labels" % (repo, line.number),
                                "-f", "labels[]=" + CONFLICT_LABEL]))
    if line.label == "remove":
        calls.append(("unlabel", ["-X", "DELETE", "repos/%s/issues/%d/labels/%s" % (repo, line.number, CONFLICT_LABEL)]))
    failures = []
    for what, args in calls:
        result = gh(*args)
        if result.returncode != 0:
            detail = (result.stderr or result.stdout).strip().split("\n")[0]
            failures.append("#%d %s failed: %s" % (line.number, what, detail))
    return failures


def describe(line):
    change = {"add": "; adds label " + CONFLICT_LABEL, "remove": "; removes label " + CONFLICT_LABEL}.get(line.label, "")
    return "#%d %s: %s%s" % (line.number, line.action, line.reason, change)


def sync(repo, args):
    """Plan one repository, print the plan, and write it when acting. Returns the exit code."""
    try:
        base, pulls = read_settled(repo, args.settle_seconds, args.needs_update)
    except CannotRead as error:
        print("pull-sync: cannot read %s: %s" % (repo, error), file=sys.stderr)
        return 2
    strict = requires_up_to_date(repo, base)
    lines, in_flight, free = plan(pulls, base, datetime.now(timezone.utc), args.max_in_flight,
                                  timedelta(minutes=args.quiet_minutes), timedelta(hours=args.stale_hours),
                                  bool(strict), args.needs_update)
    mode = "acting" if args.act else "dry run, nothing written"
    rule = {True: "merges only up-to-date heads", False: "merges heads that are behind",
            None: "could not read whether it merges heads that are behind"}[strict]
    needs = "; a failed %s asks for an update" % args.needs_update if args.needs_update else ""
    print("%s: %d open, %d holding a slot, %d free slot(s) (%s; %s %s%s)"
          % (repo, len(pulls), in_flight, free, mode, base, rule, needs))
    failures = []
    for line in lines:
        print(describe(line))
        if args.act:
            failures += write(repo, line)
    for failure in failures:
        print("pull-sync: %s: %s" % (repo, failure), file=sys.stderr)
    return 1 if failures else 0


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--repo", required=True, action="append",
                        help="owner/name; repeat it to sync several repositories, each on its own slots")
    parser.add_argument("--act", action="store_true", help="write the plan; without it, a dry run")
    parser.add_argument("--max-in-flight", type=int, default=2,
                        help="pull requests of one repository that may hold a slot at once (default 2)")
    parser.add_argument("--quiet-minutes", type=int, default=30,
                        help="leave alone a pull request that changed this recently (default 30)")
    parser.add_argument("--stale-hours", type=int, default=24,
                        help="update a pull request that is not ready once its head is this old (default 24)")
    parser.add_argument("--needs-update", default="", metavar="NAME",
                        help="a commit status that fails exactly when a pull request needs an update; its "
                             "failure alone does not make the pull request red")
    parser.add_argument("--settle-seconds", type=int, default=10,
                        help="wait this long before reading again while GitHub has not said whether a "
                             "pull request merges cleanly (default 10)")
    args = parser.parse_args()
    # Every repository is planned even when one cannot be read; the worst outcome is the exit code.
    return max(sync(repo, args) for repo in args.repo)


if __name__ == "__main__":
    sys.exit(main())
