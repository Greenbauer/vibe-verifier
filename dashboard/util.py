"""Small validation and status helpers shared by dashboard sources."""

from __future__ import annotations

from datetime import datetime, timezone
from urllib.parse import urlparse


def parse_time(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.astimezone(timezone.utc) if parsed.tzinfo else None


def iso_time(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def elapsed_seconds(start: object, end: object, now: datetime) -> int | None:
    began = parse_time(start)
    finished = parse_time(end) or now
    if not began or finished < began:
        return None
    return int((finished - began).total_seconds())


def category(status: object, conclusion: object) -> str:
    """Map GitHub's open vocabulary into the six dashboard states."""
    if status != "completed":
        return "pending" if status in {"queued", "in_progress", "pending", "requested", "waiting"} else "unknown"
    if conclusion == "success":
        return "success"
    if conclusion in {"failure", "timed_out", "startup_failure", "action_required"}:
        return "failed"
    if conclusion in {"skipped", "neutral"}:
        return "skipped"
    if conclusion in {"cancelled", "stale"}:
        return "cancelled"
    return "unknown"


def status_category(state: object) -> str:
    return {
        "success": "success", "failure": "failed", "error": "failed",
        "pending": "pending", "expected": "pending",
    }.get(state, "unknown")


def github_url(value: object, owner: str) -> str | None:
    """Return only an HTTPS github.com link rooted at the configured owner."""
    if not isinstance(value, str) or len(value) > 2048:
        return None
    parsed = urlparse(value)
    parts = [part for part in parsed.path.split("/") if part]
    if parsed.scheme != "https" or parsed.netloc.lower() != "github.com" or not parts:
        return None
    if parts[0].lower() != owner.lower() or parsed.username or parsed.password:
        return None
    return value
