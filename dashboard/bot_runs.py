"""The recent completed runs the dashboard shows for a bot."""

from __future__ import annotations

from datetime import datetime, timedelta

from .util import parse_time


def recent_bot_runs(rows: list[dict], now: datetime, hours: int, limit: int = 5) -> list[dict]:
    cutoff = now - timedelta(hours=hours)
    complete = [row for row in rows if row.get("completed_at")
                and cutoff <= parse_time(row["completed_at"]) <= now]
    complete.sort(key=lambda row: parse_time(row["completed_at"]), reverse=True)
    return complete[:limit]
