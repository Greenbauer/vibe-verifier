"""Bot activity semantics, shared workflow scans, and completed-job caching."""

import copy
import unittest
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode

from dashboard.config import BotDefinition, Config
from dashboard.gh_api import ApiError
from dashboard.github import GitHubCollector
from dashboard.service import DashboardService


NOW = datetime(2026, 9, 30, 15, 0, tzinfo=timezone.utc)
REPO = "octocat/example"
WORKFLOW = "shared.yml"


def config():
    return Config("octocat", (REPO,), {
        "reviewer": BotDefinition(WORKFLOW, ("review",)),
        "explorer": BotDefinition(WORKFLOW, ("explore",)),
        "verifier": BotDefinition(WORKFLOW, ("verify",)),
    }, None)


def endpoint(status: str) -> str:
    query = {"status": status}
    if status == "completed":
        query["created"] = ">=" + (NOW - timedelta(days=7)).date().isoformat()
    query["per_page"] = "100"
    return f"repos/{REPO}/actions/workflows/{WORKFLOW}/runs?{urlencode(query)}"


def run(run_id: int, status: str, minutes: int = 1) -> dict:
    return {"id": run_id, "run_attempt": 1, "head_sha": str(run_id) * 40,
            "status": status, "created_at": (NOW - timedelta(minutes=minutes + 1)).isoformat(),
            "updated_at": (NOW - timedelta(minutes=minutes)).isoformat(),
            "repository": {"full_name": REPO}}


def job(run_id: int, name: str, status: str, conclusion=None, minutes: int = 1) -> dict:
    return {"id": run_id * 10, "run_id": run_id, "head_sha": str(run_id) * 40, "name": name,
            "status": status, "conclusion": conclusion,
            "created_at": (NOW - timedelta(minutes=minutes + 2)).isoformat(),
            "started_at": (NOW - timedelta(minutes=minutes + 1)).isoformat(),
            "completed_at": (NOW - timedelta(minutes=minutes)).isoformat() if status == "completed" else None,
            "html_url": f"https://github.com/{REPO}/actions/runs/{run_id}/job/{run_id * 10}",
            "steps": []}


def jobs_endpoint(run_id: int) -> str:
    return f"repos/{REPO}/actions/runs/{run_id}/attempts/1/jobs?per_page=100"


class BotAPI:
    max_calls = 200

    def __init__(self, *, active=None, completed=None, jobs=None):
        self.active = active if active is not None else []
        self.completed = completed if completed is not None else []
        self.jobs = jobs or {}
        self.calls = []
        self.cache_cleared = False

    def clear_cache(self):
        self.cache_cleared = True

    def items(self, requested, key=None):
        self.calls.append(("items", requested, key))
        if requested == f"repos/{REPO}/actions/workflows?per_page=100":
            return [{"path": ".github/workflows/" + WORKFLOW}]
        if requested == endpoint("in_progress"):
            value = self.active
        else:
            value = self.jobs[requested]
        if isinstance(value, Exception):
            raise value
        return copy.deepcopy(value)

    def page_items(self, requested, key=None):
        self.calls.append(("pages", requested, key))
        self.assert_endpoint(requested)
        if isinstance(self.completed, Exception):
            raise self.completed
        yield copy.deepcopy(self.completed), False

    @staticmethod
    def assert_endpoint(requested):
        if requested != endpoint("completed"):
            raise AssertionError(requested)


class BotStates(unittest.TestCase):
    def test_shared_workflow_is_fetched_once_for_all_roles(self):
        api = BotAPI()
        result = GitHubCollector(config(), api, clock=lambda: NOW)._bots(NOW)
        self.assertEqual(sum(call[1] == endpoint("in_progress") for call in api.calls), 1)
        self.assertEqual(sum(call[1] == endpoint("completed") for call in api.calls), 1)
        self.assertTrue(all(row["state"] == "idle" for row in result["roles"].values()))

    def test_queued_job_is_not_working_but_in_progress_job_is(self):
        queued_run = run(1, "in_progress")
        queued = BotAPI(active=[queued_run], jobs={jobs_endpoint(1): [job(1, "review", "queued")]})
        queued_result = GitHubCollector(config(), queued, clock=lambda: NOW)._bots(NOW)
        self.assertEqual(queued_result["roles"]["reviewer"]["state"], "idle")
        self.assertEqual(queued_result["roles"]["reviewer"]["active"], [])

        active = BotAPI(active=[queued_run], jobs={jobs_endpoint(1): [job(1, "review", "in_progress")]})
        active_result = GitHubCollector(config(), active, clock=lambda: NOW)._bots(NOW)
        self.assertEqual(active_result["roles"]["reviewer"]["state"], "working")
        self.assertEqual(active_result["roles"]["reviewer"]["active"][0]["status"], "in_progress")

    def test_unavailable_active_scan_is_unknown_and_history_is_explicitly_partial(self):
        api = BotAPI(active=ApiError("unavailable"), completed=ApiError("request_budget_exhausted"))
        result = GitHubCollector(config(), api, clock=lambda: NOW)._bots(NOW)
        self.assertTrue(result["partial"])
        self.assertEqual(result["roles"]["reviewer"]["state"], "unknown")
        self.assertEqual(result["roles"]["reviewer"]["coverage"],
                         {"active": "unavailable", "history": "partial"})

    def test_newer_success_clears_failure_highlight(self):
        old_failure, new_success = run(1, "completed", 20), run(2, "completed", 10)
        api = BotAPI(completed=[new_success, old_failure], jobs={
            jobs_endpoint(1): [job(1, "review", "completed", "failure", 20)],
            jobs_endpoint(2): [job(2, "review", "completed", "success", 10)],
        })
        result = GitHubCollector(config(), api, clock=lambda: NOW)._bots(NOW)
        reviewer = result["roles"]["reviewer"]
        self.assertEqual([row["category"] for row in reviewer["recent_7d"]], ["success", "failed"])
        self.assertIsNone(reviewer["latest_failure"])

    def test_history_scan_does_not_silently_stop_at_twenty_runs(self):
        runs = [run(number, "completed", number) for number in range(1, 22)]
        jobs = {jobs_endpoint(number): [job(number, "other", "completed", "success", number)]
                for number in range(1, 21)}
        jobs[jobs_endpoint(21)] = [job(21, "review", "completed", "success", 21)]
        outcome = GitHubCollector(config(), BotAPI(completed=runs, jobs=jobs), clock=lambda: NOW)._bot_workflow(
            REPO, WORKFLOW, ["reviewer"], NOW)
        self.assertEqual([row["run_id"] for row in outcome["rows"]["reviewer"]["completed"]], [21])
        self.assertTrue(outcome["history_complete"])


class HistoryBounds(unittest.TestCase):
    def history(self):
        runs = [run(number, "completed", number * 10 if number < 5 else number * 10 + 100)
                for number in range(1, 101)]
        jobs = {jobs_endpoint(row["id"]): [job(row["id"], name, "completed", "success",
                     row["id"] * 10 if row["id"] < 5 else row["id"] * 10 + 100)
                 for name in ("review", "explore", "verify")] for row in runs}
        return runs, jobs

    def test_hundred_run_page_fetches_only_five_job_lists_for_all_roles(self):
        runs, jobs = self.history()
        api = BotAPI(completed=runs, jobs=jobs)
        result = GitHubCollector(config(), api, clock=lambda: NOW)._bots(NOW)
        fetched = [call for call in api.calls if "/attempts/" in call[1]]
        self.assertEqual(len(fetched), 5)
        for role in result["roles"].values():
            self.assertEqual([row["run_id"] for row in role["recent_7d"]], [1, 2, 3, 4, 5])
            self.assertEqual([row["run_id"] for row in role["recent_2h"]], [1, 2, 3, 4])
            self.assertEqual(role["coverage"]["history"], "complete")

    def test_later_page_recent_rerun_is_sorted_before_early_stopping(self):
        runs, jobs = self.history()
        rerun = run(101, "completed", 5)
        rerun["created_at"] = (NOW - timedelta(days=3)).isoformat()
        jobs[jobs_endpoint(101)] = [job(101, name, "completed", "failure", 5)
                                   for name in ("review", "explore", "verify")]

        class PagedAPI(BotAPI):
            def page_items(self, requested, key=None):
                self.assert_endpoint(requested)
                yield copy.deepcopy(runs), True
                yield [copy.deepcopy(rerun)], False

        api = PagedAPI(jobs=jobs)
        result = GitHubCollector(config(), api, clock=lambda: NOW)._bots(NOW)
        self.assertEqual([row["run_id"] for row in result["roles"]["reviewer"]["recent_7d"]],
                         [101, 1, 2, 3, 4])
        self.assertEqual(sum("/attempts/" in call[1] for call in api.calls), 5)

    def test_absent_workflow_listing_preserves_other_repository_history_and_cache(self):
        other = "octocat/without-bots"
        selected = config()
        selected = Config(selected.owner, (REPO, other), selected.bots, None)

        class AbsentAPI(BotAPI):
            def items(self, requested, key=None):
                if requested == f"repos/{other}/actions/workflows?per_page=100":
                    self.calls.append(("items", requested, key))
                    return []
                return super().items(requested, key)

        api = AbsentAPI(completed=[run(1, "completed")],
                        jobs={jobs_endpoint(1): [job(1, "review", "completed", "success")]})
        collector = GitHubCollector(selected, api, clock=lambda: NOW)
        first = collector._bots(NOW)
        self.assertFalse(first["partial"])
        self.assertEqual(first["errors"], [])
        self.assertTrue(all(row["state"] == "idle" for row in first["roles"].values()))
        self.assertEqual(len(first["roles"]["reviewer"]["recent_7d"]), 1)
        self.assertFalse(DashboardService._source_error_codes({"bots": first}))
        collector._jobs = {}
        second = collector._bots(NOW)
        self.assertEqual(second["roles"]["reviewer"]["recent_7d"], first["roles"]["reviewer"]["recent_7d"])
        self.assertEqual(sum("/attempts/" in call[1] for call in api.calls), 1)
        self.assertEqual(sum("/actions/workflows?" in call[1] for call in api.calls), 2)
        self.assertFalse(any(f"repos/{other}/actions/workflows/" in call[1] for call in api.calls))
        collector.clear_private_cache()
        self.assertEqual(collector._workflows, {})
        self.assertEqual(collector._completed_jobs, {})
        self.assertTrue(api.cache_cleared)

    def test_failed_workflow_discovery_is_unknown_but_positive_activity_survives(self):
        other = "octocat/unreadable"
        selected = config()
        selected = Config(selected.owner, (REPO, other), selected.bots, None)

        class FailedAPI(BotAPI):
            def items(self, requested, key=None):
                if requested == f"repos/{other}/actions/workflows?per_page=100":
                    raise ApiError("forbidden")
                return super().items(requested, key)

        api = FailedAPI(active=[run(1, "in_progress")],
                        jobs={jobs_endpoint(1): [job(1, "review", "in_progress")]})
        result = GitHubCollector(selected, api, clock=lambda: NOW)._bots(NOW)
        self.assertTrue(result["partial"])
        self.assertEqual(result["roles"]["reviewer"]["state"], "working")
        self.assertEqual(result["roles"]["explorer"]["state"], "unknown")
        self.assertIn("forbidden", DashboardService._source_error_codes({"bots": result}))

    def test_workflow_cache_age_does_not_slide_on_hits(self):
        wall = [NOW]
        api = BotAPI()
        collector = GitHubCollector(config(), api, clock=lambda: wall[0])
        collector._workflow_names(REPO)
        wall[0] += timedelta(minutes=4)
        collector._workflow_names(REPO)
        wall[0] += timedelta(minutes=2)
        collector._workflow_names(REPO)
        self.assertEqual(sum("/actions/workflows?" in call[1] for call in api.calls), 2)


class CompletedJobCache(unittest.TestCase):
    def test_only_completed_jobs_are_reused_across_refreshes(self):
        active_run, completed_run = run(1, "in_progress"), run(2, "completed")
        api = BotAPI(active=[active_run], completed=[completed_run], jobs={
            jobs_endpoint(1): [job(1, "review", "in_progress")],
            jobs_endpoint(2): [job(2, "review", "completed", "success")],
        })
        collector = GitHubCollector(config(), api, clock=lambda: NOW)
        collector._bots(NOW)
        collector._jobs = {}
        collector._bots(NOW)
        self.assertEqual(sum(call[1] == jobs_endpoint(1) for call in api.calls), 2)
        self.assertEqual(sum(call[1] == jobs_endpoint(2) for call in api.calls), 1)

    def test_completed_cache_expires_after_seven_days(self):
        completed_run = run(2, "completed")
        api = BotAPI(jobs={jobs_endpoint(2): [job(2, "review", "completed", "success")]})
        collector = GitHubCollector(config(), api, clock=lambda: NOW)
        collector._run_jobs(REPO, completed_run)
        collector._jobs = {}
        collector._prune_job_cache(NOW + timedelta(days=8))
        collector._run_jobs(REPO, completed_run)
        self.assertEqual(sum(call[1] == jobs_endpoint(2) for call in api.calls), 2)


if __name__ == "__main__":
    unittest.main()
