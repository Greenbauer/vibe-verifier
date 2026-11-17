"""apply-down --auto --min-age-hours (gates/_pin_age.py, actions/pin-bump): an automatic bump delivers only a catalog
commit that has been on main for the minimum age, so a bad one can be reverted before it spreads."""
import subprocess
import time
import unittest

from helpers import ROOT, clean_env, git, make_repo, write
from test_apply_down import stub_pinned_to
from test_pin_bump import LANE, PinBumpHarness
import _pin_age  # noqa: E402  (gates/ is on the path through helpers)


class PinAge(PinBumpHarness):
    def aged_source(self, *hours_ago):
        """A catalog whose first commit (gates/a.py "0") and each later one are the given hours old; their SHAs."""
        self.source = make_repo(self, {"gates/a.py": "0", "tools/lint/package-lock.json": "1"})
        shas = []
        for index, hours in enumerate(hours_ago):
            if index:
                write(self.source, {"gates/a.py": str(index)})
            at = "@%d +0000" % (time.time() - hours * 3600)
            git(self.source, "add", "-A")
            subprocess.run(["git", "-C", self.source, "commit", "-q", "--allow-empty", "-m", "c%d" % index]
                           + (["--amend"] if not index else []), check=True, capture_output=True,
                           env=clean_env({"GIT_COMMITTER_DATE": at, "GIT_AUTHOR_DATE": at}))
            shas.append(self.head())
        return shas

    def test_it_pins_the_newest_catalog_commit_at_least_min_age_old(self):
        old, aged, fresh = self.aged_source(72, 30, 1)
        self.consumer("acme/app", stub_pinned_to(old))
        result = self.bump("--act", "--now", "--min-age-hours", "24")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("%s -> %s" % (old[:10], aged[:10]), result.stdout)
        written = [c for c in self.writes() if c["method"] == "PUT"]
        self.assertTrue(written and all(aged in c["content"] and fresh not in c["content"] for c in written))

    def test_with_no_commit_old_enough_nothing_is_bumped(self):
        old, _ = self.aged_source(30, 1)
        self.consumer("acme/app", stub_pinned_to(old))
        result = self.bump("--act", "--now", "--min-age-hours", "48")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("nothing to bump: no commit on main is 48h old yet", result.stdout)
        self.assertEqual(self.writes(), [])

    def test_a_pin_past_the_aged_commit_is_never_moved_back(self):
        _, _, young, _ = self.aged_source(72, 30, 2, 1)  # a release commit came after `young`, so it is stale
        self.consumer("acme/app", stub_pinned_to(young))
        result = self.bump("--act", "--now", "--min-age-hours", "24")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("pins are current", result.stdout)
        self.assertEqual(self.writes(), [])

    def test_overdue_counts_from_when_a_commit_became_old_enough(self):
        self.busy_lane()
        old, _ = self.aged_source(72, 26)
        self.consumer("acme/app", stub_pinned_to(old))
        result = self.bump("--act", "--lane", LANE, "--min-age-hours", "24")
        self.assertIn("waiting: the lane is busy", result.stdout)
        self.assertIn("2.0h old of 6h", result.stdout)
        self.assertEqual(self.writes(), [])
        old, _ = self.aged_source(72, 31)
        self.consumer("acme/app", stub_pinned_to(old))
        self.assertIn("bump now: the oldest release commit waiting is 7.0h old",
                      self.bump("--lane", LANE, "--min-age-hours", "24").stdout)

    def test_a_catalog_bump_merges_only_once_its_commit_is_min_age_old(self):
        _, aged, fresh = self.aged_source(72, 30, 1)
        young = _pin_age.too_young(self.source, 24)
        self.assertIsNone(young("ci/vibe-verifier-pin-", aged[:10]))
        self.assertEqual(young("ci/vibe-verifier-pin-", fresh[:10]),
                         "%s is 1.0h old; a bump merges once the commit it pins is 24h old" % fresh[:10])
        self.assertIn("of unknown age", young("ci/vibe-verifier-pin-", "f" * 10))
        self.assertIn("of unknown age", young("ci/vibe-verifier-pin-", "--output=x"), "never reaches git as an option")
        self.assertIsNone(young("ci/wrapper-pin-", fresh[:10]), "a wrapper bump is the wrapper's own")
        self.assertIsNone(_pin_age.too_young(self.source, 0)("ci/vibe-verifier-pin-", fresh[:10]))

    def test_the_action_waits_24_hours_unless_its_caller_says_otherwise(self):
        action = (ROOT / "actions" / "pin-bump" / "action.yml").read_text()
        self.assertIn('is this old.\n    default: "24"', action)
        self.assertIn('--min-age-hours "$MIN_AGE_HOURS"', action)


if __name__ == "__main__":
    unittest.main()
