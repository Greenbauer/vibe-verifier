"""The hourly history of each plan window's used percent: kept by the collector, projected for the page."""
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from dashboard import quota_history
from dashboard.config import Config
from dashboard.telemetry import MAX_BYTES, read_telemetry
from test_dashboard_collector import COLLECTOR, config, remote_host

START = datetime(2026, 9, 30, 12, 0, tzinfo=timezone.utc)
WEEK = 7 * 24 * 3600
RESET = int((START + timedelta(days=5)).timestamp())  # the week-long window began two days before START


def stamp(hours=0.0):
    return (START + timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%SZ")


def epoch(hours=0.0):
    return int((START + timedelta(hours=hours)).timestamp())


def limits(used, reset=RESET, name="primary", minutes=10080):
    return [{"limit_id": "codex", "windows": [
        {"name": name, "duration_minutes": minutes, "used_percent": used, "resets_at": reset}]}]


def account(hours, used, **window):
    return {"observed_at": stamp(hours), "rate_limits": limits(used, **window)}


def points(history, name="primary"):
    return next(series["points"] for series in history if series["name"] == name)


class Advance(unittest.TestCase):
    def test_a_state_from_before_the_history_starts_it_with_the_reading_just_taken(self):
        for previous in (None, {}, "old", [{"limit_id": "codex", "name": "primary"}]):
            with self.subTest(previous=previous):
                self.assertEqual(quota_history.advance(previous, account(0, 22), stamp(0)), [
                    {"limit_id": "codex", "name": "primary", "points": [[epoch(0), 22]]}])

    def test_no_reading_ever_taken_keeps_no_history(self):
        self.assertEqual(quota_history.advance(None, {"observed_at": None, "rate_limits": []}, stamp(0)), [])

    def test_an_hour_keeps_its_latest_reading_and_a_new_hour_adds_a_point(self):
        history = quota_history.advance(None, account(0, 22), stamp(0))
        history = quota_history.advance(history, account(0.5, 23), stamp(0.5))
        self.assertEqual(points(history), [[epoch(0.5), 23]])
        history = quota_history.advance(history, account(1.25, 25), stamp(1.25))
        self.assertEqual(points(history), [[epoch(0.5), 23], [epoch(1.25), 25]])

    def test_a_refresh_that_read_no_quota_changes_nothing(self):
        history = quota_history.advance(None, account(0, 22), stamp(0))
        # Thirty seconds later the quota is not due, and an hour later it is failing: the account
        # section still carries the reading from 12:00.
        for later in (30 / 3600, 1.5):
            self.assertEqual(quota_history.advance(history, account(0, 22), stamp(later)), history)

    def test_an_hour_with_no_reading_has_no_point(self):
        history = quota_history.advance(None, account(0, 22), stamp(0))
        history = quota_history.advance(history, account(4, 30), stamp(4))
        self.assertEqual(points(history), [[epoch(0), 22], [epoch(4), 30]])

    def test_points_over_seven_days_old_are_dropped_and_a_window_holds_a_week_of_hours(self):
        history = None
        for hour in range(200):
            history = quota_history.advance(history, account(hour, hour % 100), stamp(hour))
        kept = points(history)
        self.assertEqual(len(kept), quota_history.MAX_POINTS)
        self.assertEqual(kept[0][0], epoch(199 - 167))
        self.assertEqual(kept[-1], [epoch(199), 99])
        # With no new reading the last point ages out too, and an empty series is not stored.
        self.assertEqual(quota_history.advance(history, account(199, 99), stamp(199 + 168.5)), [])

    def test_a_window_missing_from_a_reading_keeps_its_points(self):
        history = quota_history.advance(None, account(0, 22), stamp(0))
        history = quota_history.advance(history, account(1, 5, name="secondary", minutes=300), stamp(1))
        self.assertEqual(points(history, "primary"), [[epoch(0), 22]])
        self.assertEqual(points(history, "secondary"), [[epoch(1), 5]])

    def test_a_reset_is_not_the_collectors_business_and_what_it_stores_it_reads_back(self):
        history = quota_history.advance(None, account(0, 97.5), stamp(0))
        history = quota_history.advance(history, account(1, 1, reset=RESET + WEEK), stamp(1))
        self.assertEqual(quota_history.project(json.loads(json.dumps(history))),
                         {("codex", "primary"): [[epoch(0), 97.5], [epoch(1), 1]]})


class Project(unittest.TestCase):
    def series(self, **changes):
        return {"limit_id": "codex", "name": "primary", "points": [[epoch(0), 22], [epoch(1), 23]], **changes}

    def test_anything_the_collector_did_not_write_is_refused(self):
        bad = [
            {"codex": []},
            [self.series(), self.series()],
            [self.series(limit_id="co dex")],
            [self.series(name="tertiary")],
            [self.series(extra=1)],
            [self.series(points=[[epoch(0), 101]])],
            [self.series(points=[[epoch(0), True]])],
            [self.series(points=[[epoch(0), float("nan")]])],
            [self.series(points=[[epoch(0), 22, RESET]])],
            [self.series(points=[["2026-09-30T12:00:00Z", 22]])],
            [self.series(points=[[10 ** 15, 22]])],
            [self.series(points=[[0, 22]])],
            # Two readings in one clock hour, and readings newest first.
            [self.series(points=[[epoch(0), 22], [epoch(0.5), 23]])],
            [self.series(points=[[epoch(1), 23], [epoch(0), 22]])],
            [self.series(points=[[epoch(hour), 1] for hour in range(quota_history.MAX_POINTS + 1)])],
            [self.series(limit_id="limit-%d" % index) for index in range(quota_history.MAX_SERIES + 1)],
        ]
        for value in bad:
            with self.subTest(value=str(value)[:80]):
                with self.assertRaises(ValueError):
                    quota_history.project(value)

    def test_the_page_gets_the_points_read_since_the_window_began(self):
        # The window before this one ran out at 12:00; this one began then and was last read at 14:00.
        kept = quota_history.project([self.series(points=[
            [epoch(-2), 96], [epoch(-1), 99], [epoch(0.1), 1], [epoch(1), 3], [epoch(2), 4]])])
        window = {"name": "primary", "duration_minutes": 10080, "resets_at": epoch(168)}
        self.assertEqual(quota_history.window_history(kept, "codex", window, stamp(2)), [
            {"at": stamp(0.1), "used_percent": 1}, {"at": stamp(1), "used_percent": 3}, {"at": stamp(2), "used_percent": 4}])
        self.assertEqual(quota_history.window_history(kept, "codex", dict(window, name="secondary"), stamp(2)), [])
        self.assertEqual(quota_history.window_history(kept, "other", window, stamp(2)), [])

    def test_the_reading_that_reported_the_window_belongs_to_it_even_a_moment_before_its_start(self):
        # An idle window can be reported as starting now. The collector's stamp is from just before
        # it asked, so that one reading sits before the start; the readings before it do not belong.
        kept = quota_history.project([self.series(points=[[epoch(-1), 0], [epoch(0), 0]])])
        window = {"name": "primary", "duration_minutes": 300, "resets_at": epoch(5) + 4}
        self.assertEqual(quota_history.window_history(kept, "codex", window, stamp(0)),
                         [{"at": stamp(0), "used_percent": 0}])
        self.assertEqual(quota_history.window_history(kept, "codex", window, None), [])


class Collected(unittest.TestCase):
    """The collector keeps the history in its snapshot, and the dashboard reads it from the published file."""

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "telemetry.json"
        self.config = Config("example-ci", ("example-ci/repo",), {}, self.path)

    def refresh(self, hours, used, reset=RESET, quota_ok=True):
        quota = {"status": "ok", "rate_limits": limits(used, reset)} if quota_ok else {"status": "quota_unavailable"}
        COLLECTOR.refresh(config(), self.path, lambda *_: {"host": remote_host(), "quota": quota}, stamp(hours))

    def saved(self):
        return json.loads(self.path.read_text())

    def window(self, hours):
        view = read_telemetry(self.config, START + timedelta(hours=hours))
        return view["usage"]["accounts"][0]["quota_windows"][0]

    def test_the_snapshot_gains_one_point_an_hour_from_the_quota_readings(self):
        self.refresh(0, 22)
        self.refresh(0.1, 23)
        self.refresh(1, 25)
        self.assertEqual(self.saved()["quota_history"], [
            {"limit_id": "codex", "name": "primary", "points": [[epoch(0.1), 23], [epoch(1), 25]]}])
        window = self.window(1)
        self.assertEqual(window["history"], [{"at": stamp(0.1), "used_percent": 23}, {"at": stamp(1), "used_percent": 25}])
        # The newest point is the reading the bar shows.
        self.assertEqual(window["history"][-1]["used_percent"], window["used_percent"])

    def test_a_snapshot_written_before_the_history_keeps_working(self):
        self.refresh(0, 22)
        old = self.saved()
        del old["quota_history"]
        self.path.write_text(json.dumps(old))
        # The dashboard still reads it, with no history to draw yet.
        self.assertEqual(self.window(0)["history"], [])
        # The collector carries its samples forward and starts the history with its next reading.
        self.refresh(0.1, 23)
        self.assertEqual(len(self.saved()["samples"]), 2)
        self.assertEqual(points(self.saved()["quota_history"]), [[epoch(0.1), 23]])

    def test_a_failed_quota_read_adds_no_point_and_keeps_the_history(self):
        self.refresh(0, 22)
        self.refresh(2, 0, quota_ok=False)
        self.assertEqual(points(self.saved()["quota_history"]), [[epoch(0), 22]])
        self.assertFalse(self.saved()["accounts"][0]["available"])
        self.assertEqual(self.window(2)["history"], [{"at": stamp(0), "used_percent": 22}])

    def test_a_window_that_reset_starts_a_new_line_and_the_old_points_stay_in_the_file(self):
        self.refresh(0, 96)
        self.refresh(1, 99)
        # The plan reset at 13:30: the next reading reports a window that began then.
        self.refresh(2, 1, reset=epoch(1.5) + WEEK)
        self.refresh(3, 4, reset=epoch(1.5) + WEEK)
        self.assertEqual(len(points(self.saved()["quota_history"])), 4)
        self.assertEqual(self.window(3)["history"], [{"at": stamp(2), "used_percent": 1}, {"at": stamp(3), "used_percent": 4}])

    def test_a_history_the_dashboard_cannot_read_makes_the_file_unavailable(self):
        self.refresh(0, 22)
        value = self.saved()
        value["quota_history"][0]["points"][0][1] = 250
        self.path.write_text(json.dumps(value))
        self.assertFalse(read_telemetry(self.config, START)["available"])

    def test_the_largest_history_stays_far_under_the_telemetry_file_limit(self):
        full = [{"limit_id": "limit-%02d" % (index // 2), "name": ("primary", "secondary")[index % 2],
                 "points": [[epoch(hour), 100] for hour in range(quota_history.MAX_POINTS)]}
                for index in range(quota_history.MAX_SERIES)]
        quota_history.project(full)
        self.assertLess(len(json.dumps(full[:1], separators=(",", ":"))), 3 * 1024)
        self.assertLess(len(json.dumps(full, separators=(",", ":"))), MAX_BYTES // 10)


if __name__ == "__main__":
    unittest.main()
