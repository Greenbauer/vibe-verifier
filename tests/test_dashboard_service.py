"""Dashboard cache freshness, invalidation, and background refresh behavior."""

import copy
import threading
import time
import unittest
from unittest.mock import patch
from datetime import datetime, timedelta, timezone

from dashboard.config import BotDefinition, Config
from dashboard.gh_api import ApiError
from dashboard.service import ACTIVE_TTL_SECONDS, CURRENT_SECONDS, DashboardService


NOW = datetime(2026, 9, 30, 15, 0, tzinfo=timezone.utc)
REPO = "octocat/example"


def config():
    bots = {role: BotDefinition(role + ".yml", (role,))
            for role in ("reviewer", "explorer", "verifier")}
    return Config("octocat", (REPO,), bots, None)


def completed(minutes: int, category: str = "success") -> dict:
    return {"bot": "reviewer", "repository": REPO, "run_id": minutes, "job_id": minutes,
            "status": "completed", "conclusion": category, "category": category,
            "started_at": (NOW - timedelta(minutes=minutes + 1)).isoformat(),
            "completed_at": (NOW - timedelta(minutes=minutes)).isoformat()}


def source_sample(at: datetime) -> dict:
    run = completed(119)
    role = {"state": "working", "state_source": "github_actions", "sampled_at": at.isoformat(),
            "history_sampled_at": at.isoformat(), "active": [{**run, "status": "in_progress",
                                                                 "completed_at": None}],
            "recent_2h": [run], "recent_7d": [run], "latest_failure": None,
            "coverage": {"active": "complete", "history": "complete"}}
    return {"owner": "octocat", "sampled_at": at.isoformat(),
            "repositories": [{"repository": REPO, "subscription": "subscribed",
                              "pulls": [{"number": 1}], "errors": []}],
            "coverage": {"selected": 1, "inventory": {REPO: {"subscription": "subscribed"}}},
            "bots": {"partial": False, "roles": {"reviewer": role}, "errors": []},
            "errors": [], "partial": False, "api": {"calls": 8, "max_calls": 200}}


class FakeCollector:
    class API:
        max_calls = 200

    api = API()

    def __init__(self, values):
        self.values = iter(values)
        self.inventories = []
        self.cleared = 0

    def collect(self, inventory=None):
        self.inventories.append(copy.deepcopy(inventory))
        value = next(self.values)
        if isinstance(value, Exception):
            raise value
        return copy.deepcopy(value)

    def clear_private_cache(self):
        self.cleared += 1


class MutableClock:
    def __init__(self, value):
        self.value = value

    def __call__(self):
        return self.value


class CacheFreshness(unittest.TestCase):
    def test_repository_observation_timestamp_survives_later_bot_history_reads(self):
        value = source_sample(NOW)
        value["repositories"][0]["sampled_at"] = (NOW + timedelta(seconds=90)).isoformat()
        service = DashboardService(config(), FakeCollector([value]), monotonic=lambda: 0,
                                   wall_clock=lambda: NOW + timedelta(seconds=200))
        row = service.snapshot(force=True)["github"]["repositories"][0]
        self.assertFalse(row.get("unavailable", False))
        self.assertEqual(row["sampled_at"], (NOW + timedelta(seconds=90)).isoformat())

    def test_inventory_timestamp_advances_only_when_inventory_is_refetched(self):
        mono = MutableClock(0)
        collector = FakeCollector([source_sample(NOW), source_sample(NOW), source_sample(NOW)])
        service = DashboardService(config(), collector, monotonic=mono, wall_clock=lambda: NOW)
        service.snapshot(force=True)
        mono.value = 100
        service.snapshot(force=True)
        mono.value = 301
        service.snapshot(force=True)
        self.assertEqual(collector.inventories,
                         [None, {REPO: {"subscription": "subscribed"}}, None])

    def test_auth_failure_clears_cached_identities_history_and_completed_job_cache(self):
        collector = FakeCollector([source_sample(NOW), ApiError("authentication_failed")])
        service = DashboardService(config(), collector, monotonic=lambda: 10, wall_clock=lambda: NOW)
        service.snapshot(force=True)
        github = service.snapshot(force=True)["github"]
        self.assertEqual(github["repositories"][0]["pulls"], [])
        self.assertIsNone(github["repositories"][0]["sampled_at"])
        self.assertEqual(github["bots"]["roles"]["reviewer"]["recent_7d"], [])
        self.assertEqual(github["bots"]["roles"]["reviewer"]["state"], "unknown")
        self.assertEqual(collector.cleared, 1)

    def test_a_late_sample_keeps_the_last_rows_and_marks_them_stale(self):
        wall = MutableClock(NOW)
        mono = MutableClock(0)
        collector = FakeCollector([source_sample(NOW), ApiError("unavailable")])
        service = DashboardService(config(), collector, monotonic=mono, wall_clock=wall)
        service.snapshot(force=True)
        wall.value = NOW + timedelta(seconds=30)
        stale = service.snapshot(force=True)["github"]
        self.assertEqual(stale["repositories"][0]["pulls"], [{"number": 1}])
        self.assertTrue(stale["repositories"][0]["stale"])
        wall.value = NOW + timedelta(seconds=181)
        kept = service.snapshot()["github"]
        self.assertEqual(kept["repositories"][0]["pulls"], [{"number": 1}])
        self.assertTrue(kept["repositories"][0]["stale"])
        self.assertTrue(kept["stale"])
        self.assertEqual(kept["coverage"]["inventory"], {REPO: {"subscription": "subscribed"}})
        self.assertEqual(len(kept["bots"]["roles"]["reviewer"]["recent_7d"]), 1)
        self.assertEqual(kept["bots"]["roles"]["reviewer"]["state"], "working")

    def test_a_late_bot_reading_is_stale_and_names_its_latest_failure(self):
        value = source_sample(NOW)
        value["bots"]["roles"]["reviewer"]["recent_7d"] = [completed(30, "failed"), completed(119)]
        wall = MutableClock(NOW)
        service = DashboardService(config(), FakeCollector([value, ApiError("unavailable")]),
                                   monotonic=lambda: 0, wall_clock=wall)
        github = service.snapshot(force=True)["github"]
        reviewer = github["bots"]["roles"]["reviewer"]
        self.assertNotIn("stale", reviewer)
        self.assertFalse(github["bots"]["partial"])
        self.assertEqual(reviewer["latest_failure"]["run_id"], 30)
        wall.value = NOW + timedelta(seconds=181)
        github = service.snapshot(force=True)["github"]
        self.assertTrue(github["bots"]["roles"]["reviewer"]["stale"])
        self.assertTrue(github["bots"]["partial"])
        self.assertTrue(github["partial"])

    def test_history_windows_use_the_current_clock_on_cache_hits(self):
        wall = MutableClock(NOW)
        collector = FakeCollector([source_sample(NOW)])
        service = DashboardService(config(), collector, monotonic=lambda: 0, wall_clock=wall)
        first = service.snapshot(force=True)["github"]["bots"]["roles"]["reviewer"]
        self.assertEqual(len(first["recent_2h"]), 1)
        wall.value = NOW + timedelta(seconds=61)
        cached = service.snapshot()["github"]["bots"]["roles"]["reviewer"]
        self.assertEqual(cached["recent_2h"], [])
        self.assertEqual(len(cached["recent_7d"]), 1)


class BlockingCollector:
    class API:
        max_calls = 200

    api = API()

    def __init__(self):
        self.started = threading.Event()
        self.release = threading.Event()
        self.calls = 0

    def collect(self, inventory=None):
        self.calls += 1
        self.started.set()
        self.release.wait(2)
        return source_sample(NOW)


class EveryFetchIsCurrent(unittest.TestCase):
    def fetch(self, service, result):
        thread = threading.Thread(target=lambda: result.append(service.snapshot(nonblocking=True)))
        thread.start()
        return thread

    def test_fetches_with_no_current_reading_share_one_pass_and_wait_for_it(self):
        collector = BlockingCollector()
        service = DashboardService(config(), collector, monotonic=lambda: 0, wall_clock=lambda: NOW)
        results = []
        fetches = [self.fetch(service, results) for _ in range(5)]
        self.assertTrue(collector.started.wait(1))
        time.sleep(0.1)
        self.assertEqual(results, [])
        collector.release.set()
        for fetch in fetches:
            fetch.join(2)
        self.assertEqual(collector.calls, 1)
        self.assertEqual([row["github"]["repositories"][0]["pulls"] for row in results], [[{"number": 1}]] * 5)

    def test_a_current_reading_is_served_at_once_while_the_next_pass_runs(self):
        collector = BlockingCollector()
        collector.release.set()
        mono, wall = MutableClock(0), MutableClock(NOW)
        service = DashboardService(config(), collector, monotonic=mono, wall_clock=wall)
        service.snapshot()
        collector.release.clear()
        mono.value, wall.value = 61, NOW + timedelta(seconds=61)
        began = time.monotonic()
        served = service.snapshot(nonblocking=True)["github"]
        self.assertLess(time.monotonic() - began, 0.2)
        self.assertTrue(served["refreshing"])
        self.assertEqual(collector.calls, 2)
        collector.release.set()

    def test_a_reading_older_than_the_current_window_is_not_served_while_its_replacement_runs(self):
        collector = BlockingCollector()
        collector.release.set()
        mono, wall = MutableClock(0), MutableClock(NOW)
        service = DashboardService(config(), collector, monotonic=mono, wall_clock=wall)
        service.snapshot()
        collector.release.clear()
        mono.value, wall.value = 600, NOW + timedelta(seconds=CURRENT_SECONDS + 1)
        results = []
        fetch = self.fetch(service, results)
        fetch.join(0.3)
        self.assertEqual(results, [])
        collector.release.set()
        fetch.join(2)
        self.assertFalse(results[0]["github"]["refreshing"])

    def test_a_fetch_waits_a_bounded_time_then_is_served_the_last_reading(self):
        # A cold pass can take minutes; the deploy health check gives a restarted dashboard 10 seconds.
        collector = BlockingCollector()
        collector.release.set()
        mono, wall = MutableClock(0), MutableClock(NOW)
        service = DashboardService(config(), collector, monotonic=mono, wall_clock=wall)
        service.snapshot()
        collector.release.clear()
        mono.value, wall.value = 600, NOW + timedelta(seconds=CURRENT_SECONDS + 1)
        with patch("dashboard.service.FETCH_WAIT_SECONDS", 0.2):
            began = time.monotonic()
            github = service.snapshot(nonblocking=True)["github"]
        self.assertLess(time.monotonic() - began, 1)
        self.assertTrue(github["refreshing"])
        self.assertEqual(github["repositories"][0]["pulls"], [{"number": 1}])
        collector.release.set()

    def test_a_failed_pass_ends_the_wait_and_serves_the_last_reading(self):
        mono, wall = MutableClock(0), MutableClock(NOW)
        collector = FakeCollector([source_sample(NOW), ApiError("unavailable")])
        service = DashboardService(config(), collector, monotonic=mono, wall_clock=wall)
        service.snapshot()
        mono.value, wall.value = 600, NOW + timedelta(seconds=600)
        github = service.snapshot(nonblocking=True)["github"]
        deadline = time.monotonic() + 2
        while github["refreshing"] and time.monotonic() < deadline:
            github = service.snapshot(nonblocking=True)["github"]
        self.assertEqual((github["errors"], github["repositories"][0]["pulls"]), ([{"code": "unavailable"}], [{"number": 1}]))


class Reserve:
    def __init__(self, low):
        self.low = low

    def core_reserve_low(self):
        return self.low


class TheBackgroundPass(unittest.TestCase):
    def service(self, values, low=False):
        collector = FakeCollector(values)
        collector.api = Reserve(low)
        mono = MutableClock(0)
        return DashboardService(config(), collector, monotonic=mono, wall_clock=lambda: NOW), collector, mono

    def test_a_pass_runs_with_no_page_open_once_the_last_ended_a_minute_ago(self):
        service, collector, mono = self.service([source_sample(NOW), source_sample(NOW)])
        self.assertTrue(service.run_background_pass())
        mono.value = ACTIVE_TTL_SECONDS - 1
        self.assertFalse(service.run_background_pass())
        mono.value = ACTIVE_TTL_SECONDS
        self.assertTrue(service.run_background_pass())
        self.assertEqual(len(collector.inventories), 2)

    def test_no_background_pass_runs_while_the_shared_rest_budget_is_below_half(self):
        service, collector, _ = self.service([source_sample(NOW)], low=True)
        self.assertFalse(service.run_background_pass())
        self.assertEqual(collector.inventories, [])

    def test_the_beat_reads_heads_only_when_no_pass_is_due(self):
        service, _, _ = self.service([source_sample(NOW)])
        beats = []
        service.run_head_beat = lambda: beats.append(1)
        service.keep_current()
        service.keep_current()
        self.assertEqual(beats, [1])


if __name__ == "__main__":
    unittest.main()
