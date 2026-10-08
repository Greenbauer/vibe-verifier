"""The All-bots pace line: the even pace, the same comparison as the tick on the usage bar.

The line is the plan's size in tokens divided by the window's length in hours. Being above the line
for an hour means the same thing as being right of the tick. ``tokens_per_hour`` is that even pace,
not the rate that would spend the remainder by reset. ``delta_points`` is how far the used percent
sits from the same even burn. The size is the reported allowance when a plan gives one, otherwise
the plotted bots' tokens since the window began divided by the used percent. When other use shares
the plan, that reads the bots' tokens as standing for all of it, which holds while the bots keep
their share; the page says so.
"""
from __future__ import annotations

from datetime import datetime, timedelta

from .util import parse_time

HISTORY = timedelta(days=7)  # samples older than this are dropped on read (telemetry.HISTORY)


def _tokens(sample: dict) -> int:
    return sample["input_tokens"] + sample["output_tokens"]


def _none(reason: str) -> dict:
    return {"tokens_per_hour": None, "reason": reason}


def plan_window_start(usage: dict) -> datetime | None:
    """Start of the one plan `plan_pace` would size, or None when that plan is not unique."""
    if not isinstance(usage, dict):
        return None
    paced = [(account, window) for account in usage.get("accounts", []) for window in account.get("quota_windows", [])
             if window.get("allowance_tokens") is not None or window.get("window_minutes")]
    if len({account["id"] for account, _ in paced}) != 1:
        return None
    reported = [pair for pair in paced if pair[1].get("allowance_tokens") is not None]
    window = (reported[0] if reported else max(paced, key=lambda pair: pair[1]["window_minutes"]))[1]
    reset = parse_time(window.get("resets_at"))
    minutes = window.get("window_minutes")
    if reset is None or not minutes:
        return None
    return reset - timedelta(minutes=minutes)


def plan_pace(usage: dict, now: datetime) -> dict:
    """Pace for the one plan the plotted samples bill to, or the reason there is none.

    `usage.samples` must already be the plotted population, so the line shares the lines' unit.
    """
    if not usage.get("available"):
        return _none("usage telemetry is unavailable.")
    paced = [(account, window) for account in usage.get("accounts", []) for window in account.get("quota_windows", [])
             if window.get("allowance_tokens") is not None or window.get("window_minutes")]
    if len({account["id"] for account, _ in paced}) != 1:
        return _none("no plan reports both a window length and a reset time." if not paced else
                     "the bots bill to more than one plan, and their tokens cannot be split between them.")
    samples = usage.get("samples", [])
    reported = [pair for pair in paced if pair[1].get("allowance_tokens") is not None]
    account, window = reported[0] if reported else max(paced, key=lambda pair: pair[1]["window_minutes"])
    reset, used = parse_time(window["resets_at"]), window["used_percent"]
    remaining = (reset - now).total_seconds()
    if remaining <= 0:
        return _none("the plan's window has reset; waiting for its next reading.")
    # Points the fill sits ahead of an even burn: needs only the window's length, never its size.
    delta = None
    if window.get("window_minutes"):
        elapsed = 1 - remaining / (window["window_minutes"] * 60)
        if 0 < elapsed <= 1:
            delta = min(used, 100) - elapsed * 100

    def none(reason: str) -> dict:
        return {**_none(reason), "delta_points": delta}

    result = {"plan": account["label"], "window": window["name"], "resets_at": window["resets_at"],
              "used_percent": used, "delta_points": delta}
    if reported:
        # A reported size paces only bots that are wholly on that account.
        if not samples or any(sample["account"] != account["id"] for sample in samples):
            return none("the reported allowance belongs to a different account than the plotted tokens.")
        allowance, window_tokens, sized_from = window["allowance_tokens"], None, "reported"
    else:
        start = reset - timedelta(minutes=window["window_minutes"])
        if start < now - HISTORY:
            return none("the plan's window began before the seven days of token history kept here.")
        if usage.get("stale") or usage.get("history_stale") or usage.get("partial"):
            return none("token history is stale or incomplete, so the plan's size cannot be measured.")
        if used <= 0:
            return none("the plan shows 0% used, so its size cannot be measured yet.")
        # Hourly rows are stamped at the hour's start, so the hour the window began in is left out.
        window_tokens = sum(_tokens(sample) for sample in samples if parse_time(sample["timestamp"]) >= start)
        if window_tokens == 0:
            return none("no bot tokens are recorded since the plan's window began.")
        allowance, sized_from = window_tokens / (used / 100), "bot_tokens"
    minutes = window.get("window_minutes")
    if not minutes:
        return none("no plan reports both a window length and a reset time.")
    return {**result, "tokens_per_hour": allowance / (minutes / 60),
            "allowance_tokens": round(allowance), "window_tokens": window_tokens, "sized_from": sized_from}
