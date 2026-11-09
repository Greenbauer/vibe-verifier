"""Each QAE card is a lane slot: it shows every job of the explorer's workflow that ran on that slot, and
keeps five recent runs of its own."""

import json
import subprocess
import unittest
from collections import Counter
from datetime import timedelta
from pathlib import Path

from dashboard.agents import agent_view
from dashboard.bot_runs import pulls_bot_jobs, qae_instance, recent_bot_runs
from dashboard.config import AgentDefinition, Config
from dashboard.github import GitHubCollector
from dashboard.github_bots import HISTORY_RUN_LIMIT
from dashboard.service import DashboardService
from dashboard.required_workflows import workflow_runs
from tests.test_dashboard_bot_history import NOW, REPO, WORKFLOW, BotAPI, config, job, jobs_endpoint, run


def done(slot, minutes, runner=None):
    return {"runner_name": runner or "box-ci-qae-%s-17000" % slot, "category": "success",
            "completed_at": (NOW - timedelta(minutes=minutes)).isoformat()}


class FivePerSlot(unittest.TestCase):
    def test_each_slot_and_the_other_runners_keep_five(self):
        rows = [done(slot, slot * 10 + age) for slot in range(1, 9) for age in range(6)]
        rows += [done(None, 200 + age, runner="GitHub Actions %s" % age) for age in range(6)]
        kept = recent_bot_runs(rows, NOW, 168)
        counts = Counter(qae_instance(row["runner_name"]) for row in kept)
        self.assertEqual(counts, {**{slot: 5 for slot in range(1, 9)}, None: 5})
        self.assertEqual(kept, sorted(kept, key=lambda row: row["completed_at"], reverse=True))

    def test_runs_on_no_slot_keep_five_as_before(self):
        rows = [done(None, age, runner="GitHub Actions %s" % age) for age in range(8)]
        self.assertEqual(len(recent_bot_runs(rows, NOW, 168)), 5)

    def test_eight_cards_each_show_their_own_runs(self):
        recent = recent_bot_runs([done(slot, slot * 10 + age) for slot in range(1, 9) for age in range(6)],
                                 NOW, 168)
        github = {"bots": {"roles": {"explorer": {"active": [], "recent_2h": recent, "recent_7d": recent,
                                                  "coverage": {"active": "complete", "history": "complete"}}}}}
        roster = Config("example", ("example/site",), {}, None,
                        agents=(AgentDefinition("ci-qae", "QAE", "qae", "explorer"),))
        telemetry = {"available": True, "capacity": {"available": True, "limits": {"qae_concurrency": 8}},
                     "usage": {"samples": []}}
        rows = agent_view(roster, github, telemetry, NOW)["rows"]
        self.assertEqual([len(row["recent_2h"]) for row in rows], [5] * 8)


class SlotJobs(unittest.TestCase):
    def jobs(self):
        return [job(1, "explore", "in_progress", runner="box-ci-qae-1-1"),
                {**job(1, "main walk", "in_progress", runner="box-ci-qae-2-1"), "id": 11},
                {**job(1, "verify", "in_progress", runner="box-ci-qae-3-1"), "id": 12},
                {**job(1, "lint", "in_progress", runner="GitHub Actions 2"), "id": 13}]

    def test_an_unnamed_job_on_a_slot_is_the_explorers(self):
        api = BotAPI(active=[run(1, "in_progress")], jobs={jobs_endpoint(1): self.jobs()})
        roles = GitHubCollector(config(), api, clock=lambda: NOW)._bots(NOW)["roles"]
        self.assertEqual([row["name"] for row in roles["explorer"]["active"]], ["explore", "main walk"])
        self.assertEqual([row["name"] for row in roles["verifier"]["active"]], ["verify"])
        self.assertEqual([row["name"] for row in roles["reviewer"]["active"]], [])

    def test_a_workflow_without_the_explorer_takes_no_slot_job(self):
        api = BotAPI(active=[run(1, "in_progress")], jobs={jobs_endpoint(1): self.jobs()})
        outcome = GitHubCollector(config(), api, clock=lambda: NOW)._bot_workflow(
            REPO, WORKFLOW, ["reviewer", "verifier"], NOW, workflow_runs(REPO, WORKFLOW))
        self.assertEqual([row["name"] for row in outcome["rows"]["verifier"]["active"]], ["verify"])
        self.assertEqual(outcome["rows"]["reviewer"]["active"], [])

    def test_pull_request_runs_take_slot_jobs(self):
        payloads = {REPO: {"pulls": [{"runs": [{"path": ".github/workflows/" + WORKFLOW, "jobs": [
            job(1, "proof audit", "completed", "success", runner="box-ci-qae-4-1"),
            job(1, "lint", "completed", "success", runner="GitHub Actions 2")]}]}]}}
        found = pulls_bot_jobs(payloads, config().bots)
        self.assertEqual([(role, row["name"]) for _, role, _, row in found], [("explorer", "proof audit")])


class SlotHistoryScan(unittest.TestCase):
    def test_a_slot_met_late_in_the_scan_is_still_read(self):
        """Five runs on slot 1, then an older one on slot 2: a role-wide five would stop before it."""
        runs = [run(number, "completed", 130 + number) for number in range(1, 7)]
        jobs = {jobs_endpoint(number): [job(number, "explore", "completed", "success", 130 + number,
                                            runner="box-ci-qae-%s-1" % (2 if number == 6 else 1))]
                for number in range(1, 7)}
        api = BotAPI(completed=runs, jobs=jobs)
        explorer = GitHubCollector(config(), api, clock=lambda: NOW)._bots(NOW)["roles"]["explorer"]
        self.assertEqual([row["runner_name"] for row in explorer["recent_7d"]][-1], "box-ci-qae-2-1")
        self.assertEqual(len(explorer["recent_7d"]), 6)


def cards(source, concurrency=2):
    roster = Config("example", ("example/site",), {}, None,
                    agents=(AgentDefinition("ci-qae", "QAE", "qae", "explorer"),))
    telemetry = {"available": True, "capacity": {"available": True, "limits": {"qae_concurrency": concurrency}},
                 "usage": {"samples": []}}
    return agent_view(roster, {"bots": {"roles": {"explorer": source}}}, telemetry, NOW)["rows"]


class SlotCoverage(unittest.TestCase):
    def role(self, rows, floor_minutes=None, scanned=True):
        return {"active": [], "recent_2h": recent_bot_runs(rows, NOW, 2), "recent_7d": recent_bot_runs(rows, NOW, 168),
                "coverage": {"active": "complete", "history": "complete"}, "history_scanned": scanned,
                "history_floor": (NOW - timedelta(minutes=floor_minutes)).isoformat() if floor_minutes else None}

    def test_a_slot_with_five_runs_newer_than_the_unread_ones_is_complete_and_one_short_is_not(self):
        rows = [done(1, 10 + age) for age in range(5)] + [done(2, 20)]
        slot_one, slot_two = cards(self.role(rows, floor_minutes=300))
        self.assertEqual(slot_one["coverage"]["history"], "complete")
        self.assertEqual(slot_two["coverage"]["history"], "partial")
        self.assertEqual([slot_one["coverage"]["recent"], slot_two["coverage"]["recent"]], ["complete"] * 2)

    def test_unread_runs_inside_two_hours_leave_the_last_two_hours_partial(self):
        slot_one, _ = cards(self.role([done(1, 10 + age) for age in range(5)], floor_minutes=60))
        self.assertEqual(slot_one["coverage"]["recent"], "partial")
        self.assertEqual(slot_one["coverage"]["history"], "complete")

    def test_every_slot_is_complete_when_no_run_was_left_unread_and_partial_when_a_scan_failed(self):
        self.assertEqual([card["coverage"]["history"] for card in cards(self.role([done(1, 10)]))], ["complete"] * 2)
        self.assertEqual({card["coverage"]["recent"] for card in cards(self.role([done(1, 10)], scanned=False))},
                         {"partial"})

    def test_a_slot_whose_runs_fell_past_the_run_limit_is_partial(self):
        """Every run within the limit is on slot 1; slot 2's runs are older, past the limit."""
        count = HISTORY_RUN_LIMIT + 6
        runs = [run(number, "completed", 130 + number) for number in range(1, count + 1)]
        jobs = {jobs_endpoint(number): [job(number, "explore", "completed", "success", 130 + number,
                                            runner="box-ci-qae-%s-1" % (2 if number > HISTORY_RUN_LIMIT else 1))]
                for number in range(1, count + 1)}
        explorer = GitHubCollector(config(), BotAPI(completed=runs, jobs=jobs), clock=lambda: NOW)._bots(NOW)["roles"]["explorer"]
        self.assertIsNotNone(explorer["history_floor"])
        slot_one, slot_two = cards(explorer)
        self.assertEqual([slot_one["coverage"]["history"], slot_two["coverage"]["history"]], ["complete", "partial"])


class AgedHistory(unittest.TestCase):
    def test_aging_a_role_keeps_five_per_slot(self):
        rows = [done(slot, slot * 10 + age) for slot in range(1, 9) for age in range(6)]
        role = {"recent_2h": rows, "recent_7d": rows, "active": []}
        grace = type("Grace", (), {"_within_grace": staticmethod(lambda stamp, now: True)})()
        DashboardService._expire_role(grace, {}, role, NOW)
        self.assertEqual(Counter(qae_instance(row["runner_name"]) for row in role["recent_7d"]),
                         {slot: 5 for slot in range(1, 9)})


class RecentLabel(unittest.TestCase):
    def test_an_idle_card_says_no_runs_only_when_its_last_two_hours_were_read(self):
        source = r"""
const app=require('fs').readFileSync('./dashboard/static/app.js','utf8');
const label=new Function(app.match(/(  function recentRunLabel\([\s\S]*?\n  \})/)[1]+'\nreturn recentRunLabel;')();
console.log(JSON.stringify([
  label({coverage:{history:'partial',recent:'complete'}},[]),
  label({coverage:{history:'complete',recent:'partial'}},[]),
  label({coverage:{history:'complete'}},[])]));
"""
        result = subprocess.run(["node", "-e", source], cwd=Path(__file__).resolve().parents[1],
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), ["No runs in 2h", "History incomplete", "No runs in 2h"])


if __name__ == "__main__":
    unittest.main()
