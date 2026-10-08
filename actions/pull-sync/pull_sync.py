#!/usr/bin/env python3
"""Bring a repository's open pull requests up to date with its default branch, a few at a time.

After a merge every other open pull request is behind. Updating all of them at once starts every
pull request workflow on every one of them, and a new head voids whatever was verified on the old
one, so this updates only the pull requests it is worth updating now, and never more than the
runners can take:

  - never: a pull request with the `no-auto-sync` label, one from a fork, one into another branch
    than the default, one that is not behind, one whose checks are running, or one that changed in
    the last --quiet-minutes (someone, or something, is working on it).
  - conflict: a pull request that conflicts with the default branch is not updated. It gets the
    `sync-conflict` label, which is taken off again once it merges cleanly, so whoever owns the pull
    request can find it by the label. Nothing here resolves a conflict.
  - ready: not a draft, and every check on its head passed. Updated first, oldest first.
  - stale: any other pull request, once its head commit is older than --stale-hours. An update is
    itself a head commit, so a pull request that is not ready is updated at most that often.
  - slots: at most --max-in-flight pull requests of a repository may hold a slot at once, counting
    the ones people pushed. A pull request holds one while any check on its head is unfinished, and
    while its head commit is newer than --quiet-minutes (its checks may not have registered yet).
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
# The states of a check that has not finished. The rollup's own state cannot say this: it turns
# FAILURE at the first failed check, while the rest of that head's checks are still running.
UNFINISHED = {"IN_PROGRESS", "PENDING", "QUEUED", "WAITING", "EXPECTED"}
# How many more times a repository is read while GitHub has not said whether a pull request merges.
SETTLE_READS = 3

DEFAULT_BRANCH = "query($owner:String!,$name:String!){repository(owner:$owner,name:$name){defaultBranchRef{name}}}"
# `headRef.compare(headRef: $base).aheadBy` is how many commits the base has that the head lacks.
PULLS = """query($owner:String!,$name:String!,$base:String!){
  repository(owner:$owner,name:$name){
    pullRequests(states:OPEN,first:100,orderBy:{field:CREATED_AT,direction:ASC}){
      pageInfo{hasNextPage}
      nodes{
        number isDraft isCrossRepository mergeable updatedAt baseRefName headRefOid
        labels(first:100){nodes{name}}
        headRef{compare(headRef:$base){aheadBy}}
        commits(last:1){nodes{commit{committedDate statusCheckRollup{state contexts(first:1){
          checkRunCountsByState{state count} statusContextCountsByState{state count}}}}}}
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


def read(repo):
    """The default branch and every open pull request."""
    owner, _, name = repo.partition("/")
    default = graphql(DEFAULT_BRANCH, owner=owner, name=name)["repository"]["defaultBranchRef"]
    if not default:
        raise CannotRead("%s has no default branch" % repo)
    data = graphql(PULLS, owner=owner, name=name, base=default["name"])
    pulls = data["repository"]["pullRequests"]
    if pulls["pageInfo"]["hasNextPage"]:
        raise CannotRead("%s has more than 100 open pull requests" % repo)
    return default["name"], pulls["nodes"]


def read_settled(repo, settle_seconds):
    """`read`, again while GitHub has not said whether a pull request this run would judge merges."""
    for reads_left in range(SETTLE_READS, -1, -1):
        base, pulls = read(repo)
        unknown = any(pull["mergeable"] == "UNKNOWN" and not out_of_scope(pull, base) for pull in pulls)
        if not unknown or not reads_left:
            return base, pulls
        time.sleep(settle_seconds)


def when(stamp):
    return datetime.fromisoformat(stamp.replace("Z", "+00:00"))


def head_commit(pull):
    return pull["commits"]["nodes"][0]["commit"]


def checks(pull):
    return (head_commit(pull)["statusCheckRollup"] or {}).get("state", "")


def running(pull):
    """Whether any check on the head has not finished."""
    rollup = head_commit(pull)["statusCheckRollup"]
    if not rollup:
        return False
    counts = rollup["contexts"]["checkRunCountsByState"] + rollup["contexts"]["statusContextCountsByState"]
    return any(row["count"] for row in counts if row["state"] in UNFINISHED)


def holds_slot(pull, now, quiet):
    """Checks are running on its head, or the head is so new that they may not have registered."""
    return running(pull) or now - when(head_commit(pull)["committedDate"]) < quiet


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


def tier(pull, now, stale):
    """ready, stale, or the reason a pull request that could be updated is not updated yet."""
    if not pull["isDraft"] and checks(pull) == "SUCCESS":
        return "ready"
    if now - when(head_commit(pull)["committedDate"]) >= stale:
        return "stale"
    return "not ready, and its head is newer than %d hours" % (stale.total_seconds() // 3600)


def judge(pull, base, now, quiet, stale):
    """This pull request's row before slots are given out: a candidate has the action `sync`."""
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
        return Line(number, "skip", "checks running", label=unlabel)
    if now - when(pull["updatedAt"]) < quiet:
        return Line(number, "skip", "changed in the last %d minutes" % (quiet.total_seconds() // 60), label=unlabel)
    kind = tier(pull, now, stale)
    if kind not in ("ready", "stale"):
        return Line(number, "skip", kind, label=unlabel)
    return Line(number, "sync", "%s, %d behind %s" % (kind, behind, base), head, unlabel, kind == "ready")


def plan(pulls, base, now, max_in_flight, quiet, stale):
    """Every pull request's row, how many hold a slot, and how many slots were free. Ready candidates
    take slots first, then stale ones; within each, the order GitHub listed them in (oldest first)."""
    in_flight = sum(1 for pull in pulls if holds_slot(pull, now, quiet))
    free = max(0, max_in_flight - in_flight)
    lines = [judge(pull, base, now, quiet, stale) for pull in pulls]
    candidates = sorted((line for line in lines if line.action == "sync"), key=lambda line: not line.ready)
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
        base, pulls = read_settled(repo, args.settle_seconds)
    except CannotRead as error:
        print("pull-sync: cannot read %s: %s" % (repo, error), file=sys.stderr)
        return 2
    lines, in_flight, free = plan(pulls, base, datetime.now(timezone.utc), args.max_in_flight,
                                  timedelta(minutes=args.quiet_minutes), timedelta(hours=args.stale_hours))
    mode = "acting" if args.act else "dry run, nothing written"
    print("%s: %d open, %d holding a slot, %d free slot(s) (%s)" % (repo, len(pulls), in_flight, free, mode))
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
    parser.add_argument("--settle-seconds", type=int, default=10,
                        help="wait this long before reading again while GitHub has not said whether a "
                             "pull request merges cleanly (default 10)")
    args = parser.parse_args()
    # Every repository is planned even when one cannot be read; the worst outcome is the exit code.
    return max(sync(repo, args) for repo in args.repo)


if __name__ == "__main__":
    sys.exit(main())
