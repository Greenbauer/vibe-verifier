"""The feature re-walk: `vibe-verifier features` (what the explore job hands the explorer), the
acceptance-verdict gate's regression-check requirement (what the verify job enforces), and
actions/features. All three apply one selection rule (gates/_features.py), driven here through their
command lines against a temp repository with a base branch and a pull request branch."""
import os
import re
import subprocess
import tempfile
import unittest
from pathlib import Path

from helpers import ROOT, clean_env, commit, gate, git, make_repo, png, runner, write


def feature(title, *globs, verify="- `test/app.test.ts::the app starts and answers`"):
    sources = "".join("- source: `%s`\n" % glob for glob in globs)
    return "# %s\n\n## Surfaces\n\n%s\n## Reach\n\nOpen it.\n\n## Verify\n\n%s\n\n## Gotchas\n\n- none\n" % (title, sources, verify)


QAE = ("acceptance-verdict --criteria qae-inputs/pr-body.md --verdict qae-inputs/verdict.md --artifacts qae-artifacts"
       " --features docs/features --changed-files qae-inputs/changed-files\n")
BASE = {
    "app/auth/login.ts": "1\n", "app/auth/reset.ts": "1\n", "app/projects/list.ts": "1\n", "app/billing/plan.ts": "1\n",
    "app/shell/nav.ts": "1\n", "app/styles.css": "1\n", "app/router.ts": "1\n",
    "test/app.test.ts": "test('the app starts and answers', () => {})\n",
    "docs/features/README.md": "# Feature map\n",
    # styles.css is listed by three features: past --shared-over 2, it says nothing about which one changed.
    "docs/features/auth.md": feature("Sign-in", "app/auth/**", "app/styles.css", "app/router.ts"),
    "docs/features/projects.md": feature("Projects", "app/projects/**", "app/styles.css", "app/router.ts"),
    "docs/features/billing.md": feature("Billing", "app/billing/**", "app/styles.css"),
    "docs/features/shell.md": feature("Shell", "app/shell/**"),
    ".vibe-verifier-qae": QAE,
}


class Selection(unittest.TestCase):
    def branch(self, changes, base=None):
        """A repository whose main is `base` (BASE by default) and whose checked-out branch applies `changes`
        (a path mapped to None is deleted)."""
        repo = make_repo(self, base or BASE)
        git(repo, "checkout", "-q", "-b", "feat")
        for path, text in changes.items():
            if text is None:
                os.remove(os.path.join(repo, path))
        commit(repo, {path: text for path, text in changes.items() if text is not None})
        return repo

    def changed(self, repo, *paths):
        path = os.path.join(repo, ".git", "changed-files")
        Path(path).write_text("".join(p + "\n" for p in paths))
        return path

    def features(self, repo, *paths, out=None, manifest=".vibe-verifier-qae"):
        args = ["features", "--repo", repo, "--manifest", os.path.join(repo, manifest), "--changed-files", self.changed(repo, *paths)]
        return runner(*args, *(["--out", out, "--listing", out + ".md"] if out else []))

    def test_features_whose_globs_match_are_picked_best_first(self):
        repo = self.branch({"app/projects/list.ts": "2\n", "app/auth/login.ts": "2\n", "app/auth/reset.ts": "2\n"})
        result = self.features(repo, "app/projects/list.ts", "app/auth/login.ts", "app/auth/reset.ts")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(result.stdout, "features: 2\nre-walk: auth (2 changed files)\nre-walk: projects (1 changed file)\n")

    def test_a_file_more_features_list_than_shared_over_selects_none(self):
        # The pilot's cost: a shared stylesheet selected nearly every feature. Two listers still count.
        repo = self.branch({"app/styles.css": "2\n", "app/router.ts": "2\n"})
        result = self.features(repo, "app/styles.css", "app/router.ts")
        self.assertEqual(result.stdout, "features: 2\nre-walk: auth (1 changed file)\nre-walk: projects (1 changed file)\n"
                                        "shared, not counted: app/styles.css (listed by 3 features)\n")

    def test_at_most_max_features_are_picked_and_the_rest_are_named(self):
        repo = self.branch({p: "2\n" for p in ("app/auth/login.ts", "app/auth/reset.ts", "app/projects/list.ts",
                                               "app/billing/plan.ts", "app/shell/nav.ts")})
        result = self.features(repo, "app/shell/nav.ts", "app/billing/plan.ts", "app/projects/list.ts",
                               "app/auth/login.ts", "app/auth/reset.ts")
        self.assertEqual(result.stdout.splitlines(), [
            "features: 3", "re-walk: auth (2 changed files)", "re-walk: billing (1 changed file)",
            "re-walk: projects (1 changed file)", "over the cap, not re-walked: shell (1 changed file)"])

    def test_a_feature_the_base_has_selects_by_its_base_surfaces(self):
        # Narrowing auth's globs on the branch does not take auth out of its own re-walk; a feature the
        # branch adds selects by its own globs; one it deletes is retired and selects nothing.
        repo = self.branch({
            "docs/features/auth.md": feature("Sign-in", "app/auth/reset.ts"),
            "app/auth/login.ts": "2\n",
            "docs/features/search.md": feature("Search", "app/search/**"), "app/search/box.ts": "1\n",
            "docs/features/billing.md": None, "app/billing/plan.ts": None,
        })
        result = self.features(repo, "docs/features/auth.md", "app/auth/login.ts", "docs/features/search.md",
                               "app/search/box.ts", "docs/features/billing.md", "app/billing/plan.ts")
        self.assertEqual(result.stdout, "features: 2\nre-walk: auth (1 changed file)\nre-walk: search (1 changed file)\n")

    def test_out_holds_the_head_version_of_each_selected_file_and_nothing_else(self):
        head = feature("Sign-in", "app/auth/**", "app/styles.css", "app/router.ts", verify="1. Sign in as the test user.")
        repo = self.branch({"docs/features/auth.md": head, "app/auth/login.ts": "2\n"})
        out = os.path.join(repo, ".git", "qae-inputs", "features")
        self.assertEqual(self.features(repo, "app/auth/login.ts", out=out).returncode, 0)
        self.assertEqual(sorted(os.listdir(out)), ["auth.md"])
        self.assertEqual(Path(out, "auth.md").read_text(), head)
        none = os.path.join(repo, ".git", "nothing")
        self.assertEqual(self.features(repo, "README.md", out=none).stdout, "features: 0\n")
        self.assertFalse(os.path.exists(none))  # the explorer's prompt keys on the directory existing
        self.assertFalse(os.path.exists(none + ".md"))

    def test_a_pull_request_that_updates_a_feature_is_re_walked_against_its_own_description(self):
        # The selection reads the base's Surfaces (login.ts is auth's only by the base's glob); the explorer
        # reads the pull request's Reach and Verify, so a change of behaviour lands with its feature file.
        head = feature("Sign-in", "app/auth/reset.ts", verify="1. Sign in as the test user: the welcome page opens.")
        repo = self.branch({"docs/features/auth.md": head, "app/auth/login.ts": "2\n"})
        out = os.path.join(repo, ".git", "qae-inputs", "features")
        result = self.features(repo, "docs/features/auth.md", "app/auth/login.ts", out=out)
        self.assertEqual(result.stdout, "features: 1\nre-walk: auth (1 changed file)\n")
        self.assertEqual(Path(out, "auth.md").read_text(), head)

    def test_the_listing_tells_the_explorer_how_many_states_to_walk_and_which_files_selected_each_feature(self):
        # A consumer's features list eight or more states each, and an explorer told to walk them all ran out
        # of time on three features (2026-10-07). The bound is the manifest's, three unless it says otherwise.
        repo = self.branch({"app/projects/list.ts": "2\n", "app/auth/login.ts": "2\n", "app/auth/reset.ts": "2\n"})
        out = os.path.join(repo, ".git", "qae-inputs", "features")
        self.assertEqual(self.features(repo, "app/projects/list.ts", "app/auth/login.ts", "app/auth/reset.ts", out=out).returncode, 0)
        listing = Path(out + ".md").read_text()
        self.assertIn("Walk at most 3 states of each, first the states closest to the changed files listed for it.", listing)
        self.assertTrue(listing.endswith("\n- auth: app/auth/login.ts, app/auth/reset.ts\n- projects: app/projects/list.ts\n"), listing)
        self.assertEqual(sorted(os.listdir(out)), ["auth.md", "projects.md"])  # the list sits beside the files, not among them

    def test_max_states_is_the_bases_and_one_or_more(self):
        one = dict(BASE, **{".vibe-verifier-qae": QAE.replace("\n", " --max-states 1\n")})
        repo = self.branch({"app/auth/login.ts": "2\n", ".vibe-verifier-qae": QAE.replace("\n", " --max-states 9\n")}, base=one)
        out = os.path.join(repo, ".git", "qae-inputs", "features")
        self.assertEqual(self.features(repo, "app/auth/login.ts", out=out).returncode, 0)
        self.assertIn("Walk at most 1 state of each,", Path(out + ".md").read_text())
        none = self.branch({"app/auth/login.ts": "2\n"}, base=dict(BASE, **{".vibe-verifier-qae": QAE.replace("\n", " --max-states 0\n")}))
        result = self.features(none, "app/auth/login.ts")
        self.assertEqual(result.returncode, 2)
        self.assertIn("--max-states must be 1 or more", result.stderr)

    def test_off_unless_the_base_line_has_features(self):
        without = QAE.replace(" --features docs/features --changed-files qae-inputs/changed-files", "")
        repo = self.branch({"app/auth/login.ts": "2\n"}, base=dict(BASE, **{".vibe-verifier-qae": without}))
        result = self.features(repo, "app/auth/login.ts")
        self.assertEqual(result.returncode, 0)
        self.assertTrue(result.stdout.startswith("features: 0 (the re-walk is off:"), result.stdout)
        # A branch that turns it on is judged by the base's line, so it bites from the next pull request;
        # one that turns it off is still judged with it.
        turned_on = self.branch({".vibe-verifier-qae": QAE, "app/auth/login.ts": "2\n"}, base=dict(BASE, **{".vibe-verifier-qae": without}))
        judged = self.features(turned_on, "app/auth/login.ts")
        self.assertTrue(judged.stdout.startswith("features: 0"), judged.stdout)
        self.assertIn("judged by the manifest at main", judged.stderr)
        turned_off = self.branch({".vibe-verifier-qae": without, "app/auth/login.ts": "2\n"})
        self.assertTrue(self.features(turned_off, "app/auth/login.ts").stdout.startswith("features: 1"))

    def test_what_cannot_be_read_cannot_run(self):
        repo = self.branch({"app/auth/login.ts": "2\n"})
        unreadable = runner("features", "--repo", repo, "--manifest", os.path.join(repo, ".vibe-verifier-qae"),
                            "--changed-files", "/nonexistent/changed-files")
        self.assertEqual(unreadable.returncode, 2)
        self.assertIn("readable file of changed paths", unreadable.stderr)
        self.assertEqual(self.features(repo, "x", manifest="absent").returncode, 2)
        moved = self.branch({"app/auth/login.ts": "2\n"}, base=dict(BASE, **{".vibe-verifier-qae": QAE.replace("docs/features", "docs/map")}))
        result = self.features(moved, "app/auth/login.ts")
        self.assertEqual(result.returncode, 2)
        self.assertIn("no feature files in docs/map/", result.stderr)


class RegressionChecks(unittest.TestCase):
    """acceptance-verdict --features: one anchored PASS over at least one walked step per feature the changes touch,
    selected by the gate. A re-walk is partial: what was left unwalked is printed, never judged."""

    BODY = "## Acceptance criteria\n\n- The login form shows an error on a wrong password\n"
    AC = "acceptance-check: AC1 -- PASS -- shown (qae/AC1.md::step 1: wrong password -> an error)\n"

    def setUp(self):
        self.repo = make_repo(self, BASE)
        git(self.repo, "checkout", "-q", "-b", "feat")
        commit(self.repo, {"app/auth/login.ts": "2\n", "app/projects/list.ts": "2\n"})
        self.work = tempfile.mkdtemp(prefix="vv-rw-")
        self.addCleanup(subprocess.run, ["rm", "-rf", self.work])
        write(self.work, {
            "changed-files": "app/auth/login.ts\napp/projects/list.ts\n",
            "qae-artifacts/qae/AC1.md": "- step 1: wrong password -> an error\n",
            "qae-artifacts/qae/features/auth.md": "- step 1: signed in as the test user -> the dashboard\n",
            "qae-artifacts/qae/features/projects.md": "- step 1: opened /projects -> two projects listed\n",
        })
        for fid in ("auth", "projects"):
            self.shot(fid).write_bytes(png(1280))

    def shot(self, fid, step=1):
        return Path(self.work, "qae-artifacts", "qae", "features", "%s-step-%d.png" % (fid, step))

    def verdict(self, text, *extra, body=None):
        write(self.work, {"verdict.md": text, "pr-body.md": body or self.BODY})
        return gate("acceptance-verdict", self.repo, "--criteria", os.path.join(self.work, "pr-body.md"),
                    "--verdict", os.path.join(self.work, "verdict.md"), "--artifacts", os.path.join(self.work, "qae-artifacts"),
                    *extra)

    def rewalk(self, text, *extra, body=None):
        return self.verdict(text, "--features", "docs/features", "--changed-files", os.path.join(self.work, "changed-files"),
                            *extra, body=body)

    AUTH = "regression-check: auth -- PASS -- still signs in (qae/features/auth.md::step 1: signed in as the test user -> the dashboard)\n"
    PROJECTS = "regression-check: projects -- PASS -- listed (qae/features/projects.md::step 1: opened /projects -> two projects listed)\n"

    def test_every_selected_feature_with_an_anchored_pass_passes(self):
        result = self.rewalk(self.AC + self.AUTH + self.PROJECTS)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("acceptance-verdict: re-walk: auth (1 changed file)", result.stdout)

    def test_a_selected_feature_without_its_line_is_refused(self):
        result = self.rewalk(self.AC + self.AUTH)
        self.assertEqual(result.returncode, 1)
        self.assertIn("projects has no `regression-check: projects` line (docs/features/projects.md)", result.stdout)

    def test_a_fail_or_an_unanchored_pass_is_refused(self):
        failed = self.AUTH.replace("PASS -- still signs in", "FAIL -- the dashboard never opened")
        result = self.rewalk(self.AC + failed + "regression-check: projects -- PASS -- it looked fine\n")
        self.assertEqual(result.returncode, 1)
        self.assertIn("auth is a FAIL: regression-check: auth -- FAIL", result.stdout)
        self.assertIn("projects has no anchor", result.stdout)
        dangling = self.PROJECTS.replace("two projects listed)", "three projects listed)")
        self.assertIn("projects: no anchor resolves at the head", self.rewalk(self.AC + self.AUTH + dangling).stdout)
        neither = self.AUTH.replace("PASS -- still signs in", "PARTIAL -- two of eight states")
        self.assertIn("auth is not a PASS: regression-check: auth -- PARTIAL", self.rewalk(self.AC + neither + self.PROJECTS).stdout)

    def test_a_fail_says_to_fix_the_regression_or_update_the_feature_file(self):
        # A step that contradicts the feature file because the pull request means the new behaviour is fixed by
        # updating the file in that pull request: the explorer reads the pull request's own copy of it.
        failed = self.AUTH.replace("PASS -- still signs in", "FAIL -- sign-in now lands on /welcome")
        result = self.rewalk(self.AC + failed + self.PROJECTS)
        self.assertEqual(result.returncode, 1)
        self.assertIn("A step the explorer walked no longer matches docs/features/auth.md: fix the regression or, if this "
                      "pull request means to change that behaviour, update docs/features/auth.md in this pull request",
                      result.stdout)
        self.assertIn("acceptance-verdict: 1 finding(s), blocking", result.stdout)

    def test_states_left_unwalked_are_printed_and_never_a_finding(self):
        # Two real verdicts on a consumer (2026-10-07) were FAILs that said only that states were not re-walked.
        # Unwalked states now go on a regression-skip line, which fails nothing, whatever it says.
        skips = ("regression-skip: auth -- 4, 5, 7\n"
                 "regression-skip: projects -- the download, mobile and signed-out states: out of time, FAIL\n")
        result = self.rewalk(self.AC + self.AUTH + self.PROJECTS + skips)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("acceptance-verdict: auth: 1 step logged with a screenshot\n"
                      "acceptance-verdict: auth: states not walked, as the explorer reports: 4, 5, 7\n", result.stdout)
        self.assertIn("projects: states not walked, as the explorer reports: the download, mobile and signed-out states", result.stdout)

    def test_a_feature_with_no_walked_step_is_refused_whatever_its_line_says(self):
        # Zero walked steps is a re-walk that did not happen, not a pass and not a regression: a step line
        # without its screenshot, an empty screenshot and a missing step log all count for nothing.
        self.shot("auth").unlink()
        self.shot("projects").write_bytes(b"")
        result = self.rewalk(self.AC + self.AUTH + self.PROJECTS.replace("PASS -- listed", "FAIL -- ran out of time"))
        self.assertEqual(result.returncode, 1)
        self.assertIn("acceptance-verdict: auth: 0 steps logged with a screenshot", result.stdout)
        for fid in ("auth", "projects"):
            self.assertIn("%s was not re-walked: its step log qae/features/%s.md holds no `- step k:` line with its "
                          "screenshot" % (fid, fid), result.stdout)
        self.assertNotIn("is a FAIL", result.stdout)
        os.remove(os.path.join(self.work, "qae-artifacts", "qae", "features", "auth.md"))
        self.assertIn("auth was not re-walked", self.rewalk(self.AC + self.AUTH + self.PROJECTS).stdout)

    def test_a_skip_line_never_stands_in_for_the_check_line(self):
        result = self.rewalk(self.AC + self.AUTH + "regression-skip: projects -- 1, 2, 3\n")
        self.assertEqual(result.returncode, 1)
        self.assertIn("projects has no `regression-check: projects` line (docs/features/projects.md)", result.stdout)
        self.assertIn("projects: states not walked, as the explorer reports: 1, 2, 3", result.stdout)

    def test_only_the_gate_decides_which_features_were_required(self):
        # A line for a feature nobody selected changes nothing; a criteria failure still counts.
        extra = "regression-check: billing -- FAIL -- not part of this change (qae/features/auth.md::step 1: x)\n"
        self.assertEqual(self.rewalk(self.AC + self.AUTH + self.PROJECTS + extra).returncode, 0)
        self.assertIn("AC1 has no", self.rewalk(self.AUTH + self.PROJECTS).stdout)

    def test_without_features_the_gate_asks_for_no_regression_line(self):
        self.assertEqual(self.verdict(self.AC).returncode, 0)

    def test_a_declared_none_needs_nothing(self):
        result = self.rewalk("", body="## Acceptance criteria\n\n- None: a CI-only change.\n")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_features_without_changed_paths_cannot_run(self):
        result = self.verdict(self.AC + self.AUTH + self.PROJECTS, "--features", "docs/features", "--soak")
        self.assertEqual(result.returncode, 2)
        self.assertIn("--features needs --changed-files", result.stderr)

    def test_features_without_the_evidence_cannot_run(self):
        # The walked steps are read from the run's evidence, so a line without --artifacts judges nothing.
        write(self.work, {"verdict.md": self.AC.replace("qae/AC1.md::step 1: wrong password -> an error", "test/app.test.ts:1"),
                          "pr-body.md": self.BODY})
        result = gate("acceptance-verdict", self.repo, "--criteria", os.path.join(self.work, "pr-body.md"),
                      "--verdict", os.path.join(self.work, "verdict.md"), "--features", "docs/features",
                      "--changed-files", os.path.join(self.work, "changed-files"), "--soak")
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn("--features needs --artifacts", result.stderr)


class Action(unittest.TestCase):
    """actions/features runs the command and hands the explore job its count."""

    def run_action(self, repo, changed, entries=""):
        text = (ROOT / "actions" / "features" / "action.yml").read_text()
        script = re.search(r"^      run: \|\n((?:        .*\n|\n)+)", text, re.MULTILINE).group(1)
        script = "".join(line[8:] if line.strip() else "\n" for line in script.splitlines(True))
        output = os.path.join(repo, ".git", "github-output")
        Path(output).write_text("")
        env = clean_env({"GITHUB_ACTION_PATH": str(ROOT / "actions" / "features"), "GITHUB_OUTPUT": output,
                         "RUNNER_TEMP": os.path.join(repo, ".git"), "VV_MANIFEST": ".vibe-verifier-qae",
                         "VV_ENTRIES": entries, "VV_CHANGED": changed, "VV_OUT": "qae-inputs/features"})
        result = subprocess.run(["bash", "-e", "-c", script], cwd=repo, capture_output=True, text=True, env=env)
        return result, Path(output).read_text()

    def test_the_count_and_the_copies(self):
        repo = make_repo(self, BASE)
        write(repo, {"changed": "app/auth/login.ts\n"})
        result, output = self.run_action(repo, "changed")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(output, "count=1\n")
        self.assertTrue(os.path.isfile(os.path.join(repo, "qae-inputs", "features", "auth.md")))
        self.assertIn("- auth: app/auth/login.ts\n", Path(repo, "qae-inputs", "features.md").read_text())

    def test_off_is_a_count_of_zero_and_a_wrapper_passes_its_list(self):
        repo = make_repo(self, dict(BASE, **{".vibe-verifier-qae": "acceptance-verdict --criteria a --verdict b\n"}))
        write(repo, {"changed": "app/auth/login.ts\n"})
        self.assertEqual(self.run_action(repo, "changed")[1], "count=0\n")
        result, output = self.run_action(repo, "changed", entries=QAE)
        self.assertEqual((result.returncode, output), (0, "count=1\n"), result.stdout + result.stderr)

    def test_a_command_that_cannot_run_fails_the_step(self):
        repo = make_repo(self, BASE)
        result, output = self.run_action(repo, "missing-file")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(output, "")


if __name__ == "__main__":
    unittest.main()
