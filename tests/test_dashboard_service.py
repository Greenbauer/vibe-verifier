"""Dashboard cache freshness, invalidation, and background refresh behavior."""

import copy
import threading
import time
import unittest
from datetime import datetime, timedelta, timezone

from dashboard.config import BotDefinition, Config
from dashboard.gh_api import ApiError
from dashboard.service import DashboardService


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
            "coverage": {"selected": 1, "readable": 1, "label": "Selected repositories",
                         "inventory": {REPO: {"subscription": "subscribed"}}},
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
        self.assertEqual(kept["coverage"]["readable"], 1)
        self.assertEqual(kept["coverage"]["inventory"], {REPO: {"subscription": "subscribed"}})
        self.assertEqual(len(kept["bots"]["roles"]["reviewer"]["recent_7d"]), 1)
        self.assertEqual(kept["bots"]["roles"]["reviewer"]["state"], "working")

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


class NonblockingRefresh(unittest.TestCase):
    def test_concurrent_requests_share_one_background_refresh_and_return_loading_fast(self):
        collector = BlockingCollector()
        service = DashboardService(config(), collector, monotonic=lambda: 0, wall_clock=lambda: NOW)
        began = time.monotonic()
        first = service.snapshot(nonblocking=True)
        self.assertLess(time.monotonic() - began, 0.2)
        self.assertTrue(first["github"]["refreshing"])
        self.assertTrue(collector.started.wait(1))
        snapshots = [service.snapshot(nonblocking=True) for _ in range(5)]
        self.assertTrue(all(row["github"]["refreshing"] for row in snapshots))
        self.assertEqual(collector.calls, 1)
        collector.release.set()
        loaded = service.snapshot()
        self.assertEqual(collector.calls, 1)
        self.assertEqual(loaded["github"]["repositories"][0]["pulls"], [{"number": 1}])


if __name__ == "__main__":
    unittest.main()
