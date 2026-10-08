"""Token history across a restart, a failed scan and lost access."""

import copy
import threading
import unittest
from datetime import timedelta
from unittest.mock import patch

from dashboard.config import AgentDefinition, Config
from dashboard.gh_api import ApiError
from dashboard.live_service import LiveService
from dashboard.service import DashboardService
from dashboard.usage_artifacts import UsageArtifacts
from test_dashboard_restart import BOTS, NOW, OTHER, REPO, Collector, RestartCase, config, reading


class Listings:
    """An artifact API with nothing to list, which refuses one repository when told to."""

    def __init__(self):
        self.refused, self.listed = None, []

    def begin(self):
        pass

    def one(self, endpoint):
        repository = "/".join(endpoint.split("/")[1:3])
        self.listed.append(repository)
        if repository == self.refused:
            raise ApiError("not_found")
        return {"artifacts": []}


USAGE = {"available": True, "sampled_at": NOW.isoformat(), "stale": False, "accounts": [{"id": "openai:plan", "label": "plan", "provider": "openai", "quota_windows": []}],
         "samples": [{"account": "openai:plan", "bot": "reviewer", "timestamp": NOW.isoformat(), "input_tokens": 900, "output_tokens": 100, "partial": False}],
         "gaps": [], "hard_partial": False, "listing_complete": True, "covered_until": None, "partial": False, "completeness": "Observed."}


class UsageAcrossARestart(RestartCase):
    service_class = LiveService

    def live(self, *values, settings=None):
        """A live service whose token history is read only when a test says so, and whose pass,
        started in the background by its own snapshot, is held until the test ends."""
        service, collector = self.start(*values, settings=settings)
        service._usage_next = float("inf")
        collector.hold = threading.Event()
        self.addCleanup(self.read_github, service, collector)
        return service, collector

    @staticmethod
    def read_github(service, collector):
        """One whole pass, finished before this returns. A live service's own snapshot never waits."""
        collector.hold.set()
        return DashboardService.snapshot(service, force=bool(collector.values))

    def read_usage(self, service, outcome):
        service._usage_running = True
        with patch.object(service.usage_reader, "collect", side_effect=[outcome]):
            service._refresh_usage()

    def test_token_history_and_its_records_are_served_right_after_a_restart(self):
        first, collector = self.live(reading())
        self.read_github(first, collector)
        first.usage_reader.cache = {(REPO, 9): {"gap_at": NOW.isoformat()}}
        self.read_usage(first, copy.deepcopy(USAGE))
        self.assertEqual(self.files(), ["reading.json.z", "usage-responses.json.z", "usage.json.z"])
        self.wall = NOW + timedelta(minutes=3)
        service, _ = self.live(reading(self.wall))
        self.assertTrue(service._kept)
        self.assertEqual(service.usage_reader.cache, {(REPO, 9): {"gap_at": NOW.isoformat()}})
        usage = service.snapshot()["telemetry"]["usage"]
        self.assertEqual((usage["samples"][0]["input_tokens"], usage["sampled_at"], usage["stale"]), (900, NOW.isoformat(), False))
        self.wall = NOW + timedelta(minutes=11)
        self.assertTrue(service.snapshot()["telemetry"]["usage"]["stale"])

    def test_a_read_that_fails_without_losing_access_keeps_the_last_token_history(self):
        service, _ = self.live(reading())
        self.read_usage(service, copy.deepcopy(USAGE))
        reader = service.usage_reader
        for failure in (ApiError("rate_limited"), ApiError("unavailable"), ApiError("teapot"), OSError("timed out"), ValueError("bad")):
            with self.subTest(failure=repr(failure)):
                self.read_usage(service, failure)
                self.assertEqual(service._usage_result["samples"][0]["input_tokens"], 900)
                self.assertIs(service.usage_reader, reader)
                self.assertFalse(service._usage_running)
                self.assertIn("usage.json.z", self.files())

    def test_lost_access_on_the_usage_source_deletes_the_token_history_and_nothing_else(self):
        service, collector = self.live(reading())
        self.read_github(service, collector)
        reader = service.usage_reader
        self.read_usage(service, copy.deepcopy(USAGE))
        self.assertEqual(self.files(), ["reading.json.z", "usage-responses.json.z", "usage.json.z"])
        self.read_usage(service, ApiError("forbidden"))
        self.assertIsNone(service._usage_result)
        self.assertIsNot(service.usage_reader, reader)
        self.assertEqual(self.files(), ["reading.json.z"])

    def test_a_scan_that_meets_lost_access_on_a_listing_keeps_nothing_on_disk(self):
        service, _ = self.live(reading())
        self.read_usage(service, copy.deepcopy(USAGE))
        self.assertEqual(self.files(), ["usage-responses.json.z", "usage.json.z"])
        emptied = {**copy.deepcopy(USAGE), "accounts": [], "samples": [], "partial": True}

        def scan():
            service.usage_reader.lost_access = True
            return emptied

        service._usage_running = True
        with patch.object(service.usage_reader, "collect", side_effect=scan):
            service._refresh_usage()
        self.assertEqual((self.files(), service._usage_result["samples"], service._usage_result["partial"]), ([], [], True))

    def test_lost_access_seen_by_a_pass_deletes_the_token_history_and_stops_a_read_in_flight(self):
        service, collector = self.live(reading(), ApiError("forbidden"))
        self.read_github(service, collector)
        self.read_usage(service, copy.deepcopy(USAGE))
        reader, generation = service.usage_reader, service._usage_generation
        self.read_github(service, collector)
        self.assertEqual((self.files(), service._usage_result, collector.cleared), ([], None, 1))
        self.assertIsNot(service.usage_reader, reader)
        service._usage_running = True
        service._finish_usage(reader, generation, copy.deepcopy(USAGE), False)
        self.assertEqual((self.files(), service._usage_result, service._usage_running), ([], None, False))

    def test_a_kept_record_this_code_cannot_read_is_forgotten_once(self):
        first, collector = self.live(reading())
        self.read_github(first, collector)
        self.read_usage(first, copy.deepcopy(USAGE))
        service, collector = self.live(reading())
        self.read_usage(service, KeyError("a record of another shape"))
        self.assertEqual((self.files(), service._usage_result, service._kept, collector.cleared), ([], None, False, 0))
        self.assertFalse(service._usage_running)
        with self.assertRaises(KeyError):
            self.read_usage(service, KeyError("a defect, with nothing kept to blame"))
        self.assertFalse(service._usage_running)
        self.read_github(service, collector)
        self.assertEqual(collector.cleared, 1)

    def test_token_history_is_not_read_before_the_owner_wide_repository_list_exists(self):
        everything = Config("octocat", (), BOTS, None, all_repositories=True)
        service = LiveService(everything, collector=Collector(), monotonic=lambda: 1000.0, wall_clock=lambda: NOW)
        github = {"errors": [], "repositories": [], "coverage": {"selected": None}}
        with patch("dashboard.live_service.DashboardService.snapshot", return_value={"github": github, "telemetry": {"available": False}}), \
                patch("dashboard.live_service.threading.Thread") as thread:
            service.snapshot()
            self.assertEqual((thread.call_count, service._usage_next, service._usage_running), (0, 0, False))
            service._github = {"coverage": {"selected": 1}, "repositories": [{"repository": REPO}]}
            service.snapshot()
            self.assertEqual((thread.call_count, service._usage_running), (1, True))

    def test_a_bot_read_before_the_restart_carries_its_age_onto_the_strip(self):
        roster = config(agents=(AgentDefinition("swe", "SWE", "swe", "reviewer"),))
        first, collector = self.live(reading(), settings=roster)
        self.read_github(first, collector)
        self.wall = NOW + timedelta(seconds=40)
        service, _ = self.live(reading(self.wall), settings=roster)
        row = service.snapshot()["agents"]["rows"][0]
        self.assertEqual((row["state"], row["stale"], row["sampled_at"], len(row["recent_2h"])), ("idle", True, NOW.isoformat(), 1))
        fresh = self.read_github(service, service.collector)
        self.assertFalse(fresh["github"]["bots"]["roles"]["reviewer"].get("stale"))

    def test_a_real_scan_that_loses_access_to_one_repository_leaves_no_usage_file(self):
        service, _ = self.live(reading())
        api, ticks = Listings(), [0]
        service.usage_reader = UsageArtifacts(config(), api=api, downloader=None, clock=lambda: ticks[0])
        service._usage_running = True
        service._refresh_usage()
        self.assertEqual((self.files(), service._usage_result["partial"]), (["usage-responses.json.z", "usage.json.z"], False))
        api.refused, ticks[0] = OTHER, 301
        service._usage_running = True
        with patch.object(service.store, "save") as written:
            service._refresh_usage()
        written.assert_not_called()
        self.assertEqual((self.files(), service._usage_result["partial"], service._usage_result["samples"]), ([], True, []))
        self.assertEqual(api.listed[-2:], [REPO, OTHER])

    def test_the_usage_files_are_written_outside_the_lock_every_request_takes(self):
        service, _ = self.live(reading())
        held, real = [], service.store.save

        def save(name, document):
            held.append(service._usage_lock.locked())
            return real(name, document)

        with patch.object(service.store, "save", side_effect=save):
            self.read_usage(service, copy.deepcopy(USAGE))
        self.assertEqual((held, self.files()), ([False, False], ["usage-responses.json.z", "usage.json.z"]))

    def test_lost_access_noticed_while_the_usage_files_are_written_still_leaves_none(self):
        service, _ = self.live(reading())
        real = service.store.save

        def save(name, document):
            real(name, document)
            if name == "usage-responses":
                service._revoke()
                real("usage", document)  # as a write that lands after the pass cleared the directory

        with patch.object(service.store, "save", side_effect=save):
            self.read_usage(service, copy.deepcopy(USAGE))
        self.assertEqual((self.files(), service._usage_result), ([], None))

    def test_a_token_refused_to_the_head_beat_also_drops_the_token_history(self):
        service, collector = self.live(reading())
        self.read_github(service, collector)
        self.read_usage(service, copy.deepcopy(USAGE))
        reader = service.usage_reader
        self.mono += 1000
        collector.beat = ApiError("authentication_failed")
        service.run_head_beat()
        self.assertEqual((self.files(), service._usage_result, collector.cleared), ([], None, 1))
        self.assertIsNot(service.usage_reader, reader)


if __name__ == "__main__":
    unittest.main()
