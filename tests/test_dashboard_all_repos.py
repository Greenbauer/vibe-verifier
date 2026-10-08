"""Owner-wide discovery, and a per-pass budget that cannot starve the same repositories forever."""

import copy
import json
import subprocess
import unittest
from pathlib import Path
from datetime import datetime, timezone

from dashboard.config import BotDefinition, Config
from dashboard.gh_api import ApiError
from dashboard.github import CHECKS_NOT_LOADED, GitHubCollector
from dashboard.owner_set import OwnerSet
from dashboard.service import DashboardService
from dashboard.usage_artifacts import UsageArtifacts


NOW = datetime(2026, 10, 8, 4, 0, tzinfo=timezone.utc)
SHA = "a" * 40
BOTS = {role: BotDefinition(role + ".yml", (role,)) for role in ("reviewer", "explorer", "verifier")}


def listed_config(*names):
    return Config("octocat", names, BOTS, None)


def all_config():
    return Config("octocat", (), BOTS, None, all_repositories=True)


def pull(repository, number):
    return {"number": number, "title": "Change %s" % number, "user": {"login": "octocat"},
            "created_at": "2026-10-01T00:00:00Z", "updated_at": "2026-10-02T00:00:00Z",
            "head": {"sha": SHA}, "draft": False,
            "html_url": "https://github.com/%s/pull/%s" % (repository, number)}


def repo_row(name, archived=False, owner="octocat", kind="User"):
    return {"full_name": owner + "/" + name, "archived": archived,
            "owner": {"login": owner, "type": kind}}


class BudgetAPI:
    """Counts every call and stops at max_calls, the way GitHubAPI does."""

    def __init__(self, max_calls, pulls, rows, kind="User", installation=True):
        self.max_calls = max_calls
        self.pulls = pulls
        self.rows = rows
        self.kind = kind
        self.installation = installation
        self.calls = 0
        self.log = []
        self.lowest_remaining = 4000
        self.search_incomplete = False
        self.forbid = set()

    def begin(self):
        self.calls = 0

    def clear_cache(self):
        pass

    def take(self, endpoint):
        if endpoint in self.forbid:
            self.calls += 1
            self.log.append(endpoint)
            raise ApiError("forbidden")
        if self.calls >= self.max_calls:
            raise ApiError("request_budget_exhausted")
        self.calls += 1
        self.log.append(endpoint)

    def rate(self):
        self.take("rate_limit")
        return {"remaining": 4000, "limit": 5000, "resets_at_epoch": 0}

    def items(self, endpoint, key=None):
        self.take(endpoint)
        if endpoint == "installation/repositories":
            if not self.installation:
                raise ApiError("forbidden")
            return copy.deepcopy(self.rows)
        if "/pulls?" in endpoint:
            name = endpoint.split("/")[1] + "/" + endpoint.split("/")[2]
            return copy.deepcopy(self.pulls.get(name, []))
        if endpoint.startswith("orgs/") or "/repos?" in endpoint:
            return copy.deepcopy(self.rows)
        return []

    def one(self, endpoint):
        self.take(endpoint)
        if endpoint.startswith("search/issues"):
            if self.search_incomplete:
                return {"incomplete_results": True, "items": []}
            items = [{"repository_url": "https://api.github.com/repos/" + name}
                     for name, rows in self.pulls.items() if rows]
            return {"incomplete_results": False, "items": items}
        if endpoint.count("/") == 1 and endpoint.startswith("users/"):
            return {"login": "octocat", "type": self.kind}
        if "/pulls/" in endpoint:
            return {"head": {"sha": SHA}, "draft": False, "comments": 0, "review_comments": 0,
                    "mergeable_state": "clean"}
        if endpoint.endswith("/status"):
            return {"statuses": []}
        return {}


def collector(config, api):
    return GitHubCollector(config, api, clock=lambda: NOW)


def pulls_of(result):
    found = {}
    for row in result["repositories"]:
        found[row["repository"]] = row["pulls"]
    return found


class BudgetFairness(unittest.TestCase):
    def test_collection_comes_from_the_owner_set(self):
        self.assertIs(GitHubCollector.collect, OwnerSet.collect)

    def test_a_short_budget_lists_every_pull_and_rotates_detail(self):
        names = tuple("octocat/r%s" % index for index in range(4))
        api = BudgetAPI(1 + 4 + 5, {name: [pull(name, 1)] for name in names}, [])
        client = collector(listed_config(*names), api)
        inventory = {name: {"subscription": "unknown"} for name in names}
        seen = set()
        for _ in range(4):
            result = client.collect(inventory)
            found = pulls_of(result)
            self.assertEqual(set(found), set(names))
            for name, rows in found.items():
                self.assertEqual(len(rows), 1)
                self.assertNotIn("unavailable", rows[0])
                if rows[0]["evidence_available"]:
                    seen.add(name)
                else:
                    self.assertEqual(rows[0]["attention_reason"], CHECKS_NOT_LOADED)
                    self.assertEqual(rows[0]["source_error"], "request_budget_exhausted")
            self.assertLessEqual(api.calls, api.max_calls)
        self.assertEqual(seen, set(names))

    def test_fifteen_repositories_are_all_detailed_within_three_passes_when_five_fit(self):
        # Listings are 1 rate + 15 pulls. Five pull-request details are 25 calls. That is the
        # whole budget, so five repositories finish per pass and the fifteenth is reached on pass 3.
        names = tuple("octocat/r%s" % index for index in range(15))
        api = BudgetAPI(1 + 15 + 5 * 5, {name: [pull(name, 1)] for name in names}, [])
        client = collector(listed_config(*names), api)
        inventory = {name: {"subscription": "unknown"} for name in names}
        detailed = set()
        for pass_number in range(3):
            result = client.collect(inventory)
            found = pulls_of(result)
            self.assertEqual(set(found), set(names))
            for name, rows in found.items():
                self.assertFalse(rows[0].get("unavailable"))
                if rows[0]["evidence_available"]:
                    detailed.add(name)
            if pass_number < 2:
                self.assertNotIn(names[-1], {name for name, rows in found.items() if rows[0]["evidence_available"]})
        self.assertEqual(detailed, set(names))

    def test_a_repository_added_while_the_budget_is_exhausted_is_read_within_the_bound(self):
        names = ["octocat/r%s" % index for index in range(15)]
        pulls = {name: [pull(name, 1)] for name in names}
        api = BudgetAPI(1 + 16 + 5 * 5, pulls, [])
        client = collector(listed_config(*names), api)
        inventory = {name: {"subscription": "unknown"} for name in names}
        client.collect(inventory)
        added = "octocat/r15"
        names.append(added)
        pulls[added] = [pull(added, 7)]
        inventory[added] = {"subscription": "unknown"}
        client.config = listed_config(*names)
        appeared = False
        detailed = False
        for _ in range(4):
            result = client.collect(inventory)
            rows = pulls_of(result)[added]
            self.assertEqual(rows[0]["number"], 7)
            self.assertFalse(rows[0].get("unavailable"))
            appeared = True
            detailed = detailed or rows[0]["evidence_available"]
        self.assertTrue(appeared)
        self.assertTrue(detailed)

    def test_a_real_access_failure_stays_unavailable(self):
        names = ("octocat/open", "octocat/closed")
        api = BudgetAPI(50, {names[0]: [pull(names[0], 1)], names[1]: [pull(names[1], 2)]}, [])
        api.forbid.add("repos/octocat/closed/pulls?state=open&per_page=100")
        service = DashboardService(listed_config(*names), collector(listed_config(*names), api),
                                   wall_clock=lambda: NOW)
        # The collector's config and the service config must be the same object the collector holds.
        service.collector = collector(service.config, api)
        result = service.snapshot()
        rows = {row["repository"]: row for row in result["github"]["repositories"]}
        self.assertEqual(rows["octocat/open"]["pulls"][0]["number"], 1)
        self.assertFalse(rows["octocat/open"].get("unavailable"))
        self.assertTrue(rows["octocat/closed"]["unavailable"])
        self.assertEqual(rows["octocat/closed"]["pulls"], [])
        self.assertIsNone(rows["octocat/closed"]["sampled_at"])


class Discovery(unittest.TestCase):
    def world(self, rows, pulls, kind="User", installation=True, max_calls=200):
        api = BudgetAPI(max_calls, pulls, rows, kind=kind, installation=installation)
        return api, collector(all_config(), api)

    def test_an_installation_token_discovers_a_user_or_an_organization_and_skips_the_idle(self):
        rows = [repo_row("busy", kind="Organization"), repo_row("quiet", kind="Organization"),
                repo_row("old", archived=True, kind="Organization")]
        pulls = {"octocat/busy": [pull("octocat/busy", 4)]}
        api, client = self.world(rows, pulls, kind="Organization")
        result = client.collect()
        self.assertEqual(result["repository_names"], ["octocat/busy", "octocat/quiet"])
        self.assertEqual(result["coverage"]["label"], "All repositories of octocat")
        found = pulls_of(result)
        self.assertEqual(found["octocat/busy"][0]["number"], 4)
        self.assertEqual(found["octocat/quiet"], [])
        self.assertNotIn("octocat/old", found)
        self.assertFalse(any("/pulls?" in endpoint and "quiet" in endpoint for endpoint in api.log))
        self.assertFalse(any(endpoint.startswith("users/") or endpoint.startswith("orgs/") for endpoint in api.log))
        again = client.collect({name: {"subscription": "unknown"} for name in result["repository_names"]})
        self.assertEqual(again["repository_names"], result["repository_names"])
        self.assertEqual(sum(endpoint == "installation/repositories" for endpoint in api.log), 2)

    def test_a_personal_token_reads_the_owner_after_the_installation_probe_is_refused(self):
        rows = [repo_row("busy"), repo_row("quiet")]
        for kind, marker in (("User", "users/octocat/repos?type=owner"), ("Organization", "orgs/octocat/repos")):
            api, client = self.world(rows, {"octocat/busy": [pull("octocat/busy", 1)]}, kind=kind, installation=False)
            api.rows = [repo_row("busy", kind=kind), repo_row("quiet", kind=kind)]
            result = client.collect()
            self.assertEqual(result["repository_names"], ["octocat/busy", "octocat/quiet"])
            self.assertIn("installation/repositories", api.log)
            self.assertIn(marker, api.log)
            before = len(api.log)
            client.collect()
            self.assertNotIn("installation/repositories", api.log[before:])

    def test_a_foreign_or_malformed_row_refuses_the_whole_discovery(self):
        api, client = self.world([repo_row("busy"), repo_row("nope", owner="other")], {})
        with self.assertRaises(ApiError) as raised:
            client.collect()
        self.assertEqual(raised.exception.code, "invalid_response")
        api.rows = [repo_row("busy"), {"full_name": "not-a-repo", "archived": False}]
        with self.assertRaises(ApiError) as raised:
            client.collect()
        self.assertEqual(raised.exception.code, "invalid_response")

    def test_an_incomplete_search_is_not_treated_as_an_empty_owner(self):
        api, client = self.world([repo_row("busy")], {"octocat/busy": [pull("octocat/busy", 1)]})
        api.search_incomplete = True
        with self.assertRaises(ApiError) as raised:
            client.collect()
        self.assertEqual(raised.exception.code, "unavailable")

    def test_a_repository_that_appears_or_disappears_is_the_next_refresh(self):
        api, client = self.world([repo_row("busy")], {"octocat/busy": [pull("octocat/busy", 1)]})
        self.assertEqual(client.collect()["repository_names"], ["octocat/busy"])
        api.rows.append(repo_row("fresh"))
        api.pulls["octocat/fresh"] = [pull("octocat/fresh", 9)]
        self.assertEqual(client.collect()["repository_names"], ["octocat/busy", "octocat/fresh"])
        api.rows = [repo_row("fresh")]
        api.pulls = {"octocat/fresh": [pull("octocat/fresh", 9)]}
        self.assertEqual(client.collect()["repository_names"], ["octocat/fresh"])

    def test_a_configured_list_does_not_search_or_probe(self):
        api = BudgetAPI(50, {"octocat/example": [pull("octocat/example", 1)]}, [])
        result = collector(listed_config("octocat/example"), api).collect({"octocat/example": {"subscription": "unknown"}})
        self.assertEqual(result["coverage"]["label"], "Selected repositories")
        self.assertFalse(any(endpoint.startswith("search/") or endpoint == "installation/repositories" for endpoint in api.log))
        self.assertTrue(result["repositories"][0]["pulls"][0]["evidence_available"])


class CallCost(unittest.TestCase):
    def test_warm_refresh_of_fifteen_repositories_seven_of_them_open(self):
        names = tuple("octocat/r%s" % index for index in range(15))
        open_names = names[:7]
        pulls = {name: [pull(name, 1)] for name in open_names}
        rows = [repo_row("r%s" % index) for index in range(15)]
        inventory = {name: {"subscription": "unknown"} for name in names}
        listed = collector(listed_config(*names), BudgetAPI(200, pulls, []))
        listed.collect(inventory)
        list_workflows_cold = len(listed.api.log)
        listed.api.log.clear()
        listed.collect(inventory)
        list_warm = list(listed.api.log)
        every = collector(all_config(), BudgetAPI(200, pulls, rows))
        every.collect(inventory)
        all_workflows_cold = len(every.api.log)
        every.api.log.clear()
        every.collect(inventory)
        all_warm = list(every.api.log)
        bare = collector(all_config(), BudgetAPI(200, pulls, rows))
        bare.collect(None)
        listed_bare = collector(listed_config(*names), BudgetAPI(200, pulls, []))
        listed_bare.collect(None)
        # No inventory was cached. A list reads two content paths for every repository.
        # `all` reads them only for the seven that have an open pull request.
        self.assertEqual(sum("/contents/" in endpoint for endpoint in listed_bare.api.log), 30)
        self.assertEqual(len(listed_bare.api.log), 1 + 15 + 30 + 7 * 5 + 15)
        self.assertEqual(sum("/contents/" in endpoint for endpoint in bare.api.log), 14)
        self.assertEqual(sum("/actions/workflows" in endpoint for endpoint in bare.api.log), 15)
        self.assertEqual(len(bare.api.log), 1 + 1 + 1 + 7 + 14 + 7 * 5 + 15)
        self.assertEqual(list_workflows_cold, 1 + 15 + 7 * 5 + 15)
        self.assertEqual(all_workflows_cold, 1 + 1 + 1 + 7 + 7 * 5 + 15)
        self.assertEqual(len(list_warm), 1 + 15 + 7 * 5)
        self.assertEqual(len(all_warm), 1 + 1 + 1 + 7 + 7 * 5)
        self.assertEqual(sum("/pulls?" in endpoint for endpoint in all_warm), 7)
        self.assertFalse(any("r14" in endpoint and "/pulls?" in endpoint for endpoint in all_warm))
        self.assertFalse(any("/contents/" in endpoint for endpoint in all_warm))


class FollowsTheSet(unittest.TestCase):
    def test_usage_and_bots_read_the_discovered_names_and_stop_at_the_budget(self):
        class UsageAPI:
            max_calls = 1

            def __init__(self):
                self.calls = 0
                self.seen = []

            def begin(self):
                self.calls = 0

            def one(self, endpoint):
                if self.calls >= self.max_calls:
                    raise ApiError("request_budget_exhausted")
                self.calls += 1
                self.seen.append(endpoint)
                return {"artifacts": []}

        api = UsageAPI()
        reader = UsageArtifacts(all_config(), api=api, clock=lambda: 0)
        result = reader.collect(NOW, repositories=("octocat/a", "octocat/b", "octocat/c"))
        self.assertEqual(api.calls, 1)
        self.assertEqual(api.seen, ["repos/octocat/a/actions/artifacts?per_page=100&page=1"])
        self.assertTrue(result["partial"])
        self.assertLessEqual(api.calls, api.max_calls)

        bots = BudgetAPI(10, {}, [])
        client = collector(all_config(), bots)
        client._bots(NOW, names=("octocat/a", "octocat/b"))
        self.assertEqual([endpoint for endpoint in bots.log if "workflows" in endpoint],
                         ["repos/octocat/a/actions/workflows?per_page=100",
                          "repos/octocat/b/actions/workflows?per_page=100"])


class DiscoveryFailure(unittest.TestCase):
    def sample(self, names):
        return {"owner": "octocat", "sampled_at": NOW.isoformat(), "repository_names": list(names),
                "repositories": [{"repository": name, "subscription": "unknown", "pulls": [],
                                  "errors": [], "sampled_at": NOW.isoformat()} for name in names],
                "coverage": {"selected": len(names), "readable": len(names),
                             "label": "All repositories of octocat", "inventory": {}},
                "bots": {"partial": False, "roles": {}, "errors": []},
                "errors": [], "partial": False, "api": {"calls": 3, "max_calls": 200}}

    def test_a_failed_discovery_keeps_the_last_set_and_a_first_failure_is_not_zero(self):
        first = self.sample(("octocat/a", "octocat/b"))
        collector = _Values([first, ApiError("unavailable")])
        service = DashboardService(all_config(), collector, monotonic=lambda: 0, wall_clock=lambda: NOW)
        loaded = service.snapshot()
        self.assertEqual([row["repository"] for row in loaded["github"]["repositories"]], ["octocat/a", "octocat/b"])
        self.assertIsNone(loaded["github"].get("repository_names"))
        again = service.snapshot(force=True)
        self.assertTrue(again["github"]["stale"])
        self.assertEqual([row["repository"] for row in again["github"]["repositories"]], ["octocat/a", "octocat/b"])

        empty = DashboardService(all_config(), _Values([ApiError("unavailable")]),
                                 monotonic=lambda: 0, wall_clock=lambda: NOW)
        failed = empty.snapshot()
        self.assertEqual(failed["github"]["repositories"], [])
        self.assertIsNone(failed["github"]["coverage"]["selected"])
        self.assertIn("All repositories of octocat", failed["github"]["coverage"]["label"])

    def test_a_repository_missing_from_a_successful_discovery_is_dropped(self):
        collector = _Values([self.sample(("octocat/a", "octocat/b")), self.sample(("octocat/b",))])
        service = DashboardService(all_config(), collector, monotonic=lambda: 0, wall_clock=lambda: NOW)
        service.snapshot()
        again = service.snapshot(force=True)
        self.assertEqual([row["repository"] for row in again["github"]["repositories"]], ["octocat/b"])
        self.assertFalse(again["github"].get("stale"))


class _Values:
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

    def clear_private_cache(self):
        pass


class Banner(unittest.TestCase):
    def test_a_repository_with_no_open_pull_request_is_absent_and_all_mode_banner_says_so(self):
        source = r'''
const h=require('./dashboard/static/helpers.js');
const pulls=[{repository:'octocat/busy',number:1,created_at:'2026-10-01T00:00:00Z',updated_at:'2026-10-02T00:00:00Z'}];
const quiet='octocat/quiet';
console.log(JSON.stringify({
 groups:h.groupPulls(pulls).map(g=>g.repository),
 filter:h.repositoriesWithPulls(pulls),
 quietGrouped:h.groupPulls(pulls).some(g=>g.repository===quiet),
 reading:h.coverageBanner({label:'All repositories of octocat',selected:null,readable:0},{refreshing:true,errors:[]}),
 unavailable:h.coverageBanner({label:'All repositories of octocat',selected:null,readable:0},{refreshing:false,errors:[{code:'unavailable'}]}),
 counted:h.coverageBanner({label:'All repositories of octocat',selected:2,readable:2},{errors:[],stale:false}),
 selected:h.coverageBanner({label:'Selected repositories',selected:1,readable:1},{errors:[],stale:false})
}));
'''
        root = Path(__file__).resolve().parent.parent
        result = subprocess.run(["node", "-e", source], cwd=root, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        parsed = json.loads(result.stdout)
        self.assertEqual(parsed["groups"], ["octocat/busy"])
        self.assertEqual(parsed["filter"], ["octocat/busy"])
        self.assertNotIn("octocat/quiet", parsed["filter"])
        self.assertFalse(parsed["quietGrouped"])
        self.assertEqual(parsed["reading"], "Reading repositories.")
        self.assertEqual(parsed["unavailable"], "Repository list is unavailable.")
        self.assertEqual(parsed["counted"], "All repositories of octocat: 2. 2 read successfully in this sample.")
        self.assertEqual(parsed["selected"], "Selected repositories: 1. 1 read successfully in this sample.")


if __name__ == "__main__":
    unittest.main()
