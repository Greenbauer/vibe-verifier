"""A lane's recent history for the Capacity view: what its runners did, and how long jobs waited.

The collector calls capture() on every refresh (stdlib only; it also runs as a plain script). Two
inputs come from the lane's listener through the sampler, and both are optional:

- queue: per scale set, the jobs GitHub has assigned to it and how many of those are running, and
  the set's warm-pool size. Assigned minus running is the number of jobs waiting for a runner.
- jobs: the listener's newest started jobs with GitHub's own timestamps: queued, assigned to the
  lane's scale set, assigned to a runner. The wait is queued to runner, split into routing (GitHub
  sending it to the lane) and start (the lane starting a runner that takes it).

Three things are kept in the collector's root-only state, each pruned to seven days: samples (one
per refresh, now with running and waiting counts), waits (one per job), and the derived history the
dashboard reads. Only history is published; read_history() is the dashboard's reader of it.
"""

import datetime as dt
import math
import re

HISTORY_SECONDS = 7 * 24 * 60 * 60
MAX_SAMPLES = 20160
MAX_WAITS = 20000
MAX_WAIT_SECONDS = HISTORY_SECONDS
# A sample stands for the time until the next one, but a gap (the collector down) counts for at most
# this long, so an outage is not read as hours of whatever the last sample saw.
SAMPLE_SPAN_SECONDS = 120
# (name, length, bucket) in seconds: 30, 48 and 56 bars.
RANGES = (("1h", 3600, 120), ("24h", 86400, 1800), ("7d", 604800, 10800))
KINDS = ("ci", "qae", "wait")
TIME_FORMAT = "%Y-%m-%dT%H:%M:%SZ"
TIME_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
RUNNER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
SAMPLE_NUMBERS = ("cpu_busy_percent", "memory_available_bytes", "workspace_free_bytes",
                  "slots_occupied", "slots_remaining_on_demand")


def parse_time(value):
    if not isinstance(value, str) or not TIME_RE.fullmatch(value):
        return None
    try:
        return dt.datetime.strptime(value, TIME_FORMAT).replace(tzinfo=dt.timezone.utc)
    except ValueError:
        return None


def format_time(value):
    return value.strftime(TIME_FORMAT)


def _count(value, maximum=10000):
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= maximum:
        raise ValueError
    return value


def _number(value, maximum=None):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
        raise ValueError
    if maximum is not None and value > maximum:
        raise ValueError
    return value


def project_queue(raw):
    """Jobs waiting for a runner and warm runners kept on purpose, summed over the lane's sets."""
    if not isinstance(raw, dict) or raw.get("version") != 1 or not isinstance(raw.get("sets"), list):
        return None
    waiting = warm = 0
    try:
        for row in raw["sets"][:64]:
            if not isinstance(row, dict) or row.get("kind") not in KINDS:
                raise ValueError
            waiting += max(0, _count(row.get("assigned")) - _count(row.get("running")))
            warm += _count(row.get("min_runners", 0))
    except ValueError:
        return None
    return {"waiting": waiting, "warm": warm}


def _seconds(start, end):
    return int((end - start).total_seconds()) if start and end and end >= start else None


def project_job(row):
    """One wait record from a listener job entry, or None when its timestamps cannot form one."""
    if not isinstance(row, dict) or row.get("kind") not in KINDS:
        return None
    runner = row.get("runner_name")
    if not isinstance(runner, str) or not RUNNER_RE.fullmatch(runner):
        return None
    queued, routed = parse_time(row.get("queued_at")), parse_time(row.get("scale_set_assigned_at"))
    assigned, started = parse_time(row.get("runner_assigned_at")), parse_time(row.get("started_at"))
    wait = _seconds(queued, assigned)
    if wait is None or wait > MAX_WAIT_SECONDS or started is None:
        return None
    routing = _seconds(queued, routed)
    if routing is not None and routing > wait:
        routing = None
    return {"runner": runner, "kind": row["kind"], "started_at": format_time(started),
            "wait_seconds": wait, "routing_seconds": routing}


def _clean_wait(row):
    try:
        if parse_time(row.get("started_at")) is None or not RUNNER_RE.fullmatch(row.get("runner", "")):
            return None
        routing = row.get("routing_seconds")
        return {"runner": row["runner"], "kind": row["kind"] if row.get("kind") in KINDS else None,
                "started_at": row["started_at"], "wait_seconds": _count(row.get("wait_seconds"), MAX_WAIT_SECONDS),
                "routing_seconds": None if routing is None else _count(routing, MAX_WAIT_SECONDS)}
    except (AttributeError, TypeError, ValueError):
        return None


def waits(previous_rows, raw_jobs, now):
    """The kept waits plus the listener's new ones, one per runner, newest seven days."""
    cutoff = now - dt.timedelta(seconds=HISTORY_SECONDS)
    rows = [row for row in (previous_rows if isinstance(previous_rows, list) else [])
            if isinstance(row, dict)]
    jobs = raw_jobs.get("jobs") if isinstance(raw_jobs, dict) and raw_jobs.get("version") == 1 else None
    rows += [row for row in (jobs if isinstance(jobs, list) else [])]
    unique = {}
    for row in rows:
        clean = _clean_wait(row) if "runner" in row else project_job(row)
        if clean and parse_time(clean["started_at"]) >= cutoff:
            unique[clean["runner"]] = clean
    return sorted(unique.values(), key=lambda row: (row["started_at"], row["runner"]))[-MAX_WAITS:]


def running_count(host):
    """Shared-pool runners the host recorded a job on. The wait pool is not part of the budget."""
    slots = host.get("slots") if isinstance(host, dict) else None
    records = slots.get("occupied") if isinstance(slots, dict) else None
    return sum(1 for row in records or [] if row.get("kind") != "wait" and row.get("job_name"))


def _clean_sample(sample, cutoff):
    observed = parse_time(sample.get("observed_at"))
    metrics = sample["host"]
    clean = {name: _number(metrics[name], 100 if name == "cpu_busy_percent" else None) for name in SAMPLE_NUMBERS}
    for name in ("slots_running", "jobs_waiting"):
        if metrics.get(name) is not None:
            clean[name] = _count(metrics[name])
    if not observed or observed < cutoff:
        return None
    return {"observed_at": sample["observed_at"], "host": clean}


def samples(previous, host, success, now, queue):
    """The kept samples plus this refresh's when it succeeded, newest seven days."""
    cutoff = parse_time(now) - dt.timedelta(seconds=HISTORY_SECONDS)
    kept = []
    for sample in previous.get("samples", []) if isinstance(previous, dict) else []:
        try:
            clean = _clean_sample(sample, cutoff)
        except (AttributeError, KeyError, TypeError, ValueError):
            continue
        if clean:
            kept.append(clean)
    if success:
        kept.append({"observed_at": now, "host": {
            "cpu_busy_percent": host["cpu"]["busy_percent"],
            "memory_available_bytes": host["memory"]["available_bytes"],
            "workspace_free_bytes": host["workspace"]["free_bytes"],
            "slots_occupied": host["slots"]["occupied_count"],
            "slots_remaining_on_demand": host["slots"]["remaining_on_demand"],
            "slots_running": running_count(host),
            "jobs_waiting": queue["waiting"] if queue else None}})
    unique = {sample["observed_at"]: sample for sample in kept}
    return [unique[key] for key in sorted(unique)][-MAX_SAMPLES:]


def _percentile(sorted_values, fraction):
    return sorted_values[max(0, math.ceil(fraction * len(sorted_values)) - 1)]


def wait_summary(rows):
    """Nearest-rank median and 95th percentile of the wait, and the mean of each part."""
    values = sorted(row["wait_seconds"] for row in rows)
    if not values:
        return None
    split = [row for row in rows if row["routing_seconds"] is not None]
    routing = sum(row["routing_seconds"] for row in split) / len(split) if split else None
    start = sum(row["wait_seconds"] - row["routing_seconds"] for row in split) / len(split) if split else None
    return {"jobs": len(values), "p50_seconds": _percentile(values, .5), "p95_seconds": _percentile(values, .95),
            "routing_seconds": None if routing is None else round(routing, 1),
            "start_seconds": None if start is None else round(start, 1)}


def _usable(sample):
    return "slots_running" in sample["host"]


def _bucket(start, points):
    if not points:
        return {"start": format_time(start)}
    running = sum(point["host"]["slots_running"] for point in points) / len(points)
    occupied = sum(point["host"]["slots_occupied"] for point in points) / len(points)
    waiting = [point["host"]["jobs_waiting"] for point in points if point["host"].get("jobs_waiting") is not None]
    return {"start": format_time(start), "running": round(running, 2), "reserved": round(max(0, occupied - running), 2),
            "waiting": max(waiting) if waiting else None}


def _spans(points, now):
    times = [parse_time(point["observed_at"]) for point in points] + [now]
    return [min(SAMPLE_SPAN_SECONDS, (times[i + 1] - times[i]).total_seconds()) for i in range(len(points))]


def range_view(name, length, step, points, wait_rows, limit, now):
    """One range's bars and totals. Bars are aligned to the clock; the last one is still filling."""
    end = dt.datetime.fromtimestamp((int(now.timestamp()) // step + 1) * step, dt.timezone.utc)
    start = end - dt.timedelta(seconds=length)
    inside = [point for point in points if parse_time(point["observed_at"]) >= start]
    slots = {}
    for point in inside:
        index = int((parse_time(point["observed_at"]) - start).total_seconds()) // step
        slots.setdefault(index, []).append(point)
    buckets = [_bucket(start + dt.timedelta(seconds=index * step), slots.get(index, []))
               for index in range(length // step)]
    spans = _spans(inside, now)
    covered = sum(spans)
    running = sum(span * point["host"]["slots_running"] for span, point in zip(spans, inside))
    reserved = sum(span * max(0, point["host"]["slots_occupied"] - point["host"]["slots_running"])
                   for span, point in zip(spans, inside))
    since = format_time(now - dt.timedelta(seconds=length))
    return {"name": name, "bucket_seconds": step, "buckets": buckets,
            "covered_hours": round(covered / 3600, 2),
            "busy_percent": round(100 * running / (covered * limit), 1) if covered and limit else None,
            "reserved_hours": round(reserved / 3600, 2) if covered else None,
            "wait": wait_summary([row for row in wait_rows if row["started_at"] >= since])}


def history(kept_samples, wait_rows, host, host_ok, queue, now_text):
    """What the dashboard reads: the shared pool's limit, the queue now, and the three ranges."""
    now = parse_time(now_text)
    slots = host.get("slots") if isinstance(host, dict) else None
    limit = slots.get("limit") if isinstance(slots, dict) else None
    points = [sample for sample in kept_samples if _usable(sample)]
    started = {row["runner"]: row["started_at"] for row in wait_rows}
    occupied = [row.get("runner_name") for row in (slots or {}).get("occupied") or [] if row.get("runner_name")]
    return {"observed_at": now_text, "limit": limit,
            "waiting_now": queue["waiting"] if host_ok and queue else None,
            "warm": queue["warm"] if queue else None,
            "started": {runner: started[runner] for runner in occupied if runner in started},
            "ranges": [range_view(name, length, step, points, wait_rows, limit, now) for name, length, step in RANGES]}


def capture(previous, response, host, host_ok, now):
    """The three state sections one refresh writes: samples, waits, and the published history."""
    raw = response.get("host") if host_ok and isinstance(response, dict) else None
    queue = project_queue(raw.get("queue")) if isinstance(raw, dict) else None
    previous = previous if isinstance(previous, dict) else {}
    kept = samples(previous, host, host_ok, now, queue)
    rows = waits(previous.get("waits"), raw.get("jobs") if isinstance(raw, dict) else None, parse_time(now))
    return {"samples": kept, "waits": rows, "history": history(kept, rows, host, host_ok, queue, now)}


def _optional(value, reader):
    return None if value is None else reader(value)


def _read_bucket(row):
    if not isinstance(row, dict) or set(row) - {"start", "running", "reserved", "waiting"} or not parse_time(row.get("start")):
        raise ValueError
    clean = {"start": row["start"]}
    if "running" in row:
        clean.update(running=_number(row["running"], 10000), reserved=_number(row["reserved"], 10000),
                     waiting=_optional(row.get("waiting"), _count))
    return clean


def _read_wait(value):
    if not isinstance(value, dict):
        raise ValueError
    return {"jobs": _count(value.get("jobs"), MAX_WAITS),
            "p50_seconds": _count(value.get("p50_seconds"), MAX_WAIT_SECONDS),
            "p95_seconds": _count(value.get("p95_seconds"), MAX_WAIT_SECONDS),
            "routing_seconds": _optional(value.get("routing_seconds"), lambda item: _number(item, MAX_WAIT_SECONDS)),
            "start_seconds": _optional(value.get("start_seconds"), lambda item: _number(item, MAX_WAIT_SECONDS))}


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
            "reserved_hours": _optional(row.get("reserved_hours"), lambda item: _number(item, 64 * length / 3600)),
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
    return {"observed_at": value["observed_at"], "limit": _optional(value.get("limit"), lambda item: _count(item, 64)),
            "waiting_now": _optional(value.get("waiting_now"), _count),
            "warm": _optional(value.get("warm"), lambda item: _count(item, 64)), "started": dict(started),
            "ranges": [_read_range(row, expected) for row, expected in zip(ranges, RANGES)]}
