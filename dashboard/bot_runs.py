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
    by_file: dict[str, list[str]] = {}
    for role, definition in bots.items():
        by_file.setdefault(definition.workflow, []).append(role)
    found = []
    for repository, payload in payloads.items():
        if not isinstance(payload, dict):
            continue
        for pull in payload.get("pulls") or []:
            found.extend(_pull_jobs(repository, pull, by_file, bots))
    return found


def _pull_jobs(repository: str, pull: dict, by_file: dict, bots: dict) -> list[tuple]:
    found = []
    for run in pull.get("runs") or []:
        roles = by_file.get(workflow_basename(run.get("path"))) or []
        for job in run.get("jobs") or []:
            if bot_job_ran(job):
                found.extend((repository, role, run, job)
                             for role in roles if job.get("name") in bots[role].jobs)
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
    cutoff = now - timedelta(hours=hours)
    complete = [row for row in rows if row.get("completed_at")
                and cutoff <= parse_time(row["completed_at"]) <= now]
    complete.sort(key=lambda row: parse_time(row["completed_at"]), reverse=True)
    return complete[:limit]
