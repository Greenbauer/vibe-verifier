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

    record --repo <owner/name> --sha <commit> --url https://<host>

writes a commit status on the commit, context `preview`, state success, its link the address. A
pull request's checks list then shows a `preview` row whose link is the preview under test, whether
or not the host wrote anything. A later record on the same commit (a re-sync) is newer, and newest
wins.

    find --repo <owner/name> --sha <commit> --check <name> [--wait-minutes 40]

waits for the check named --check (the job that records) to pass on the commit, then prints the
newest recorded address and nothing else. Of that check's runs on the commit, the newest one that
was not skipped decides: a label event can add a newer skipped run while the real one runs, and a
commit can collect many skipped runs, so every page of runs is read. Because the check records
before it can pass, a passed check with no address is a broken recorder, not a wait: it fails at
once, naming the check.

Exit 0: recorded, or the address printed. Exit 1: there is no preview to give (the check failed,
was cancelled or timed out, has only skipped runs for three minutes, did not pass within
--wait-minutes, or passed and recorded nothing), or a record was refused. Exit 2: GitHub could not
be read six times in a row. Errors are printed as `::error::` lines, so a workflow shows them.
Reads and writes go through `gh` with the token in GH_TOKEN; a record needs `statuses: write`, a
find `checks: read` and `statuses: read`.
"""
import argparse
import json
import subprocess
import sys
import time

CONTEXT = "preview"
FAILED_READS_ALLOWED = 6
# Polls of only-skipped runs before a commit is judged to have no preview: three minutes at the
# default interval. The counts are fixed in polls, so a test that sleeps 0 sees the same budget.
SKIPPED_POLLS = 18
DEFAULT_INTERVAL = 10


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

    def read(self, args):
        result = subprocess.run(["gh", "api", *args], capture_output=True, text=True)
        if result.returncode == 0:
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


def newest_address(lines):
    """The link of the newest `preview` status, one `created_at<TAB>target_url` line each, or ""."""
    rows = [line.split("\t") for line in lines.splitlines() if line.strip()]
    rows = [row for row in rows if len(row) == 2 and row[1]]
    return max(rows)[1] if rows else ""


def record(args):
    if not args.url.startswith("https://"):
        fail("the preview address to record must start with https://, got %r" % args.url)
    payload = json.dumps({"state": "success", "context": CONTEXT, "target_url": args.url.rstrip("/"),
                          "description": "The preview of this commit under test"})
    for attempt in range(1, 4):
        result = subprocess.run(["gh", "api", "-X", "POST", "repos/%s/statuses/%s" % (args.repo, args.sha),
                                 "--input", "-"], input=payload, capture_output=True, text=True)
        if result.returncode == 0:
            print("recorded the preview of %s: %s" % (args.sha, args.url.rstrip("/")))
            return
        sys.stderr.write("recording failed (attempt %d of 3): %s\n" % (attempt, result.stderr.strip()))
        if attempt < 3:
            time.sleep(args.interval)
    fail("could not record the preview of %s as a `%s` status; the job needs `statuses: write`" % (args.sha, CONTEXT))


def wait_for_check(args, reader):
    polls = max(1, args.wait_minutes * 60 // DEFAULT_INTERVAL)
    skipped = 0
    for _ in range(polls):
        lines = reader.read(["repos/%s/commits/%s/check-runs" % (args.repo, args.sha), "--paginate",
                             "--method", "GET", "-f", "check_name=" + args.check, "-f", "per_page=100",
                             "--jq", '.check_runs[] | "\\(.started_at // "")\\t\\(.status)\\t\\(.conclusion)"'])
        verdict = "unread" if lines is None else newest_run(lines)
        if verdict == "completed success":
            return
        if verdict == "only skipped":
            skipped += 1
            if skipped >= SKIPPED_POLLS:
                fail('"%s" has only skipped runs on %s: no preview. Re-run once the preview has synced.'
                     % (args.check, args.sha))
        if verdict in ("completed failure", "completed cancelled", "completed timed_out"):
            fail('"%s" ended in %s, so %s has no preview' % (args.check, verdict.split()[1], args.sha))
        time.sleep(args.interval)
    fail('"%s" did not pass for %s within %d minutes' % (args.check, args.sha, args.wait_minutes))


def find(args):
    reader = Reader()
    try:
        wait_for_check(args, reader)
        lines = None
        while lines is None:
            lines = reader.read(["repos/%s/commits/%s/statuses" % (args.repo, args.sha), "--paginate",
                                 "--jq", '.[] | select(.context == "%s") | "\\(.created_at)\\t\\(.target_url // "")"' % CONTEXT])
            if lines is None:
                time.sleep(args.interval)
    except Unreadable as error:
        fail("gh api %s failed %d times in a row (gh's own error is above each), so the preview of %s "
             "was not found. Re-run this job." % (error, FAILED_READS_ALLOWED, args.sha), code=2)
    address = newest_address(lines)
    if not address:
        fail('"%s" passed on %s but recorded no `%s` status: its job needs the catalog\'s preview record '
             "step (actions/preview/README.md). Re-running this job will not help." % (args.check, args.sha, CONTEXT))
    print(address)


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("record", "find"):
        command = commands.add_parser(name)
        command.add_argument("--repo", required=True)
        command.add_argument("--sha", required=True)
        command.add_argument("--interval", type=float, default=DEFAULT_INTERVAL, help="seconds between polls")
    commands.choices["record"].add_argument("--url", required=True)
    commands.choices["find"].add_argument("--check", required=True)
    commands.choices["find"].add_argument("--wait-minutes", type=int, default=40)
    args = parser.parse_args()
    record(args) if args.command == "record" else find(args)


if __name__ == "__main__":
    main()
