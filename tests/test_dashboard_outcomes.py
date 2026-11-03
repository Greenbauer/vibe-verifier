"""The Outcomes view's reader: merged pull requests read once after they merge, within a budget of
their own, kept across a restart and forgotten on lost access; and the page's node tests."""

import copy
import subprocess
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from dashboard.config import Config
from dashboard.gh_api import ApiError
from dashboard.github import _endpoint
from dashboard.outcomes import OUTCOME_DAYS, OutcomesFeed, OutcomesReader, merged_pull
from dashboard.state_store import StateStore

ROOT = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 11, 3, 15, 0, tzinfo=timezone.utc)
REPO = "octocat/example"
HEAD = "b" * 40
OLD = "a" * 40


def config(repositories=(REPO,)):
    return Config("octocat", repositories, {}, None)


def node(number=7, merged="2026-11-03T12:00:00Z", ready="2026-11-03T10:30:00Z", **extra):
    value = {"number": number, "title": "Faster tests", "url": f"https://github.com/{REPO}/pull/{number}",
             "createdAt": "2026-11-03T09:00:00Z", "mergedAt": merged, "headRefName": "feat/fast",
             "headRefOid": HEAD, "repository": {"nameWithOwner": REPO}, "headRepository": {"nameWithOwner": REPO},
             "timelineItems": {"nodes": [{"createdAt": ready}] if ready else []}}
    return {**value, **extra}


def search(*nodes):
    return {"data": {"search": {"pageInfo": {"hasNextPage": False, "endCursor": None}, "nodes": list(nodes)}}}


def runs_endpoint(number_node=None):
    pull = merged_pull(number_node or node(), "octocat")
    return _endpoint(f"repos/{REPO}/actions/runs", branch=pull["head_ref"],
                     created="%s..%s" % (pull["created_at"], pull["merged_at"]), per_page=100)


def jobs_endpoint(run_id, attempt):
    return _endpoint(f"repos/{REPO}/actions/runs/{run_id}/attempts/{attempt}/jobs", per_page=100)


def run(run_id, sha, attempt=1, status="completed", name="CI", repository=REPO):
    return {"id": run_id, "run_attempt": attempt, "check_suite_id": run_id * 10, "head_sha": sha, "name": name,
            "path": ".github/workflows/ci.yml", "status": status, "created_at": "2026-11-03T10:00:00Z",
            "head_repository": {"full_name": repository}}


def job(job_id, run_id, name, created, started, completed, *, runner="ci-1-1", labels=("self-hosted",), conclusion="success"):
    return {"id": job_id, "run_id": run_id, "name": name, "status": "completed", "conclusion": conclusion,
            "created_at": created, "started_at": started, "completed_at": completed,
            "runner_name": runner, "labels": list(labels), "html_url": f"https://github.com/{REPO}/actions/runs/{run_id}/job/{job_id}"}


class FakeAPI:
    def __init__(self, items=None, graphql=None, low=False, max_calls=100):
        self.item_values, self.graphql_values = items or {}, list(graphql or [])
        self.calls, self.low, self.max_calls, self.cache = [], low, max_calls, []

    def begin(self):
        self.used = 0

    def _spend(self):
        self.used += 1
        if self.used > self.max_calls:
            raise ApiError("request_budget_exhausted")

    def items(self, endpoint, key=None):
        self._spend()
        self.calls.append(endpoint)
        value = self.item_values[endpoint]
        if isinstance(value, Exception):
            raise value
        return copy.deepcopy(value)

    def graphql(self, query, variables, budgeted=True):
        self._spend()
        self.calls.append(("graphql", variables.get("search") or variables.get("login")))
        value = self.graphql_values.pop(0)
        if isinstance(value, Exception):
            raise value
        return value

    def core_reserve_low(self):
        return self.low

    def export_cache(self):
        return self.cache

    def import_cache(self, rows):
        self.cache = rows or []
        return len(self.cache)


class Live:
    """The live collector's parts the reader reads: owner kind, REST counter and finished job lists."""
    def __init__(self, kept=None, low=False):
        self._kind = "User"
        self._job_lock = threading.Lock()
        self._completed_jobs = kept or {}
        self.api = FakeAPI(low=low)


def standard_items():
    """Two pushes: the old head ran CI twice (attempt 2 re-ran test), the merged head ran CI and QA.
    QA waited 10 minutes for a runner; lint was skipped; deploy ran on a GitHub-hosted runner."""
    return {
        runs_endpoint(): [run(1, OLD, attempt=2), run(2, HEAD), run(3, HEAD, name="QA"),
                          run(9, HEAD, repository="fork/example")],
        jobs_endpoint(1, 1): [job(11, 1, "test", "2026-11-03T09:10:00Z", "2026-11-03T09:10:10Z", "2026-11-03T09:20:10Z",
                                  conclusion="failure")],
        # A re-run of failed jobs lists the jobs it kept again; job 11 counts once.
        jobs_endpoint(1, 2): [job(11, 1, "test", "2026-11-03T09:10:00Z", "2026-11-03T09:10:10Z", "2026-11-03T09:20:10Z",
                                  conclusion="failure"),
                              job(12, 1, "test", "2026-11-03T09:30:00Z", "2026-11-03T09:30:10Z", "2026-11-03T09:40:10Z")],
        jobs_endpoint(2, 1): [job(21, 2, "test", "2026-11-03T10:00:00Z", "2026-11-03T10:00:10Z", "2026-11-03T10:10:10Z"),
                              job(22, 2, "lint", "2026-11-03T10:00:00Z", "2026-11-03T10:00:00Z", "2026-11-03T10:00:00Z",
                                  runner=None, labels=(), conclusion="skipped"),
                              job(23, 2, "deploy", "2026-11-03T10:10:10Z", "2026-11-03T10:10:20Z", "2026-11-03T10:12:20Z",
                                  runner="GitHub Actions 3", labels=("ubuntu-latest",))],
        jobs_endpoint(3, 1): [job(31, 3, "explore", "2026-11-03T10:12:20Z", "2026-11-03T10:22:20Z", "2026-11-03T10:42:20Z",
                                  runner="qa-1-1")],
    }


def reader(items=None, graphql=None, live=None, **options):
    api = FakeAPI(items if items is not None else standard_items(), graphql if graphql is not None else [search(node())], **options)
    return OutcomesReader(config(), api, live=live or Live(), clock=lambda: NOW)


class MergedPull(unittest.TestCase):
    def test_ready_time_is_the_last_ready_event_or_the_opening_when_it_never_was_a_draft(self):
        self.assertEqual(merged_pull(node(), "octocat")["ready_at"], "2026-11-03T10:30:00Z")
        self.assertEqual(merged_pull(node(ready=None), "octocat")["ready_at"], "2026-11-03T09:00:00Z")

    def test_a_result_missing_what_the_reader_needs_is_left_out(self):
        for broken in ({"mergedAt": None}, {"headRefOid": None}, {"headRepository": None}, {"number": True}):
            self.assertIsNone(merged_pull(node(**broken), "octocat"))


class Reader(unittest.TestCase):
    def test_a_merged_pull_request_is_read_once_into_its_head_round_and_runner_time_per_check(self):
        outcomes = reader()
        outcomes.collect([REPO])
        view = outcomes.view()
        self.assertEqual((view["available"], view["pending"], view["error"], view["window_days"]), (True, 0, None, OUTCOME_DAYS))
        record = view["pulls"][0]
        self.assertEqual((record["number"], record["ready_at"], record["merged_at"]), (7, "2026-11-03T10:30:00Z", "2026-11-03T12:00:00Z"))
        # The merged head's runs only, the fork's run left out, each job with the times the page splits.
        self.assertEqual([(row["id"], [j["id"] for j in row["jobs"]]) for row in record["head_runs"]], [(2, [21, 22, 23]), (3, [31])])
        self.assertEqual(set(record["head_runs"][1]["jobs"][0]), {"id", "name", "status", "conclusion", "category",
                                                                   "created_at", "started_at", "completed_at"})
        # Every attempt of every run on the branch, the skipped job left out; self-hosted time apart.
        totals = {row["name"]: (row["jobs"], row["seconds"], row["self_hosted_seconds"]) for row in record["checks"]}
        self.assertEqual(totals, {"CI / test": (3, 1800, 1800), "CI / deploy": (1, 120, 0), "QA / explore": (1, 1200, 1200)})
        calls = len(outcomes.api.calls)
        outcomes.api.graphql_values.append(search(node()))
        outcomes.collect([REPO])
        # The second cycle lists merged pull requests and reads nothing else: the record never changes.
        self.assertEqual(outcomes.api.calls[calls:], [("graphql", "is:pr is:merged user:octocat merged:>=2026-10-20 sort:updated-desc")])

    def test_the_search_follows_its_pages_and_refuses_an_answer_without_results(self):
        first = search(node())
        first["data"]["search"]["pageInfo"] = {"hasNextPage": True, "endCursor": "next"}
        outcomes = reader(graphql=[first, search(node(number=8, merged="2026-11-02T12:00:00Z"))])
        outcomes.api.item_values[runs_endpoint(node(number=8, merged="2026-11-02T12:00:00Z"))] = []
        outcomes.collect([REPO])
        self.assertEqual([row["number"] for row in outcomes.view()["pulls"]], [7, 8])
        broken = reader(graphql=[{"data": {"search": None}}])
        broken.collect([REPO])
        self.assertEqual(broken.view()["error"], "invalid_response")

    def test_runs_not_all_finished_leave_no_record_and_are_read_again(self):
        items = standard_items()
        items[runs_endpoint()] = [run(2, HEAD, status="in_progress")]
        outcomes = reader(items)
        outcomes.collect([REPO])
        self.assertEqual((outcomes.view()["pulls"], outcomes.view()["pending"]), ([], 1))

    def test_a_finished_run_the_live_collector_kept_costs_no_call_unless_it_predates_the_runner_kind(self):
        kept = {(REPO, 2, 1): (NOW, [{"id": 21, "name": "test", "status": "completed", "conclusion": "success",
                                       "category": "success", "created_at": "2026-11-03T10:00:00Z",
                                       "started_at": "2026-11-03T10:00:10Z", "completed_at": "2026-11-03T10:10:10Z",
                                       "elapsed_seconds": 600, "runner_name": "ci-1-1", "self_hosted": True, "steps": []}]),
                (REPO, 3, 1): (NOW, [{"id": 31, "name": "explore", "elapsed_seconds": 5, "runner_name": "qa-1-1", "steps": []}])}
        outcomes = reader(live=Live(kept))
        outcomes.collect([REPO])
        self.assertNotIn(jobs_endpoint(2, 1), outcomes.api.calls)
        self.assertIn(jobs_endpoint(3, 1), outcomes.api.calls)
        self.assertNotIn("steps", outcomes.view()["pulls"][0]["head_runs"][0]["jobs"][0])

    def test_a_spent_budget_keeps_what_was_read_and_the_next_cycle_goes_on_without_reading_it_again(self):
        outcomes = reader(max_calls=3)
        outcomes.collect([REPO])
        self.assertEqual((outcomes.view()["pulls"], outcomes.view()["pending"], outcomes.view()["error"]), ([], 1, None))
        first = list(outcomes.api.calls)
        outcomes.api.max_calls = 100
        outcomes.api.graphql_values.append(search(node()))
        outcomes.collect([REPO])
        self.assertEqual(len(outcomes.view()["pulls"]), 1)
        self.assertEqual([call for call in outcomes.api.calls[len(first):] if call in first[1:]], [runs_endpoint()])

    def test_the_window_drops_old_records_and_other_repositories_are_not_read(self):
        outcomes = reader(graphql=[search(node(), node(number=8, repository={"nameWithOwner": "octocat/other"}))])
        outcomes.records[("octocat/example", 1)] = {"repository": REPO, "number": 1,
                                                     "merged_at": (NOW - timedelta(days=OUTCOME_DAYS, minutes=1)).isoformat()}
        outcomes.collect([REPO])
        self.assertEqual([row["number"] for row in outcomes.view()["pulls"]], [7])

    def test_the_reader_spends_a_budget_of_its_own_not_the_live_passes(self):
        from dashboard.outcomes import MAX_CALLS
        live = Live()
        own = OutcomesReader(config(), live=live).api
        self.assertIsNot(own, live.api)
        self.assertEqual(own.max_calls, MAX_CALLS)

    def test_reading_waits_while_the_live_passes_need_the_rest_counter(self):
        outcomes = reader(live=Live(low=True))
        outcomes.collect([REPO])
        self.assertEqual((outcomes.api.calls, outcomes.view()["error"]), ([], "github_budget_low"))

    def test_a_failed_read_keeps_the_records_and_says_why(self):
        outcomes = reader()
        outcomes.collect([REPO])
        outcomes.api.graphql_values.append(ApiError("unavailable"))
        outcomes.collect([REPO])
        self.assertEqual((len(outcomes.view()["pulls"]), outcomes.view()["error"]), (1, "unavailable"))

    def test_lost_access_raises_for_the_feed_to_forget(self):
        outcomes = reader(graphql=[ApiError("forbidden")])
        with self.assertRaises(ApiError):
            outcomes.collect([REPO])


class Feed(unittest.TestCase):
    def feed(self, directory, api):
        store = StateStore(directory, config(), clock=lambda: NOW)
        made = OutcomesReader(config(), api, live=Live(), clock=lambda: NOW)
        return OutcomesFeed(config(), live=Live(), store=store, clock=lambda: NOW, reader=made), store

    def test_records_are_kept_across_a_restart_and_lost_access_removes_them_from_disk(self):
        with tempfile.TemporaryDirectory() as directory:
            feed, store = self.feed(directory, FakeAPI(standard_items(), [search(node())]))
            feed._cycle(feed.reader, feed._generation, (REPO,))
            self.assertEqual(len(store.load("outcomes")["records"]), 1)
            again, _ = self.feed(directory, FakeAPI({}, []))
            self.assertEqual([row["number"] for row in again.view()["pulls"]], [7])
            # Served at once after a restart, with when it was read, not as "not read yet".
            self.assertEqual((again.view()["available"], again.view()["sampled_at"], again.view()["pending"]),
                             (True, "2026-11-03T15:00:00Z", 0))
            again.reader.api.graphql_values.append(ApiError("not_found"))
            again._cycle(again.reader, again._generation, (REPO,))
            self.assertEqual((again.view()["pulls"], store.load("outcomes")), ([], None))

    def test_a_cycle_whose_write_lands_after_lost_access_leaves_no_file(self):
        with tempfile.TemporaryDirectory() as directory:
            feed, store = self.feed(directory, FakeAPI(standard_items(), [search(node())]))
            save = store.save
            def save_then_lose_access(name, document):
                save(name, document)
                if name == "outcomes":
                    feed.reset()
            store.save = save_then_lose_access
            feed._cycle(feed.reader, feed._generation, (REPO,))
            self.assertEqual((store.load("outcomes"), store.load("outcomes-responses")), (None, None))

    def test_lost_access_on_a_live_pass_and_a_forgotten_restart_reset_the_outcomes(self):
        from dashboard.live_service import LiveService
        with tempfile.TemporaryDirectory() as directory:
            store = StateStore(directory, config(), clock=lambda: NOW)
            service = LiveService(config(), collector=Live(), store=store, wall_clock=lambda: NOW)
            started = []
            service.outcomes.start = started.append
            self.assertEqual((service.outcome_view()["window_days"], started), (OUTCOME_DAYS, [(REPO,)]))
            for forget in (service._revoke, lambda: (setattr(service, "_kept", True), service._forget_kept())):
                service.outcomes.reader.records[(REPO, 7)] = {"repository": REPO, "number": 7, "merged_at": "2026-11-03T12:00:00Z",
                                                               "head_runs": [], "checks": []}
                store.save("outcomes", {"records": []})
                forget()
                self.assertEqual((service.outcome_view()["pulls"], store.load("outcomes")), ([], None))

    def test_a_cycle_starts_only_once_the_repositories_are_known_and_not_again_before_it_is_due(self):
        with tempfile.TemporaryDirectory() as directory:
            feed, _ = self.feed(directory, FakeAPI(standard_items(), [search(node())]))
            feed.start(None)
            self.assertFalse(feed._running)
            feed._next = float("inf")
            feed.start((REPO,))
            self.assertFalse(feed._running)


class Page(unittest.TestCase):
    def test_the_outcomes_view_node_tests_pass(self):
        # outcomes.test.js: the split and merge wait per pull request, the week's medians, runner time
        # per check, and the view rendered from /api/outcomes in each state.
        result = subprocess.run(["node", "--test", "--test-reporter=tap", str(ROOT / "dashboard/static/outcomes.test.js")],
                                cwd=ROOT, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertRegex(result.stdout, r"(?m)^# pass [1-9]")
        self.assertIn("# fail 0", result.stdout)

    def test_the_page_is_served_and_named_in_the_navigation(self):
        from dashboard.server import FILES
        index = (ROOT / "dashboard/static/index.html").read_text()
        self.assertEqual(FILES["/assets/outcomes.js"][0], "outcomes.js")
        self.assertIn('<button data-view="outcomes">Outcomes</button>', index)
        self.assertLess(index.index("/assets/timeline.js"), index.index("/assets/outcomes.js"))


if __name__ == "__main__":
    unittest.main()
