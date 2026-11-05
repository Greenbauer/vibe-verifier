"""A pull request row lists the checks GitHub's own pull request page lists for its head.

The real collector reads tests/dashboard_fake_checks.py, a head where a naive reading goes wrong
in each way the dashboard once did: a repeated run of the same workflow, a queued rerun, a check a
ruleset requires and one classic branch protection requires that never report, and a second
repository on the same base branch that requires less. The row's checks, statuses and Expected
checks must be exactly the page's list: the newest run of each check, and each required check
nothing reported."""

import unittest

from dashboard.config import BotDefinition, Config
from dashboard.gh_api import GitHubAPI
from dashboard.github import GitHubCollector

from dashboard_fake_checks import CheckedHead
from dashboard_fake_github import NOW, OWNER, REPO


def collect(fake, names):
    config = Config(OWNER, tuple(names), {"reviewer": BotDefinition("review.yml", ("review",))}, None)
    api = GitHubAPI(runner=fake, clock=lambda: NOW.timestamp(), max_calls=5000)
    result = GitHubCollector(config, api, clock=lambda: NOW).collect()
    return {row["repository"]: row["pulls"][0] for row in result["repositories"] if row.get("pulls")}


def listed(row):
    shown = [(check["name"], check["conclusion"] if check["status"] == "completed" else check["status"])
             for check in row.get("checks", []) + row.get("statuses", [])]
    return sorted(shown + [(check["name"], "expected") for check in row.get("expected", [])])


class Verdict(unittest.TestCase):
    def test_the_row_lists_what_the_pull_request_page_lists(self):
        fake = CheckedHead()
        self.assertEqual(fake.page(), [("External analysis", "queued"), ("build", "expected"), ("gate", "success"),
                                       ("lint", "expected"), ("test", "success")])
        self.assertEqual(listed(collect(fake, [REPO]).get(REPO, {})), fake.page())

    def test_each_repository_on_the_same_base_keeps_its_own_required_checks(self):
        other = CheckedHead(OWNER + "/other", required=("test",), protected=())
        fake = CheckedHead(siblings=(other,))
        rows = collect(fake, [REPO, other.repo])
        for name, world in ((REPO, fake), (other.repo, other)):
            with self.subTest(repository=name):
                self.assertEqual(listed(rows.get(name, {})), world.page())


if __name__ == "__main__":
    unittest.main()
