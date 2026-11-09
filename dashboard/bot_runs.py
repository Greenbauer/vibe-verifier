"""The recent completed runs the dashboard shows for a bot."""

from __future__ import annotations

import re
from datetime import datetime, timedelta

from .util import parse_time

# Listener contract (lanes/listener): a JIT runner is `<name_prefix>-<kind>-<n>-<unix time>`.
# name_prefix matches [A-Za-z0-9][A-Za-z0-9-]*, kind is ci, qae, or wait, n starts at 1.
LANE_RUNNER = re.compile(
    r"^(?P<prefix>[A-Za-z0-9][A-Za-z0-9-]*)-(?P<kind>ci|qae|wait)-(?P<instance>[1-9][0-9]*)-(?P<epoch>[0-9]+)\Z")


def runner_name(value: object) -> str | None:
    """A runner name the jobs API can be trusted to have sent, or None."""
    if isinstance(value, str) and value.isascii() and value.isprintable() and 0 < len(value) <= 128:
        return value
    return None


# GitHub names each job of a matrix `<job> (<values>)`, as a split walk's `Acceptance criteria: walk (7)`.
MATRIX_JOB = re.compile(r"(?P<job>.+) \([^()]*\)\Z")


def job_named(name: object, names) -> bool:
    """The job is one of names, by its own name or as one job of that name's matrix."""
    if not isinstance(name, str):
        return False
    match = MATRIX_JOB.fullmatch(name)
    return name in names or bool(match and match.group("job") in names)


def job_roles(job: dict, role_jobs: dict) -> list[str]:
    """The roles of one job of a bot workflow: each role that names it, or, when none does, the explorer
    for a job that ran on a QAE lane slot, so that slot's card shows all of its work."""
    roles = [role for role, names in role_jobs.items() if job_named(job.get("name"), names)]
    if not roles and "explorer" in role_jobs and qae_instance(job.get("runner_name")):
        return ["explorer"]
    return roles


def bot_job_ran(job: dict) -> bool:
    """A job counts as a bot run when it had a runner and was not skipped."""
    return bool(job.get("runner_name")) and job.get("conclusion") != "skipped"


def workflow_basename(path: object) -> str | None:
    """The workflow file at the end of a run path, including a required workflow from another repo."""
    if not isinstance(path, str) or not path:
        return None
    return path.rsplit("/", 1)[-1]


def pulls_bot_jobs(payloads: object, bots: dict) -> list[tuple]:
    """Bot jobs already read on open pull requests. This adds no API calls."""
    if not isinstance(payloads, dict):
        return []
    by_file: dict[str, dict] = {}
    for role, definition in bots.items():
        by_file.setdefault(definition.workflow, {})[role] = definition.jobs
    found = []
    for repository, payload in payloads.items():
        if not isinstance(payload, dict):
            continue
        for pull in payload.get("pulls") or []:
            found.extend(_pull_jobs(repository, pull, by_file))
    return found


def _pull_jobs(repository: str, pull: dict, by_file: dict) -> list[tuple]:
    """The bot jobs of one pull request's runs; by_file maps each workflow file to its roles' job names."""
    found = []
    for run in pull.get("runs") or []:
        role_jobs = by_file.get(workflow_basename(run.get("path"))) or {}
        for job in run.get("jobs") or []:
            if bot_job_ran(job):
                found.extend((repository, role, run, job) for role in job_roles(job, role_jobs))
    return found


def qae_instance(runner_name: object) -> int | None:
    """The QAE lane instance in a runner name, or None when the name is not one."""
    if not isinstance(runner_name, str):
        return None
    match = LANE_RUNNER.fullmatch(runner_name)
    if not match or match.group("kind") != "qae":
        return None
    return int(match.group("instance"))


def recent_bot_runs(rows: list[dict], now: datetime, hours: int, limit: int = 5) -> list[dict]:
    """The newest completed runs in the window, newest first: `limit` for each QAE lane slot, which
    has a card of its own, and `limit` for the runs on any other runner."""
    cutoff = now - timedelta(hours=hours)
    complete = [row for row in rows if row.get("completed_at")
                and cutoff <= parse_time(row["completed_at"]) <= now]
    complete.sort(key=lambda row: parse_time(row["completed_at"]), reverse=True)
    kept, per_slot = [], {}
    for row in complete:
        slot = qae_instance(row.get("runner_name"))
        if per_slot.get(slot, 0) < limit:
            per_slot[slot] = per_slot.get(slot, 0) + 1
            kept.append(row)
    return kept
