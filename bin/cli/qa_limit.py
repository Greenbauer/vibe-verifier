"""`qa-limit`: whether every explore job of a QAE run failed on a model usage limit, read from their failure
annotations, so the verify job passes that head with a warning instead of judging a walk that never ran."""
import json
import os
import re
import time

from _pin_bump import CannotRead, gh_json

# A model usage limit, as each lane's explore job reports it in a failure annotation: the Claude lane's
# pinned action fails with "... Claude Code returned an error result: You've hit your limit · resets 9am
# (UTC)" (and writes no execution log, so nothing in the job can read it), and actions/qae-codex with
# "Codex ended with an error: You've hit your usage limit. ... try again at <time>." A server-side rate
# limit ("not your usage limit") does not match: a rerun fixes that one, so it stays red.
USAGE_LIMIT = re.compile(r"(?P<limit>You(?:'|’)ve (?:hit|reached) your (?:[\w-]+ ){0,3}limit)\b(?P<rest>.*)", re.I | re.S)
LIMIT_RESET = re.compile(r"\b(?:resets|try again (?:at|in))\s+(?P<when>[^·\n]+?)\s*\.?\s*$", re.I)
# Its name, or the one docs/check-names.md replaced, which a copy made before it still carries.
EXPLORE_JOB = re.compile(r"(?:Acceptance criteria: walk|qae-explore)(?: \([0-9]+\))?")
# The explorer's step in both lanes, and the runner's annotation for a step that exited 1, as a real limit
# leaves them (Greenbauer/zack.land run 38286901786). An annotation is text any step can print, a pull request's
# build script among them, so a limit counts only when the explorer's step is the job's one failed step, it exited
# 1, and every other failure annotation is the limit: a killed explorer exits 137 or 143, and the lanes' own
# failures (the Claude action's error, actions/qae-codex's on any Codex exit) add an annotation of their own.
EXPLORER_STEP = "Explore the acceptance criteria in a real browser"
EXIT_CODE = re.compile(r"Process completed with exit code 1\.")


def explorer_limits(repo, run_id, wait=420, interval=15):
    """[{job, limit, reset}] (reset '' when the message names none), one per explore job of the run's latest
    attempt, when every one of them failed on a model usage limit, so no explorer walked anything. None when
    any explorer succeeded (its verdict is the gates' to judge) or failed for another reason, when there is
    none, or when the jobs or their annotations cannot be read (the workflow then lacks `actions: read` and
    `checks: read`). A job of a reusable workflow is named `<caller> / Acceptance criteria: walk`.

    GitHub lists a finished job's steps and annotations some time after the job ends (Greenbauer/zack.land run
    41110815005: no steps listed 5 minutes after both walks failed, all of them about 15 minutes after), so a
    failed explorer whose steps or failure annotations are not listed yet is read again every `interval` seconds
    for up to `wait`, inside the verify job's 10 minutes; still unlisted then, nothing is excused. A listed
    explorer that failed another way ends the wait at once: whatever the others show, the run is not excused."""
    deadline = time.monotonic() + wait
    try:
        while True:
            settled, limits = read_limits(repo, run_id)
            if settled:
                return limits
            if time.monotonic() >= deadline:
                print("::warning::GitHub had not listed every explore job's steps and failure annotations after %ds, "
                      "so the cause of the failure is unknown: rerun this job once they are listed" % wait)
                return None
            time.sleep(interval)
    except (CannotRead, ValueError, KeyError, TypeError, AttributeError) as error:
        print("::warning::could not tell why the explore job failed: %s" % error)
        return None


def read_limits(repo, run_id):
    """One read of explorer_limits: (True, its answer) once the answer is settled, (False, None) while a failed
    explorer's steps or annotations are not listed yet and none listed failed another way."""
    jobs = gh_json("repos/%s/actions/runs/%s/jobs?filter=latest&per_page=100" % (repo, run_id)).get("jobs", [])
    explorers = [job for job in jobs if EXPLORE_JOB.fullmatch(job.get("name", "").split(" / ")[-1])]
    if not explorers or any(job.get("conclusion") != "failure" for job in explorers):
        return True, None
    originals = {}  # read again on every pass: an earlier attempt's steps can be the ones still unlisted
    read = [(job, listed_failures(repo, job)) for job in (ran_job(repo, run_id, job, originals) for job in explorers)]
    limits = [job_limit(job, notes) for job, notes in read if notes is not None]
    if None in limits or len(limits) == len(read):
        return True, limits if None not in limits else None
    return False, None


def ran_job(repo, run_id, job, originals):
    """The job as it ran. Rerunning only the verify job copies the explore jobs into the new attempt under new
    ids, with their steps and times but none of their annotations (run 41110815005, attempt 2), so a copy is read
    as the earliest attempt's job of the same name, start and end. `originals` keeps each attempt's jobs for one
    pass."""
    for attempt in range(1, int(job.get("run_attempt") or 1)):
        if attempt not in originals:
            originals[attempt] = gh_json("repos/%s/actions/runs/%s/attempts/%d/jobs?per_page=100"
                                         % (repo, run_id, attempt)).get("jobs", [])
        for earlier in originals[attempt]:
            if [earlier.get(key) for key in ("name", "started_at", "completed_at")] == [
                    job.get(key) for key in ("name", "started_at", "completed_at")]:
                return earlier
    return job


def listed_failures(repo, job):
    """The failed job's failure annotation messages, or None while GitHub has not listed them yet: no steps, a
    step with no conclusion, or no failure annotation (every failed step leaves at least its exit code's)."""
    steps = job.get("steps") or []
    if not steps or any(step.get("conclusion") is None for step in steps):
        return None
    notes = gh_json("repos/%s/check-runs/%s/annotations?per_page=100" % (repo, job["id"]))
    return [note.get("message") or "" for note in notes if note.get("annotation_level") == "failure"] or None


def job_limit(job, messages):
    """{job, limit, reset} when the job's one failed step is the explorer's and its failure annotations are a
    usage limit and the exit code (reset '' when the limit names none); None otherwise."""
    if [step.get("name") for step in job["steps"] if step.get("conclusion") == "failure"] != [EXPLORER_STEP]:
        return None
    found = [match for match in map(USAGE_LIMIT.search, messages) if match]
    if not found or any(not USAGE_LIMIT.search(message) and not EXIT_CODE.fullmatch(message) for message in messages):
        return None
    reset = LIMIT_RESET.search(found[0].group("rest").strip())
    return {"job": job["name"], "limit": found[0].group("limit"), "reset": reset.group("when").strip() if reset else ""}


def describe_limits(limits):
    """Each explorer's limit and its reset, one string each: `Acceptance criteria: walk: You've hit your limit, resets 9am`."""
    return ["%s: %s, %s" % (found["job"], found["limit"], "resets " + found["reset"] if found["reset"]
                            else "its message names no reset time") for found in limits]


def qa_limit_command(args):
    """The QAE verify job's usage-limit step: when every explorer hit its model usage limit, warn with each limit and
    its reset and write `limited=true` and the limits (JSON) to $GITHUB_OUTPUT, so the steps that judge a walk are
    skipped and the job passes with the warning (operator, 2026-10-29); otherwise `limited=false`."""
    limits = explorer_limits(args.repo, args.run_id, args.wait, args.interval)
    if limits:
        print("::warning::The acceptance walk did not run on %s: every explorer hit its model usage limit (%s). This "
              "head was NOT checked in a browser; the QA review records it as limited, never as a walk. Rerun the "
              "workflow after the reset, so the next walk checks it." % (args.head[:10], "; ".join(describe_limits(limits))))
    else:
        print("not a model usage limit on every explorer: the walk is judged as usual")
    output = os.environ.get("GITHUB_OUTPUT")
    if output:
        with open(output, "a", encoding="utf-8") as handle:
            handle.write("limited=%s\nlimits=%s\n" % ("true" if limits else "false", json.dumps(limits or [])))
    return 0


def add_arguments(parser):
    parser.add_argument("--repo", required=True, metavar="OWNER/NAME")
    parser.add_argument("--run-id", required=True, metavar="ID")
    parser.add_argument("--head", required=True, metavar="SHA", help="the head commit the run judged")
    parser.add_argument("--wait", type=int, default=420, metavar="SECONDS", help="how long to wait for GitHub to list a "
                        "failed explore job's steps and annotations (default 420, inside the verify job's 10 minutes)")
    parser.add_argument("--interval", type=int, default=15, metavar="SECONDS", help="seconds between reads while waiting")
