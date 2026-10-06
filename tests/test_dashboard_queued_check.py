"""A queued rerun must replace the older pass, which is what GitHub's pull request page shows."""

import unittest

from dashboard.github import GitHubCollector

try:
    from test_dashboard_github import NOW, config, endpoints, joined_api, pull_row
except ModuleNotFoundError:  # `unittest discover -s tests` puts tests/ on the path; a package import does not.
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from test_dashboard_github import NOW, config, endpoints, joined_api, pull_row

REPO_PULL = pull_row()


class QueuedCheck(unittest.TestCase):
    def test_a_queued_rerun_with_no_timestamps_replaces_the_older_pass(self):
        # filter=latest drops this queued run. The higher id is the current attempt.
        api = joined_api()
        paths = endpoints()
        done = {**api.item_values[paths["checks"]][0], "conclusion": "success"}
        queued = {**done, "id": 95, "status": "queued", "conclusion": None, "started_at": None, "completed_at": None}
        api.item_values[paths["checks"]] = [done, queued]
        result = GitHubCollector(config(), api, clock=lambda: NOW)._pull(
            "octocat/example", REPO_PULL, {"subscription": "subscribed"})
        self.assertEqual([(row["id"], row["category"]) for row in result["checks"]], [(95, "pending")])
