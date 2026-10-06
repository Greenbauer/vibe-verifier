"""Owner isolation and the optional dashboard telemetry contract."""

import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from dashboard.config import ConfigError, load_config
from dashboard.telemetry import read_telemetry


NOW = datetime(2026, 9, 30, 15, 0, tzinfo=timezone.utc)


def iso(value):
    return value.isoformat().replace("+00:00", "Z")


def config_value(owner="octocat", telemetry=None, proxy_origin=None):
    value = {
        "version": 1,
        "owner": owner,
        "repositories": [owner + "/example"],
        "bots": {
            "reviewer": {"workflow": "review.yml", "jobs": ["review"]},
            "explorer": {"workflow": "explore.yml", "jobs": ["explore"]},
            "verifier": {"workflow": "verify.yml", "jobs": ["verify"]},
        },
    }
    if telemetry:
        value["telemetry_file"] = str(telemetry)
    if proxy_origin is not None:
        value["proxy_origin"] = proxy_origin
    return value


def telemetry_value(owner="octocat", lanes=None, allowance=None):
    lanes = lanes if lanes is not None else [{
        "id": "lane-01", "state": "busy", "registered": True, "labels": ["linux"],
        "job": {"repository": owner + "/example", "name": "test",
                "url": "https://github.com/%s/example/actions/runs/1" % owner},
    }]
    window = {"name": "7 days", "used_percent": 25, "resets_at": iso(NOW + timedelta(days=2))}
    if allowance is not None:
        window["allowance_tokens"] = allowance
    return {
        "version": 1,
        "owner": owner,
        "capacity": {
            "sampled_at": iso(NOW - timedelta(seconds=30)),
            "host": {"cpu_percent": 50, "memory_used_bytes": 50, "memory_total_bytes": 100,
                     "workspace_disk_free_bytes": 75, "workspace_disk_total_bytes": 100},
            "lanes": lanes,
        },
        "usage": {
            "sampled_at": iso(NOW - timedelta(seconds=20)),
            "accounts": [{"id": "account-a", "label": "Primary", "provider": "Example provider",
                          "quota_windows": [window]}],
            "samples": [{"owner": owner, "account": "account-a", "bot": "reviewer",
                         "timestamp": iso(NOW - timedelta(hours=1)), "input_tokens": 100, "output_tokens": 50},
                        {"owner": owner, "account": "account-a", "bot": "explorer",
                         "timestamp": iso(NOW - timedelta(days=8)), "input_tokens": 99, "output_tokens": 1}],
            "completeness": "Samples cover completed bot calls from the configured collector.",
        },
        "bots": {"sampled_at": iso(NOW - timedelta(seconds=10)),
                 "states": [{"owner": owner, "bot": "reviewer", "state": "idle", "detail": "Source reports available"}]},
    }


class ConfigIsolation(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="vv-dashboard-")
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)

    def write(self, name, value):
        path = self.root / name
        path.write_text(json.dumps(value), encoding="utf-8")
        return path

    def test_same_owner_repositories_load_and_cross_owner_is_rejected(self):
        good = load_config(self.write("good.json", config_value()))
        self.assertEqual(good.owner, "octocat")
        self.assertEqual(good.repositories, ("octocat/example",))
        bad = config_value()
        bad["repositories"].append("example/foreign")
        with self.assertRaisesRegex(ConfigError, "outside configured owner"):
            load_config(self.write("bad.json", bad))

    def test_two_instances_keep_their_owners_immutable_and_separate(self):
        first = load_config(self.write("first.json", config_value("octocat")))
        second = load_config(self.write("second.json", config_value("example")))
        self.assertEqual(first.repositories, ("octocat/example",))
        self.assertEqual(second.repositories, ("example/example",))
        self.assertNotEqual(first.owner, second.owner)
        self.assertIsNone(first.proxy_origin)
        with self.assertRaises(Exception):
            first.repositories += ("example/example",)
        with self.assertRaises(Exception):
            first.proxy_origin = "https://dashboard.example.test"

    def test_proxy_origin_accepts_only_a_strict_https_origin(self):
        loaded = load_config(self.write(
            "proxy.json", config_value(proxy_origin="https://dashboard.example.test:8443")))
        self.assertEqual(loaded.proxy_origin, "https://dashboard.example.test:8443")
        invalid = (
            "http://dashboard.example.test",
            "https://dashboard.example.test/",
            "https://dashboard.example.test/path",
            "https://dashboard.example.test?view=all",
            "https://dashboard.example.test#top",
            "https://user@dashboard.example.test",
            "https://*.example.test",
            "https://dashboard.example.test\n",
            "https://dashboard.example.test:0",
            "https://dashboard.example.test:",
            "https://dashboard.example.test:65536",
            "https://dashboard.example.test:port",
            "https://",
        )
        for index, origin in enumerate(invalid):
            with self.subTest(origin=origin):
                with self.assertRaisesRegex(ConfigError, "proxy_origin"):
                    load_config(self.write("invalid-proxy-%s.json" % index,
                                           config_value(proxy_origin=origin)))

    def test_config_requires_all_explicit_bot_identifiers(self):
        value = config_value()
        del value["bots"]["verifier"]
        with self.assertRaisesRegex(ConfigError, "reviewer, explorer, and verifier"):
            load_config(self.write("missing-bot.json", value))


class TelemetryContract(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="vv-dashboard-")
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)

    def load(self, telemetry, owner="octocat"):
        telemetry_path = self.root / (owner + "-telemetry.json")
        telemetry_path.write_text(json.dumps(telemetry), encoding="utf-8")
        config_path = self.root / (owner + "-config.json")
        config_path.write_text(json.dumps(config_value(owner, telemetry_path)), encoding="utf-8")
        return read_telemetry(load_config(config_path), NOW)

    def test_valid_snapshot_filters_history_to_seven_days_and_preserves_current_owner(self):
        result = self.load(telemetry_value())
        self.assertTrue(result["available"])
        self.assertEqual([row["bot"] for row in result["usage"]["samples"]], ["reviewer"])
        self.assertEqual(result["capacity"]["lanes"][0]["job"]["repository"], "octocat/example")
        self.assertFalse(result["capacity"]["stale"])

    def test_any_mixed_owner_row_rejects_the_whole_snapshot(self):
        value = telemetry_value()
        value["usage"]["samples"].append({"owner": "example", "account": "account-a", "bot": "reviewer",
                                           "timestamp": iso(NOW), "input_tokens": 1, "output_tokens": 1})
        result = self.load(value)
        self.assertEqual(result, {"available": False, "reason": "invalid_or_unavailable"})

    def test_cross_owner_busy_job_is_rejected(self):
        value = telemetry_value()
        value["capacity"]["lanes"][0]["job"]["repository"] = "example/foreign"
        self.assertFalse(self.load(value)["available"])

    def test_sixteen_lanes_and_on_demand_are_distinct_from_registration(self):
        lanes = [{"id": "lane-%02d" % index,
                  "state": "provisionable" if index == 0 else "ready",
                  "registered": False if index == 0 else True, "labels": []}
                 for index in range(16)]
        result = self.load(telemetry_value(lanes=lanes))
        self.assertEqual(len(result["capacity"]["lanes"]), 16)
        self.assertEqual(result["capacity"]["lanes"][0]["state"], "provisionable")
        self.assertFalse(result["capacity"]["lanes"][0]["registered"])

    def test_quota_window_keeps_an_optional_allowance_and_window_length(self):
        without = self.load(telemetry_value())["usage"]["accounts"][0]["quota_windows"][0]
        with_allowance = self.load(telemetry_value(allowance=1_000_000))["usage"]["accounts"][0]["quota_windows"][0]
        self.assertIsNone(without["allowance_tokens"])
        self.assertIsNone(without["window_minutes"])
        self.assertEqual(with_allowance["allowance_tokens"], 1_000_000)
        value = telemetry_value()
        value["usage"]["accounts"][0]["quota_windows"][0]["window_minutes"] = 10080
        self.assertEqual(self.load(value)["usage"]["accounts"][0]["quota_windows"][0]["window_minutes"], 10080)

    def test_invalid_window_length_rejects_the_snapshot(self):
        for minutes in (0, -5, True, 1.5, "10080", 60 * 24 * 32):
            with self.subTest(minutes=minutes):
                value = telemetry_value()
                value["usage"]["accounts"][0]["quota_windows"][0]["window_minutes"] = minutes
                self.assertFalse(self.load(value)["available"])

    def test_stale_sections_are_labeled_not_refreshed(self):
        value = telemetry_value()
        value["capacity"]["sampled_at"] = iso(NOW - timedelta(minutes=6))
        result = self.load(value)
        self.assertTrue(result["capacity"]["stale"])
        self.assertEqual(result["capacity"]["sampled_at"], iso(NOW - timedelta(minutes=6)))
        self.assertEqual(result["capacity"]["lanes"][0]["state"], "busy")
        self.assertEqual(result["capacity"]["lanes"][0]["job"]["name"], "test")


if __name__ == "__main__":
    unittest.main()
