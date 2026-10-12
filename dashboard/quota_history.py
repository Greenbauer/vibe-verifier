"""Hourly history of each plan window's used percent: what the usage bar did over time.

The collector keeps it in its snapshot under `quota_history`, from the quota readings it already
takes and from nothing else: one point per window per clock hour (the hour's latest reading) for
seven days, 168 points a window. A point is [time read, used percent], in epoch seconds and the
provider's own percent. An hour with no reading has no point, and none is ever filled in.

Nothing here decides when a window reset. The dashboard draws the points read since the window
began (its reset minus its length, both the provider's), so a window that reset starts a new line
while the points of the one before it age out.

Standard library only and no relative import: collector.py, which also runs as a script, loads this
file by path, and collector_view.py imports it.
"""
import calendar
import math
import re
import time

HOUR = 3600
MAX_POINTS = 7 * 24  # a week of clock hours
KEEP_SECONDS = MAX_POINTS * HOUR
MAX_SERIES = 64  # the collector accepts at most 32 limits of two windows each
LIMIT = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")  # collector.TOKEN_RE
STAMP = "%Y-%m-%dT%H:%M:%SZ"
YEAR_10000 = 253402300800  # a time read from a STAMP is before it, so it always formats back


def _epoch(stamp):
    """Epoch seconds of a collector timestamp, or None when there is none."""
    try:
        return calendar.timegm(time.strptime(stamp, STAMP))
    except (TypeError, ValueError):
        return None


def _point(value):
    if not isinstance(value, list) or len(value) != 2:
        raise ValueError("history point is invalid")
    read_at, used = value
    if isinstance(read_at, bool) or not isinstance(read_at, int) or not 0 < read_at < YEAR_10000:
        raise ValueError("history time is invalid")
    if isinstance(used, bool) or not isinstance(used, (int, float)) or not math.isfinite(used) or not 0 <= used <= 100:
        raise ValueError("history percent is invalid")
    return [read_at, used]


def _series(value):
    if not isinstance(value, dict) or set(value) != {"limit_id", "name", "points"}:
        raise ValueError("history series is invalid")
    limit_id, name, points = value["limit_id"], value["name"], value["points"]
    if (not isinstance(limit_id, str) or not LIMIT.match(limit_id) or name not in ("primary", "secondary")
            or not isinstance(points, list) or len(points) > MAX_POINTS):
        raise ValueError("history series is invalid")
    points = [_point(point) for point in points]
    if any(later[0] // HOUR <= earlier[0] // HOUR for earlier, later in zip(points, points[1:])):
        raise ValueError("history points are not one per hour, oldest first")
    return (limit_id, name), points


def project(value):
    """The kept history as {(limit_id, window name): points, oldest first}, allowlisted like every
    other section of the snapshot. Raises ValueError for anything `advance` did not write."""
    if not isinstance(value, list) or len(value) > MAX_SERIES:
        raise ValueError("quota history is invalid")
    kept = dict(_series(series) for series in value)
    if len(kept) != len(value):
        raise ValueError("a window has two histories")
    return kept


def advance(previous, account, now):
    """The history to store after this refresh.

    `previous` is the last snapshot's `quota_history`. A snapshot from before this field existed, or
    a value this module cannot read, starts the history empty. `account` is the collector's account
    section: its reading takes the place of an earlier reading in the same clock hour, so a refresh
    that did not read the quota re-applies the reading it already had and changes nothing. A window
    missing from the reading keeps its points until they age out after seven days.
    """
    try:
        kept = project(previous)
    except (TypeError, ValueError):
        kept = {}
    read_at = _epoch(account["observed_at"])
    for limit in account["rate_limits"] if read_at else ():
        for window in limit["windows"]:
            points = kept.setdefault((limit["limit_id"], window["name"]), [])
            points[:] = sorted([point for point in points if point[0] // HOUR != read_at // HOUR]
                               + [[read_at, window["used_percent"]]])
    oldest = _epoch(now) - KEEP_SECONDS
    history = [{"limit_id": limit_id, "name": name,
                "points": [point for point in points if point[0] >= oldest][-MAX_POINTS:]}
               for (limit_id, name), points in sorted(kept.items())]
    return [series for series in history if series["points"]][:MAX_SERIES]


def window_history(kept, limit_id, window, observed_at):
    """One window's line for the page: the kept points read since the window began, oldest first.

    `observed_at` is when the window itself was read. That reading always belongs: the collector
    stamps a reading before it asks, so it can sit a moment before the start the provider answers with.
    """
    start, current = window["resets_at"] - window["duration_minutes"] * 60, _epoch(observed_at)
    return [{"at": time.strftime(STAMP, time.gmtime(read_at)), "used_percent": used}
            for read_at, used in kept.get((limit_id, window["name"]), []) if read_at >= start or read_at == current]
