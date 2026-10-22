"""The "Agent mergeable" pill's data: the configured merge policy, the catalog's decision fed through the
dashboard's API, one reading per head, and the collector's merge_grant field."""

import base64
import copy
import json
import os
import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

from dashboard.config import ConfigError, load_config
from dashboard.gh_api import ApiError
from dashboard.github import GitHubCollector
from dashboard.merge_grant import MergeGrants, load_policy

from test_dashboard_config_telemetry import config_value
from test_dashboard_github import REPO, SHA, config, joined_api, pull_row

NOW = datetime(2026, 9, 30, 15, 0, tzinfo=timezone.utc)
DEFAULT = load_policy(Path(__file__).resolve().parents[1] / "gates" / "_merge_grant.default.json")


def encoded(text):
    return {"encoding": "base64", "content": base64.b64encode(text.encode()).decode()}


class API:
    """Files and contents of one pull request; every call is recorded."""

    def __init__(self, files, contents=None, fail=None):
        self.files, self.contents, self.fail, self.calls = files, contents or {}, fail, []

    def items(self, endpoint, key=None):
        self.calls.append(endpoint)
        if self.fail:
            raise ApiError(self.fail)
        return copy.deepcopy(self.files)

    def one(self, endpoint):
        self.calls.append(endpoint)
        if endpoint not in self.contents:
            raise ApiError("not_found")
        return encoded(self.contents[endpoint])


def current(head="h1", base="b1", count=1, title="docs: notes", number=3):
    return {"number": number, "title": title, "changed_files": count, "head": {"sha": head}, "base": {"sha": base}}


class Grants(unittest.TestCase):
    def test_eligible_and_not(self):
        grants = MergeGrants(DEFAULT)
        self.assertEqual(grants.grant(API([{"status": "modified", "filename": "docs/a.md"}]), REPO, current()),
                         "non-product grant")
        self.assertIsNone(grants.grant(API([{"status": "modified", "filename": "src/a.ts"}]), REPO, current(number=4)))

    def test_one_reading_per_head_base_and_title(self):
        grants, api = MergeGrants(DEFAULT), API([{"status": "modified", "filename": "docs/a.md"}])
        grants.grant(api, REPO, current())
        grants.grant(api, REPO, current())
        self.assertEqual(len(api.calls), 1)
        grants.grant(api, REPO, current(head="h2"))
        grants.grant(api, REPO, current(head="h2", base="b2"))
        self.assertEqual(len(api.calls), 3)
        grants.grant(api, REPO, current(head="h2", base="b2", title="feat: x"))   # a title grant reads the title
        self.assertEqual(len(api.calls), 4)

    def test_contents_are_read_at_the_base_and_the_head_and_a_missing_file_is_absent(self):
        api = API([{"status": "added", "filename": "package.json"}],
                  {"repos/%s/contents/package.json?ref=h1" % REPO: json.dumps({"dependencies": {"a": "1"}})})
        self.assertEqual(MergeGrants(DEFAULT).grant(api, REPO, current()), "non-product grant")
        self.assertEqual(api.calls[1:], ["repos/%s/contents/package.json?ref=b1" % REPO,
                                         "repos/%s/contents/package.json?ref=h1" % REPO])

    def test_an_empty_pull_request_is_a_no_as_the_command_says(self):
        self.assertIsNone(MergeGrants(DEFAULT).grant(API([]), REPO, current(count=0)))

    def test_past_github_3000_file_listing_the_no_lasts(self):
        grants, api = MergeGrants(DEFAULT), API([{"status": "modified", "filename": "docs/%d.md" % n} for n in range(3000)])
        self.assertIsNone(grants.grant(api, REPO, current(count=3001)))
        grants.grant(api, REPO, current(count=3001))
        self.assertEqual(len(api.calls), 1)

    def test_a_short_listing_or_a_failed_read_asks_again(self):
        grants, short = MergeGrants(DEFAULT), API([{"status": "modified", "filename": "docs/a.md"}])
        self.assertIsNone(grants.grant(short, REPO, current(count=2)))
        self.assertEqual(grants.grant(short, REPO, current(count=1)), "non-product grant")   # the listing caught up
        spent = API([], fail="request_budget_exhausted")
        self.assertIsNone(grants.grant(spent, REPO, current(number=5)))
        grants.grant(spent, REPO, current(number=5))
        self.assertEqual(len(spent.calls), 2)


class ClearCache(unittest.TestCase):
    def test_clearing_the_private_cache_drops_answers_and_keeps_the_policy(self):
        api = joined_api()
        api.clear_cache = lambda: None
        collector = GitHubCollector(replace(config(), merge_policy=DEFAULT), api, clock=lambda: NOW)
        collector._grants._known[(REPO, 3)] = (("h", "b", "t"), "non-product grant")
        collector.clear_private_cache()
        self.assertIs(collector._grants.policy, DEFAULT)
        self.assertEqual(collector._grants._known, {})


class PolicyConfig(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="vv-grant-")
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)

    def load(self, policy):
        path = self.root / "merge-policy.json"
        path.write_text(json.dumps(policy), encoding="utf-8")
        os.chmod(path, 0o600)
        dashboard = self.root / "dashboard.json"
        dashboard.write_text(json.dumps({**config_value(), "merge_policy_file": str(path)}), encoding="utf-8")
        return load_config(dashboard)

    def test_a_policy_file_is_loaded_and_checked(self):
        loaded = self.load({"version": 1, "extends": "default", "grants": [{"name": "w", "repos": ["octocat/*"]}]})
        self.assertEqual([grant["name"] for grant in loaded.merge_policy["grants"]], ["w"])
        with self.assertRaisesRegex(ConfigError, "merge_policy_file: policy version must be 1"):
            self.load({"grants": []})
        dashboard = self.root / "relative.json"
        dashboard.write_text(json.dumps({**config_value(), "merge_policy_file": "policy.json"}), encoding="utf-8")
        with self.assertRaisesRegex(ConfigError, "absolute path"):
            load_config(dashboard)

    def test_no_policy_no_grant(self):
        dashboard = self.root / "plain.json"
        dashboard.write_text(json.dumps(config_value()), encoding="utf-8")
        self.assertIsNone(load_config(dashboard).merge_policy)


class Collector(unittest.TestCase):
    def collect(self, policy, files):
        api = joined_api()
        api.one_values["repos/%s/pulls/3" % REPO] = {"head": {"sha": SHA}, "base": {"sha": "b" * 40}, "number": 3,
                                                     "title": "Improve example", "changed_files": len(files)}
        api.item_values["repos/%s/pulls/3/files" % REPO] = [{"status": "modified", "filename": name} for name in files]
        collector = GitHubCollector(replace(config(), merge_policy=policy), api, clock=lambda: NOW)
        return collector._pull(REPO, pull_row(), {"subscription": "subscribed"})

    def test_the_pull_carries_the_grant_that_covers_it(self):
        self.assertEqual(self.collect(DEFAULT, ["README.md"])["merge_grant"], "non-product grant")
        self.assertIsNone(self.collect(DEFAULT, ["src/a.ts"])["merge_grant"])
        self.assertIsNone(self.collect(None, ["README.md"])["merge_grant"])



if __name__ == "__main__":
    unittest.main()
