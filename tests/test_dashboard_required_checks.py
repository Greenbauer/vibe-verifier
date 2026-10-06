"""Required checks the base branch still expects, from rulesets and classic protection."""

import unittest

from dashboard.gh_api import ApiError
from dashboard.github import GitHubCollector
from dashboard.required_checks import required_contexts
from test_dashboard_github import (
    HISTORY, NOW, REPO, SHA, config, endpoints, joined_api, pinned_api, pull_row, required_workflow_rule,
)


class RequiredChecks(unittest.TestCase):
    def pull(self, api, rules, collector=None):
        collector = collector or GitHubCollector(config(), api, clock=lambda: NOW)
        return collector._pull(REPO, pull_row(), {"subscription": "subscribed"}, rules)

    def test_a_required_workflow_with_no_run_on_the_head_is_expected_not_complete(self):
        rule = required_workflow_rule()
        rule["parameters"]["workflows"][0]["path"] = ".github/workflows/auth-probe.yml"
        result = self.pull(joined_api(), [rule])
        self.assertEqual([(row["name"], row["category"]) for row in result["expected"]], [("auth-probe.yml", "pending")])
        self.assertTrue(result["attention"])

    def test_a_run_from_before_the_pin_moved_does_not_satisfy_the_rule(self):
        api = pinned_api("2026-09-30T14:40:00Z")
        api.item_values[endpoints()["checks"]][0]["conclusion"] = "success"
        result = self.pull(api, [required_workflow_rule()])
        self.assertEqual([row["name"] for row in result["expected"]], ["CI"])
        self.assertEqual(result["attention_reason"], "1 required check not run on this head")

    def test_a_run_at_the_current_pin_satisfies_the_rule_and_the_pin_start_is_cached(self):
        api = pinned_api("2026-09-30T14:50:00Z")
        api.item_values[endpoints()["checks"]][0]["conclusion"] = "success"
        collector = GitHubCollector(config(), api, clock=lambda: NOW)
        self.assertEqual(self.pull(api, [required_workflow_rule()], collector)["expected"], [])
        history_calls = [call for call in api.calls if HISTORY in call[1]]
        self.assertNotIn(("one", f"{HISTORY}/1"), history_calls)
        result = self.pull(api, [required_workflow_rule()], collector)
        self.assertEqual(([call for call in api.calls if HISTORY in call[1]]), history_calls)
        self.assertFalse(result["attention"])

    def test_a_refused_ruleset_history_counts_any_run_and_keeps_the_evidence(self):
        # GitHub serves organization ruleset history only with organization administration write.
        api = pinned_api("2026-09-30T14:40:00Z")
        api.item_values[f"{HISTORY}?per_page=100"] = ApiError("forbidden")
        collector = GitHubCollector(config(), api, clock=lambda: NOW)
        result = self.pull(api, [required_workflow_rule()], collector)
        self.assertTrue(result["evidence_available"])
        self.assertEqual(result["expected"], [])
        self.pull(api, [required_workflow_rule()], collector)
        self.assertEqual(sum(HISTORY in call[1] for call in api.calls), 1)

    def test_a_refused_history_still_expects_a_required_workflow_that_never_ran(self):
        api = pinned_api("2026-09-30T14:40:00Z", required=False)
        api.item_values[f"{HISTORY}?per_page=100"] = ApiError("forbidden")
        result = self.pull(api, [required_workflow_rule()])
        self.assertEqual([row["name"] for row in result["expected"]], ["ci.yml"])

    def test_other_ruleset_history_failures_still_mark_the_pull_unavailable(self):
        api = pinned_api("2026-09-30T14:50:00Z")
        api.item_values[f"{HISTORY}?per_page=100"] = ApiError("unavailable")
        with self.assertRaises(ApiError):
            self.pull(api, [required_workflow_rule()])

    def test_the_repositorys_own_workflow_at_the_same_path_does_not_satisfy_the_rule(self):
        result = self.pull(pinned_api("2026-09-30T14:50:00Z", required=False), [required_workflow_rule()])
        self.assertEqual([row["name"] for row in result["expected"]], ["ci.yml"])

    def test_a_required_status_check_counts_only_once_reported(self):
        rule = {"type": "required_status_checks", "parameters": {"required_status_checks": [
            {"context": "legacy/status"}, {"context": "deploy/preview"}]}}
        classic = {"type": "required_status_checks", "parameters": {"required_status_checks": [
            {"context": "deploy/preview"}, {"context": "lint"}]}}
        result = self.pull(joined_api(), [rule, classic])
        self.assertEqual([(row["name"], row["provider"]) for row in result["expected"]],
                         [("deploy/preview", "Required status check"), ("lint", "Required status check")])

    def test_classic_branch_protection_lists_required_checks_the_rules_api_omits(self):
        protection = f"repos/{REPO}/branches/main/protection/required_status_checks"
        rules_endpoint = f"repos/{REPO}/rules/branches/main?per_page=100"
        api = joined_api()
        api.item_values[rules_endpoint] = []
        api.one_values[protection] = {"checks": [{"context": "legacy/status", "app_id": None},
                                                 {"context": "lint", "app_id": 1}],
                                      "contexts": ["legacy/status", "lint"]}
        api.item_values[endpoints()["checks"]][0]["conclusion"] = "success"
        rules = GitHubCollector(config(), api, clock=lambda: NOW)._branch_rules(REPO, "main")
        result = self.pull(api, rules)
        self.assertEqual([row["name"] for row in result["expected"]], ["lint"])
        self.assertEqual(result["attention_reason"], "1 required check not run on this head")

    def test_classic_protection_falls_back_to_contexts_and_ignores_a_missing_branch_rule(self):
        protection = f"repos/{REPO}/branches/main/protection/required_status_checks"
        rules_endpoint = f"repos/{REPO}/rules/branches/main?per_page=100"
        api = joined_api()
        api.item_values[rules_endpoint] = []
        api.one_values[protection] = {"checks": [], "contexts": ["lint"]}
        collector = GitHubCollector(config(), api, clock=lambda: NOW)
        self.assertEqual([row["name"] for row in self.pull(api, collector._branch_rules(REPO, "main"))["expected"]], ["lint"])
        api.one_values[protection] = ApiError("not_found")
        self.assertEqual(collector._branch_rules(REPO, "main"), [])
        with self.assertRaises(ApiError):
            required_contexts({"checks": ["lint"]})

    def test_a_refused_classic_protection_read_keeps_ruleset_evidence(self):
        protection = f"repos/{REPO}/branches/main/protection/required_status_checks"
        pulls = f"repos/{REPO}/pulls?state=open&per_page=100"
        rules = f"repos/{REPO}/rules/branches/main?per_page=100"
        api = joined_api()
        api.item_values[pulls] = [{**pull_row(), "base": {"ref": "main"}}]
        api.item_values[rules] = [{"type": "required_status_checks", "parameters": {
            "required_status_checks": [{"context": "deploy/preview"}]}}]
        api.one_values[protection] = ApiError("forbidden")
        api.one_values[f"repos/{REPO}/pulls/3"] = {"head": {"sha": SHA}}
        result = GitHubCollector(config(), api, clock=lambda: NOW)._repository(REPO, {"subscription": "subscribed"})
        self.assertEqual([row["name"] for row in result["pulls"][0]["expected"]], ["deploy/preview"])
        self.assertEqual(result["errors"], [])

    def test_rules_are_read_once_per_base_branch(self):
        pulls, rules = f"repos/{REPO}/pulls?state=open&per_page=100", f"repos/{REPO}/rules/branches/main?per_page=100"
        api = joined_api()
        api.item_values[pulls] = [{**pull_row(), "base": {"ref": "main"}}, {**pull_row(), "base": {"ref": "main"}}]
        api.item_values[rules] = []
        api.one_values[f"repos/{REPO}/branches/main/protection/required_status_checks"] = {"checks": [], "contexts": []}
        api.one_values[f"repos/{REPO}/pulls/3"] = {"head": {"sha": SHA}}
        result = GitHubCollector(config(), api, clock=lambda: NOW)._repository(REPO, {"subscription": "subscribed"})
        self.assertEqual(len(result["pulls"]), 2)
        protection = f"repos/{REPO}/branches/main/protection/required_status_checks"
        self.assertEqual([call for call in api.calls if call[1] == rules], [("items", rules, None)])
        self.assertEqual([call for call in api.calls if call[1] == protection], [("one", protection)])


    def test_a_plan_without_rulesets_still_shows_the_pull_requests_evidence(self):
        # A private repository on a free personal account answers the branch-rules request with 403.
        pulls, rules = f"repos/{REPO}/pulls?state=open&per_page=100", f"repos/{REPO}/rules/branches/main?per_page=100"
        api = joined_api()
        api.item_values[pulls] = [{**pull_row(), "base": {"ref": "main"}}]
        api.item_values[rules] = ApiError("forbidden")
        api.one_values[f"repos/{REPO}/branches/main/protection/required_status_checks"] = ApiError("not_found")
        result = GitHubCollector(config(), api, clock=lambda: NOW)._repository(REPO, {"subscription": "subscribed"})
        self.assertTrue(result["pulls"][0]["evidence_available"])
        self.assertEqual(result["pulls"][0]["expected"], [])
        self.assertEqual(result["errors"], [])

    def test_other_branch_rule_failures_still_mark_the_pull_unavailable(self):
        pulls, rules = f"repos/{REPO}/pulls?state=open&per_page=100", f"repos/{REPO}/rules/branches/main?per_page=100"
        api = joined_api()
        api.item_values[pulls] = [{**pull_row(), "base": {"ref": "main"}}]
        api.item_values[rules] = ApiError("unavailable")
        result = GitHubCollector(config(), api, clock=lambda: NOW)._repository(REPO, {"subscription": "subscribed"})
        self.assertFalse(result["pulls"][0]["evidence_available"])
        self.assertEqual(result["errors"], [{"pull": 3, "code": "unavailable"}])
