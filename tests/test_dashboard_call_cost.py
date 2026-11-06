"""A refresh's GitHub cost follows what is open, not how much history there is.

The real collector runs against tests/dashboard_fake_github.py, and the history grows while the
open work stays the same (one pull request, one head):

- more check suites on the head, and a longer week of bot runs, add no first-page reads of any
  kind (a later page of the same listing is GitHub's paging, not a new read);
- a refresh with nothing changed is answered from kept responses: no REST read is answered in full
  again (each is a 304, or the same 404 as before);
- no GraphQL query costs more than 2 points (GitHub's own formula) with one pull request open.

The explorer bot names a job that never runs, so bot history has to stop looking for it."""

import unittest
from urllib.parse import parse_qsl, urlsplit

from dashboard.config import BotDefinition, Config
from dashboard.gh_api import GitHubAPI
from dashboard.github import GitHubCollector

from dashboard_fake_github import NOW, REPO, FakeGitHub
from dashboard_fake_graphql import graphql_cost


def config():
    return Config("octocat", (REPO,), {"reviewer": BotDefinition("review.yml", ("review",)),
                                       "explorer": BotDefinition("explore.yml", ("explore-gate",))}, None)


def collector(fake):
    # A budget no refresh here reaches, so a capped read cannot hide a growing one.
    api = GitHubAPI(runner=fake, clock=lambda: NOW.timestamp(), max_calls=5000)
    return GitHubCollector(config(), api, clock=lambda: NOW)


def first_pages(fake: FakeGitHub) -> dict[str, int]:
    counts: dict[str, int] = {}
    for read in fake.log:
        if int(dict(parse_qsl(urlsplit(read.endpoint).query)).get("page", "1")) == 1:
            counts[read.family] = counts.get(read.family, 0) + 1
    return counts


def one_pass(**history) -> dict[str, int]:
    fake = FakeGitHub(**history)
    collector(fake).collect()
    return first_pages(fake)


class CallCost(unittest.TestCase):
    def assert_no_kind_grows(self, small: dict, large: dict, what: str):
        for kind in sorted(large):
            with self.subTest(family=kind):
                self.assertLessEqual(large[kind], small.get(kind, 0),
                                     "%s made %s reads of %s, up from %s" % (what, large[kind], kind, small.get(kind, 0)))

    def test_more_check_suites_on_the_head_cost_no_more_reads(self):
        small, large = one_pass(suites=3), one_pass(suites=40)
        self.assertIn("repos/*/*/commits/*/check-suites", small)
        self.assert_no_kind_grows(small, large, "40 check suites")

    def test_a_longer_week_of_bot_runs_costs_no_more_reads(self):
        small, large = one_pass(history=60), one_pass(history=250)
        self.assertIn("repos/*/*/actions/workflows/*/runs", small)
        self.assert_no_kind_grows(small, large, "250 runs of each bot workflow")

    def test_an_unchanged_refresh_is_answered_from_kept_responses(self):
        fake = FakeGitHub()
        reader = collector(fake)
        reader.collect()
        first = [read for read in fake.log if read.family not in ("graphql", "rate_limit")]
        fake.log.clear()
        reader.collect()
        full = sorted({read.family for read in fake.log
                       if read.family not in ("graphql", "rate_limit") and read.status == 200})
        self.assertGreater(len(first), 10)
        self.assertEqual(full, [], "an unchanged refresh read these again in full")

    def test_the_fake_prices_a_query_as_github_did(self):
        # The pull-signals query before #118 asked for 50 pull requests, 100 threads (1 comment each) and
        # 20 commits (1 parent each); GitHub charged it 61 points (#118). One pull request costs 1.
        query = ("query($count: Int!) { repository(owner: \"o\", name: \"r\") { pullRequests(first: $count) { nodes {"
                 " reviewThreads(first: 100) { nodes { comments(first: 1) { totalCount } } }"
                 " commits(last: 20) { nodes { commit { parents(first: 1) { totalCount } } } } } } } }")
        self.assertEqual(graphql_cost(query, {"count": "50"}), 61)
        self.assertEqual(graphql_cost(query, {"count": "1"}), 1)

    def test_no_graphql_query_costs_more_than_two_points_with_one_pull_request_open(self):
        fake = FakeGitHub(closed_pulls=30)
        reader = collector(fake)
        for _ in range(2):
            reader.collect()
        costs = [read.points for read in fake.log if read.family == "graphql"]
        self.assertTrue(costs)
        self.assertLessEqual(max(costs), 2, costs)


if __name__ == "__main__":
    unittest.main()
