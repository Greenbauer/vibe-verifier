"""The dashboard's reader of a published lane history, and a lane whose QAE has slots of its own."""
import json
import unittest

from dashboard import lane_history as LH
from dashboard.collector_view import _qae_apart
from dashboard.history_read import read_history
from test_dashboard_lane_history import NOW, ago, allocated, sample, stamp


class Reader(unittest.TestCase):
    def published(self):
        rows = [{"runner": "now", "kind": "ci", "started_at": ago(60), "wait_seconds": 10, "routing_seconds": 4}]
        return LH.history([sample(30, 3, 2, 1)], rows, {"slots": {"limit": 8, "occupied": [{"runner_name": "now"}]}},
                          True, {"waiting": 1, "warm": 0}, stamp(NOW))

    def test_a_published_history_reads_back_unchanged(self):
        value = json.loads(json.dumps(self.published()))
        self.assertEqual(read_history(value), value)

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
                              ("warm slot", lambda v: v.update(warm_slots=["ci-0"])),
                              ("qae slots", lambda v: v.update(qae_slots=65))):
            with self.subTest(label), self.assertRaises((ValueError, TypeError, AttributeError)):
                read_history(broken(change))


class QAEApart(unittest.TestCase):
    """The listener names QAE's own pool in queue.json; the runner slots then count CI alone."""

    def test_the_queue_names_qae_slots_only_when_the_listener_does(self):
        raw = {"version": 1, "sets": [{"kind": "qae", "assigned": 2, "running": 1}], "qae_slots": 3}
        self.assertEqual(LH.project_queue(raw)["qae_slots"], 3)
        self.assertNotIn("qae_slots", LH.project_queue({**raw, "qae_slots": 0}))
        self.assertIsNone(LH.project_queue({**raw, "qae_slots": 65}))
        self.assertEqual((LH.shared_kinds({"qae_slots": 3}), LH.shared_kinds({}), LH.shared_kinds(None)),
                         (("ci",), ("ci", "qae"), ("ci", "qae")))

    def test_samples_and_history_count_the_runner_slots_without_qae(self):
        host = {"slots": {"limit": 4, "occupied_count": 3, "remaining_on_demand": 1, "occupied": [
            allocated("ci", 1, "a", "build"), allocated("qae", 1, "q1", "walk"), allocated("qae", 2, "q2", "walk")]},
            "cpu": {"busy_percent": 1}, "memory": {"available_bytes": 1}, "workspace": {"free_bytes": 1}}
        queue = {"waiting": 0, "warm": 0, "warm_slots": [], "qae_slots": 3}
        shared = LH.samples({}, host, True, stamp(NOW), {"waiting": 0, "warm": 0})[-1]["host"]
        apart = LH.samples({}, host, True, stamp(NOW), queue)[-1]["host"]
        self.assertEqual((shared["slots_occupied"], shared["slots_running"]), (3, 3))
        self.assertEqual((apart["slots_occupied"], apart["slots_running"]), (1, 1))
        rows = [{"runner": "q1", "kind": "qae", "started_at": ago(600), "finished_at": ago(60), "wait_seconds": 1, "routing_seconds": None}]
        history = LH.history([sample(30, 1, 1)], rows, host, True, queue, stamp(NOW))
        self.assertEqual(history["qae_slots"], 3)
        self.assertEqual(read_history(json.loads(json.dumps(history)))["qae_slots"], 3)
        self.assertEqual(LH.job_spans(rows, set(), NOW), [(LH.parse_time(ago(600)), LH.parse_time(ago(60)))])
        self.assertEqual(LH.job_spans(rows, set(), NOW, LH.shared_kinds(queue)), [])

    def test_the_view_gives_qae_its_own_pool_and_frees_the_runner_slots_it_held(self):
        lanes = [{"id": "ci-1", "labels": ["ci"]}, {"id": "qae-1", "labels": ["qae"]}, {"id": "qae-2", "labels": ["qae"]},
                 {"id": "on-demand-1", "state": "provisionable", "registered": False, "labels": []}]
        capacity = {"limits": {"slots": 4}, "listener_state": "up", "lanes": lanes}
        _qae_apart(capacity, 3)
        self.assertEqual(capacity["limits"]["qae_slots"], 3)
        self.assertEqual([lane["id"] for lane in lanes if lane["id"].startswith("on-demand-")],
                         ["on-demand-1", "on-demand-2", "on-demand-3"])
        self.assertEqual(lanes[-1], {"id": "on-demand-3", "state": "provisionable", "registered": False, "labels": []})


if __name__ == "__main__":
    unittest.main()
