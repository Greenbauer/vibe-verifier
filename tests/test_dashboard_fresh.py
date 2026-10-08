"""A green title is only a check reading from this pass. Heads are read cheaply on every pass."""

import copy
import json
import subprocess
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from dashboard.config import BotDefinition, Config
from dashboard.gh_api import ApiError
from dashboard.github import CHECKS_NOT_LOADED, GitHubCollector
from dashboard.head_state import (
    BEAT_SECONDS, BEFORE_CHANGE, HEADS, PREVIOUS_PUSH, calls_per_hour, read_head_page,
    search_text, withhold_if_old,
)
from dashboard.service import DashboardService

ROOT = Path(__file__).resolve().parent.parent
NOW = datetime(2026, 10, 8, 16, 0, tzinfo=timezone.utc)
OLD = "a" * 40
NEW = "b" * 40
BOTS = {role: BotDefinition(role + ".yml", (role,)) for role in ("reviewer", "explorer", "verifier")}


def listed(*names, owner="octocat"):
    return Config(owner, names, BOTS, None)


class Clock:
    def __init__(self, value):
        self.value = value

    def __call__(self):
        return self.value


class HeadAPI:
    """REST calls stop at max_calls. The head search does not."""

    def __init__(self, max_calls, pulls):
        self.max_calls = max_calls
        self.pulls = pulls
        self.calls = 0
        self.head_calls = 0
        self.graphql_points = 0
        self.lowest_remaining = 4000
        self.log = []
        self.searches = []
        self.rollup = {}

    def begin(self):
        self.calls = 0
        self.head_calls = 0
        self.graphql_points = 0
        self.log = []

    def clear_cache(self):
        pass

    def add_points(self, cost):
        if isinstance(cost, int) and not isinstance(cost, bool) and cost >= 0:
            self.graphql_points += cost

    def take(self, endpoint):
        if self.calls >= self.max_calls:
            raise ApiError("request_budget_exhausted")
        self.calls += 1
        self.log.append(endpoint)

    def rate(self):
        self.take("rate_limit")
        return {"remaining": 4000, "limit": 5000, "resets_at_epoch": 0}

    def _sha(self, endpoint):
        parts = endpoint.split("/")
        if "commits" in parts:
            return parts[parts.index("commits") + 1].split("?")[0]
        return OLD

    def items(self, endpoint, key=None):
        self.take(endpoint)
        if "/pulls?" in endpoint:
            name = endpoint.split("/")[1] + "/" + endpoint.split("/")[2]
            return copy.deepcopy(self.pulls.get(name, []))
        if "check-suites" in endpoint:
            return [{"id": 1, "head_sha": self._sha(endpoint)}]
        if "check-runs" in endpoint:
            sha = self._sha(endpoint)
            done = sha == OLD
            return [{"id": 7, "name": "test", "status": "completed" if done else "queued",
                     "conclusion": "success" if done else None, "head_sha": sha,
                     "check_suite": {"id": 1}, "app": {"slug": "actions", "name": "Actions"},
                     "started_at": "2026-10-08T00:00:00Z",
                     "completed_at": "2026-10-08T00:01:00Z" if done else None}]
        return []

    def one(self, endpoint):
        self.take(endpoint)
        if "/pulls/" in endpoint:
            name = endpoint.split("/")[1] + "/" + endpoint.split("/")[2]
            sha = self.pulls[name][0]["head"]["sha"]
            return {"head": {"sha": sha}, "draft": False, "comments": 0, "review_comments": 0,
                    "mergeable_state": "clean", "mergeable": True}
        if endpoint.endswith("/status"):
            return {"statuses": []}
        return {}

    def graphql(self, query, variables, budgeted=True):
        if "statusCheckRollup" in query:
            self.searches.append(variables.get("search"))
            if budgeted:
                self.take("graphql-head")
            else:
                self.head_calls += 1
            return self._heads()
        if budgeted:
            self.take("graphql:" + variables.get("name", ""))
        return self._signals(variables)

    def _heads(self):
        nodes = []
        for name, rows in self.pulls.items():
            for row in rows:
                nodes.append({"number": row["number"], "headRefOid": row["head"]["sha"],
                              "mergeStateStatus": "CLEAN", "mergeable": "MERGEABLE",
                              "reviewDecision": "APPROVED", "repository": {"nameWithOwner": name},
                              "statusCheckRollup": {"state": self.rollup.get((name, row["number"]), "SUCCESS")}})
        return {"data": {"rateLimit": {"cost": 1, "remaining": 4000, "resetAt": "2026-10-08T17:00:00Z"},
                         "search": {"issueCount": len(nodes),
                                    "pageInfo": {"hasNextPage": False}, "nodes": nodes}}}

    def _signals(self, variables):
        repo = "octocat/" + variables["name"]
        number = self.pulls[repo][0]["number"]
        node = {"number": number, "baseRefName": "main", "reviewDecision": "APPROVED",
                "reviewThreads": {"pageInfo": {"hasNextPage": False}, "nodes": []},
                "commits": {"nodes": []}}
        return {"data": {"repository": {"pullRequests": {
            "pageInfo": {"hasNextPage": False}, "nodes": [node]}}}}


def pull(repository, number, sha=OLD):
    return {"number": number, "title": "Change %s" % number, "user": {"login": "octocat"},
            "created_at": "2026-10-01T00:00:00Z", "updated_at": "2026-10-02T00:00:00Z",
            "head": {"sha": sha}, "draft": False,
            "html_url": "https://github.com/%s/pull/%s" % (repository, number)}


def collect(client, api, inventory):
    return {row["repository"]: row["pulls"] for row in client.collect(inventory)["repositories"]}


class HeadReading(unittest.TestCase):
    def test_the_query_uses_user_or_org_and_costs_one_call_an_interval(self):
        self.assertEqual(search_text("octocat", "User"), "is:pr is:open user:octocat")
        self.assertEqual(search_text("octocat", "Organization"), "is:pr is:open org:octocat")
        for field in ("headRefOid", "mergeStateStatus", "mergeable", "reviewDecision", "statusCheckRollup"):
            self.assertIn(field, HEADS)
        self.assertEqual(calls_per_hour(1), 3600 // BEAT_SECONDS)
        self.assertEqual(calls_per_hour(1), 20)

    def test_a_changed_head_is_not_green_and_is_detailed_first(self):
        self._next_pass(new_sha=True)

    def test_a_pending_rollup_on_the_same_head_is_not_green_and_is_detailed_first(self):
        self._next_pass(new_sha=False)

    def _next_pass(self, new_sha):
        names = ("octocat/one", "octocat/two")
        pulls = {name: [pull(name, index + 1)] for index, name in enumerate(names)}
        api = HeadAPI(80, pulls)
        clock = Clock(NOW)
        client = GitHubCollector(listed(*names), api, clock=clock)
        client._kind = "User"
        inventory = {name: {"subscription": "unknown"} for name in names}
        first = collect(client, api, inventory)
        self.assertTrue(first["octocat/one"][0]["merge_ready"])
        self.assertTrue(first["octocat/two"][0]["merge_ready"])
        self.assertNotIn("checks_age_seconds", first["octocat/one"][0])
        if new_sha:
            pulls["octocat/two"][0]["head"]["sha"] = NEW
        else:
            api.rollup[("octocat/two", 2)] = "PENDING"
        clock.value = NOW + timedelta(seconds=300)
        api.max_calls = 1 + len(names) + 1 + 5
        second = collect(client, api, inventory)
        changed, kept = second["octocat/two"][0], second["octocat/one"][0]
        self.assertFalse(changed["merge_ready"])
        self.assertNotIn("checks_age_seconds", changed)
        self.assertFalse(kept["merge_ready"])
        self.assertTrue(kept["stale"])
        self.assertEqual(kept["checks_age_seconds"], 300)
        suites = [item for item in api.log if "check-suites" in item]
        self.assertIn("octocat/two", suites[0])
        self.assertTrue(all("octocat/one" not in item for item in suites))

    def test_the_cheap_pass_runs_for_a_user_and_an_org_when_detail_budget_is_spent(self):
        for kind, qualifier in (("User", "user"), ("Organization", "org")):
            names = ("octocat/one", "octocat/two")
            pulls = {name: [pull(name, 1)] for name in names}
            api = HeadAPI(1 + len(names), pulls)
            api.rollup[("octocat/one", 1)] = "PENDING"
            api.rollup[("octocat/two", 1)] = "FAILURE"
            client = GitHubCollector(listed(*names), api, clock=lambda: NOW)
            client._kind = kind
            found = collect(client, api, {name: {"subscription": "unknown"} for name in names})
            self.assertLessEqual(api.calls, api.max_calls)
            self.assertEqual(api.head_calls, 1)
            self.assertEqual(api.graphql_points, 1)
            self.assertEqual(api.searches, ["is:pr is:open %s:octocat" % qualifier])
            self.assertTrue(all(not row["evidence_available"] for rows in found.values() for row in rows))
            self.assertTrue(all(row["attention_reason"] == CHECKS_NOT_LOADED for rows in found.values() for row in rows))
            self.assertEqual(found["octocat/one"][0]["rollup_category"], "pending")
            self.assertEqual(found["octocat/one"][0]["head_sha"], OLD)
            self.assertEqual(found["octocat/two"][0]["rollup_category"], "failed")

    def test_a_skipped_pass_keeps_last_checks_and_a_moved_head_says_so(self):
        names = ("octocat/one", "octocat/two")
        pulls = {name: [pull(name, index + 1)] for index, name in enumerate(names)}
        api = HeadAPI(80, pulls)
        clock = Clock(NOW)
        client = GitHubCollector(listed(*names), api, clock=clock)
        client._kind = "User"
        inventory = {name: {"subscription": "unknown"} for name in names}
        first = collect(client, api, inventory)
        self.assertTrue(first["octocat/two"][0]["checks"])
        pulls["octocat/two"][0]["head"]["sha"] = NEW
        api.rollup[("octocat/one", 1)] = "PENDING"
        clock.value = NOW + timedelta(seconds=300)
        api.max_calls = 1 + len(names)
        second = collect(client, api, inventory)
        moved, pending = second["octocat/two"][0], second["octocat/one"][0]
        self.assertEqual(api.calls, api.max_calls)
        self.assertFalse(moved["merge_ready"])
        self.assertEqual(moved["attention_reason"], PREVIOUS_PUSH)
        self.assertEqual(moved["checks"], first["octocat/two"][0]["checks"])
        self.assertGreater(moved["checks_age_seconds"], 0)
        self.assertFalse(pending["merge_ready"])
        self.assertEqual(pending["attention_reason"], BEFORE_CHANGE)
        self.assertEqual(pending["checks"], first["octocat/one"][0]["checks"])
        self.assertEqual(pending["checks_age_seconds"], 300)

    def test_three_capped_passes_never_drop_checks_once_read(self):
        names = tuple("octocat/r%s" % index for index in range(4))
        pulls = {name: [pull(name, 1)] for name in names}
        api = HeadAPI(1 + len(names) + 6, pulls)
        client = GitHubCollector(listed(*names), api, clock=lambda: NOW)
        client._kind = "User"
        inventory = {name: {"subscription": "unknown"} for name in names}
        had = {}
        for _ in range(3):
            result = client.collect(inventory)
            self.assertEqual(result["api"]["calls"], api.max_calls)
            for row in result["repositories"]:
                item = row["pulls"][0]
                key = row["repository"]
                if had.get(key):
                    self.assertTrue(item["checks"], key)
                had[key] = bool(item.get("checks"))
        self.assertTrue(any(had.values()))

    def test_a_head_page_keeps_sha_and_rollup_and_rejects_a_broken_payload(self):
        payload = HeadAPI(1, {"octocat/one": [pull("octocat/one", 4)]})._heads()
        found, cursor, cost = read_head_page(payload)
        self.assertEqual(found[("octocat/one", 4)]["sha"], OLD)
        self.assertEqual(found[("octocat/one", 4)]["rollup"], "SUCCESS")
        self.assertIsNone(cursor)
        self.assertEqual(cost, 1)
        self.assertIsNone(read_head_page({"errors": [{"message": "no"}]}))


class PaintAndBeat(unittest.TestCase):
    def sample(self, at):
        return {"owner": "octocat", "sampled_at": at.isoformat(),
                "repositories": [{"repository": "octocat/example", "subscription": "unknown",
                                  "pulls": [{"number": 7, "title": "Ship", "merge_ready": True,
                                             "head_sha": OLD, "rollup": "SUCCESS", "rollup_known": True,
                                             "checks": [{"name": "test", "category": "success"}],
                                             "checks_sampled_at": at.isoformat(), "stale": False}],
                                  "errors": [], "sampled_at": at.isoformat()}],
                "coverage": {"selected": 1, "readable": 1, "label": "Selected repositories", "inventory": {}},
                "bots": {"partial": False, "roles": {}, "errors": []},
                "errors": [], "partial": False, "api": {"calls": 3, "max_calls": 200}}

    def test_the_beat_reads_heads_only_and_states_its_hourly_calls(self):
        wall, mono = Clock(NOW), Clock(0)
        collector = BeatCollector([self.sample(NOW)])
        service = DashboardService(listed("octocat/example"), collector, monotonic=mono, wall_clock=wall)
        service.snapshot(force=True)
        self.assertIsNone(service.run_head_beat())
        self.assertEqual(collector.heads_reads, 0)
        mono.value = BEAT_SECONDS
        wall.value = NOW + timedelta(seconds=BEAT_SECONDS)
        updated = service.run_head_beat()
        pull = updated["repositories"][0]["pulls"][0]
        self.assertEqual(collector.collects, 1)
        self.assertEqual(collector.heads_reads, 1)
        self.assertFalse(pull["merge_ready"])
        self.assertEqual(pull["checks"], [{"name": "test", "category": "success"}])
        self.assertEqual(pull["attention_reason"], PREVIOUS_PUSH)
        self.assertEqual(updated["head_reading"]["calls"], 1)
        self.assertEqual(updated["head_reading"]["points"], 1)
        self.assertEqual(updated["head_reading"]["calls_per_hour"], 20)
        self.assertEqual(updated["head_reading"]["points_per_hour"], 20)

    def test_the_first_request_after_a_long_idle_paints_nothing_old_as_green(self):
        wall, mono = Clock(NOW), Clock(0)
        collector = BeatCollector([self.sample(NOW), self.sample(NOW)])
        service = DashboardService(listed("octocat/example"), collector, monotonic=mono, wall_clock=wall)
        service.snapshot(force=True)
        wall.value = NOW + timedelta(minutes=40)
        mono.value = 40 * 60
        painted = service.snapshot(nonblocking=True)
        pull = painted["github"]["repositories"][0]["pulls"][0]
        self.assertFalse(pull["merge_ready"])
        self.assertTrue(pull["stale"])
        self.assertGreater(pull["checks_age_seconds"], BEAT_SECONDS)
        service.snapshot()

    def test_a_fresh_reading_is_left_green_and_an_old_one_is_not(self):
        fresh = {"merge_ready": True, "checks_sampled_at": NOW.isoformat(), "stale": False}
        withhold_if_old(fresh, NOW, False)
        self.assertTrue(fresh["merge_ready"])
        self.assertNotIn("checks_age_seconds", fresh)
        old = {"merge_ready": True, "checks_sampled_at": NOW.isoformat(), "stale": False}
        withhold_if_old(old, NOW + timedelta(seconds=BEAT_SECONDS + 1), False)
        self.assertFalse(old["merge_ready"])
        self.assertTrue(old["stale"])
        self.assertGreater(old["checks_age_seconds"], BEAT_SECONDS)


class BeatCollector:
    class API:
        max_calls = 200

    api = API()

    def __init__(self, values):
        self.values = iter(values)
        self.collects = 0
        self.heads_reads = 0

    def collect(self, inventory=None):
        self.collects += 1
        return copy.deepcopy(next(self.values))

    def read_open_heads(self, github):
        self.heads_reads += 1
        return {"heads": {("octocat/example", 7): {"sha": NEW, "rollup": "PENDING"}}, "calls": 1, "points": 1}

    def clear_private_cache(self):
        pass


class RowAge(unittest.TestCase):
    def test_a_stale_row_shows_its_check_age_and_is_not_green(self):
        script = r"""
const fs = require('fs');
const helpers = require('./dashboard/static/helpers.js');
const app = fs.readFileSync('./dashboard/static/app.js', 'utf8');
const source = app.slice(app.indexOf('  function runningNames('), app.indexOf('  function renderPulls('));
function el(tag, attrs={}, ...children) {
  return {tag, attrs, children: children.flat().filter(value => value !== null && value !== undefined)};
}
const prRow = new Function('el', 'safeUrl', 'snapshot', 'combinedCategory', 'badge', 'progress', 'since', 'ageClass', 'runningWork',
  source + 'return prRow;')(el, helpers.safeUrl, {owner: 'octocat'}, () => 'success',
  status => el('span', {}, status), () => el('div'), helpers.since, helpers.ageClass, helpers.runningWork);
const base = {repository: 'octocat/example', number: 3, title: 'Ship the gate', author: 'octocat',
  head_sha: 'abcdef1234567890', html_url: 'https://github.com/octocat/example/pull/3',
  attention_reason: 'No current-head failure observed', created_at: '2026-10-01T00:00:00Z', merge_ready: true};
const text = node => typeof node === 'string' ? node : node.children.map(text).join(' ');
const fresh = prRow({...base, stale: false});
const stale = prRow({...base, stale: true, checks_age_seconds: 400,
  checks_sampled_at: new Date(Date.now() - 400000).toISOString()});
const title = row => row.children[0].children[0];
const kept = helpers.flattenPulls({github: {repositories: [{stale: false, pulls: [{...base, stale: true}]}]}});
const fromRepo = helpers.flattenPulls({github: {repositories: [{stale: true, pulls: [{...base, stale: false}]}]}});
const behind = prRow({...base, stale: true, merge_ready: true, checks_age_seconds: 400,
  checks: [{name: 'test', category: 'success'}],
  checks_sampled_at: new Date(Date.now() - 400000).toISOString(),
  attention_reason: 'Checks shown are from the previous push. Newer checks are loading.'});
console.log(JSON.stringify({
  freshClass: title(fresh).attrs.class,
  freshText: text(fresh),
  staleClass: stale.attrs.class,
  staleTitle: title(stale).attrs.class || '',
  staleText: text(stale),
  keptStale: kept[0].stale,
  repoStale: fromRepo[0].stale,
  behindText: text(behind),
  behindTitle: title(behind).attrs.class || '',
  pending: helpers.combinedCategory({checks: [], statuses: [], expected: [], rollup_category: 'pending'}),
  detailed: helpers.combinedCategory({checks: [{category: 'success'}], statuses: [], expected: [], rollup_category: 'pending'})
}));
"""
        result = json.loads(subprocess.run(["node", "-e", script], cwd=ROOT, check=True,
                                            capture_output=True, text=True).stdout)
        self.assertEqual(result["freshClass"], "merge-ready")
        self.assertNotIn("checks ", result["freshText"])
        self.assertIn("stale-row", result["staleClass"])
        self.assertNotIn("merge-ready", result["staleTitle"])
        self.assertIn("checks ", result["staleText"])
        self.assertIn("ago", result["staleText"])
        self.assertTrue(result["keptStale"])
        self.assertTrue(result["repoStale"])
        self.assertIn("previous push", result["behindText"])
        self.assertIn("loading", result["behindText"])
        self.assertNotIn("merge-ready", result["behindTitle"])
        self.assertEqual(result["pending"], "pending")
        self.assertEqual(result["detailed"], "success")
