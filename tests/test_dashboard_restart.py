"""A restart, a failed read and lost access: the last reading stays with its age, or is deleted."""

import copy
import io
import tempfile
import threading
import unittest
from contextlib import redirect_stderr
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from dashboard.config import BotDefinition, Config
from dashboard.gh_api import ApiError
from dashboard.server import make_server
from dashboard.service import DashboardService
from dashboard.state_store import StateStore

NOW = datetime(2026, 10, 8, 15, 0, tzinfo=timezone.utc)
ROOT = Path(__file__).resolve().parent.parent
REPO, OTHER = "octocat/example", "octocat/second"
BOTS = {role: BotDefinition(role + ".yml", (role,)) for role in ("reviewer", "explorer", "verifier")}


def config(**changes):
    return Config("octocat", (REPO, OTHER), BOTS, None, **changes)


def reading(at=NOW, repositories=(REPO, OTHER)):
    run = {"bot": "reviewer", "repository": REPO, "run_id": 1, "job_id": 1, "status": "completed",
           "conclusion": "success", "category": "success", "completed_at": (at - timedelta(minutes=5)).isoformat()}
    role = {"state": "idle", "state_source": "github_actions", "sampled_at": at.isoformat(),
            "history_sampled_at": at.isoformat(), "active": [], "recent_2h": [run], "recent_7d": [run],
            "latest_failure": None, "coverage": {"active": "complete", "history": "complete"}}
    rows = [{"repository": name, "subscription": "subscribed", "sampled_at": at.isoformat(), "errors": [],
             "pulls": [{"number": index + 1, "title": "Change %d" % (index + 1), "merge_ready": True,
                        "checks_sampled_at": at.isoformat()}]}
            for index, name in enumerate(repositories)]
    return {"owner": "octocat", "sampled_at": at.isoformat(), "repositories": rows,
            "coverage": {"selected": 2, "readable": len(rows), "label": "Selected repositories", "inventory": {}},
            "bots": {"partial": False, "roles": {"reviewer": role}, "errors": []},
            "errors": [], "partial": False, "api": {"calls": 55, "max_calls": 200}}


class Collector:
    class API:
        max_calls = 200

    api = API()

    def __init__(self, *values):
        self.values = list(values)
        self.cleared = 0
        self.hold = None
        # What the head beat meets: an error to raise, and an event that holds it in flight.
        self.beat, self.beat_hold, self.beat_started = None, None, threading.Event()

    def collect(self, inventory=None):
        if self.hold:
            self.hold.wait(2)
        value = self.values.pop(0)
        if isinstance(value, Exception):
            raise value
        return copy.deepcopy(value)

    def clear_private_cache(self):
        self.cleared += 1

    def read_open_heads(self, github):
        self.beat_started.set()
        if self.beat_hold:
            self.beat_hold.wait(2)
        if self.beat:
            raise self.beat
        return {"heads": {}, "calls": 1, "points": 1, "complete": False}


class RestartCase(unittest.TestCase):
    service_class = DashboardService

    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.wall, self.mono = NOW, 1000.0

    def store(self):
        return StateStore(self.folder.name, config(), clock=lambda: self.wall)

    def files(self):
        return sorted(path.name for path in (Path(self.folder.name) / "octocat").iterdir())

    def start(self, *values, settings=None):
        collector = Collector(*values)
        service = self.service_class(settings or config(), collector=collector, monotonic=lambda: self.mono,
                                     wall_clock=lambda: self.wall, store=self.store())
        return service, collector

    def restarted(self, *values):
        """A service that read once and was stopped, then the one started in its place."""
        first, _ = self.start(reading())
        first.snapshot(force=True)
        self.wall = NOW + timedelta(seconds=40)
        return self.start(*values)


class ServesTheLastReading(RestartCase):
    def test_a_restart_serves_the_previous_reading_at_once_marked_old(self):
        service, collector = self.restarted(reading(NOW + timedelta(seconds=40)))
        collector.hold = threading.Event()
        github = service.snapshot(nonblocking=True)["github"]
        self.assertTrue(github["restored"] and github["stale"] and github["refreshing"])
        self.assertEqual(github["sampled_at"], NOW.isoformat())
        self.assertEqual([row["pulls"][0]["title"] for row in github["repositories"]], ["Change 1", "Change 2"])
        self.assertEqual([row["stale"] for row in github["repositories"]], [True, True])
        self.assertEqual([row["pulls"][0]["merge_ready"] for row in github["repositories"]], [False, False])
        role = github["bots"]["roles"]["reviewer"]
        self.assertTrue(role["stale"])
        self.assertEqual((role["state"], role["sampled_at"], len(role["recent_2h"])), ("idle", NOW.isoformat(), 1))
        collector.hold.set()
        fresh = service.snapshot()["github"]
        self.assertNotIn("restored", fresh)
        self.assertFalse(fresh["stale"])
        self.assertEqual([row["stale"] for row in fresh["repositories"]], [False, False])
        self.assertTrue(fresh["repositories"][0]["pulls"][0]["merge_ready"])

    def test_a_first_pass_that_fails_after_a_restart_keeps_the_previous_reading(self):
        for code in ("rate_limited", "unavailable", "request_budget_exhausted", "invalid_response"):
            with self.subTest(code=code):
                service, _ = self.restarted(ApiError(code))
                github = service.snapshot(force=True)["github"]
                self.assertTrue(github["restored"] and github["stale"])
                self.assertEqual(github["errors"], [{"code": code}])
                self.assertEqual([len(row["pulls"]) for row in github["repositories"]], [1, 1])
                self.assertEqual(self.files(), ["reading.json.z"])

    def test_a_repository_the_first_pass_misses_keeps_its_previous_row(self):
        partial = reading(NOW + timedelta(seconds=40), repositories=(REPO,))
        partial["errors"] = [{"repository": OTHER, "code": "rate_limited"}]
        service, _ = self.restarted(partial)
        rows = service.snapshot(force=True)["github"]["repositories"]
        self.assertEqual([(row["repository"], row["stale"], len(row["pulls"])) for row in rows],
                         [(REPO, False, 1), (OTHER, True, 1)])
        self.assertEqual(rows[1]["source_error"], "rate_limited")
        self.assertFalse(rows[1]["pulls"][0]["merge_ready"])
        self.assertEqual([row.get("restored") for row in rows], [None, True])

    def test_a_first_start_shows_loading_and_then_keeps_what_it_read(self):
        service, collector = self.start(reading())
        collector.hold = threading.Event()
        github = service.snapshot(nonblocking=True)["github"]
        self.assertEqual((github["sampled_at"], github["errors"]), (None, [{"code": "loading"}]))
        self.assertNotIn("restored", github)
        collector.hold.set()
        service.snapshot()
        self.assertEqual(self.files(), ["reading.json.z"])

    def test_a_failed_pass_does_not_replace_the_kept_reading(self):
        service, _ = self.start(reading(), ApiError("unavailable"))
        service.snapshot(force=True)
        self.assertTrue(service.snapshot(force=True)["github"]["stale"])
        kept = self.store().load("reading")
        self.assertEqual((kept["sampled_at"], kept.get("stale")), (NOW.isoformat(), None))
        self.assertFalse(kept["repositories"][0]["stale"])

    def test_a_reading_older_than_a_day_is_not_served(self):
        first, _ = self.start(reading())
        first.snapshot(force=True)
        self.wall = NOW + timedelta(hours=24, seconds=1)
        service, collector = self.start(reading(self.wall))
        collector.hold = threading.Event()
        self.assertEqual(service.snapshot(nonblocking=True)["github"]["repositories"][0]["pulls"], [])
        self.assertEqual(self.files(), [])
        collector.hold.set()
        self.assertEqual(len(service.snapshot()["github"]["repositories"][0]["pulls"]), 1)

    def test_another_selection_of_repositories_never_sees_the_kept_reading(self):
        first, _ = self.start(reading())
        first.snapshot(force=True)
        narrowed = Config("octocat", (REPO,), BOTS, None)
        service = DashboardService(narrowed, Collector(reading()), monotonic=lambda: 1000.0, wall_clock=lambda: NOW,
                                   store=StateStore(self.folder.name, narrowed, clock=lambda: NOW))
        self.assertIsNone(service._github)
        self.assertEqual(self.files(), [])


class OnlyLostAccessDeletes(RestartCase):
    def test_lost_access_deletes_the_kept_reading_and_the_next_start_is_empty(self):
        for code in ("authentication_failed", "forbidden", "not_found"):
            with self.subTest(code=code):
                service, collector = self.restarted(ApiError(code))
                self.assertEqual(self.files(), ["reading.json.z"])
                github = service.snapshot(force=True)["github"]
                self.assertEqual([row["pulls"] for row in github["repositories"]], [[], []])
                self.assertEqual((github["errors"], collector.cleared, self.files()), ([{"code": code}], 1, []))
                again, _ = self.start(ApiError("unavailable"))
                self.assertIsNone(again._github)

    def test_a_pass_that_meets_lost_access_on_one_source_keeps_nothing(self):
        revoked = reading(NOW + timedelta(seconds=40))
        revoked["repositories"][1]["errors"] = [{"pull": 2, "code": "forbidden"}]
        service, collector = self.restarted(revoked)
        service.snapshot(force=True)
        self.assertEqual((collector.cleared, self.files()), (1, []))

    def test_a_failure_this_code_does_not_name_keeps_the_last_reading(self):
        service, _ = self.start(reading(), ApiError("teapot"))
        service.snapshot(force=True)
        github = service.snapshot(force=True)["github"]
        self.assertEqual([len(row["pulls"]) for row in github["repositories"]], [1, 1])
        self.assertEqual(([row["stale"] for row in github["repositories"]], github["errors"]), ([True, True], [{"code": "teapot"}]))

    def test_a_repository_that_fails_in_a_way_this_code_does_not_name_keeps_its_row(self):
        partial = reading(repositories=(REPO,))
        partial["errors"] = [{"repository": OTHER, "code": "teapot"}]
        service, _ = self.start(reading(), partial)
        service.snapshot(force=True)
        kept = service.snapshot(force=True)["github"]["repositories"][1]
        self.assertEqual((kept["repository"], kept["stale"], kept["source_error"], len(kept["pulls"])), (OTHER, True, "teapot", 1))

    def test_a_repository_that_lost_access_does_not_keep_its_row(self):
        partial = reading(repositories=(REPO,))
        partial["errors"] = [{"repository": OTHER, "code": "not_found"}]
        service, _ = self.start(reading(), partial)
        service.snapshot(force=True)
        gone = service.snapshot(force=True)["github"]["repositories"][1]
        self.assertEqual((gone["pulls"], gone["unavailable"], gone["sampled_at"]), ([], True, None))


class ForgetsWhatItCannotUse(RestartCase):
    def unreadable(self, *values):
        """A start handed a reading this version cannot serve, with its first pass held in flight."""
        kept = reading()
        kept["bots"]["roles"]["reviewer"]["recent_2h"] = ["a row from another version"]
        self.store().save("reading", kept)
        service, collector = self.start(*values)
        collector.hold = threading.Event()
        self.assertTrue(service._github["restored"])
        return service, collector

    def loading(self, service):
        github = service.snapshot(nonblocking=True)["github"]
        self.assertEqual((github["errors"], github["repositories"][0]["pulls"]), ([{"code": "loading"}], []))
        self.assertEqual((self.files(), service._kept), ([], False))

    def test_a_kept_reading_this_code_cannot_serve_is_dropped_instead_of_failing_every_request(self):
        service, collector = self.unreadable(reading(), reading())
        self.loading(service)
        collector.hold.set()
        self.assertEqual(len(service.snapshot()["github"]["repositories"][0]["pulls"]), 1)
        self.assertEqual(collector.cleared, 0)
        service.snapshot(force=True)
        self.assertEqual(collector.cleared, 1)

    def test_a_pass_in_flight_does_not_bring_a_forgotten_reading_back(self):
        service, collector = self.unreadable(ApiError("rate_limited"), reading())
        self.loading(service)
        collector.hold.set()
        for _ in range(3):
            github = service.snapshot()["github"]
            self.assertNotIn("restored", github)
            self.assertEqual((github["errors"], [row["pulls"] for row in github["repositories"]]),
                             ([{"code": "unavailable"}], [[], []]))
        self.assertEqual(len(service.snapshot(force=True)["github"]["repositories"][0]["pulls"]), 1)

    def test_a_beat_in_flight_does_not_bring_a_forgotten_reading_back(self):
        service, collector = self.unreadable(reading())
        collector.beat_hold = threading.Event()
        beat = threading.Thread(target=service.run_head_beat)
        beat.start()
        self.assertTrue(collector.beat_started.wait(2))
        self.loading(service)
        collector.beat_hold.set()
        beat.join(2)
        self.assertFalse(beat.is_alive())
        self.assertIsNone(service._github)
        collector.hold.set()
        self.assertEqual(len(service.snapshot()["github"]["repositories"][0]["pulls"]), 1)

    def test_a_row_carried_from_the_kept_reading_goes_when_kept_state_is_forgotten(self):
        partial = reading(NOW + timedelta(seconds=40), repositories=(REPO,))
        partial["errors"] = [{"repository": OTHER, "code": "rate_limited"}]
        service, _ = self.restarted(partial)
        service.snapshot(force=True)
        self.assertTrue(service._forget_kept())
        rows = service.snapshot()["github"]["repositories"]
        self.assertEqual([(row["repository"], len(row["pulls"]), row.get("unavailable", False)) for row in rows],
                         [(REPO, 1, False), (OTHER, 0, True)])
        self.assertFalse(service._forget_kept())

    def test_a_failure_with_nothing_kept_still_reaches_the_caller(self):
        service, _ = self.start(reading())
        with patch.object(DashboardService, "_expire", side_effect=KeyError("defect")), self.assertRaises(KeyError):
            service.snapshot(force=True)

    def test_a_pass_that_fails_unexpectedly_blames_what_was_kept_once(self):
        service, collector = self.restarted(KeyError("a cached row of another shape"), KeyError("again"), reading())
        github = service.snapshot(force=True)["github"]
        self.assertEqual((github["errors"], github["repositories"][0]["pulls"]), ([{"code": "unavailable"}], []))
        self.assertEqual((collector.cleared, self.files()), (0, []))
        service.snapshot(force=True)
        self.assertEqual(collector.cleared, 1)
        self.assertEqual(len(service.snapshot(force=True)["github"]["repositories"][0]["pulls"]), 1)
        self.assertEqual(collector.cleared, 1)

    def test_an_unexpected_failure_keeps_a_reading_this_process_made_even_after_a_restart(self):
        later = NOW + timedelta(seconds=40)
        service, collector = self.restarted(reading(later), KeyError("defect"), reading(later))
        service.snapshot(force=True)
        github = service.snapshot(force=True)["github"]
        self.assertEqual(([len(row["pulls"]) for row in github["repositories"]], github["errors"]), ([1, 1], [{"code": "unavailable"}]))
        self.assertEqual((github["sampled_at"], [row["stale"] for row in github["repositories"]]), (later.isoformat(), [True, True]))
        self.assertEqual((self.files(), collector.cleared), ([], 0))
        service.snapshot(force=True)
        self.assertEqual((self.files(), collector.cleared), (["reading.json.z"], 1))

    def test_an_unexpected_failure_with_nothing_kept_keeps_the_last_reading(self):
        service, collector = self.start(reading(), KeyError("defect"))
        service.snapshot(force=True)
        github = service.snapshot(force=True)["github"]
        self.assertEqual(([len(row["pulls"]) for row in github["repositories"]], collector.cleared), ([1, 1], 0))
        self.assertEqual(self.files(), ["reading.json.z"])


class TheHeadBeat(RestartCase):
    """With the page closed only the beat reads GitHub, so it is the beat that meets a refused token."""

    def quiet(self, *values):
        service, collector = self.start(reading(), *values)
        service.snapshot(force=True)
        self.assertEqual(self.files(), ["reading.json.z"])
        self.mono += 1000
        return service, collector

    def test_a_token_refused_to_the_beat_deletes_everything_read_with_it(self):
        for code in ("authentication_failed", "forbidden", "not_found"):
            with self.subTest(code=code):
                service, collector = self.quiet()
                collector.beat = ApiError(code)
                service.run_head_beat()
                github = service.snapshot(nonblocking=True)["github"]
                self.assertEqual((github["errors"], [row["pulls"] for row in github["repositories"]]), ([{"code": code}], [[], []]))
                self.assertEqual((self.files(), collector.cleared), ([], 1))

    def test_a_beat_that_fails_without_losing_access_deletes_nothing(self):
        for code in ("rate_limited", "unavailable", "invalid_response"):
            with self.subTest(code=code):
                service, collector = self.quiet()
                collector.beat = ApiError(code)
                self.assertIsNone(service.run_head_beat())
                self.assertEqual([len(row["pulls"]) for row in service._github["repositories"]], [1, 1])
                self.assertEqual((self.files(), collector.cleared), (["reading.json.z"], 0))


class Wiring(unittest.TestCase):
    def test_the_launcher_hands_the_state_directory_to_the_service(self):
        with tempfile.TemporaryDirectory() as folder:
            server = make_server(config(), 0, state_dir=folder)
            try:
                self.assertEqual(server.service.store.directory, Path(folder) / "octocat")
            finally:
                server.server_close()
            plain = make_server(config(), 0)
            try:
                self.assertIsNone(plain.service.store)
            finally:
                plain.server_close()
        launcher = (ROOT / "bin" / "vibe-dashboard").read_text()
        self.assertIn('parser.add_argument("--state-dir", default=os.environ.get("VIBE_DASHBOARD_STATE_DIR"),', launcher)
        self.assertIn("make_server(config, args.port, state_dir=args.state_dir)", launcher)

    def test_a_state_directory_that_cannot_be_used_still_starts_the_dashboard(self):
        with tempfile.NamedTemporaryFile() as blocker, redirect_stderr(io.StringIO()) as errors:
            server = make_server(config(), 0, state_dir=blocker.name)
            try:
                self.assertIsNone(server.service.store)
            finally:
                server.server_close()
        self.assertIn("keeping nothing across a restart", errors.getvalue())


if __name__ == "__main__":
    unittest.main()
