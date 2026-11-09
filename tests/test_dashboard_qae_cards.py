"""Each QAE card is a lane slot: it shows every job of the explorer's workflow that ran on that slot, and
keeps five recent runs of its own."""

import unittest
from collections import Counter
from datetime import timedelta

from dashboard.agents import agent_view
from dashboard.bot_runs import pulls_bot_jobs, qae_instance, recent_bot_runs
from dashboard.config import AgentDefinition, Config
from dashboard.github import GitHubCollector
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


if __name__ == "__main__":
    unittest.main()
