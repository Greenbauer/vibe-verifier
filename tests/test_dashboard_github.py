"""Exact current-head joins, status accounting, pagination, and stale caching."""

import copy
import json
import subprocess
import unittest
from datetime import datetime, timedelta, timezone

from dashboard.config import BotDefinition, Config
from dashboard.gh_api import ApiError, GitHubAPI
from dashboard.github import GitHubCollector, recent_bot_runs, step_summary
from dashboard.service import DashboardService


NOW = datetime(2026, 9, 30, 15, 0, tzinfo=timezone.utc)
REPO = "octocat/example"
SHA = "a" * 40


def config():
    return Config("octocat", (REPO,), {
        "reviewer": BotDefinition("review.yml", ("review",)),
        "explorer": BotDefinition("explore.yml", ("explore",)),
        "verifier": BotDefinition("verify.yml", ("verify",)),
    }, None)


class FakeAPI:
    max_calls = 200

    def __init__(self, items=None, ones=None):
        self.item_values = items or {}
        self.one_values = ones or {}
        self.calls = []

    def items(self, endpoint, key=None):
        self.calls.append(("items", endpoint, key))
        value = self.item_values[endpoint]
        if isinstance(value, Exception):
            raise value
        return copy.deepcopy(value)

    def one(self, endpoint):
        self.calls.append(("one", endpoint))
        value = self.one_values[endpoint]
        if isinstance(value, Exception):
            raise value
        return copy.deepcopy(value)


def endpoints():
    return {
        "suites": f"repos/{REPO}/commits/{SHA}/check-suites?per_page=100",
        "checks": f"repos/{REPO}/commits/{SHA}/check-runs?per_page=100&filter=all",
        "status": f"repos/{REPO}/commits/{SHA}/status",
        "runs": f"repos/{REPO}/actions/runs?head_sha={SHA}&per_page=100",
        "jobs2": f"repos/{REPO}/actions/runs/11/attempts/2/jobs?per_page=100",
        "pull": f"repos/{REPO}/pulls/3",
    }


def joined_api(current_sha=SHA):
    paths = endpoints()
    run = {"id": 11, "run_attempt": 2, "check_suite_id": 7, "head_sha": SHA, "name": "CI",
           "path": ".github/workflows/ci.yml", "status": "in_progress", "conclusion": None,
           "created_at": "2026-09-30T14:50:00Z", "run_started_at": "2026-09-30T14:51:00Z",
           "updated_at": "2026-09-30T14:59:00Z", "html_url": f"https://github.com/{REPO}/actions/runs/11",
           "repository": {"full_name": REPO}}
    return FakeAPI(items={
        paths["suites"]: [{"id": 7, "head_sha": SHA}],
        paths["checks"]: [{"id": 91, "name": "External analysis", "status": "completed", "conclusion": "failure",
                            "started_at": "2026-09-30T14:52:00Z", "completed_at": "2026-09-30T14:53:00Z",
                            "details_url": f"https://github.com/{REPO}/checks/91", "check_suite": {"id": 7},
                            "app": {"id": 40, "name": "Example checks"}}],
        paths["runs"]: [{**run, "run_attempt": 1}, run],
        paths["jobs2"]: [{"id": 21, "run_id": 11, "head_sha": SHA, "name": "test", "status": "in_progress",
                           "conclusion": None, "created_at": "2026-09-30T14:50:00Z",
                           "started_at": "2026-09-30T14:51:00Z", "completed_at": None,
                           "html_url": f"https://github.com/{REPO}/actions/runs/11/job/21",
                           "steps": [
                               {"number": 1, "name": "setup", "status": "completed", "conclusion": "success",
                                "started_at": "2026-09-30T14:51:00Z", "completed_at": "2026-09-30T14:52:00Z"},
                               {"number": 2, "name": "optional", "status": "completed", "conclusion": "skipped",
                                "started_at": "2026-09-30T14:52:00Z", "completed_at": "2026-09-30T14:52:00Z"},
                               {"number": 3, "name": "old", "status": "completed", "conclusion": "cancelled",
                                "started_at": "2026-09-30T14:52:00Z", "completed_at": "2026-09-30T14:52:30Z"},
                               {"number": 4, "name": "run", "status": "in_progress", "conclusion": None,
                                "started_at": "2026-09-30T14:52:30Z", "completed_at": None},
                           ]}],
    }, ones={
        paths["status"]: {"statuses": [{"id": 5, "context": "legacy/status", "state": "success",
                                          "created_at": "2026-09-30T14:40:00Z", "updated_at": "2026-09-30T14:41:00Z",
                                          "target_url": f"https://github.com/{REPO}/status/5"}]},
        paths["pull"]: {"head": {"sha": current_sha}},
    })


def pull_row():
    return {"number": 3, "title": "Improve example", "user": {"login": "octocat"},
            "created_at": "2026-09-29T15:00:00Z", "updated_at": "2026-09-30T14:00:00Z",
            "head": {"sha": SHA}, "draft": False, "html_url": f"https://github.com/{REPO}/pull/3"}


class CurrentHeadJoin(unittest.TestCase):
    def test_suite_identity_head_sha_and_latest_attempt_drive_the_join(self):
        api = joined_api()
        result = GitHubCollector(config(), api, clock=lambda: NOW)._pull(REPO, pull_row(), {"subscription": "subscribed"})
        self.assertFalse(result["head_changed"])
        self.assertEqual(result["runs"][0]["attempt"], 2)
        self.assertEqual(result["runs"][0]["suite_id"], 7)
        self.assertEqual(result["checks"][0]["provider"], "Example checks")
        self.assertEqual(result["statuses"][0]["name"], "legacy/status")
        self.assertNotIn(("items", f"repos/{REPO}/actions/runs/11/attempts/1/jobs?per_page=100", "jobs"), api.calls)

    def test_step_counts_keep_skipped_cancelled_pending_and_unknown_separate(self):
        result = GitHubCollector(config(), joined_api(), clock=lambda: NOW)._pull(
            REPO, pull_row(), {"subscription": "subscribed"})
        summary = result["runs"][0]["step_summary"]
        self.assertEqual((summary["completed"], summary["total"], summary["remaining"], summary["percent"]), (3, 4, 1, 75))
        self.assertEqual(summary["counts"], {"success": 1, "failed": 0, "skipped": 1,
                                              "cancelled": 1, "pending": 1, "unknown": 0})
        unknown = step_summary([{"steps": None}])
        self.assertFalse(unknown["known"])
        self.assertIsNone(unknown["percent"])

    def test_a_skipped_job_with_no_steps_keeps_the_run_total_known(self):
        steps = [{"status": "completed", "category": "success"}]
        summary = step_summary([{"status": "completed", "steps": steps},
                                {"status": "completed", "conclusion": "skipped", "steps": []}])
        self.assertTrue(summary["known"])
        self.assertEqual((summary["completed"], summary["total"], summary["percent"]), (1, 1, 100))
        self.assertFalse(step_summary([{"status": "queued", "steps": []}])["known"])

    def test_a_head_change_drops_all_old_evidence_instead_of_showing_it_as_current(self):
        result = GitHubCollector(config(), joined_api("b" * 40), clock=lambda: NOW)._pull(
            REPO, pull_row(), {"subscription": "subscribed"})
        self.assertTrue(result["head_changed"])
        self.assertEqual((result["checks"], result["statuses"], result["runs"]), ([], [], []))
        self.assertIn("Head changed", result["attention_reason"])

    def test_invalid_external_links_are_removed(self):
        api = joined_api()
        api.item_values[endpoints()["checks"]][0]["details_url"] = "https://example.invalid/steal"
        result = GitHubCollector(config(), api, clock=lambda: NOW)._pull(REPO, pull_row(), {"subscription": "subscribed"})
        self.assertIsNone(result["checks"][0]["details_url"])

    def test_passing_rerun_replaces_prior_failure_for_the_same_provider(self):
        api = joined_api()
        failed = api.item_values[endpoints()["checks"]][0]
        api.item_values[endpoints()["checks"]].append({**failed, "id": 92, "conclusion": "success",
                                                        "started_at": "2026-09-30T14:54:00Z",
                                                        "completed_at": "2026-09-30T14:55:00Z"})
        result = GitHubCollector(config(), api, clock=lambda: NOW)._pull(
            REPO, pull_row(), {"subscription": "subscribed"})
        self.assertEqual([(row["id"], row["category"]) for row in result["checks"]], [(92, "success")])
        self.assertFalse(result["attention"])

    def test_same_check_name_from_two_providers_is_not_deduplicated(self):
        api = joined_api()
        original = api.item_values[endpoints()["checks"]][0]
        api.item_values[endpoints()["checks"]].append({**original, "id": 93,
                                                        "app": {"id": 41, "name": "Another provider"}})
        result = GitHubCollector(config(), api, clock=lambda: NOW)._pull(
            REPO, pull_row(), {"subscription": "subscribed"})
        self.assertEqual({row["provider"] for row in result["checks"]},
                         {"Example checks", "Another provider"})

    def test_a_rerun_of_the_same_workflow_on_the_head_supersedes_the_older_run(self):
        # Every event that starts a workflow on the same head makes a new run and suite.
        api = joined_api()
        paths = endpoints()
        older = {**api.item_values[paths["runs"]][1], "id": 12, "run_attempt": 1, "check_suite_id": 8,
                 "status": "completed", "conclusion": "failure", "created_at": "2026-09-30T14:40:00Z"}
        other = {**older, "id": 13, "check_suite_id": 9, "name": "Lint", "path": ".github/workflows/lint.yml",
                 "status": "completed", "conclusion": "success"}
        api.item_values[paths["runs"]] += [older, other]
        step = {"number": 1, "name": "run", "status": "completed", "conclusion": "failure",
                "started_at": "2026-09-30T14:41:00Z", "completed_at": "2026-09-30T14:42:00Z"}
        job = {"head_sha": SHA, "name": "test", "status": "completed", "conclusion": "failure",
               "created_at": "2026-09-30T14:40:00Z", "started_at": "2026-09-30T14:41:00Z",
               "completed_at": "2026-09-30T14:42:00Z", "steps": [step, {**step, "number": 2}]}
        api.item_values[f"repos/{REPO}/actions/runs/12/attempts/1/jobs?per_page=100"] = [{**job, "id": 22, "run_id": 12}]
        api.item_values[f"repos/{REPO}/actions/runs/13/attempts/1/jobs?per_page=100"] = [
            {**job, "id": 23, "run_id": 13, "conclusion": "success", "steps": [{**step, "conclusion": "success"}]}]
        api.item_values[paths["suites"]] += [{"id": 8, "head_sha": SHA}, {"id": 9, "head_sha": SHA}]
        check = {"name": "test", "status": "completed", "app": {"id": 15368, "name": "GitHub Actions"},
                 "started_at": "2026-09-30T14:41:00Z", "completed_at": "2026-09-30T14:42:00Z"}
        api.item_values[paths["checks"]] += [
            {**check, "id": 31, "check_suite": {"id": 7}, "status": "in_progress", "completed_at": None},
            {**check, "id": 32, "conclusion": "failure", "check_suite": {"id": 8}},
            {**check, "id": 33, "conclusion": "success", "check_suite": {"id": 9}}]
        result = GitHubCollector(config(), api, clock=lambda: NOW)._pull(REPO, pull_row(), {"subscription": "subscribed"})
        self.assertEqual(sorted(run["id"] for run in result["runs"]), [11, 13])
        self.assertEqual(sorted(row["id"] for row in result["checks"] if row["name"] == "test"), [31, 33])
        summary = [run["step_summary"] for run in result["runs"] if run["id"] == 11][0]
        self.assertEqual((summary["completed"], summary["total"]), (3, 4))
        self.assertEqual(sum("/check-runs" in call[1] for call in api.calls), 1)

    def test_check_runs_come_from_one_head_listing_and_only_from_this_heads_suites(self):
        api = joined_api()
        paths = endpoints()
        api.item_values[paths["suites"]] += [{"id": 8, "head_sha": SHA}, {"id": 9, "head_sha": "other"}]
        check = {"status": "completed", "conclusion": "success", "app": {"id": 40, "name": "Example checks"},
                 "started_at": "2026-09-30T14:52:00Z", "completed_at": "2026-09-30T14:53:00Z"}
        api.item_values[paths["checks"]] += [{**check, "id": 92, "name": "Second suite", "check_suite": {"id": 8}},
                                             {**check, "id": 93, "name": "Other head", "check_suite": {"id": 9}},
                                             {**check, "id": 94, "name": "Unlisted suite", "check_suite": {"id": 10}}]
        checks, suites = GitHubCollector(config(), api, clock=lambda: NOW)._check_runs(REPO, SHA)
        self.assertEqual(sorted(row["id"] for row in checks), [91, 92])
        self.assertEqual(suites, {7, 8})
        self.assertEqual([call[1] for call in api.calls if "/check-runs" in call[1]], [paths["checks"]])


HISTORY = "orgs/octocat/rulesets/5/history"


def required_workflow_rule(sha="c" * 40):
    return {"type": "workflows", "ruleset_id": 5, "ruleset_source": "octocat", "ruleset_source_type": "Organization",
            "parameters": {"workflows": [{"path": ".github/workflows/ci.yml", "repository_id": 9, "sha": sha}]}}


def pinned_api(run_created, *, required=True):
    """The joined fixture whose CI run comes from a ruleset-required workflow; the pin moved to "c" at 14:45."""
    api = joined_api()
    for run in api.item_values[endpoints()["runs"]]:
        run["created_at"] = run_created
        run["workflow_url"] = f"https://api.github.com/repos/{REPO}/actions/%s/1" % (
            "required_workflows" if required else "workflows")
    api.item_values[f"{HISTORY}?per_page=100"] = [
        {"version_id": 1, "updated_at": "2026-09-30T14:00:00Z"}, {"version_id": 3, "updated_at": "2026-09-30T14:45:00Z"},
        {"version_id": 2, "updated_at": "2026-09-30T14:30:00Z"}]
    for version, sha in ((1, "b"), (2, "b"), (3, "c")):
        api.one_values[f"{HISTORY}/{version}"] = {"state": {"rules": [required_workflow_rule(sha * 40)]}}
    return api


class BotHistory(unittest.TestCase):
    def test_two_hour_boundary_is_inclusive_newest_first_and_capped_at_five(self):
        rows = []
        for minutes in (120, 90, 60, 40, 20, 10, 0):
            rows.append({"completed_at": (NOW - timedelta(minutes=minutes)).isoformat(), "marker": minutes})
        result = recent_bot_runs(rows, NOW, 2)
        self.assertEqual([row["marker"] for row in result], [0, 10, 20, 40, 60])
        self.assertEqual(len(result), 5)

    def test_rows_before_boundary_and_future_rows_are_excluded(self):
        rows = [{"completed_at": (NOW - timedelta(hours=2, seconds=1)).isoformat(), "marker": "old"},
                {"completed_at": (NOW + timedelta(seconds=1)).isoformat(), "marker": "future"}]
        self.assertEqual(recent_bot_runs(rows, NOW, 2), [])


class SubscriptionEvidence(unittest.TestCase):
    def test_direct_manifest_is_subscribed_but_absence_is_unknown_not_false(self):
        manifest = f"repos/{REPO}/contents/.vibe-verifier"
        stub = f"repos/{REPO}/contents/.github/workflows/vibe-verifier.yml"
        api = FakeAPI(ones={manifest: {"type": "file", "content": "Z2l0bGVha3MK"}, stub: ApiError("not_found")})
        self.assertEqual(GitHubCollector(config(), api).inventory(REPO)["subscription"], "subscribed")
        absent = FakeAPI(ones={manifest: ApiError("not_found"), stub: ApiError("not_found")})
        result = GitHubCollector(config(), absent).inventory(REPO)
        self.assertEqual(result, {"subscription": "unknown", "evidence": "wrapper_or_ruleset_not_resolved"})


class RepositoryCoverage(unittest.TestCase):
    def test_known_open_pull_survives_current_head_detail_failure(self):
        pulls = f"repos/{REPO}/pulls?state=open&per_page=100"
        suites = endpoints()["suites"]
        api = FakeAPI(items={pulls: [pull_row()], suites: ApiError("unavailable")})
        result = GitHubCollector(config(), api, clock=lambda: NOW)._repository(
            REPO, {"subscription": "subscribed"})
        self.assertEqual([row["number"] for row in result["pulls"]], [3])
        self.assertFalse(result["pulls"][0]["evidence_available"])
        self.assertEqual(result["pulls"][0]["source_error"], "unavailable")
        self.assertEqual(result["errors"], [{"pull": 3, "code": "unavailable"}])


def http(status, body="", etag=None, extra=()):
    """What `gh api --include` prints: a status line, CRLF headers, a blank line, then the body."""
    lines = ["Content-Type: application/json", *(["Etag: " + etag] if etag else []), *extra]
    return "HTTP/2.0 %d Status\n%s\r\n%s" % (status, "".join(line + "\r\n" for line in lines), body)


class ApiTransport(unittest.TestCase):
    def test_each_explicit_page_consumes_one_request_budget_unit(self):
        commands = []

        def runner(command, **kwargs):
            commands.append(command)
            page = 2 if "page=2" in command[3] else 1
            rows = [{"id": number} for number in range(100)] if page == 1 else [{"id": 100}]
            return subprocess.CompletedProcess(command, 0, http(200, json.dumps(rows)), "")
        api = GitHubAPI(runner=runner)
        self.assertEqual(len(api.items("repos/octocat/example/pulls")), 101)
        self.assertEqual(api.calls, 2)
        self.assertEqual(commands[0], ["gh", "api", "--include", "repos/octocat/example/pulls?per_page=100&page=1"])
        self.assertEqual(commands[1], ["gh", "api", "--include", "repos/octocat/example/pulls?per_page=100&page=2"])

    def test_unchanged_response_is_reused_and_returns_its_budget_unit(self):
        commands, answers = [], [(0, http(200, '{"state": "pending"}', 'W/"v1"'), ""),
                                 (1, http(304, etag='"v1"'), "gh: HTTP 304"),
                                 (0, http(200, '{"state": "success"}', 'W/"v2"'), "")]

        def runner(command, **kwargs):
            commands.append(command)
            return subprocess.CompletedProcess(command, *answers[len(commands) - 1])
        api = GitHubAPI(runner=runner)
        api.begin()
        self.assertEqual(api.one("repos/o/r/commits/a/status"), {"state": "pending"})
        api.begin()
        unchanged = api.one("repos/o/r/commits/a/status")
        self.assertEqual(unchanged, {"state": "pending"})
        self.assertEqual(api.calls, 0)
        self.assertEqual(commands[1][-2:], ["--header", 'If-None-Match: W/"v1"'])
        unchanged["state"] = "mutated by a caller"
        api.begin()
        self.assertEqual(api.one("repos/o/r/commits/a/status"), {"state": "success"})
        self.assertEqual(api.calls, 1)

    def test_cache_keeps_only_last_refresh_entries_and_clears_on_request(self):
        def runner(command, **kwargs):
            return subprocess.CompletedProcess(command, 0, http(200, "{}", '"tag"'), "")
        api = GitHubAPI(runner=runner)
        api.begin()
        api.one("old")
        api.one("kept")
        api.begin()
        api.one("kept")
        api.begin()
        self.assertEqual(set(api._cache), {"kept"})
        api.clear_cache()
        self.assertEqual(api._cache, {})

    def test_failure_on_a_cached_endpoint_is_not_served_from_cache(self):
        answers = [(0, http(200, "{}", '"tag"'), ""), (1, "", "HTTP 404: Not Found")]

        def runner(command, **kwargs):
            return subprocess.CompletedProcess(command, *answers.pop(0))
        api = GitHubAPI(runner=runner)
        api.one("repos/o/r/pulls/1")
        with self.assertRaisesRegex(ApiError, "not_found"):
            api.one("repos/o/r/pulls/1")
        self.assertEqual(api.calls, 2)

    def test_rate_limit_errors_are_sanitized_and_back_off(self):
        def runner(command, **kwargs):
            return subprocess.CompletedProcess(command, 1, "", "secret token: API rate limit exceeded")
        api = GitHubAPI(runner=runner, clock=lambda: 100)
        with self.assertRaisesRegex(ApiError, "rate_limited") as caught:
            api.items("rate_limit")
        self.assertNotIn("secret", str(caught.exception))
        self.assertEqual(api.backoff_until, 160)

    def test_a_spent_hourly_counter_pauses_only_its_endpoint_family_until_the_reported_reset(self):
        # GitHub meters some endpoint families against a separate counter (seen 2026-10-05: attempt
        # jobs spent while pull requests had thousands left), so one family must not stop the rest.
        now, commands = [100], []

        def runner(command, **kwargs):
            commands.append(command[3])
            if "/attempts/" in command[3]:
                return subprocess.CompletedProcess(command, 1, http(403, "{}", extra=(
                    "X-Ratelimit-Remaining: 0", "X-Ratelimit-Reset: 700")), "gh: API rate limit exceeded for user ID 1.")
            return subprocess.CompletedProcess(command, 0, http(200, "{}", extra=("X-Ratelimit-Remaining: 3370",)), "")
        api = GitHubAPI(runner=runner, clock=lambda: now[0])
        api.begin()
        with self.assertRaisesRegex(ApiError, "rate_limited"):
            api.one("repos/o/r/actions/runs/1/attempts/1/jobs")
        self.assertEqual(api.one("repos/o/r/pulls/1"), {})
        with self.assertRaisesRegex(ApiError, "rate_limited"):
            api.one("repos/o/other/actions/runs/2/attempts/3/jobs")
        self.assertEqual(len(commands), 2)
        self.assertEqual(api.backoff_until, 0)
        api.begin()
        self.assertEqual(api.lowest_remaining, None)
        now[0] = 700
        api.begin()
        with self.assertRaisesRegex(ApiError, "rate_limited"):
            api.one("repos/o/r/actions/runs/1/attempts/1/jobs")
        self.assertEqual(len(commands), 3)

    def test_the_lowest_remaining_count_on_any_response_is_reported(self):
        answers = [(0, http(200, "{}", extra=("X-Ratelimit-Remaining: 3370",)), ""),
                   (1, http(403, "{}", extra=("X-Ratelimit-Remaining: 0", "X-Ratelimit-Reset: 700")),
                    "gh: API rate limit exceeded"),
                   (0, http(200, "{}", extra=("X-Ratelimit-Remaining: 3369",)), "")]
        api = GitHubAPI(runner=lambda command, **kwargs: subprocess.CompletedProcess(command, *answers.pop(0)),
                        clock=lambda: 100)
        api.begin()
        api.one("repos/o/r/pulls/1")
        with self.assertRaises(ApiError):
            api.one("repos/o/r/actions/workflows")
        api.one("repos/o/r/pulls/2")
        self.assertEqual(api.lowest_remaining, 0)

    def test_request_budget_is_a_hard_bound(self):
        api = GitHubAPI(max_calls=0)
        with self.assertRaisesRegex(ApiError, "request_budget_exhausted"):
            api.items("anything")

    def test_authentication_failure_is_sanitized_and_does_not_retry_pages(self):
        def runner(command, **kwargs):
            return subprocess.CompletedProcess(command, 1, "", "secret value: HTTP 401 authentication required")
        api = GitHubAPI(runner=runner)
        with self.assertRaisesRegex(ApiError, "authentication_failed") as caught:
            api.items("repos/octocat/example/pulls")
        self.assertNotIn("secret", str(caught.exception))
        self.assertEqual(api.calls, 1)


class FakeCollector:
    class API:
        max_calls = 200
    api = API()

    def __init__(self, values):
        self.values = iter(values)

    def collect(self, inventory=None):
        value = next(self.values)
        if isinstance(value, Exception):
            raise value
        return copy.deepcopy(value)


def source_sample(at):
    return {"owner": "octocat", "sampled_at": at.isoformat(),
            "repositories": [{"repository": REPO, "subscription": "subscribed", "pulls": [{"number": 1}], "errors": []}],
            "coverage": {"selected": 1, "readable": 1, "label": "Selected repositories",
                         "inventory": {REPO: {"subscription": "subscribed"}}},
            "bots": {"partial": False, "roles": {}}, "errors": [], "partial": False,
            "api": {"calls": 8, "max_calls": 200}}


class CacheTruthfulness(unittest.TestCase):
    def test_transient_failure_returns_stale_data_without_advancing_source_timestamp(self):
        collector = FakeCollector([source_sample(NOW), ApiError("rate_limited")])
        service = DashboardService(config(), collector, monotonic=lambda: 10,
                                   wall_clock=lambda: NOW + timedelta(seconds=30))
        first = service.snapshot(force=True)["github"]
        second = service.snapshot(force=True)["github"]
        self.assertEqual(second["sampled_at"], first["sampled_at"])
        self.assertTrue(second["repositories"][0]["stale"])
        self.assertEqual(second["repositories"][0]["source_error"], "rate_limited")

    def test_revocation_does_not_keep_derived_repository_data(self):
        collector = FakeCollector([source_sample(NOW), ApiError("forbidden")])
        service = DashboardService(config(), collector, monotonic=lambda: 10, wall_clock=lambda: NOW)
        service.snapshot(force=True)
        result = service.snapshot(force=True)["github"]["repositories"][0]
        self.assertTrue(result["unavailable"])
        self.assertEqual(result["pulls"], [])
        self.assertIsNone(result["sampled_at"])


if __name__ == "__main__":
    unittest.main()
