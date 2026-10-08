"""The plan window the page paces, and how far its fill sits from an even burn.

The even pace is in the plan's own unit: 100% over the window's length, the tick on the usage bar.
`delta_points` is the used percent minus the share of the window already elapsed. It needs the
window's length and reset and nothing about the bots. A provider reports a plan as a percent used,
never as a size in tokens, so there is no pace in tokens: a size worked out from the bots' tokens
follows how much the bots use, on a plan that other use shares.
"""
from __future__ import annotations

from datetime import datetime, timedelta

from .util import parse_time


def _paced_window(usage: dict) -> dict | None:
    """The longest window of the one plan that reports a window length, or None when no single plan does."""
    paced = [(account["id"], window) for account in usage.get("accounts", [])
             for window in account.get("quota_windows", []) if window.get("window_minutes")]
    if len({account for account, _ in paced}) != 1:
        return None
    return max((window for _, window in paced), key=lambda window: window["window_minutes"])


def plan_window_start(usage: dict) -> datetime | None:
    """When the paced window began, or None when there is none."""
    window = _paced_window(usage) if isinstance(usage, dict) else None
    reset = parse_time(window.get("resets_at")) if window else None
    return reset - timedelta(minutes=window["window_minutes"]) if reset else None


def plan_pace(usage: dict, now: datetime) -> dict:
    """`delta_points` for the paced window: None when there is none, or it has already reset."""
    window = _paced_window(usage) if usage.get("available") else None
    if window is None:
        return {"delta_points": None}
    remaining = (parse_time(window["resets_at"]) - now).total_seconds()
    elapsed = 1 - remaining / (window["window_minutes"] * 60)
    if remaining <= 0 or not 0 < elapsed <= 1:
        return {"delta_points": None}
    return {"delta_points": min(window["used_percent"], 100) - elapsed * 100}
