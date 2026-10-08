"""The recent completed runs the dashboard shows for a bot."""

from __future__ import annotations

import re
from datetime import datetime, timedelta

from .util import parse_time

# Listener contract (lanes/listener): a JIT runner is `<name_prefix>-<kind>-<n>-<unix time>`.
# name_prefix matches [A-Za-z0-9][A-Za-z0-9-]*, kind is ci, qae, or wait, n starts at 1.
LANE_RUNNER = re.compile(
    r"^(?P<prefix>[A-Za-z0-9][A-Za-z0-9-]*)-(?P<kind>ci|qae|wait)-(?P<instance>[1-9][0-9]*)-(?P<epoch>[0-9]+)\Z")


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
