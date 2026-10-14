"""A row says how far its head is behind the repository's default branch."""

import unittest

from dashboard.behind import behind_default
from dashboard.github import GitHubCollector
from dashboard.head_state import HEADS, apply_head_reading, read_head_page
from test_dashboard_fresh import NOW, Clock, HeadAPI, collect, listed, pull


class Behind(unittest.TestCase):
    def test_a_head_counts_default_branch_commits_it_lacks_only_against_that_base(self):
        def node(ahead=3, base="main", fork=False, default="main"):
            return {"baseRefName": base, "isCrossRepository": fork,
                    "headRef": {"compare": {"aheadBy": ahead}},
                    "repository": {"nameWithOwner": "octocat/one", "defaultBranchRef": {"name": default}}}
        self.assertEqual(behind_default(node()), (3, "main"))
        self.assertEqual(behind_default(node(ahead=0)), (0, "main"))
        self.assertEqual(behind_default(node(default="trunk", base="trunk")), (3, "trunk"))
        # A fork resolves HEAD in the fork; a pull request into another branch has another base.
        self.assertEqual(behind_default(node(fork=True)), (None, "main"))
        self.assertEqual(behind_default(node(base="release")), (None, "main"))
        self.assertEqual(behind_default({**node(), "headRef": None}), (None, "main"))
        self.assertEqual(behind_default({**node(), "repository": {"nameWithOwner": "octocat/one"}}), (None, None))
        self.assertIn('compare(headRef: "HEAD") { aheadBy }', HEADS)
        self.assertIn("defaultBranchRef { name }", HEADS)

    def test_a_pass_and_a_beat_both_put_the_distance_from_main_on_the_row(self):
        names = ("octocat/one",)
        api = HeadAPI(80, {"octocat/one": [pull("octocat/one", 4)]})
        api.behind[("octocat/one", 4)] = 5
        client = GitHubCollector(listed(*names), api, clock=Clock(NOW))
        client._kind = "User"
        row = collect(client, api, {"octocat/one": {"subscription": "unknown"}})["octocat/one"][0]
        self.assertEqual((row["behind_by"], row["default_branch"]), (5, "main"))
        github = {"owner": "octocat", "repositories": [{"repository": "octocat/one", "pulls": [row]}]}
        heads = read_head_page(api._heads())[0]
        heads[("octocat/one", 4)]["behind_by"] = 0
        updated = apply_head_reading(github, heads, NOW, 1, 1)
        self.assertEqual(updated["repositories"][0]["pulls"][0]["behind_by"], 0)


if __name__ == "__main__":
    unittest.main()
