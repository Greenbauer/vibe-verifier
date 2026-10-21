"""A re-walked feature whose Verify section the pull request changed is named, never blocked (gates/_verify_edits.py):
in `vibe-verifier features` and the acceptance-verdict log (gates/_features.py's describe), and in the QA review
comment's row for that feature (`vibe-verifier qa-review`). The re-walk reads the pull request's own copy of the
feature file, so a pull request can rewrite the steps that judge it; the reviewer sees that it did."""
import os
from pathlib import Path

from helpers import commit, gate, git, make_repo, runner, write
from _verify_edits import start_point  # after helpers, which puts gates/ on the path
from qa_review_fixture import GhFixture
from test_features import BASE, RegressionWalk, feature

STEP = "1. Open /projects: two projects listed."
EDITED = "1. Open /projects: a list opens."
TEST = "- `test/app.test.ts::the app starts and answers`"
CHANGED = ["app/auth/login.ts", "app/projects/list.ts", "docs/features/projects.md", "app/search/box.ts", "docs/features/search.md"]
AC = "acceptance-check: AC1 -- PASS -- shown (qae/AC1.md::step 1: wrong password -> an error)\n"
REWALKS = "".join("regression-check: %s -- PASS -- fine (qae/features/%s.md::step 1: opened it)\n" % (fid, fid)
                  for fid in ("auth", "projects", "search"))
AUTH_NOTE = "re-walk: auth (1 changed file)\n"
PROJECTS_NOTE = "re-walk: projects (1 changed file); this pull request changed its Verify section: 1 line added, 1 removed\n"
SEARCH_NOTE = "re-walk: search (1 changed file); this pull request added this feature, so its Verify section (2 lines) is its own\n"


def branch(test, projects_verify):
    """main has projects judged by STEP and a test; the branch changes auth's code only (its Verify untouched),
    gives projects `projects_verify` and adds a search feature."""
    repo = make_repo(test, dict(BASE, **{"docs/features/projects.md": feature(
        "Projects", "app/projects/**", "app/styles.css", "app/router.ts", verify=TEST + "\n" + STEP)}))
    git(repo, "checkout", "-q", "-b", "feat")
    commit(repo, {"app/auth/login.ts": "2\n", "app/projects/list.ts": "2\n", "app/search/box.ts": "1\n",
                  "docs/features/projects.md": feature("Projects", "app/projects/**", "app/styles.css", "app/router.ts",
                                                       verify=projects_verify),
                  "docs/features/search.md": feature("Search", "app/search/**", verify=TEST + "\n1. Search for a: one hit."),
                  ".git/changed-files": "".join(path + "\n" for path in CHANGED)})
    return repo


def features(repo):
    return runner("features", "--repo", repo, "--manifest", os.path.join(repo, ".vibe-verifier-qae"),
                  "--changed-files", os.path.join(repo, ".git", "changed-files"))


class Log(GhFixture):
    def test_unchanged_edited_and_added_features_in_the_features_and_gate_logs(self):
        repo = branch(self, TEST + "\n\n" + EDITED)  # a blank line is no change; the edited step is one added, one removed
        listed = features(repo)
        self.assertEqual(listed.returncode, 0, listed.stdout + listed.stderr)
        self.assertEqual(listed.stdout, "features: 3\n" + AUTH_NOTE + PROJECTS_NOTE + SEARCH_NOTE)
        work = os.path.join(repo, ".git", "work")
        write(work, {"pr-body.md": "## Acceptance criteria\n\n- The login form shows an error\n", "verdict.md": AC + REWALKS})
        judged = gate("acceptance-verdict", repo, "--criteria", os.path.join(work, "pr-body.md"), "--verdict",
                      os.path.join(work, "verdict.md"), "--artifacts", work, "--features", "docs/features",
                      "--changed-files", os.path.join(repo, ".git", "changed-files"))
        for line in (AUTH_NOTE, PROJECTS_NOTE, SEARCH_NOTE):
            self.assertIn("acceptance-verdict: " + line, judged.stdout)

    def test_steps_moved_but_not_reworded_are_no_change(self):
        repo = branch(self, STEP + "\n" + TEST)
        self.assertIn("\nre-walk: projects (1 changed file)\n", features(repo).stdout)

    def test_a_base_branch_that_moved_on_is_not_the_pull_requests_edit(self):
        # Against the merge base: a step main reworded after the branch left it is main's change, not this branch's.
        repo = branch(self, TEST + "\n" + STEP)
        git(repo, "checkout", "-q", "main")
        commit(repo, {"docs/features/projects.md": feature("Projects", "app/projects/**", "app/styles.css", "app/router.ts",
                                                           verify=TEST + "\n" + EDITED)})
        git(repo, "checkout", "-q", "feat")
        self.assertIn("\nre-walk: projects (1 changed file)\n", features(repo).stdout)


class NeverBlocks(RegressionWalk):
    def test_a_walk_that_passes_passes_whatever_the_pull_request_did_to_the_steps(self):
        commit(self.repo, {"docs/features/projects.md": feature("Projects", "app/projects/**", "app/styles.css",
                                                                "app/router.ts", verify=EDITED)})
        result = self.rewalk(self.AC + self.AUTH + self.PROJECTS)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("acceptance-verdict: re-walk: projects (1 changed file); this pull request changed its Verify "
                      "section: 1 line added, 1 removed\n", result.stdout)


class StartPoint(GhFixture):
    def test_the_merge_base_and_the_base_itself_when_there_is_none(self):
        repo = branch(self, TEST + "\n" + STEP)
        git(repo, "checkout", "-q", "--orphan", "unrelated")  # no history shared with main, as in a shallow clone
        commit(repo, {"x": "1\n"})
        self.assertEqual(start_point(repo, "main"), "main")
        git(repo, "checkout", "-q", "feat")
        self.assertRegex(start_point(repo, "main"), "^[0-9a-f]{40}$")


class Review(GhFixture):
    def rows(self, checkout, manifest):
        self.verdict = self.file("verdict.md", AC + REWALKS)
        body = self.posted(extra=["--manifest", manifest, "--checkout", checkout])
        return [line for line in body.splitlines() if line.startswith("- Feature") or line.startswith("- Whether")]

    def test_each_re_walked_row_says_whether_the_pull_request_changed_its_verify_section(self):
        repo = branch(self, TEST + "\n" + EDITED)
        self.assertEqual(self.rows(repo, os.path.join(repo, ".vibe-verifier-qae")), [
            "- Feature `auth` re-walked, explorer says PASS",
            "- Feature `projects` re-walked, explorer says PASS. Note: this pull request changed its Verify section: "
            "1 line added, 1 removed",
            "- Feature `search` re-walked, explorer says PASS. Note: this pull request added this feature, so its "
            "Verify section (2 lines) is its own"])

    def test_no_note_without_a_qae_manifest_and_a_reason_when_the_base_cannot_be_read(self):
        repo = branch(self, TEST + "\n" + EDITED)
        self.assertEqual(self.rows(repo, os.path.join(repo, "missing")), [
            "- Feature `%s` re-walked, explorer says PASS" % fid for fid in ("auth", "projects", "search")])
        bare = os.path.join(self.fixtures, "export")  # the files, with no git history to find a base in
        for path in ("docs/features/projects.md", "docs/features/search.md", "docs/features/auth.md", ".vibe-verifier-qae"):
            write(bare, {path: Path(repo, path).read_text()})
        rows = self.rows(bare, os.path.join(bare, ".vibe-verifier-qae"))
        self.assertEqual(rows[:3], ["- Feature `%s` re-walked, explorer says PASS" % fid for fid in ("auth", "projects", "search")])
        self.assertRegex(rows[3], r"^- Whether this pull request changed the Verify section of a re-walked feature could "
                                  r"not be read: .*no base ref")
