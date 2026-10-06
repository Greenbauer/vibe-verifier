"""Configured identities cannot inherit unrelated CI jobs or runner health."""
import unittest
from datetime import datetime, timedelta, timezone

from dashboard.agents import agent_view
from dashboard.config import AgentDefinition, Config, ConfigError, _parse_agents
from dashboard.telemetry import _agents, _lanes, TelemetryError
from dashboard.collector_view import join_runner_jobs

NOW = datetime(2026, 9, 30, 15, tzinfo=timezone.utc)


class AgentIdentities(unittest.TestCase):
    def config(self, workflow=False):
        agent = AgentDefinition("swe-runner", "SWE" if workflow else "SWE2", "swe",
                                "reviewer" if workflow else None)
        return Config("octocat", ("octocat/example",), {}, None, agents=(agent,))

    def runtime(self):
        return {"sampled_at": NOW.isoformat(), "rows": [{"id": "swe-runner", "state": "paused", "runs": [
            {"id": "run-1", "category": "failed", "completed_at": (NOW - timedelta(minutes=10)).isoformat(), "elapsed_seconds": 45},
            {"id": "run-2", "category": "success", "completed_at": (NOW - timedelta(hours=3)).isoformat(), "elapsed_seconds": 90}]}]}

    def test_configuration_preserves_real_numbering_and_refuses_fake_identity(self):
        roster = _parse_agents([{"id": "agent-a", "name": "QAE3", "role": "qae"}])
        self.assertEqual(roster[0].name, "QAE3")
        for row in ({"id": "a", "name": "QAE verifier", "role": "qae"},
                    {"id": "a", "name": "QAE", "role": "qae", "workflow_role": "verifier"},
                    {"id": "a", "name": "SWE1", "role": "swe", "workflow_role": "reviewer"}):
            with self.subTest(row=row), self.assertRaises(ConfigError):
                _parse_agents([row])
        for identity in ("reviewer", "explorer", "verifier", "total"):
            with self.subTest(identity=identity), self.assertRaises(ConfigError):
                _parse_agents([{"id": identity, "name": "SWE1", "role": "swe"}])

    def test_runtime_identity_state_history_and_tokens_share_the_same_key(self):
        config = self.config()
        telemetry = {"available": True, "agents": _agents(self.runtime(), config, NOW),
                     "usage": {"available": True, "samples": [{"bot": "swe-runner", "input_tokens": 3},
                                                              {"bot": "reviewer", "input_tokens": 999}]}}
        view = agent_view(config, {"bots": {"roles": {"reviewer": {"state": "working"}}}}, telemetry, NOW)
        self.assertEqual(view["rows"][0]["name"], "SWE2")
        self.assertEqual(view["rows"][0]["state"], "paused")
        self.assertEqual(len(view["rows"][0]["recent_2h"]), 1)
        self.assertEqual(len(view["rows"][0]["recent_7d"]), 2)
        self.assertEqual(view["rows"][0]["recent_2h"][0]["category"], "failed")
        self.assertEqual(view["usage"]["samples"], [{"bot": "swe-runner", "input_tokens": 3}])
        stale = agent_view(config, {}, telemetry, NOW + timedelta(hours=8))
        self.assertEqual(stale["rows"][0]["recent_2h"], [])

    def test_unknown_stale_and_foreign_runtime_are_not_reported_idle(self):
        config = self.config()
        self.assertEqual(agent_view(config, {}, {}, NOW)["rows"][0]["state"], "unknown")
        runtime = _agents(self.runtime(), config, NOW + timedelta(minutes=6))
        self.assertTrue(runtime["stale"])
        row = agent_view(config, {}, {"available": True, "agents": runtime}, NOW)["rows"][0]
        self.assertEqual(row["state"], "paused")
        self.assertEqual(row["coverage"]["history"], "stale")
        raw = self.runtime()
        raw["rows"][0]["id"] = "foreign-agent"
        with self.assertRaises(TelemetryError):
            _agents(raw, config, NOW)

    def test_ci_mapping_ignores_runner_health_and_never_adds_gate_as_agent(self):
        config = self.config(workflow=True)
        github = {"bots": {"roles": {"reviewer": {"state": "down", "coverage": {"active": "complete"}},
                                       "verifier": {"state": "working"}}}}
        row = agent_view(config, github, {}, NOW)["rows"][0]
        self.assertEqual(row["state"], "idle")
        self.assertEqual(row["source"], "CI workflow activity")
        github["bots"]["roles"]["reviewer"]["active"] = [{"id": 1}]
        self.assertEqual(agent_view(config, github, {}, NOW)["rows"][0]["state"], "working")
        self.assertEqual(agent_view(Config("octocat", (), {}, None), github, {}, NOW)["rows"], [])

    def test_registered_busy_runner_keeps_unknown_job_until_owner_evidence_matches(self):
        config = self.config()
        lanes = _lanes([{"id": "org-runner", "state": "busy", "registered": True,
                         "runner_id": 7}], config)
        self.assertNotIn("job", lanes[0])
        telemetry = {"capacity": {"available": True, "stale": False, "lanes": lanes}}
        github = {"repositories": [{"repository": "octocat/example", "pulls": [{"runs": [{"jobs": [
            {"runner_id": 7, "status": "in_progress", "name": "test", "html_url": None}]}]}]}]}
        join_runner_jobs(telemetry, github)
        self.assertEqual(lanes[0]["job"]["repository"], "octocat/example")
        self.assertEqual(lanes[0]["job"]["name"], "test")
        with self.assertRaises(TelemetryError):
            _lanes([{"id": "unknown", "state": "busy", "registered": False}], config)

    def test_run_snapshot_rejects_raw_logs_and_more_than_five_outcomes(self):
        raw = self.runtime()
        raw["rows"][0]["runs"][0]["log"] = "private log"
        with self.assertRaises(TelemetryError):
            _agents(raw, self.config(), NOW)
        raw = self.runtime()
        raw["rows"][0]["runs"] *= 3
        with self.assertRaises(TelemetryError):
            _agents(raw, self.config(), NOW)


if __name__ == "__main__":
    unittest.main()
