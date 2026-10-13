"""The latest push's label: the kind of commit, a sync with the base, or a merge of another branch."""
import unittest

from dashboard.pull_signals import classify_push, link_merged_pulls


def pushed(headline, at, parents=1, committed=None):
    return {"headline": headline, "pushed_at": at, "committed_at": committed or at, "parents": parents}


class PushKind(unittest.TestCase):
    def test_the_newest_push_is_labeled_and_older_pushes_are_ignored(self):
        earlier, later = "2026-09-29T12:00:00Z", "2026-09-30T14:00:00Z"
        rows = [pushed("feat: add the gate", earlier), pushed("fix(scope): close it", later),
                pushed("Merge branch 'main' into fix/gate", later, parents=2)]
        self.assertEqual(classify_push(rows, "main"), {"pushed_at": later, "kind": "bug fix"})
        self.assertEqual(classify_push([pushed("feat(quote)!: dates", later)], "main")["kind"], "feature")
        self.assertEqual(classify_push([pushed("feature: dates", later)], "main")["kind"], "feature")
        self.assertEqual(classify_push([pushed("docs: notes", later)], "main")["kind"], "docs")
        self.assertEqual(classify_push([pushed("hotfix: stop", later)], "main")["kind"], "bug fix")
        self.assertEqual(classify_push([pushed("wip: scratch", later)], "main")["kind"], "work in progress")
        self.assertEqual(classify_push([pushed("please review", later)], "main")["kind"], "update")
        self.assertEqual(classify_push([pushed("fix:", later)], "main")["kind"], "update")
        self.assertIsNone(classify_push([], "main"))

    def test_a_base_merge_is_a_sync_unless_the_push_also_has_other_work(self):
        at = "2026-09-30T14:10:00Z"
        sync = pushed("Merge remote-tracking branch 'origin/main' into feat/quote", at, parents=2)
        self.assertEqual(classify_push([pushed("feat: add", "2026-09-29T12:00:00Z"), sync], "main"),
                         {"pushed_at": at, "kind": "sync with main"})
        self.assertEqual(classify_push([sync, pushed("fix: close", at)], "main")["kind"], "bug fix")
        develop = pushed("Merge branch 'develop' into feat/quote", at, parents=2)
        self.assertEqual(classify_push([develop], "develop")["kind"], "sync with develop")
        self.assertEqual(classify_push([develop], "main"),
                         {"pushed_at": at, "kind": "merged in develop", "merged_branch": "develop"})
        self.assertEqual(classify_push([pushed("sync with main", at)], "main")["kind"], "sync with main")

    def test_a_hand_written_merge_of_the_base_is_a_sync(self):
        at = "2026-10-13T12:33:48Z"
        for headline in ("Merge main (#43 keep the banner readable) into feat/rates-table…",
                         "Merge main into fix/sign-in-copy",
                         "Merge origin/main into fix/x",
                         "Merge branch 'main' of github.com:octocat/example into fix/x"):
            self.assertEqual(classify_push([pushed(headline, at, parents=2)], "main"),
                             {"pushed_at": at, "kind": "sync with main"}, headline)
        # One parent is a commit that only says "Merge", not a merge.
        self.assertEqual(classify_push([pushed("Merge main navigation into the header", at)], "main")["kind"], "update")

    def test_a_merge_of_another_branch_names_the_branch(self):
        at = "2026-10-13T12:35:48Z"
        stacked = pushed("Merge remote-tracking branch 'origin/feat/rates-table' into …", at, parents=2)
        self.assertEqual(classify_push([stacked], "main"),
                         {"pushed_at": at, "kind": "merged in feat/rates-table",
                          "merged_branch": "feat/rates-table"})
        long_branch = pushed("Merge branch 'feat/a-branch-name-well-past-the-label-width' into x", at, parents=2)
        self.assertEqual(classify_push([long_branch], "main")["kind"], "merged in feat/a-branch-name-well-past-t")
        github = pushed("Merge pull request #12 from octocat/feat/dates", at, parents=2)
        self.assertEqual(classify_push([github], "main")["merged_branch"], "feat/dates")
        self.assertEqual(classify_push([pushed("Merge commit 'abc123' into fix/x", at, parents=2)], "main"),
                         {"pushed_at": at, "kind": "merged another branch"})
        self.assertEqual(classify_push([stacked, pushed("fix: close", at)], "main"),
                         {"pushed_at": at, "kind": "bug fix"})

    def test_a_merged_branch_that_is_an_open_pull_names_that_pull(self):
        at = "2026-10-13T12:35:48Z"
        found = {
            12: {"head_ref": "feat/rates-table", "push": {"pushed_at": at, "kind": "sync with main"}},
            13: {"head_ref": "feat/quote-lines", "push": {"pushed_at": at, "kind": "merged in feat/rates-table",
                                                                "merged_branch": "feat/rates-table"}},
            900: {"head_ref": "fix/x", "push": {"pushed_at": at, "kind": "merged in gone/branch",
                                                 "merged_branch": "gone/branch"}},
            901: {"head_ref": "fix/y", "push": None},
            902: {"head_ref": "Feat/Own", "push": {"pushed_at": at, "kind": "merged in feat/own",
                                                    "merged_branch": "feat/own"}},
        }
        linked = link_merged_pulls(found)
        # A `git pull` that merged the pull request's own remote branch is an update, matched without case.
        self.assertEqual(linked[902]["push"], {"pushed_at": at, "kind": "update"})
        self.assertEqual(linked[13], {"push": {"pushed_at": at, "kind": "picked up #12"}})
        self.assertEqual(linked[900]["push"], {"pushed_at": at, "kind": "merged in gone/branch"})
        self.assertEqual(linked[12]["push"]["kind"], "sync with main")
        self.assertNotIn("head_ref", linked[901])

    def test_without_a_push_time_the_newest_commit_time_stands_in(self):
        row = {"headline": "fix: close", "pushed_at": None, "committed_at": "2026-09-30T13:00:00Z", "parents": 1}
        older = {"headline": "feat: add", "pushed_at": None, "committed_at": "2026-09-29T13:00:00Z", "parents": 1}
        self.assertEqual(classify_push([older, row], "main"),
                         {"pushed_at": "2026-09-30T13:00:00Z", "kind": "bug fix"})
        self.assertIsNone(classify_push([{"headline": "fix: x", "pushed_at": None, "committed_at": None, "parents": 1}], "main"))


if __name__ == "__main__":
    unittest.main()
