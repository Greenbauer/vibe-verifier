"""The dashboard's reader of a lane's published history (lane_history.history()): every field
bounded, or ValueError. Kept apart from lane_history.py, which the collector runs.
"""

import re

from .lane_history import MAX_WAIT_SECONDS, MAX_WAITS, RANGES, RUNNER_RE, _count, _number, parse_time


def _optional(value, reader):
    return None if value is None else reader(value)


def _read_bucket(row):
    if (not isinstance(row, dict) or set(row) - {"start", "running", "warm", "reserved", "waiting"}
            or not parse_time(row.get("start"))):
        raise ValueError
    clean = {"start": row["start"]}
    if "running" in row:
        clean.update(running=_number(row["running"], 10000), warm=_number(row.get("warm", 0), 10000),
                     reserved=_number(row["reserved"], 10000), waiting=_optional(row.get("waiting"), _count))
    return clean


def _read_wait(value):
    if not isinstance(value, dict):
        raise ValueError
    return {"jobs": _count(value.get("jobs"), MAX_WAITS),
            "p50_seconds": _count(value.get("p50_seconds"), MAX_WAIT_SECONDS),
            "p95_seconds": _count(value.get("p95_seconds"), MAX_WAIT_SECONDS),
            "routing_seconds": _optional(value.get("routing_seconds"), lambda item: _number(item, MAX_WAIT_SECONDS))}


def _read_range(row, expected):
    name, length, step = expected
    if not isinstance(row, dict) or row.get("name") != name or row.get("bucket_seconds") != step:
        raise ValueError
    buckets = row.get("buckets")
    if not isinstance(buckets, list) or len(buckets) != length // step:
        raise ValueError
    return {"name": name, "bucket_seconds": step, "buckets": [_read_bucket(item) for item in buckets],
            "covered_hours": _number(row.get("covered_hours"), length / 3600 + 1),
            "busy_percent": _optional(row.get("busy_percent"), lambda item: _number(item, 100)),
            "warm_hours": _optional(row.get("warm_hours"), lambda item: _number(item, 64 * length / 3600)),
            "idle_hours": _optional(row.get("idle_hours"), lambda item: _number(item, 64 * length / 3600)),
            "wait": _optional(row.get("wait"), _read_wait)}


def read_history(value):
    """The dashboard's check of a published history: every field bounded, or ValueError."""
    if not isinstance(value, dict) or not parse_time(value.get("observed_at")):
        raise ValueError
    ranges = value.get("ranges")
    started = value.get("started")
    if not isinstance(ranges, list) or len(ranges) != len(RANGES) or not isinstance(started, dict) or len(started) > 256:
        raise ValueError
    if any(not RUNNER_RE.fullmatch(key) or not parse_time(item) for key, item in started.items()):
        raise ValueError
    warm_slots = value.get("warm_slots", [])
    if (not isinstance(warm_slots, list) or len(warm_slots) > 64
            or any(not isinstance(item, str) or not re.fullmatch(r"(ci|qae|wait)-[1-9][0-9]?", item) for item in warm_slots)):
        raise ValueError
    return {"observed_at": value["observed_at"], "limit": _optional(value.get("limit"), lambda item: _count(item, 64)),
            "waiting_now": _optional(value.get("waiting_now"), _count),
            "warm": _optional(value.get("warm"), lambda item: _count(item, 64)), "warm_slots": list(warm_slots),
            "qae_slots": _count(value.get("qae_slots", 0), 64),
            "started": dict(started),
            "ranges": [_read_range(row, expected) for row, expected in zip(ranges, RANGES)]}
