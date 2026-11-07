"""A matrix job (`<job> (<values>)`, a split walk's `Acceptance criteria: walk (7)`) counts as its job."""

import unittest

from dashboard.bot_runs import job_named, pulls_bot_jobs
from dashboard.github import GitHubCollector
from tests.test_dashboard_bot_history import NOW, REPO, WORKFLOW, BotAPI, config, job, jobs_endpoint, run


class JobNamed(unittest.TestCase):
    def test_a_matrix_job_is_its_job(self):
        names = {"Acceptance criteria: walk"}
        self.assertTrue(job_named("Acceptance criteria: walk", names))
        self.assertTrue(job_named("Acceptance criteria: walk (7)", names))
        self.assertTrue(job_named("Acceptance criteria: walk (first-use, 2)", names))

    def test_another_job_is_not(self):
        names = {"Acceptance criteria"}
        self.assertFalse(job_named("Acceptance criteria: walk (2)", names))
        self.assertFalse(job_named("Acceptance criteria (2) extra", names))
        self.assertFalse(job_named("Acceptance criteria(2)", names))
        self.assertFalse(job_named(None, names))


class MatrixJobs(unittest.TestCase):
    def test_running_shards_make_the_role_working_with_each_runner(self):
        shards = [job(1, "explore", "in_progress", runner="lane-qae-2-1"),
                  {**job(1, "explore (7)", "in_progress", runner="lane-qae-7-1"), "id": 11},
                  {**job(1, "verify (2)", "in_progress", runner="lane-qae-3-1"), "id": 12}]
        api = BotAPI(active=[run(1, "in_progress")], jobs={jobs_endpoint(1): shards})
        roles = GitHubCollector(config(), api, clock=lambda: NOW)._bots(NOW)["roles"]
        self.assertEqual(roles["explorer"]["state"], "working")
        self.assertEqual([row["runner_name"] for row in roles["explorer"]["active"]],
                         ["lane-qae-2-1", "lane-qae-7-1"])
        self.assertEqual([row["name"] for row in roles["verifier"]["active"]], ["verify (2)"])

    def test_completed_shards_are_in_the_history(self):
        api = BotAPI(completed=[run(1, "completed")], jobs={jobs_endpoint(1): [
            job(1, "explore (3)", "completed", "success", runner="lane-qae-3-1")]})
        roles = GitHubCollector(config(), api, clock=lambda: NOW)._bots(NOW)["roles"]
        self.assertEqual([row["name"] for row in roles["explorer"]["recent_2h"]], ["explore (3)"])

    def test_pull_request_runs_count_shards(self):
        payloads = {REPO: {"pulls": [{"runs": [{"path": ".github/workflows/" + WORKFLOW, "jobs": [
            job(1, "explore (4)", "completed", "success"), job(1, "explorer notes", "completed", "success")]}]}]}}
        found = pulls_bot_jobs(payloads, config().bots)
        self.assertEqual([(role, row["name"]) for _, role, _, row in found], [("explorer", "explore (4)")])


if __name__ == "__main__":
    unittest.main()
