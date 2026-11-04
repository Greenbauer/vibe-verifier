"""What `apply-down` writes, and what `apply-down --auto` decides before it writes.

`open_bump` opens one pin-bump pull request and `supersede` closes the older bumps it replaces, so a
repository has at most one open bump of each level; `--merge` merges them once they are ready (_pin_merge.py).
`--auto` is the scheduled form (actions/pin-bump): it holds a repository whose bump would move a tool
version, and bumps the rest only when it is time, the rule a wrapper's ruleset pin mover can use for its
own pins: on demand (`--now`), once the oldest release commit waiting has waited `--overdue-hours`, or
when the runner lane the bumps' checks run on is quiet. It confirms its own plan's digest, so what it
writes is what it printed.
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
    parser.add_argument("--merge", action="store_true", help="with --auto --act: first squash-merge each open bump that "
                        "is merge-ready, and close one that conflicts or has nothing left to change (gates/_pin_merge.py); "
                        "with --auto alone, check the token can read the checks a merge reads")
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


def holds(source, planned, target):
    """{repo: [why]} for each catalog-level bump in `planned` that must wait for a person: it moves a tool version
    (tool_moves), or a workflow it rewrites passes a `with:` input the action no longer declares at `target`,
    which would fail every run of that workflow from the merge on."""
    held = {repo: ["moves %s, so sweep that gate with --all on every consumer first" % " ".join(paths)]
                   for repo, paths in tool_moves(source, planned, target).items()}
    for repo, level, _, changes in planned:
        missing = [why for path, _, text in changes for why in undeclared_inputs(source, target, path, text)] if level is None else []
        if missing:
            held.setdefault(repo, []).extend(missing)
    return held


USES = re.compile(r"^([ \t]*(?:- +)?)uses: *\S*vibe-verifier/actions/([A-Za-z0-9_-]+)@")
KEY = re.compile(r"^( *)([A-Za-z0-9_-]+):")


def undeclared_inputs(source, target, path, text):
    """Why each input `text` passes to a catalog action is not one of that action's inputs at `target`."""
    found = []
    for action, key in passed_inputs(text):
        declared = action_inputs(source, target, action)
        if declared is None:
            found.append("%s uses actions/%s, which %s does not have" % (path, action, target[:10]))
        elif key not in declared:
            found.append("%s passes `%s` to actions/%s, which %s does not declare" % (path, key, action, target[:10]))
    return sorted(set(found))


def passed_inputs(text):
    """[(action, input)] for each `with:` key of each step of `text` that uses a catalog action. Read line by line
    (the catalog parses no YAML): a step's keys sit at the column of its `uses:`, its inputs one level under `with:`,
    which may come before or after `uses:`, or inline as `with: {a: 1}`."""
    lines, found = text.splitlines(), []
    for at, line in enumerate(lines):
        use = USES.match(line)
        if use:
            column = len(use.group(1))
            found += [(use.group(2), key) for key in with_keys(lines[step_start(lines, at, column):], column)]
    return found


def step_start(lines, at, column):
    """The index after the `- ` line that opens the step whose `uses:` is on line `at` (that line itself when
    `uses:` is the step's first key, whose own text is then not a key at `column`)."""
    if lines[at].lstrip().startswith("- "):
        return at + 1
    for back in range(at - 1, -1, -1):
        stripped = lines[back].lstrip()
        if stripped.startswith("- ") and len(lines[back]) - len(stripped) == column - 2:
            return back + 1
    return at + 1


WITH = re.compile(r"^with:\s*(\{[^}]*\})?\s*(?:#.*)?$")


def with_keys(rest, column):
    """The keys under the `with:` of a step whose keys sit at `column`, from the step's lines."""
    lines = step_lines(rest, column)
    start = next((at for at, (indent, text) in enumerate(lines) if indent == column and WITH.match(text)), None)
    if start is None:
        return []
    inline = WITH.match(lines[start][1]).group(1)
    if inline:
        return re.findall(r"([A-Za-z0-9_-]+)\s*:", inline)
    block = []
    for indent, text in lines[start + 1:]:
        if indent <= column:
            break
        block.append((indent, text))
    depth = block[0][0] if block else None
    return [KEY.match(text).group(2) for indent, text in block if indent == depth and KEY.match(text)]


def step_lines(rest, column):
    """[(indent, stripped text)] of the step's own lines, blank lines and comments left out: up to the next step
    (a `- ` at the list's column) or the first line left of the step's keys."""
    lines = []
    for line in rest:
        text, indent = line.strip(), len(line) - len(line.lstrip())
        if not text or text.startswith("#"):
            continue
        if indent < column or (indent == column - 2 and text.startswith("- ")):
            break
        lines.append((indent, text))
    return lines


def action_inputs(source, target, action):
    """The inputs actions/<action>/action.yml declares at `target`, or None when there is no such action."""
    shown = git(source, "show", "%s:actions/%s/action.yml" % (target, action))
    if shown.returncode:
        return None
    lines = shown.stdout.splitlines()
    start = next((at for at, line in enumerate(lines) if re.match(r"^inputs:\s*(?:#.*)?$", line)), len(lines))
    body = [line for line in lines[start + 1:] if line.strip() and not line.lstrip().startswith("#")]
    end = next((at for at, line in enumerate(body) if not line.startswith(" ")), len(body))
    depth = min((len(line) - len(line.lstrip()) for line in body[:end]), default=0)
    return [KEY.match(line).group(2) for line in body[:end] if KEY.match(line) and len(KEY.match(line).group(1)) == depth]


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
    try:
        gh_json("repos/%s/git/refs" % repo, method="POST", fields={"ref": "refs/heads/" + branch, "sha": head})
    except CannotRead as error:
        if "Reference already exists" not in str(error):
            raise
        # This tool's own branch, left by a bump that failed part way or was closed by hand with its branch
        # kept; no open pull request has it (the caller looked), so it starts again from the default branch.
        gh_json("repos/%s/git/refs/heads/%s" % (repo, branch), method="PATCH", body={"sha": head, "force": True})
    what = "the %s workflow" % wrapper if wrapper else "the Vibe Verifier action"
    why = ("The workflow a caller runs comes from the pinned commit, so this is how a repository takes up the wrapper's changes."
           if wrapper else "The gates and actions a workflow runs come from the pinned commit, so this is how a repository takes up catalog changes.")
    for path, shas, text in changes:
        blob = gh_json("repos/%s/contents/%s?ref=%s" % (repo, path, default_branch))["sha"]
        message = "ci: bump %s pin to %s in %s\n\nPin lines only. %s\n" % (what, target[:10], path, why)
        # The file goes in the request body, never on gh's command line: a large workflow (a QAE harness copy)
        # is past Linux's limit for one argument, and the run died with "Argument list too long".
        gh_json("repos/%s/contents/%s" % (repo, path), method="PUT", body={
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
            close(repo, pull["number"], "Superseded by %s, which moves the same pins to a newer commit." % url)
            closed.append(pull.get("html_url", "#%s" % pull["number"]))
    return closed


def close(repo, number, comment):
    """Close a bump with `comment`, and delete its branch."""
    result = subprocess.run(["gh", "pr", "close", str(number), "--repo", repo, "--delete-branch",  # nosec B603 B607
                             "--comment", comment], capture_output=True, text=True)
    if result.returncode != 0:
        raise CannotRead((result.stderr.strip() or "gh pr close failed").splitlines()[-1])


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
