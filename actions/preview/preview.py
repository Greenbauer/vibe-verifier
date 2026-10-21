#!/usr/bin/env python3
"""Record, and find, the address of a pull request's preview: one answer, written by the job that
proved it, read by every check that needs it.

A preview host such as Vercel builds a commit and may tell GitHub about it (a deployment record
with a status and a URL). Checks that read those records depend on the host writing them, and on
guessing which of a commit's builds is the one to test: a repository that points each preview at a
branch database redeploys after the first build, so a commit has two builds and only the second
reads the branch. On 2026-10-12 Vercel built one repository's previews but wrote nothing back to
GitHub for hours: the job that set each preview up found it through Vercel's own API
and passed, while every check that read GitHub's records failed with "no Preview deployment".

So the job that sets the preview up, and already knows the one address it proved, records it:

    record --repo <owner/name> --sha <commit> --url https://<host> [--database <supabase project ref>]

writes a commit status on the commit, context `preview`, state success, its link the address. A
pull request's checks list then shows a `preview` row whose link is the preview under test, whether
or not the host wrote anything. A later record on the same commit (a re-sync) is newer, and newest
wins.

A preview that reads a database of its own (a Supabase branch) has the same problem one level down:
every check that needs that database used to find it again by name, and a project can hold two
branches for one pull request (a workflow's `pr-<N>` and the one Supabase's GitHub integration makes
for the git branch, and a lookup by name once took the one the preview never read). The job that pointed
the preview at its database knows which one it is, so --database records it beside the address: a
second status, context `preview-database`, its link `https://<ref>.supabase.co`.

    find --repo <owner/name> --sha <commit> --check <names> [--wait-minutes 40] [--running-minutes N]

waits for the check named --check (the job that records) to pass on the commit, then prints the
newest recorded address, and on a second line the newest recorded database's project ref when one
was recorded; nothing else. Two clocks: the whole wait is --wait-minutes, and inside
it the check may spend at most --running-minutes actually running (default: the whole wait). Time
the check spends queued for a runner, or not yet created, is not the check being slow, so only polls
that see it `in_progress` count against the running budget. One poll every --poll-seconds (30): every
job of a repository shares its token's hourly request budget, and several checks may wait at once. Of that check's runs on the commit, the newest one that
was not skipped decides: a label event can add a newer skipped run while the real one runs, and a
commit can collect many skipped runs, so every page of runs is read. Because the check records
before it can pass, a passed check with no address is a broken recorder, not a wait: it fails at
once, naming the check. --check may name the check several ways, one name per line (a check being
renamed, docs/check-names.md): the runs of every name on the commit are judged together, as one
check's, and every message names each of them.

Exit 0: recorded, or the address printed. Exit 1: there is no preview to give (the check failed,
was cancelled or timed out, has only skipped runs for three minutes, ran past --running-minutes,
did not pass within --wait-minutes, or passed and recorded nothing), or a record was refused. Exit 2: GitHub could not
be read six times in a row. Exit 3: --wait-minutes ended with the check's run still queued or
running, so a preview may yet come: a job that cannot wait longer can hand the wait to a second job
on this code alone (the action's `handover`). A commit with no run of the check at the end of the
wait is exit 1, never 3. Errors are printed as `::error::` lines, so a workflow shows them.
Reads and writes go through `gh` with the token in GH_TOKEN; a record needs `statuses: write`, a
find `checks: read` and `statuses: read`.
"""
import argparse
import json
import re
import subprocess
import sys
import time

CONTEXT = "preview"
DATABASE_CONTEXT = "preview-database"
# The names docs/check-names.md gives them, read beside the old so the record step can move to them later.
ADDRESS_CONTEXTS = (CONTEXT, "Preview / Address")
DATABASE_CONTEXTS = (DATABASE_CONTEXT, "Preview / Database address")
# A Supabase project ref: 20 lower-case letters. Its API address is https://<ref>.supabase.co.
DATABASE_REF = re.compile(r"^[a-z]{20}$")
DATABASE_URL = re.compile(r"^https://([a-z]{20})\.supabase\.co$")
FAILED_READS_ALLOWED = 6
# The wait ended with the check's run neither passed nor ended: it is still owed.
STILL_OWED = 3
# Only-skipped runs for this long mean the commit has no preview, rather than holding the wait.
SKIPPED_SECONDS = 180


class Unreadable(Exception):
    """GitHub could not be read FAILED_READS_ALLOWED times in a row."""


def fail(message, code=1):
    print("::error::" + message)
    sys.exit(code)


class Reader:
    """Reads through `gh api`, counting failed reads in a row. A failed read is not an answer: it
    costs one poll, and the sixth in a row ends the run (exit 2)."""

    def __init__(self):
        self.failed = 0

    def read(self, args, settles=True):
        """The answer, or None for a failed read. A read that `settles` resets the count when it
        answers; one poll of several reads resets it itself, once every read of the poll answered."""
        result = subprocess.run(["gh", "api", *args], capture_output=True, text=True)
        if result.returncode == 0:
            if settles:
                self.failed = 0
            return result.stdout
        self.failed += 1
        sys.stderr.write("gh api %s failed (%d in a row): %s\n" % (args[0], self.failed, result.stderr.strip()))
        if self.failed >= FAILED_READS_ALLOWED:
            raise Unreadable(args[0])
        return None


def newest_run(lines):
    """The verdict of a check's runs, one `started_at<TAB>status<TAB>conclusion` line each: the
    newest run that was not skipped as "status conclusion", else "only skipped", else "none"."""
    runs = [line.split("\t") for line in lines.splitlines() if line.strip()]
    real = [run for run in runs if len(run) == 3 and run[2] != "skipped"]
    if real:
        newest = max(real, key=lambda run: run[0])
        return "%s %s" % (newest[1], newest[2])
    return "only skipped" if runs else "none"


def newest_link(lines, contexts):
    """The link of the newest status named any of `contexts`, from one `context<TAB>created_at<TAB>target_url`
    line each, or ""."""
    rows = [line.split("\t") for line in lines.splitlines() if line.strip()]
    rows = [row[1:] for row in rows if len(row) == 3 and row[0] in contexts and row[2]]
    return max(rows)[1] if rows else ""


def newest_database(lines):
    """The project ref of the newest `preview-database` status, or "" when none was recorded."""
    match = DATABASE_URL.match(newest_link(lines, DATABASE_CONTEXTS))
    return match.group(1) if match else ""


def write_status(args, context, link, description):
    payload = json.dumps({"state": "success", "context": context, "target_url": link, "description": description})
    for attempt in range(1, 4):
        result = subprocess.run(["gh", "api", "-X", "POST", "repos/%s/statuses/%s" % (args.repo, args.sha),
                                 "--input", "-"], input=payload, capture_output=True, text=True)
        if result.returncode == 0:
            print("recorded the %s of %s: %s" % (context, args.sha, link))
            return
        sys.stderr.write("recording failed (attempt %d of 3): %s\n" % (attempt, result.stderr.strip()))
        if attempt < 3:
            time.sleep(args.interval)
    fail("could not record the preview of %s as a `%s` status; the job needs `statuses: write`" % (args.sha, context))


def record(args):
    if not args.url.startswith("https://"):
        fail("the preview address to record must start with https://, got %r" % args.url)
    if args.database and not DATABASE_REF.match(args.database):
        fail("the preview database to record must be a Supabase project ref (20 lower-case letters), got %r" % args.database)
    # The database first: a reader that sees the address after a pass must also see the database.
    if args.database:
        write_status(args, DATABASE_CONTEXT, "https://%s.supabase.co" % args.database,
                     "The database the preview of this commit reads")
    write_status(args, CONTEXT, args.url.rstrip("/"), "The preview of this commit under test")


def read_verdict(args, reader, answered):
    """(verdict, answered): the check's verdict on this poll, "unread" when GitHub did not answer,
    and the last verdict GitHub did answer, which a failed read leaves as it was."""
    lines = [reader.read(["repos/%s/commits/%s/check-runs" % (args.repo, args.sha), "--paginate",
                          "--method", "GET", "-f", "check_name=" + name, "-f", "per_page=100",
                          "--jq", '.check_runs[] | "\\(.started_at // "")\\t\\(.status)\\t\\(.conclusion)"'],
                         settles=False)
             for name in args.checks]
    if None in lines:
        return "unread", answered
    # A poll with one name always unreadable never reads as an answer, so its failures add up.
    reader.failed = 0
    verdict = newest_run("\n".join(lines))
    return verdict, verdict


def wait_ended_code(answered):
    """The exit code of a wait that ran out. STILL_OWED when the last answer named a run that has not
    ended; 1 when it named none, only skipped ones, or one that ended without passing: no longer wait
    would see those pass."""
    return 1 if answered.split()[0] in ("none", "only", "completed") else STILL_OWED


def wait_for_check(args, reader):
    """Every budget is counted in polls of --poll-seconds, so a test that sleeps 0 between polls
    sees the same budgets as a real run."""
    polls = max(1, args.wait_minutes * 60 // args.poll_seconds)
    running_minutes = args.running_minutes or args.wait_minutes
    # The polls a kind of verdict may be seen before the wait fails, and what it then says.
    budgets = {
        "only skipped": (max(1, -(-SKIPPED_SECONDS // args.poll_seconds)) - 1,
                         '%s has only skipped runs on %s: no preview. Re-run once the preview has synced.'),
        "in_progress": (running_minutes * 60 // args.poll_seconds,
                        '%%s ran for %d minutes on %%s without passing' % running_minutes),
    }
    seen = dict.fromkeys(budgets, 0)
    verdict = answered = "none"
    for _ in range(polls):
        verdict, answered = read_verdict(args, reader, answered)
        if verdict == "completed success":
            return
        if verdict in ("completed failure", "completed cancelled", "completed timed_out"):
            fail('%s ended in %s, so %s has no preview' % (args.named, verdict.split()[1], args.sha))
        kind = verdict.split()[0] if verdict.startswith("in_progress") else verdict
        if kind in seen:
            seen[kind] += 1
            if seen[kind] > budgets[kind][0]:
                fail(budgets[kind][1] % (args.named, args.sha))
        time.sleep(args.interval)
    fail('%s did not pass for %s within %d minutes; it ran for about %d of them, and was queued for a '
         "runner or not yet created the rest (last seen: %s)"
         % (args.named, args.sha, args.wait_minutes, seen["in_progress"] * args.poll_seconds // 60, verdict),
         code=wait_ended_code(answered))


def find(args):
    reader = Reader()
    try:
        wait_for_check(args, reader)
        lines = None
        while lines is None:
            lines = reader.read(["repos/%s/commits/%s/statuses" % (args.repo, args.sha), "--paginate",
                                 "--jq", '.[] | select(.context | IN(%s)) | "\\(.context)\\t\\(.created_at)\\t\\(.target_url // "")"'
                                 % ", ".join(json.dumps(name) for name in ADDRESS_CONTEXTS + DATABASE_CONTEXTS)])
            if lines is None:
                time.sleep(args.interval)
    except Unreadable as error:
        fail("gh api %s failed %d times in a row (gh's own error is above each), so the preview of %s "
             "was not found. Re-run this job." % (error, FAILED_READS_ALLOWED, args.sha), code=2)
    address = newest_link(lines, ADDRESS_CONTEXTS)
    if not address:
        fail('%s passed on %s but recorded no `%s` status: its job needs the catalog\'s preview record '
             "step (actions/preview/README.md). Re-running this job will not help." % (args.named, args.sha, CONTEXT))
    print(address)
    database = newest_database(lines)
    if database:
        print(database)


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("record", "find"):
        command = commands.add_parser(name)
        command.add_argument("--repo", required=True)
        command.add_argument("--sha", required=True)
        command.add_argument("--poll-seconds", type=int, default=30, help="seconds between polls; every budget is counted in polls")
        command.add_argument("--interval", type=float, help="seconds actually slept between polls (default: --poll-seconds)")
    commands.choices["record"].add_argument("--url", required=True)
    commands.choices["record"].add_argument("--database", default="", help="the Supabase project ref the preview reads")
    commands.choices["find"].add_argument("--check", required=True, help="the check's name; several, one per line")
    commands.choices["find"].add_argument("--wait-minutes", type=int, default=40)
    commands.choices["find"].add_argument("--running-minutes", type=int, default=0)
    args = parser.parse_args()
    if args.interval is None:
        args.interval = args.poll_seconds
    if args.command == "find":
        args.checks = [name.strip() for name in args.check.splitlines() if name.strip()]
        args.named = " or ".join('"%s"' % name for name in args.checks)
    record(args) if args.command == "record" else find(args)


if __name__ == "__main__":
    main()
