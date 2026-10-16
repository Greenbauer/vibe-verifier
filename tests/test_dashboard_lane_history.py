"""The lane history behind the Capacity view: queue, job waits, samples, bars and the dashboard's reader."""
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from dashboard import lane_history as LH
from dashboard.collector import refresh
from dashboard.collector_view import collector_view
from dashboard.config import Config
from dashboard.telemetry import read_telemetry
from test_dashboard_collector import config, quota, remote_host

NOW = datetime(2026, 10, 14, 12, 0, 30, tzinfo=timezone.utc)


def stamp(moment):
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def ago(seconds):
    return stamp(NOW - timedelta(seconds=seconds))


def job(runner, queued, routed, assigned, started, kind="ci", finished=None):
    row = {"kind": kind, "instance": 1, "runner_name": runner, "started_at": started}
    for key, value in (("queued_at", queued), ("scale_set_assigned_at", routed), ("runner_assigned_at", assigned),
                       ("finished_at", finished)):
        if value is not None:
            row[key] = value
    return row


def allocated(kind, index, runner, job_name=None):
    row = {"kind": kind, "index": index, "state": "allocated",
           "unit": {"active_state": "active", "sub_state": "running"}, "target_repository": "example-ci/repo",
           "set_id": 1, "runner_id": index, "runner_name": runner, "allocated_at": ago(600)}
    if job_name:
        row.update(job_repository="example-ci/repo", job_name=job_name)
    return row


def sample(seconds_ago, occupied, running, waiting=0):
    return {"observed_at": ago(seconds_ago), "host": {
        "cpu_busy_percent": 10, "memory_available_bytes": 1, "workspace_free_bytes": 1,
        "slots_occupied": occupied, "slots_remaining_on_demand": 8 - occupied,
        "slots_running": running, "jobs_waiting": waiting}}


class Queue(unittest.TestCase):
    def test_waiting_is_assigned_minus_running_per_set_and_warm_sums_the_pools(self):
        raw = {"version": 1, "sets": [{"kind": "ci", "assigned": 5, "running": 3, "min_runners": 1, "warm_instances": [3]},
                                      {"kind": "qae", "assigned": 1, "running": 2, "min_runners": 0}]}
        self.assertEqual(LH.project_queue(raw), {"waiting": 2, "warm": 1, "warm_slots": ["ci-3"]})

    def test_an_unreadable_queue_is_unknown_not_zero(self):
        for raw in (None, {"version": 2, "sets": []}, {"version": 1, "sets": [{"kind": "x", "assigned": 1, "running": 0}]},
                    {"version": 1, "sets": [{"kind": "ci", "assigned": -1, "running": 0}]},
                    {"version": 1, "sets": [{"kind": "ci", "assigned": True, "running": 0}]},
                    {"version": 1, "sets": [{"kind": "ci", "assigned": 1, "running": 0, "warm_instances": [0]}]},
                    {"version": 1, "sets": [{"kind": "ci", "assigned": 1, "running": 0, "warm_instances": "3"}]}):
            with self.subTest(raw=raw):
                self.assertIsNone(LH.project_queue(raw))


class Waits(unittest.TestCase):
    def test_a_job_waits_from_the_lane_to_a_runner_and_routing_is_githubs_part_before_it(self):
        row = LH.project_job(job("r-ci-1-1", ago(100), ago(90), ago(40), ago(39)))
        self.assertEqual(row, {"runner": "r-ci-1-1", "kind": "ci", "started_at": ago(40), "finished_at": None,
                               "wait_seconds": 50, "routing_seconds": 10})

    def test_the_shape_github_sends_a_warm_runner_job_is_a_zero_wait_without_routing(self):
        # Seen on a live lane, 2026-10-14: no queued time, the set and runner times equal to the job's
        # GitHub created_at, and the listener hearing JobStarted 19 s later, as the job finished.
        row = LH.project_job(job("r-ci-1-1", None, ago(19), ago(19), ago(0)))
        self.assertEqual(row, {"runner": "r-ci-1-1", "kind": "ci", "started_at": ago(19), "finished_at": None,
                               "wait_seconds": 0, "routing_seconds": None})

    def test_a_finished_job_keeps_githubs_finish_and_one_finishing_before_it_started_is_dropped(self):
        row = LH.project_job(job("r-ci-1-1", None, ago(40), ago(40), ago(39), finished=ago(23)))
        self.assertEqual(row["finished_at"], ago(23))
        self.assertIsNone(LH.project_job(job("r-ci-1-1", None, ago(40), ago(40), ago(39), finished=ago(41))))
        kept = LH.waits([dict(row, finished_at="soon")], None, NOW)
        self.assertEqual(kept, [])

    def test_a_job_without_both_ends_of_its_wait_is_not_a_wait(self):
        self.assertIsNone(LH.project_job(job("r-ci-1-1", ago(100), None, ago(40), ago(39))))
        self.assertIsNone(LH.project_job(job("r-ci-1-1", ago(100), ago(90), None, ago(39))))
        self.assertIsNone(LH.project_job(job("r-ci-1-1", ago(100), ago(10), ago(40), ago(39))))
        self.assertIsNone(LH.project_job(job("bad name", ago(100), ago(90), ago(40), ago(39))))

    def test_a_queued_time_after_the_set_time_drops_routing_and_keeps_the_wait(self):
        row = LH.project_job(job("r-ci-1-1", ago(80), ago(90), ago(40), ago(39)))
        self.assertEqual((row["wait_seconds"], row["routing_seconds"]), (50, None))

    def test_kept_and_new_waits_merge_one_per_runner_and_old_ones_go(self):
        kept = [{"runner": "old", "kind": "ci", "started_at": ago(8 * 86400), "wait_seconds": 5, "routing_seconds": 1},
                {"runner": "r1", "kind": "ci", "started_at": ago(500), "wait_seconds": 5, "routing_seconds": 1},
                {"runner": "broken", "kind": "ci", "started_at": "yesterday", "wait_seconds": 5}]
        raw = {"version": 1, "jobs": [job("r1", ago(600), ago(590), ago(560), ago(500)),
                                      job("r2", ago(300), ago(299), ago(290), ago(280))]}
        rows = LH.waits(kept, raw, NOW)
        self.assertEqual([row["runner"] for row in rows], ["r1", "r2"])
        self.assertEqual(rows[0]["wait_seconds"], 30)

    def test_a_foreign_jobs_file_version_adds_nothing(self):
        raw = {"version": 2, "jobs": [job("r2", ago(300), ago(299), ago(290), ago(280))]}
        self.assertEqual(LH.waits([], raw, NOW), [])

    def test_the_summary_is_nearest_rank_with_the_mean_of_each_part(self):
        rows = [{"wait_seconds": seconds, "routing_seconds": 2} for seconds in range(1, 21)]
        rows.append({"wait_seconds": 100, "routing_seconds": None})
        summary = LH.wait_summary(rows)
        self.assertEqual((summary["jobs"], summary["p50_seconds"], summary["p95_seconds"]), (21, 11, 20))
        self.assertEqual(summary["routing_seconds"], 2)
        self.assertNotIn("start_seconds", summary)
        self.assertIsNone(LH.wait_summary([]))


class Samples(unittest.TestCase):
    def test_running_counts_shared_slots_with_a_recorded_job_only(self):
        host = {"slots": {"occupied": [allocated("ci", 1, "a", "build"), allocated("ci", 2, "b"),
                                       allocated("wait", 1, "c", "deploy"), allocated("qae", 1, "d", "qa")]}}
        self.assertEqual(LH.running_count(host), 2)

    def test_a_refresh_adds_running_and_waiting_and_keeps_older_samples_without_them(self):
        old = {"observed_at": ago(60), "host": {"cpu_busy_percent": 1, "memory_available_bytes": 1,
               "workspace_free_bytes": 1, "slots_occupied": 1, "slots_remaining_on_demand": 7}}
        host = {"cpu": {"busy_percent": 5}, "memory": {"available_bytes": 2}, "workspace": {"free_bytes": 3},
                "slots": {"occupied_count": 2, "remaining_on_demand": 6,
                          "occupied": [allocated("ci", 1, "a", "build"), allocated("ci", 2, "b")]}}
        kept = LH.samples({"samples": [old, {"observed_at": ago(9 * 86400), "host": old["host"]}]},
                          host, True, stamp(NOW), {"waiting": 3, "warm": 0})
        self.assertEqual(len(kept), 2)
        self.assertNotIn("slots_running", kept[0]["host"])
        self.assertEqual((kept[1]["host"]["slots_running"], kept[1]["host"]["jobs_waiting"]), (1, 3))


class Ranges(unittest.TestCase):
    def test_each_range_has_its_bar_count_and_the_last_bar_holds_now(self):
        # The last bar is 12:00:00 to now (12:00:30). Its sample saw 3 runners up and no job recorded yet,
        # with a warm pool of 1. Job spans cover two of them: one job through the interval on a runner
        # still in a slot, one finished half way (its runner then exits). The third is the warm one.
        point = sample(30, 3, 0)
        point["host"]["warm"] = 1
        rows = [{"runner": "a", "kind": "ci", "started_at": ago(90), "finished_at": None, "wait_seconds": 0, "routing_seconds": None},
                {"runner": "b", "kind": "qae", "started_at": ago(120), "finished_at": ago(15), "wait_seconds": 0, "routing_seconds": None}]
        result = LH.history([point], rows, {"slots": {"limit": 8, "occupied": [{"runner_name": "a"}]}}, True, None, stamp(NOW))
        self.assertEqual([(row["name"], len(row["buckets"])) for row in result["ranges"]],
                         [("1h", 30), ("24h", 48), ("7d", 56)])
        last = result["ranges"][0]["buckets"][-1]
        self.assertEqual(last, {"start": "2026-10-14T12:00:00Z", "running": 1.5, "warm": 1, "reserved": 0, "waiting": 0})
        self.assertEqual(result["ranges"][0]["buckets"][0], {"start": "2026-10-14T11:02:00Z"})

    def test_a_job_shorter_than_a_sample_counts_as_busy_and_its_runner_is_not_also_idle(self):
        # Seen live on 2026-10-14: a 17 s job between 30 s samples, its runner's job recorded too late for
        # any sample to see it running. Two slots; one runner up through four samples.
        points = [sample(seconds, 1, 0) for seconds in (120, 90, 60, 30)]
        rows = [{"runner": "short", "kind": "ci", "started_at": ago(80), "finished_at": ago(63),
                 "wait_seconds": 0, "routing_seconds": None}]
        hour = LH.history(points, rows, {"slots": {"limit": 2}}, True, None, stamp(NOW))["ranges"][0]
        self.assertEqual(hour["busy_percent"], round(100 * 17 / (120 * 2), 1))
        # The bar holds three 30 s intervals of one runner up: idle, then idle 10 s until the job starts
        # and the job's 17 s (its last 3 s, the runner's cleanup, are neither), then idle.
        bar = hour["buckets"][-2]
        self.assertEqual((bar["running"], bar["warm"], bar["reserved"]), (round(17 / 90, 2), 0, round(70 / 90, 2)))
        self.assertLessEqual(bar["running"] + bar["warm"] + bar["reserved"], 1.0)

    def test_a_job_a_sample_saw_counts_only_its_own_time_and_its_runner_is_not_idle(self):
        # The sixth audit's case: limit 2, a job 12:00:25 to 12:00:42 (finish recorded), samples at
        # 12:00:00, 12:00:30 (saw it running, or up with its job not yet recorded) and 12:01:00.
        at = lambda clock: stamp(datetime(2026, 10, 14, 12, *clock, tzinfo=timezone.utc))
        rows = [{"runner": "r", "kind": "ci", "started_at": at((0, 25)), "finished_at": at((0, 42)),
                 "wait_seconds": 0, "routing_seconds": None}]
        for running in (1, 0):
            points = [{"observed_at": at(clock), "host": dict(sample(0, occupied, ran)["host"])}
                      for clock, occupied, ran in (((0, 0), 0, 0), ((0, 30), 1, running), ((1, 0), 0, 0))]
            with self.subTest(sampled_running=running):
                rows_ = LH._intervals(points, LH.job_spans(rows, set(), NOW), 2, NOW)
                self.assertEqual(sum(row[2] for row in rows_[:2]), 17)  # running: the job's own 17 s
                self.assertEqual(sum(row[3] + row[4] for row in rows_[:2]), 0)  # nobody idle

    def test_jobs_with_no_recorded_finish_still_count_from_the_samples(self):
        # Jobs from before the listener recorded finishes, or a missed completion: no span, but every
        # sample saw all 4 slots running, so the hour is fully busy, not free.
        points = [sample(seconds, 4, 4) for seconds in range(3570, 0, -30)]
        rows = [{"runner": "old", "kind": "ci", "started_at": ago(3500), "finished_at": None, "wait_seconds": 0, "routing_seconds": None}]
        hour = LH.history(points, rows, {"slots": {"limit": 4}}, True, None, stamp(NOW))["ranges"][0]
        self.assertEqual((hour["busy_percent"], hour["idle_hours"]), (100, 0))
        self.assertEqual(hour["buckets"][-2]["running"], 4)

    def test_busy_share_counts_each_sample_window_and_splits_idle_from_warm(self):
        # 8 slots. The 7200 s sample is outside the hour. In the hour: 60 s with 4 running, then 30 s with
        # 2 running, 2 up with no job and a warm pool of 1. A wait-pool job never counts.
        points = [sample(7200, 8, 8), sample(90, 4, 4), sample(30, 4, 2)]
        points[2]["host"]["warm"] = 1
        rows = [{"runner": "wait", "kind": "wait", "started_at": ago(90), "finished_at": ago(1),
                 "wait_seconds": 0, "routing_seconds": None}]
        result = LH.history(points, rows, {"slots": {"limit": 8}}, True, None, stamp(NOW))
        hour, day = result["ranges"][0], result["ranges"][1]
        self.assertEqual(hour["busy_percent"], round(100 * (60 * 4 + 30 * 2) / (90 * 8), 1))
        self.assertEqual(hour["covered_hours"], round(90 / 3600, 2))
        self.assertEqual(day["covered_hours"], round((120 + 90) / 3600, 2))
        self.assertEqual((hour["warm_hours"], hour["idle_hours"]), (round(30 / 3600, 2), round(30 / 3600, 2)))

    def test_a_bar_holding_only_the_newest_sample_shows_what_it_saw(self):
        # The collector appends the sample it just took, whose interval has not begun. Alone in the
        # newest bar (12:00:00 to now, the sample at 12:00:30) it must not read as 0 running, all free.
        points = [sample(90, 4, 4), sample(60, 4, 4), sample(0, 4, 3)]
        points[-1]["host"]["warm"] = 1
        rows = [{"runner": "a", "kind": "ci", "started_at": ago(5), "finished_at": None, "wait_seconds": 0, "routing_seconds": None}]
        hour = LH.history(points, rows, {"slots": {"limit": 4, "occupied": [{"runner_name": "a"}]}}, True, None, stamp(NOW))["ranges"][0]
        self.assertEqual(hour["buckets"][-1], {"start": "2026-10-14T12:00:00Z", "running": 3, "warm": 1, "reserved": 0, "waiting": 0})
        # Its one second of weight is not added to the totals, which cover sampled time only.
        self.assertEqual(hour["busy_percent"], 100)

    def test_the_newest_sample_does_not_tip_a_bar_it_shares_with_older_samples_over_its_limit(self):
        # Refreshes every 30 s for an hour, the collector's newest sample at "now", at several offsets
        # against the bar edges, through capture(). 2 slots: 1 running, 1 warm. Every sampled bar of
        # every range reads exactly 1 running and 1 warm, never more than the limit.
        host = {"cpu": {"busy_percent": 1}, "memory": {"available_bytes": 1}, "workspace": {"free_bytes": 1},
                "slots": {"limit": 2, "occupied_count": 2, "remaining_on_demand": 0,
                          "occupied": [allocated("ci", 1, "busy", "build"), allocated("ci", 2, "warm")]}}
        response = {"host": {"queue": {"version": 1, "sets": [{"kind": "ci", "assigned": 1, "running": 1,
                                                                 "min_runners": 1, "warm_instances": [2]}]}}}
        for offset in (0, 7, 29):
            state, start = {}, NOW - timedelta(hours=1) + timedelta(seconds=offset)
            for step in range(120):
                state = LH.capture(state, response, host, True, stamp(start + timedelta(seconds=30 * step)))
            history = state["history"]
            self.assertEqual(history["warm_slots"], ["ci-2"])
            for view in history["ranges"]:
                for bar in (bar for bar in view["buckets"] if "running" in bar):
                    with self.subTest(offset=offset, range=view["name"], bar=bar["start"]):
                        self.assertEqual((bar["running"], bar["warm"], bar["reserved"]), (1, 1, 0))

    def test_a_later_entry_for_a_finished_job_keeps_its_finish(self):
        # A listener restarted between a job's completion and its late start once wrote a second entry
        # with no finish; the finish must not be lost.
        raw = {"version": 1, "jobs": [job("r1", None, ago(300), ago(290), ago(280), finished=ago(200)),
                                      job("r1", None, ago(300), ago(290), ago(10))]}
        self.assertEqual(LH.waits([], raw, NOW)[0]["finished_at"], ago(200))

    def test_a_bar_never_stacks_past_a_limit_that_was_lowered(self):
        # A sample from when the lane had 6 slots, read against today's limit of 2.
        hour = LH.history([sample(30, 6, 1)], [], {"slots": {"limit": 2}}, True, None, stamp(NOW))["ranges"][0]
        bar = hour["buckets"][-1]
        self.assertEqual((bar["running"], bar["warm"], bar["reserved"]), (1, 0, 1))

    def test_busy_share_never_reads_over_full(self):
        rows = [{"runner": "r%d" % n, "kind": "ci", "started_at": ago(3600), "finished_at": ago(0),
                 "wait_seconds": 0, "routing_seconds": None} for n in range(9)]
        result = LH.history([sample(90, 8, 8), sample(30, 8, 8)], rows, {"slots": {"limit": 8}}, True, None, stamp(NOW))
        self.assertEqual(result["ranges"][0]["busy_percent"], 100)

    def test_samples_from_before_running_was_recorded_leave_the_bars_empty(self):
        old = sample(30, 3, 2)
        del old["host"]["slots_running"]
        result = LH.history([old], [], {"slots": {"limit": 8}}, True, None, stamp(NOW))
        self.assertIsNone(result["ranges"][0]["busy_percent"])
        self.assertEqual(result["ranges"][0]["buckets"][-1], {"start": "2026-10-14T12:00:00Z"})

    def test_waits_are_summarised_per_range_and_occupied_runners_get_their_start(self):
        rows = [{"runner": "now", "kind": "ci", "started_at": ago(60), "wait_seconds": 10, "routing_seconds": 4},
                {"runner": "older", "kind": "ci", "started_at": ago(7200), "wait_seconds": 30, "routing_seconds": 4}]
        host = {"slots": {"limit": 8, "occupied": [{"runner_name": "now"}, {"runner_name": "unknown"}]}}
        result = LH.history([], rows, host, True, {"waiting": 2, "warm": 1}, stamp(NOW))
        self.assertEqual(result["ranges"][0]["wait"]["jobs"], 1)
        self.assertEqual(result["ranges"][1]["wait"]["p95_seconds"], 30)
        self.assertEqual(result["started"], {"now": ago(60)})
        self.assertEqual((result["waiting_now"], result["warm"]), (2, 1))

    def test_a_failed_refresh_does_not_claim_a_current_queue(self):
        result = LH.history([], [], {}, False, None, stamp(NOW))
        self.assertIsNone(result["waiting_now"])
        self.assertIsNone(result["limit"])


class Reader(unittest.TestCase):
    def published(self):
        rows = [{"runner": "now", "kind": "ci", "started_at": ago(60), "wait_seconds": 10, "routing_seconds": 4}]
        return LH.history([sample(30, 3, 2, 1)], rows, {"slots": {"limit": 8, "occupied": [{"runner_name": "now"}]}},
                          True, {"waiting": 1, "warm": 0}, stamp(NOW))

    def test_a_published_history_reads_back_unchanged(self):
        value = json.loads(json.dumps(self.published()))
        self.assertEqual(LH.read_history(value), value)

    def test_a_history_out_of_its_bounds_is_refused(self):
        def broken(change):
            value = json.loads(json.dumps(self.published()))
            change(value)
            return value
        for label, change in (("bars", lambda v: v["ranges"][0]["buckets"].pop()),
                              ("order", lambda v: v["ranges"].reverse()),
                              ("busy", lambda v: v["ranges"][0].update(busy_percent=101)),
                              ("runner", lambda v: v["started"].update({"bad name": ago(1)})),
                              ("extra", lambda v: v["ranges"][0]["buckets"][-1].update(note="x")),
                              ("negative", lambda v: v["ranges"][0]["buckets"][-1].update(running=-1)),
                              ("wait", lambda v: v["ranges"][0]["wait"].update(p50_seconds="1")),
                              ("warm slot", lambda v: v.update(warm_slots=["ci-0"]))):
            with self.subTest(label), self.assertRaises((ValueError, TypeError, AttributeError)):
                LH.read_history(broken(change))


class EndToEnd(unittest.TestCase):
    def test_the_collector_keeps_waits_and_the_dashboard_shows_history_and_job_starts(self):
        occupied = [allocated("ci", 1, "lane-ci-1-1", "build"), allocated("ci", 2, "lane-ci-2-1")]
        host = remote_host(occupied)
        host["queue"] = {"version": 1, "updated_at": ago(5), "sets": [{"kind": "ci", "assigned": 4, "running": 1, "min_runners": 0}]}
        host["jobs"] = {"version": 1, "jobs": [job("lane-ci-1-1", ago(200), ago(195), ago(150), ago(149))]}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "telemetry.json"
            refresh(config(), path, fetch=lambda *_: {"host": host, "quota": {"status": "ok", "rate_limits": quota()}},
                    now=stamp(NOW))
            state = json.loads(path.read_text())
            self.assertEqual([row["runner"] for row in state["waits"]], ["lane-ci-1-1"])
            self.assertEqual(state["samples"][-1]["host"]["jobs_waiting"], 3)
            parsed = read_telemetry(Config("example-ci", ("example-ci/repo",), {}, path), NOW)
        capacity = parsed["capacity"]
        self.assertEqual(capacity["history"]["waiting_now"], 3)
        lanes = {lane["id"]: lane for lane in capacity["lanes"]}
        self.assertEqual(lanes["ci-1"]["started_at"], ago(150))
        self.assertNotIn("started_at", lanes["ci-2"])
        self.assertTrue(all("runner_name" not in lane for lane in capacity["lanes"]))

    def test_a_snapshot_from_an_older_collector_still_reads_without_history(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "telemetry.json"
            refresh(config(), path, fetch=lambda *_: {"host": remote_host([allocated("ci", 1, "a", "build")]),
                                                     "quota": {"status": "ok", "rate_limits": quota()}}, now=stamp(NOW))
            value = json.loads(path.read_text())
        for key in ("waits", "history"):
            value.pop(key)
        capacity = collector_view(value, Config("example-ci", ("example-ci/repo",), {}, None), NOW)["capacity"]
        self.assertIsNone(capacity["history"])
        self.assertTrue(all("runner_name" not in lane for lane in capacity["lanes"]))

    def test_an_unknown_root_key_is_still_refused(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "telemetry.json"
            refresh(config(), path, fetch=lambda *_: {"host": remote_host(), "quota": {"status": "ok", "rate_limits": quota()}},
                    now=stamp(NOW))
            value = dict(json.loads(path.read_text()), extra=1)
        with self.assertRaises(ValueError):
            collector_view(value, Config("example-ci", ("example-ci/repo",), {}, None), NOW)


if __name__ == "__main__":
    unittest.main()
