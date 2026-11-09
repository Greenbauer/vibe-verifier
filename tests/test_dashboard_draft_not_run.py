"""A draft's skipped test jobs, and the test summary that failed only because of them, read as
not run (category "draft"), never as failed. dashboard/draft_checks.py mark_draft_not_run, run by dashboard/github.py."""

import unittest

from dashboard.draft_checks import mark_draft_not_run
from dashboard.github import GitHubCollector
from tests.test_dashboard_github import NOW, REPO, SHA, FakeAPI, config, endpoints, pull_row


def job(job_id, name, conclusion):
    return {"id": job_id, "run_id": 11, "head_sha": SHA, "name": name, "status": "completed",
            "conclusion": conclusion, "created_at": "2026-09-30T14:50:00Z",
            "started_at": "2026-09-30T14:51:00Z", "completed_at": "2026-09-30T14:51:02Z",
            "html_url": f"https://github.com/{REPO}/actions/runs/11/job/{job_id}", "steps": []}


def draft_api(jobs, draft=True):
    """A workflow on a draft that skips its test jobs: the design check ran, the test jobs skipped, both summaries failed."""
    paths = endpoints()
    run = {"id": 11, "run_attempt": 2, "check_suite_id": 7, "head_sha": SHA, "name": "Code",
           "path": ".github/workflows/ci.yml", "status": "completed", "conclusion": "failure",
           "created_at": "2026-09-30T14:50:00Z", "run_started_at": "2026-09-30T14:50:00Z",
           "updated_at": "2026-09-30T14:52:00Z", "html_url": f"https://github.com/{REPO}/actions/runs/11",
           "repository": {"full_name": REPO}}
    checks = [{"id": row["id"], "name": row["name"], "status": "completed", "conclusion": row["conclusion"],
               "started_at": row["started_at"], "completed_at": row["completed_at"], "check_suite": {"id": 7},
               "details_url": row["html_url"], "app": {"id": 15368, "name": "GitHub Actions"}} for row in jobs]
    return FakeAPI(items={paths["suites"]: [{"id": 7, "head_sha": SHA}], paths["checks"]: checks,
                          paths["runs"]: [run], paths["jobs2"]: jobs},
                   ones={paths["status"]: {"statuses": []}, paths["pull"]: {"head": {"sha": SHA}, "draft": draft}})


def collect(jobs, draft=True):
    row = {**pull_row(), "draft": draft}
    return GitHubCollector(config(), draft_api(jobs, draft), clock=lambda: NOW)._pull(REPO, row, {"subscription": "subscribed"})


def categories(result):
    checks = {row["name"]: row["category"] for row in result["checks"]}
    jobs = {row["name"]: row["category"] for row in result["runs"][0]["jobs"]}
    return checks, jobs


SKIPPED_TESTS = [job(1, "Design tokens", "success"), job(2, "Lint and unit tests", "skipped"),
                 job(3, "Repo rules", "skipped"), job(4, "All tests", "failure"), job(5, "tests", "failure")]


class DraftNotRun(unittest.TestCase):
    def test_a_drafts_skipped_tests_and_their_summaries_are_not_run_and_need_no_attention(self):
        result = collect(SKIPPED_TESTS)
        checks, jobs = categories(result)
        expected = {"Design tokens": "success", "Lint and unit tests": "draft", "Repo rules": "draft",
                    "All tests": "draft", "tests": "draft"}
        self.assertEqual(checks, expected)
        self.assertEqual(jobs, expected)
        self.assertFalse(result["attention"])
        self.assertFalse(result["merge_ready"])

    def test_a_job_that_ran_and_failed_keeps_the_draft_and_its_summaries_red(self):
        result = collect([job(1, "Design tokens", "failure"), *SKIPPED_TESTS[1:]])
        checks, _ = categories(result)
        self.assertEqual([checks[name] for name in ("Design tokens", "All tests", "tests", "Lint and unit tests")],
                         ["failed", "failed", "failed", "skipped"])
        self.assertTrue(result["attention"])

    def test_a_summary_with_no_skipped_job_failed_on_its_own(self):
        result = collect([job(1, "Lint and unit tests", "success"), job(4, "All tests", "failure")])
        self.assertEqual(categories(result)[0]["All tests"], "failed")

    def test_the_same_checks_on_a_pull_request_ready_for_review_stay_failed(self):
        result = collect(SKIPPED_TESTS, draft=False)
        checks, _ = categories(result)
        self.assertEqual([checks[name] for name in ("All tests", "tests", "Lint and unit tests")],
                         ["failed", "failed", "skipped"])
        self.assertTrue(result["attention"])

    def test_only_a_run_with_a_failed_test_summary_is_marked(self):
        runs = [{"jobs": [{"id": 1, "name": "Lint and unit tests", "category": "skipped"},
                          {"id": 2, "name": "All tests", "category": "cancelled"}]}]
        checks = [{"id": 1, "category": "skipped"}]
        mark_draft_not_run(checks, runs)
        self.assertEqual([checks[0]["category"], runs[0]["jobs"][1]["category"]], ["skipped", "cancelled"])


if __name__ == "__main__":
    unittest.main()
