"""Step totals and the recent completed runs the dashboard shows for a bot."""

from __future__ import annotations

from datetime import datetime, timedelta

from .util import parse_time


def _count_steps(steps: list[dict], counts: dict) -> tuple[int, int]:
    total = completed = 0
    for step in steps:
        counts[step["category"]] += 1
        total += 1
        if step.get("status") == "completed":
            completed += 1
    return total, completed


def step_summary(jobs: list[dict]) -> dict:
    counts = {name: 0 for name in ("success", "failed", "skipped", "cancelled", "pending", "unknown")}
    total = completed = 0
    known = bool(jobs)
    for job in jobs:
        steps = job.get("steps")
        # GitHub returns no steps for a skipped job; once a job completes, an empty list means zero steps.
        if not isinstance(steps, list) or not steps and job.get("status") != "completed":
            known = False
            continue
        added, done = _count_steps(steps, counts)
        total += added
        completed += done
    return {"known": known, "completed": completed if known else None, "total": total if known else None,
            "remaining": total - completed if known else None, "counts": counts,
            "percent": round(completed * 100 / total) if known and total else None}


def recent_bot_runs(rows: list[dict], now: datetime, hours: int, limit: int = 5) -> list[dict]:
    cutoff = now - timedelta(hours=hours)
    complete = [row for row in rows if row.get("completed_at")
                and cutoff <= parse_time(row["completed_at"]) <= now]
    complete.sort(key=lambda row: parse_time(row["completed_at"]), reverse=True)
    return complete[:limit]
