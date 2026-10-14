"""Owner isolation and the optional dashboard telemetry contract."""

import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from dashboard.config import AgentDefinition, Config, ConfigError, load_config
from dashboard.telemetry import HISTORY, TelemetryError, _agents, _lanes, _usage, read_telemetry


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
        with self.assertRaisesRegex(ConfigError, "^repository 'example/foreign' is outside configured owner octocat$"):
            load_config(self.write("bad.json", bad))

    def test_repositories_all_is_the_whole_owner_and_every_other_shape_is_refused(self):
        value = config_value()
        value["repositories"] = "all"
        loaded = load_config(self.write("all.json", value))
        self.assertTrue(loaded.all_repositories)
        self.assertEqual(loaded.repositories, ())
        named = config_value()
        named["repositories"] = ["octocat/all"]
        named_loaded = load_config(self.write("named-all.json", named))
        self.assertFalse(named_loaded.all_repositories)
        self.assertEqual(named_loaded.repositories, ("octocat/all",))
        refused = {
            "missing": "missing", "null": None, "empty": [], "blank": "", "star": "*",
            "upper": "ALL", "capital": "All", "list of all": ["all"], "object": {"owner": "octocat"},
            "number": 1, "bare name": "octocat/example",
            "duplicate": ["octocat/example", "octocat/Example"],
        }
        reasons = {"list of all": "^each repository must be OWNER/NAME$",
                   "duplicate": "^repositories contains a duplicate$"}
        for name, repositories in refused.items():
            broken = config_value()
            if repositories == "missing":
                broken.pop("repositories")
            else:
                broken["repositories"] = repositories
            reason = reasons.get(name, '^repositories must be a non-empty list of OWNER/NAME or the string "all"$')
            with self.assertRaisesRegex(ConfigError, reason, msg=name):
                load_config(self.write("bad-%s.json" % name, broken))

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

    def test_a_collector_job_keeps_the_configured_owner_and_drops_a_foreign_one(self):
        from dashboard.collector import refresh
        from test_dashboard_collector import config as collector_config, quota, remote_host
        row = {"kind": "ci", "index": 1, "state": "allocated",
               "unit": {"active_state": "active", "sub_state": "running"},
               "target_repository": "example-ci/repo", "set_id": 4, "runner_id": 9,
               "runner_name": "runner-ci-1", "allocated_at": iso(NOW),
               "job_name": "Build", "job_repository": "example-ci/repo"}
        foreign = dict(row, job_repository="other/repo", job_name="Foreign")
        path = self.root / "collector.json"

        def collect(occupied):
            host = remote_host(occupied)
            refresh(collector_config(), path, fetch=lambda *_: {
                "host": host, "quota": {"status": "ok", "rate_limits": quota()}}, now=iso(NOW))
            config_path = self.root / "collector-config.json"
            config_path.write_text(json.dumps(config_value("example-ci", path)), encoding="utf-8")
            return read_telemetry(load_config(config_path), NOW)

        shown = collect([row])
        self.assertEqual(shown["capacity"]["lanes"][0]["job"], {
            "repository": "example-ci/repo", "name": "Build", "url": None})
        hidden = collect([foreign])
        self.assertEqual(hidden["capacity"]["lanes"][0]["job"]["name"], "Runner allocated; job match pending")
        self.assertNotIn("other/repo", json.dumps(hidden))
        self.assertNotIn("Foreign", json.dumps(hidden))

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


class TelemetryRows(unittest.TestCase):
    """Each row of a section keeps exactly its fields, and each invalid shape is refused with its reason."""
    config = Config("octocat", ("octocat/example",), {}, None,
                    agents=(AgentDefinition("swe-runner", "SWE2", "swe"),))
    url = "https://github.com/octocat/example/actions/runs/1"

    def refuse(self, call, reason, cases):
        for name, value in cases.items():
            with self.subTest(case=name), self.assertRaisesRegex(TelemetryError, "^%s$" % reason):
                call(value)

    def lane(self, **fields):
        row = {"id": "lane-01", "state": "busy", "registered": True, "labels": ["linux"],
               "job": {"repository": "octocat/example", "name": "test", "url": self.url}}
        row.update(fields)
        return {key: value for key, value in row.items() if value is not None}

    def test_lanes_keep_their_fields_and_refuse_each_invalid_row(self):
        labels = ["linux"] * 20
        self.assertEqual(_lanes([self.lane(labels=labels, runner_id=7), self.lane(id="lane-02", runner_id=1, job=None),
                                 self.lane(id="lane-03", state="ready", registered=False, labels=None, job=None)],
                                self.config),
                         [{"id": "lane-01", "state": "busy", "registered": True, "labels": labels, "runner_id": 7,
                           "job": {"repository": "octocat/example", "name": "test", "url": self.url}},
                          {"id": "lane-02", "state": "busy", "registered": True, "labels": ["linux"], "runner_id": 1},
                          {"id": "lane-03", "state": "ready", "registered": False, "labels": []}])
        lanes = lambda rows: _lanes(rows, self.config)
        self.refuse(lanes, "lane row is invalid", {"not an object": ["lane-01"], "extra field": [self.lane(host="a")]})
        self.refuse(lanes, "lane identity or state is invalid", {
            "id pattern": [self.lane(id="lane 01")], "state": [self.lane(state="sleeping")],
            "duplicate": [self.lane(), self.lane()]})
        self.refuse(lanes, "lane registered must be true, false, or null", {"text": [self.lane(registered="yes")]})
        self.refuse(lanes, "lane labels are invalid", {"text": [self.lane(labels="linux")],
                                                       "too many": [self.lane(labels=labels + ["x"])]})
        self.refuse(lanes, "text field is invalid", {"long label": [self.lane(labels=["x" * 61])]})
        self.refuse(lanes, "runner id must be a positive integer", {
            "zero": [self.lane(runner_id=0)], "negative": [self.lane(runner_id=-1)],
            "boolean": [self.lane(runner_id=True)], "text": [self.lane(runner_id="7")]})
        self.refuse(lanes, "lane job is invalid", {"text": [self.lane(job="test")],
                                                   "extra field": [self.lane(job={"name": "test", "log": "x"})]})
        self.refuse(lanes, "a busy lane must identify its same-owner job", {
            "unregistered": [self.lane(registered=False, runner_id=7, job=None)],
            "no runner": [self.lane(job=None)]})

    def usage(self, accounts=None, samples=None):
        window = {"name": "7 days", "used_percent": 25, "resets_at": iso(NOW + timedelta(days=2))}
        account = {"id": "account-a", "label": "Primary", "provider": "p" * 60, "quota_windows": [window] * 10}
        sample = {"owner": "octocat", "account": "account-a", "bot": "reviewer",
                  "timestamp": iso(NOW - HISTORY), "input_tokens": 3, "output_tokens": 4}
        return {"sampled_at": iso(NOW), "accounts": [account] if accounts is None else accounts,
                "samples": [sample] if samples is None else samples, "completeness": "Complete."}

    def test_usage_keeps_its_fields_and_refuses_each_invalid_account_or_sample(self):
        result = _usage(self.usage(), self.config, NOW)
        self.assertEqual([(row["id"], row["label"], row["provider"], len(row["quota_windows"]))
                          for row in result["accounts"]], [("account-a", "Primary", "p" * 60, 10)])
        self.assertEqual(result["samples"], [{"account": "account-a", "bot": "reviewer", "timestamp": iso(NOW - HISTORY),
                                              "input_tokens": 3, "output_tokens": 4}])
        bare = {"id": "account-b", "label": "Second", "provider": "Example"}
        self.assertEqual(_usage(self.usage(accounts=[bare], samples=[]), self.config, NOW)["accounts"],
                         [dict(bare, quota_windows=[])])
        account, sample = self.usage()["accounts"][0], self.usage()["samples"][0]
        accounts = lambda rows: _usage(self.usage(accounts=rows, samples=[]), self.config, NOW)
        samples = lambda rows: _usage(self.usage(samples=rows), self.config, NOW)
        self.refuse(accounts, "usage account is invalid", {"not an object": ["account-a"],
                                                           "extra field": [dict(account, email="a")]})
        self.refuse(accounts, "usage account id is invalid", {"id pattern": [dict(account, id="account a")],
                                                              "duplicate": [account, account]})
        self.refuse(accounts, "quota_windows is invalid", {
            "text": [dict(account, quota_windows="7 days")],
            "too many": [dict(account, quota_windows=account["quota_windows"] + account["quota_windows"][:1])]})
        self.refuse(accounts, "text field is invalid", {"long provider": [dict(account, provider="p" * 61)]})
        self.refuse(samples, "token sample is invalid", {"not an object": ["sample"],
                                                         "missing field": [{key: value for key, value in sample.items() if key != "bot"}]})
        self.refuse(samples, "token sample references an unknown account or bot", {
            "account": [dict(sample, account="account-z")], "bot": [dict(sample, bot="stranger")]})

    def test_agent_runs_keep_their_fields_and_refuse_each_invalid_run(self):
        run = {"id": "run-1", "completed_at": iso(NOW - HISTORY), "category": "cancelled", "elapsed_seconds": 5}
        old = dict(run, id="run-0", completed_at=iso(NOW - HISTORY - timedelta(seconds=1)))
        agents = lambda runs: _agents({"sampled_at": iso(NOW), "rows": [{"id": "swe-runner", "state": "idle",
                                                                          "runs": runs}]}, self.config, NOW)
        self.assertEqual(agents([old, run])["rows"][0]["runs"], [
            {"id": "run-1", "completed_at": iso(NOW - HISTORY), "category": "cancelled", "elapsed_seconds": 5.0,
             "name": "SWE2 run", "repository": None, "html_url": None}])
        self.refuse(agents, "agent run is invalid", {
            "not an object": ["run-1"], "extra field": [dict(run, log="private")],
            "id pattern": [dict(run, id="run 1")], "category": [dict(run, category="skipped")]})


if __name__ == "__main__":
    unittest.main()
