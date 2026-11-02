"""What `apply-down` writes, and what `apply-down --auto` decides before it writes.

`open_bump` opens one pin-bump pull request and `supersede` closes the older bumps it replaces, so a
repository has at most one open bump of each level. `--auto` is the scheduled form (actions/pin-bump):
it holds a repository whose bump would move a tool version, and bumps the rest only when it is time,
the rule a wrapper's ruleset pin mover can use for its own pins: on demand (`--now`), once the
oldest release commit waiting has waited `--overdue-hours`, or when the runner lane the bumps' checks
run on is quiet. It confirms its own plan's digest, so what it writes is what it printed.
"""
import base64
import json
import os
import re
import subprocess  # nosec B404: runs gh and git, the caller's own tools, with fixed arguments
import time

#: A bump that moves any of these moves a tool version, and GATE-CONTRACT.md ("Subscribing in CI") asks for
#: the gate to be swept with --all on every consumer first; --auto holds it for a person.
TOOL_PATHS = ["gates/_tools.py", "tools"]
#: Quiet, as measured on the first wrapper's runner lane over a week of its runs (2026-10): at most this many
#: runs created in the last hour, scheduled ones left out, and then no job waiting for a slot of the lane and
#: at most MAX_RUNNING running there.
MAX_RECENT_RUNS = 6
MAX_RUNNING = 2
BRANCH_PREFIXES = ("ci/vibe-verifier-pin-", "ci/wrapper-pin-")


class CannotRead(Exception):
    """A read through gh failed for a reason other than the file not existing."""


def gh_json(*api_args, method=None, fields=None, body=None):
    """A gh api call returning parsed JSON, with an optional JSON request body. Raises CannotRead with
    gh's own last line."""
    command = ["gh", "api", *api_args]
    if method:
        command += ["-X", method]
    for name, value in (fields or {}).items():
        command += ["-f", "%s=%s" % (name, value)]
    if body is not None:
        command += ["--input", "-"]
    result = subprocess.run(command, capture_output=True, text=True,  # nosec B603
                            input=None if body is None else json.dumps(body))
    if result.returncode != 0:
        raise CannotRead((result.stderr.strip() or "gh api failed").splitlines()[-1])
    return json.loads(result.stdout) if result.stdout.strip() else {}


def add_arguments(parser):
    parser.add_argument("--auto", action="store_true", help="the scheduled form: hold bumps that move a tool version, "
                        "and confirm the rest only when it is time (--now, overdue, or a quiet --lane); never moves a ruleset")
    parser.add_argument("--act", action="store_true", help="with --auto: write when it is time (without it, a dry run)")
    parser.add_argument("--now", action="store_true", help="with --auto: bump without waiting for a quiet lane")
    parser.add_argument("--lane", metavar="LABEL", help="with --auto: the runner label the bumps' checks run on; "
                        "wait until it is quiet (without it, never wait)")
    parser.add_argument("--overdue-hours", type=float, default=6, help="with --auto: bump once the oldest release "
                        "commit waiting is this old, quiet or not (default 6)")


def git(source, *args):
    return subprocess.run(["git", "-C", source, *args], capture_output=True, text=True)  # nosec B603 B607


def tool_moves(source, planned, target):
    """{repo: [tool paths]} for each catalog-level bump in `planned` whose pins move a tool version. A pin the
    catalog's history does not have (a deleted branch, a fork, a tag) cannot be read, so it is held too."""
    held = {}
    for repo, level, _, changes in planned:
        shas = {sha for _, shas, _ in changes for sha in shas} if level is None else set()
        paths = sorted({path for sha in shas for path in moved_tools(source, sha, target)})
        if paths:
            held[repo] = paths
    return held


def moved_tools(source, sha, target):
    """The tool paths that differ between the pin `sha` and `target`."""
    diff = git(source, "diff", "--name-only", sha, target, "--", *TOOL_PATHS) if is_sha(sha) else None
    return diff.stdout.split() if diff and diff.returncode == 0 else ["(%s is not in the catalog's history)" % sha[:10]]


def first_waiting(source, planned, target, release_paths):
    """Epoch seconds of the oldest release commit any catalog-level bump in `planned` would deliver."""
    times = [waiting_since(source, sha, target, release_paths) for _, level, _, changes in planned if level is None
             for sha in {sha for _, shas, _ in changes for sha in shas}]
    times = [at for at in times if at is not None]
    return min(times) if times else None


def waiting_since(source, sha, target, release_paths):
    """Epoch seconds of the first release commit after the pin `sha`, None when there is none. A pin that is
    not in the target's history counts from the target itself."""
    if not is_sha(sha) or git(source, "merge-base", "--is-ancestor", sha, target).returncode:
        return int(git(source, "log", "-1", "--format=%ct", target).stdout.strip())
    first = git(source, "log", "--reverse", "--format=%ct", "%s..%s" % (sha, target), "--", *release_paths).stdout.split()
    return int(first[0]) if first else None


def lane(label, repos, clock=time.time):
    """(quiet, why) for the runner label across `repos`, read through gh: the run this is part of
    ($GITHUB_REPOSITORY, $GITHUB_RUN_ID) is left out."""
    since = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(clock() - 3600))
    recent = sum(recent_runs(repo, since) for repo in repos)
    if recent > MAX_RECENT_RUNS:
        return False, "%d runs started in the last hour (quiet is at most %d)" % (recent, MAX_RECENT_RUNS)
    statuses = [job.get("status") for repo in repos for job in lane_jobs(repo) if label in (job.get("labels") or [])]
    waiting, running = statuses.count("queued"), statuses.count("in_progress")
    why = "%d run(s) in the last hour, %d job(s) waiting for %s, %d running" % (recent, waiting, label, running)
    return waiting == 0 and running <= MAX_RUNNING, why


def recent_runs(repo, since):
    """Runs of `repo` created since `since`, scheduled ones left out (a page past the first counts whole)."""
    answer = gh_json("repos/%s/actions/runs?per_page=100&created=%%3E%%3D%s" % (repo, since))
    runs = answer.get("workflow_runs") or []
    return max(answer.get("total_count", 0) - len(runs), 0) + sum(1 for run in runs if run.get("event") != "schedule")


def lane_jobs(repo):
    """Every job of the queued and running runs of `repo`, but those of the run this is part of."""
    mine = (os.environ.get("GITHUB_REPOSITORY", ""), os.environ.get("GITHUB_RUN_ID", ""))
    runs = [run for status in ("queued", "in_progress")
            for run in gh_json("repos/%s/actions/runs?per_page=100&status=%s" % (repo, status)).get("workflow_runs") or []
            if (repo, str(run.get("id"))) != mine]
    return [job for run in runs for job in gh_json("repos/%s/actions/runs/%s/jobs?per_page=100" % (repo, run.get("id"))).get("jobs") or []]


def when(args, planned, target, release_paths, clock=time.time):
    """(bump now, why)."""
    if args.now:
        return True, "asked to bump now"
    oldest = first_waiting(args.source, planned, target, release_paths)
    waited = clock() - oldest if oldest is not None else 0
    if oldest is not None and waited >= args.overdue_hours * 3600:
        return True, "the oldest release commit waiting is %.1fh old (the limit is %gh)" % (waited / 3600, args.overdue_hours)
    if not args.lane:
        return True, "no lane to wait for"
    quiet, why = lane(args.lane, sorted({repo for repo, _, _, _ in planned}), clock)
    return quiet, ("the lane is quiet: " if quiet else "the lane is busy: ") + why + (
        "" if quiet else "; the oldest release commit waiting is %.1fh old of %gh" % (waited / 3600, args.overdue_hours))


def open_bump(repo, changes, target, branch, default_branch, wrapper=None):
    """One pull request changing only pin lines, one commit per workflow file. Returns its URL. With
    `wrapper` (OWNER/NAME) the pins are of that wrapper's workflows, not of catalog actions."""
    head = gh_json("repos/%s/git/ref/heads/%s" % (repo, default_branch))["object"]["sha"]
    gh_json("repos/%s/git/refs" % repo, method="POST", fields={"ref": "refs/heads/" + branch, "sha": head})
    what = "the %s workflow" % wrapper if wrapper else "the Vibe Verifier action"
    why = ("The workflow a caller runs comes from the pinned commit, so this is how a repository takes up the wrapper's changes."
           if wrapper else "The gates and actions a workflow runs come from the pinned commit, so this is how a repository takes up catalog changes.")
    for path, shas, text in changes:
        blob = gh_json("repos/%s/contents/%s?ref=%s" % (repo, path, default_branch))["sha"]
        message = "ci: bump %s pin to %s in %s\n\nPin lines only. %s\n" % (what, target[:10], path, why)
        gh_json("repos/%s/contents/%s" % (repo, path), method="PUT", fields={
            "message": message, "branch": branch, "sha": blob,
            "content": base64.b64encode(text.encode()).decode()})
    moved = "\n".join("- `%s`: %s -> `%s`" % (path, ", ".join("`%s`" % s[:10] for s in shas), target[:10]) for path, shas, _ in changes)
    proof = ("This PR's own run of the called workflow is the proof that it works here." if wrapper
             else "This PR's own run of the gate job (`Gates`, or `vibe-verifier-ok` in an older stub) is the proof "
                  "that the new catalog passes here.")
    result = subprocess.run(["gh", "pr", "create", "--repo", repo, "--base", default_branch, "--head", branch,  # nosec B603 B607
                             "--title", "ci: bump %s pin to %s" % (what, target[:10]),
                             "--body", "Moves %s pins to `%s`, and changes nothing else:\n\n%s\n\n%s %s\n\n"
                                       "## Acceptance criteria\n\n- None: this changes "
                                       "pinned commit SHAs in workflow files. No application code, route, or rendered surface "
                                       "changes, so there is nothing for a browser to check.\n\n"
                                       "🤖 Generated with [Claude Code](https://claude.com/claude-code)" % (
                                           "`%s` workflow" % wrapper if wrapper else "catalog", target[:10], moved, why, proof)],
                            capture_output=True, text=True)
    if result.returncode != 0:
        raise CannotRead((result.stderr.strip() or "gh pr create failed").splitlines()[-1])
    return result.stdout.strip().splitlines()[-1]


def supersede(repo, branch, url, older):
    """Close every other open bump of the same level in `repo` (a branch of this repository named like
    `branch` up to its target) whose target `older(short sha)` says the new one includes, saying which pull
    request replaced it. Returns the closed URLs."""
    prefix = next(p for p in BRANCH_PREFIXES if branch.startswith(p))
    closed = []
    for pull in open_pulls(repo):
        head = pull.get("head") or {}
        ref = head.get("ref", "")
        if (ref.startswith(prefix) and ref != branch and (head.get("repo") or {}).get("full_name") == repo
                and older(ref[len(prefix):])):
            result = subprocess.run(["gh", "pr", "close", str(pull["number"]), "--repo", repo, "--delete-branch",  # nosec B603 B607
                                     "--comment", "Superseded by %s, which moves the same pins to a newer commit." % url],
                                    capture_output=True, text=True)
            if result.returncode != 0:
                raise CannotRead((result.stderr.strip() or "gh pr close failed").splitlines()[-1])
            closed.append(pull.get("html_url", "#%s" % pull["number"]))
    return closed


def open_pulls(repo):
    """Every open pull request of `repo`, page by page."""
    pulls, page = [], 1
    while True:
        batch = gh_json("repos/%s/pulls?state=open&per_page=100&page=%d" % (repo, page))
        pulls += batch
        if len(batch) < 100:
            return pulls
        page += 1


def older_than(checkout, target):
    """A test for supersede: whether a short commit SHA is in `checkout` and an ancestor of `target`."""
    return lambda short: is_sha(short, 7) and git(checkout, "merge-base", "--is-ancestor", short, target).returncode == 0


def is_sha(text, shortest=40):
    """Whether `text` is a commit SHA (at least `shortest` hex digits): a pin or branch name read from another
    repository goes to git as an argument only then, never as an option such as --output=<file>."""
    return re.fullmatch(r"[0-9a-f]{%d,40}" % shortest, text or "") is not None
