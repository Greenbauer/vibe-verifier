"""The plan window the page paces, and how far its fill sits from an even burn.

The even pace is in the plan's own unit: 100% over the window's length, the tick on the usage bar.
`plan_pace` is the used percent minus the share of the window already elapsed, in points. It needs
the window's length and reset and nothing about the bots. `plan_burn` gives the All bots chart its
flat line in the same unit, percent of the plan an hour, and puts the bots on that scale by weighting
their tokens the way the plan counts them.
"""
from __future__ import annotations

from datetime import datetime, timedelta

from .util import parse_time


# A plan does not count every token alike. OpenAI's published Codex credit rates price a cached input
# token at a tenth (or less) of a fresh one and an output token at five times a fresh one. The bots'
# raw counts are mostly cache reads, so a plan size worked out from raw counts swung (107m tokens at 49%
# used, then 227m at 57% four hours later); weighted by kind they track the plan's own percent.
CACHED_WEIGHT, OUTPUT_WEIGHT = 0.1, 5


def _paced(usage: dict) -> tuple[dict, dict] | None:
    """The one plan that reports a window length, and its longest window; None when no single plan does."""
    paced = [(account, window) for account in usage.get("accounts", [])
             for window in account.get("quota_windows", []) if window.get("window_minutes")]
    if len({account["id"] for account, _ in paced}) != 1:
        return None
    return max(paced, key=lambda pair: pair[1]["window_minutes"])


def _paced_window(usage: dict) -> dict | None:
    pair = _paced(usage)
    return pair[1] if pair else None


def weighted_tokens(sample: dict) -> float:
    """A sample's tokens as the plan counts them. `input_tokens` includes the cached ones."""
    cached = min(sample.get("cached_input_tokens", 0), sample["input_tokens"])
    return sample["input_tokens"] - cached + CACHED_WEIGHT * cached + OUTPUT_WEIGHT * sample["output_tokens"]


def _percent_per_token(samples: list, billed, start: datetime, used: float, now: datetime) -> float | None:
    """Percent of the plan one weighted token is worth: the window's used percent over the weighted
    tokens its billed samples spent since it began. None when that cannot be counted."""
    # Samples are kept seven days, so a longer window's spend cannot be counted from its start.
    if start < now - timedelta(days=7) or used <= 0:
        return None
    spent = sum(weighted_tokens(sample) for sample in samples
                if billed(sample) and parse_time(sample["timestamp"]) >= start)
    return used / spent if spent > 0 else None


def plan_burn(usage: dict, now: datetime) -> tuple[dict | None, list]:
    """The paced plan's even pace, and each sample it bills given its share of the plan in percent.

    The line, `percent_per_hour`, is (100 - used%) over the hours left: exact, from the plan's own
    reading. A sample bills to the plan when its account has the plan's provider. Its `plan_percent`
    is its weighted tokens times `_percent_per_token`. That reads the bots' tokens as standing for all
    the plan's use, which holds while they keep their share of it; with no such tokens, the samples
    get none. Returns (None, samples) when there is no paced window or it has reset.
    """
    samples = list(usage.get("samples", []))
    pair = _paced(usage) if usage.get("available") else None
    reset = parse_time(pair[1]["resets_at"]) if pair else None
    if reset is None or reset <= now:
        return None, samples
    account, window = pair
    used = min(window["used_percent"], 100)
    providers = {item["id"]: str(item.get("provider", "")).lower() for item in usage.get("accounts", [])}
    billed = lambda sample: providers.get(sample["account"]) == providers[account["id"]]  # noqa: E731
    per_token = _percent_per_token(samples, billed, reset - timedelta(minutes=window["window_minutes"]), used, now)
    line = {"percent_per_hour": (100 - used) / ((reset - now).total_seconds() / 3600), "plan": account["label"],
            "window": window["name"], "used_percent": window["used_percent"], "resets_at": window["resets_at"],
            "sized": per_token is not None, "partial": bool(usage.get("partial"))}
    if per_token is None:
        return line, samples
    return line, [{**sample, "plan_percent": weighted_tokens(sample) * per_token} if billed(sample) else sample
                  for sample in samples]


def plan_window_start(usage: dict) -> datetime | None:
    """When the paced window began, or None when there is none."""
    window = _paced_window(usage) if isinstance(usage, dict) else None
    reset = parse_time(window.get("resets_at")) if window else None
    return reset - timedelta(minutes=window["window_minutes"]) if reset else None


def plan_pace(usage: dict, now: datetime) -> float | None:
    """Points the paced window's fill sits ahead of an even burn. None when there is no such window,
    or it has already reset."""
    window = _paced_window(usage) if usage.get("available") else None
    if window is None:
        return None
    remaining = (parse_time(window["resets_at"]) - now).total_seconds()
    elapsed = 1 - remaining / (window["window_minutes"] * 60)
    if remaining <= 0 or not 0 < elapsed <= 1:
        return None
    return min(window["used_percent"], 100) - elapsed * 100
