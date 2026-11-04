"""apply-down --auto --act --merge (gates/_pin_merge.py): an open pin bump is squash-merged once it is merge-ready, and
only then, against a stand-in for GitHub's REST answers that records every write."""
import base64
import contextlib
import io
import unittest
from unittest import mock

import helpers  # noqa: F401  (puts gates/ on the path)
import _pin_bump
import _pin_merge
from _pin_bump import CannotRead

REPO = "acme/app"
OLD, NEW, HEAD, BASE = "1" * 40, "2" * 40, "3" * 40, "4" * 40
STUB = ("jobs:\n  gates:\n    steps:\n      - uses: Greenbauer/vibe-verifier/actions/gates@%s # main 2026-11-01\n"
        "        with:\n          entries: x\n      - uses: actions/checkout@%s # v7\n")


def pull(number=3, ref="ci/vibe-verifier-pin-" + NEW[:10], owner="acme"):
    return {"number": number, "head": {"ref": ref, "repo": {"full_name": "%s/app" % owner}}}


class PagedStatuses(dict):
    """A combined status whose list is served a page at a time (see FakeGitHub.gh_json)."""

    def __init__(self, statuses):
        super().__init__(statuses=statuses)


class FakeGitHub:
    """The answers a bump's reads get, by endpoint (query left out, but a file's `ref`), and the writes it made."""

    def __init__(self):
        self.writes, self.closed, self.refuse = [], [], {}
        self.files = {("x.yml", BASE): STUB % (OLD, "c" * 40), ("x.yml", HEAD): STUB % (NEW, "c" * 40)}
        self.answers = {
            "repos/acme/app": {"default_branch": "main"},
            "repos/acme/app/pulls": [pull()],
            "repos/acme/app/pulls/3": {"head": {"sha": HEAD, "ref": pull()["head"]["ref"]}, "base": {"ref": "main"},
                                       "title": "ci: bump the Vibe Verifier action pin to " + NEW[:10],
                                       "changed_files": 1, "mergeable": True, "mergeable_state": "clean"},
            "repos/acme/app/compare/main...%s" % HEAD: {"behind_by": 0, "merge_base_commit": {"sha": BASE},
                                                        "files": [{"filename": ".github/workflows/x.yml", "status": "modified"}]},
            "repos/acme/app/commits/%s/check-runs" % HEAD: {"check_runs": [
                {"name": "Gates", "status": "completed", "conclusion": "success"},
                {"name": "E2E tests: run", "status": "completed", "conclusion": "skipped"}]},
            "repos/acme/app/commits/%s/status" % HEAD: {"statuses": [{"context": "Vercel", "state": "success"}]},
            "repos/acme/app/pulls/3/reviews": [],
        }

    def gh_json(self, endpoint, method=None, fields=None, body=None):
        path, _, query = endpoint.partition("?")
        if method:
            self.writes.append((method, path, body))
            if path in self.refuse:
                raise CannotRead(self.refuse[path])
            return {}
        if "/contents/" in path:
            text = self.files[(path.split("/contents/.github/workflows/")[1], query.split("ref=")[1])]
            return {"encoding": "base64", "content": base64.b64encode(text.encode()).decode()}
        if path not in self.answers:
            raise CannotRead("HTTP 404 for %s" % path)
        answer = self.answers[path]
        if isinstance(answer, PagedStatuses):
            page = int(query.split("page=")[-1])
            return {"statuses": answer["statuses"][(page - 1) * 100:page * 100]}
        return answer

    def close(self, repo, number, comment):
        self.closed.append((repo, number, comment))


class PinMerge(unittest.TestCase):
    def setUp(self):
        self.github = FakeGitHub()
        self.on_main = {"ci/vibe-verifier-pin-": lambda short: NEW.startswith(short) or OLD.startswith(short),
                        "ci/wrapper-pin-": lambda short: NEW.startswith(short)}
        for name in ("gh_json", "close"):
            patcher = mock.patch.object(_pin_bump, name, getattr(self.github, name))
            patcher.start()
            self.addCleanup(patcher.stop)

    @property
    def pr(self):
        return self.github.answers["repos/acme/app/pulls/3"]

    @property
    def runs(self):
        return self.github.answers["repos/acme/app/commits/%s/check-runs" % HEAD]["check_runs"]

    def run_pass(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = _pin_merge.merge_pass([REPO], lambda prefix: self.on_main.get(prefix, lambda short: False))
        return code, out.getvalue()

    def assert_left(self, code, text):
        result, out = self.run_pass()
        self.assertEqual(result, code, out)
        self.assertIn(text, out)
        self.assertEqual(self.github.writes, [], "a bump that is not ready is never written to")
        self.assertEqual(self.github.closed, [])

    def test_a_ready_bump_is_squash_merged_at_the_head_it_judged_and_its_branch_deleted(self):
        code, out = self.run_pass()
        self.assertEqual(code, 0, out)
        self.assertIn("#3 squash-merged at %s" % HEAD[:10], out)
        self.assertEqual(self.github.writes, [
            ("PUT", "repos/acme/app/pulls/3/merge", {"merge_method": "squash", "sha": HEAD,
                                                     "commit_title": "ci: bump the Vibe Verifier action pin to %s (#3)" % NEW[:10]}),
            ("DELETE", "repos/acme/app/git/refs/heads/ci/vibe-verifier-pin-" + NEW[:10], None)])

    def test_a_branch_already_deleted_does_not_fail_the_merge(self):
        self.github.refuse["repos/acme/app/git/refs/heads/ci/vibe-verifier-pin-" + NEW[:10]] = "Reference does not exist"
        code, out = self.run_pass()
        self.assertEqual(code, 0, out)
        self.assertIn("squash-merged", out)

    def test_only_this_tools_own_bump_branches_are_judged(self):
        self.github.answers["repos/acme/app/pulls"] = [pull(4, owner="someone"), pull(5, ref="feature/x")]
        self.assertEqual(self.run_pass(), (0, ""))
        self.assertEqual(self.github.writes, [])

    def test_a_line_that_is_not_a_catalog_pin_holds_the_bump(self):
        head = STUB % (NEW, "c" * 40)
        for changed, why in [
            (head.replace("entries: x", "entries: y"), "changes a line that is not a pin to %s: entries: y" % NEW[:10]),
            (STUB % (NEW, "d" * 40), "changes a line that is not a pin to %s: - uses: actions/checkout@" % NEW[:10]),
            (head.replace("actions/gates@", "actions/criteria@"), "changes a line that is not a pin to %s: - uses: Gr" % NEW[:10]),
            (STUB % ("f" * 40, "c" * 40), "changes a line that is not a pin to %s: - uses: Greenbauer" % NEW[:10]),
            # The branch's ten digits, then a commit the catalog's main does not have: a fork's, made to match.
            (STUB % (NEW[:10] + "e" * 30, "c" * 40), "changes a line that is not a pin to %s: - uses: Greenbauer" % NEW[:10]),
            (head + "permissions: write-all\n", "adds or removes lines")]:
            with self.subTest(why=why):
                self.github.files[("x.yml", HEAD)] = changed
                self.assert_left(1, "held: .github/workflows/x.yml " + why)

    def test_a_branch_named_for_a_commit_off_the_catalogs_main_is_held(self):
        # A pushed branch could pin an older or a fork's commit, whose own run of the gate job would pass.
        branch = "ci/vibe-verifier-pin-" + "f" * 10
        self.github.answers["repos/acme/app/pulls"] = [pull(ref=branch)]
        self.pr["head"]["ref"] = branch
        self.github.files[("x.yml", HEAD)] = STUB % ("f" * 40, "c" * 40)
        self.assert_left(1, "held: ffffffffff is not a commit on the main it pins from")

    def test_a_pin_to_an_older_catalog_commit_than_the_branch_names_is_held(self):
        self.github.files[("x.yml", HEAD)] = STUB % (OLD.replace("1", "5", 1), "c" * 40)
        self.assert_left(1, "is not a pin to %s" % NEW[:10])

    def test_a_bump_into_another_branch_than_the_default_is_held(self):
        self.pr["base"]["ref"] = "release"
        self.assert_left(1, "held: it is into release, not the default branch")

    def test_a_changed_file_other_than_a_workflow_holds_the_bump(self):
        compare = self.github.answers["repos/acme/app/compare/main...%s" % HEAD]
        for files, why in [([{"filename": ".github/CODEOWNERS", "status": "modified"}], ".github/CODEOWNERS is modified"),
                           ([{"filename": ".github/workflows/y.yml", "status": "added"}], "y.yml is added"),
                           ([], "GitHub listed 0 changed files")]:
            with self.subTest(why=why):
                compare["files"] = files
                self.assert_left(1, why)

    def test_a_wrapper_bump_may_move_only_its_own_owners_workflow_pins(self):
        branch = "ci/wrapper-pin-" + NEW[:10]
        self.github.answers["repos/acme/app/pulls"] = [pull(ref=branch)]
        self.pr["head"]["ref"] = branch
        self.runs[0]["name"] = "Call the wrapper"  # a wrapper bump's proof is its own run of the called workflow
        call = "jobs:\n  ci:\n    uses: %s/ci/.github/workflows/gates.yml@%s # main 2026-11-01\n"
        self.github.files[("x.yml", BASE)], self.github.files[("x.yml", HEAD)] = call % ("acme", OLD), call % ("acme", NEW)
        code, out = self.run_pass()
        self.assertEqual(code, 0, out)
        self.assertIn("squash-merged", out)
        self.github.writes = []
        self.github.files[("x.yml", BASE)], self.github.files[("x.yml", HEAD)] = call % ("other", OLD), call % ("other", NEW)
        self.assert_left(1, "changes a line that is not a pin")

    def test_a_bump_behind_its_base_has_the_base_merged_in_and_is_not_merged(self):
        self.github.answers["repos/acme/app/compare/main...%s" % HEAD]["behind_by"] = 2
        code, out = self.run_pass()
        self.assertEqual(code, 0, out)
        self.assertIn("2 commit(s) behind main: merged main in", out)
        self.assertEqual(self.github.writes, [("PUT", "repos/acme/app/pulls/3/update-branch", {"expected_head_sha": HEAD})])

    def test_a_conflicting_bump_or_one_with_nothing_left_is_closed(self):
        for change, why in [({"mergeable": False, "mergeable_state": "dirty"}, "Conflicts with main"),
                            ({"changed_files": 0}, "Nothing left to change: main already has these pins.")]:
            with self.subTest(why=why):
                self.github.closed = []
                self.pr.update(change)
                code, out = self.run_pass()
                self.assertEqual(code, 0, out)
                self.assertEqual([(repo, number) for repo, number, _ in self.github.closed], [(REPO, 3)])
                self.assertIn(why, self.github.closed[0][2])
                self.assertEqual(self.github.writes, [])
                self.pr.update({"mergeable": True, "mergeable_state": "clean", "changed_files": 1})

    def test_a_check_still_running_waits_and_a_failed_one_holds(self):
        self.runs.append({"name": "Code review", "status": "in_progress", "conclusion": None})
        self.assert_left(0, "#3 waiting: 1 check(s) still running")
        self.runs[-1].update(status="completed", conclusion="failure")
        self.assert_left(1, "#3 held: Code review failed on its head")
        self.runs[-1].update(conclusion="cancelled")
        self.assert_left(1, "Code review failed")
        self.runs.pop()
        statuses = self.github.answers["repos/acme/app/commits/%s/status" % HEAD]["statuses"]
        statuses[0]["state"] = "pending"
        self.assert_left(0, "1 check(s) still running")
        # Past the first page of statuses, where a failure still counts.
        statuses[:] = [{"context": "s%d" % n, "state": "success"} for n in range(100)] + [{"context": "late", "state": "failure"}]
        self.github.answers["repos/acme/app/commits/%s/status" % HEAD] = PagedStatuses(statuses)
        self.assert_left(1, "held: late failed on its head")

    def test_a_bump_whose_gate_job_did_not_pass_is_held(self):
        self.runs[0]["conclusion"] = "skipped"
        self.assert_left(1, "no gate job (Gates or vibe-verifier-ok) passed on its head")
        self.runs[0].update(name="vibe-verifier-ok", conclusion="success")
        self.assertIn("squash-merged", self.run_pass()[1])

    def test_a_review_asking_for_changes_holds_until_that_reviewer_approves(self):
        reviews = self.github.answers["repos/acme/app/pulls/3/reviews"]
        reviews += [{"user": {"login": "Greenbauer"}, "state": "CHANGES_REQUESTED"}, {"user": {"login": "x"}, "state": "COMMENTED"}]
        self.assert_left(1, "held: Greenbauer asked for changes")
        reviews.append({"user": {"login": "Greenbauer"}, "state": "APPROVED"})
        self.assertIn("squash-merged", self.run_pass()[1])

    def test_github_still_computing_waits_and_any_state_but_clean_holds(self):
        self.pr["mergeable"] = None
        self.assert_left(0, "waiting: GitHub is still working out whether it merges")
        self.pr["mergeable"] = True
        for state in ("unstable", "unknown"):  # computed before the last check finished, or not yet
            self.pr["mergeable_state"] = state
            self.assert_left(0, "waiting: GitHub says it is %s" % state)
        for state in ("blocked", "behind", "draft"):
            self.pr["mergeable_state"] = state
            self.assert_left(1, "held: GitHub says it is %s" % state)
        self.pr["mergeable_state"] = "has_hooks"
        self.assertIn("squash-merged", self.run_pass()[1])

    def test_a_merge_github_refuses_fails_the_run_and_the_next_bump_is_still_judged(self):
        self.github.answers["repos/acme/app/pulls"] = [pull(), pull(7)]
        self.github.answers["repos/acme/app/pulls/7"] = dict(self.pr, changed_files=0)
        self.github.refuse["repos/acme/app/pulls/3/merge"] = "Changes must be made through a pull request. (HTTP 405)"
        code, out = self.run_pass()
        self.assertEqual(code, 2, out)
        self.assertIn("#3 failed: Changes must be made through a pull request. (HTTP 405)", out)
        self.assertIn("#7 closed: Nothing left to change", out)


if __name__ == "__main__":
    unittest.main()
