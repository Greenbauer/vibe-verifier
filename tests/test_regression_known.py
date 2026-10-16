"""A re-walk FAIL the base branch has too (docs/feature-map.md, "A FAIL that main has too"): the explorer walks
the failed state once more on a site built from the base branch, and the gate turns that FAIL into a warning only
with that walk on record."""
import unittest
from pathlib import Path

from helpers import ROOT, png, write
from test_features import RegressionWalk

PROMPT = ROOT / "harnesses" / "qae" / "prompt.md"


class KnownOnMain(RegressionWalk):
    def test_the_prompt_walks_only_the_failed_state_on_the_base_branch_and_names_the_line(self):
        prompt = " ".join(PROMPT.read_text().split())
        for phrase in ("If qae-inputs/base-site.md exists", "walk that same state once more on that site, the same way",
                       "never a state that passed here, and never a criterion",
                       "regression-known: <id> -- <what the base branch's site shows> (qae/features/base/<id>.md::<the step line text>)"):
            self.assertIn(phrase, prompt)

    def walk_on_main(self, fid="auth", line="- step 1: signed in as the test user -> an error page"):
        write(self.work, {"qae-artifacts/qae/features/base/%s.md" % fid: line + "\n"})
        Path(self.work, "qae-artifacts", "qae", "features", "base", "%s-step-1.png" % fid).write_bytes(png(1280))

    FAILED = "regression-check: auth -- FAIL -- an error page (qae/features/auth.md::step 1: signed in as the test user -> the dashboard)\n"
    KNOWN = "regression-known: auth -- the same error page (qae/features/base/auth.md::step 1: signed in as the test user -> an error page)\n"

    def test_a_fail_the_base_branch_shows_too_is_a_warning_not_a_finding(self):
        # Replayed 2026-10-16 on 15 real re-walk FAILs: 6 were already on main, and failed pull requests that never
        # touched them (one 5 times). The same state walked on a site built from the base branch tells them apart.
        self.walk_on_main()
        result = self.rewalk(self.AC + self.FAILED + self.KNOWN + self.PROJECTS)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("advisory: auth fails the same way on the base branch, so this pull request did not cause it: "
                      "FAIL -- an error page", result.stdout)
        self.assertIn("Base branch: the same error page", result.stdout)

    def test_a_known_line_counts_only_with_its_own_walk_of_the_base_branch(self):
        failing = self.AC + self.FAILED + self.PROJECTS
        # No walk on the base branch's site: no step log, or a step without its screenshot.
        self.assertIn("auth is a FAIL", self.rewalk(failing + self.KNOWN).stdout)
        self.walk_on_main()
        Path(self.work, "qae-artifacts", "qae", "features", "base", "auth-step-1.png").unlink()
        self.assertIn("auth is a FAIL", self.rewalk(failing + self.KNOWN).stdout)
        self.walk_on_main()
        # Anchored in the pull request's own walk, or in nothing that resolves, or said twice.
        own = self.KNOWN.replace("base/auth.md::step 1: signed in as the test user -> an error page",
                                 "auth.md::step 1: signed in as the test user -> the dashboard")
        dangling = self.KNOWN.replace("-> an error page)", "-> a blank page)")
        for line in (own, dangling, self.KNOWN + self.KNOWN):
            result = self.rewalk(failing + line)
            self.assertEqual(result.returncode, 1, line)
            self.assertIn("auth is a FAIL", result.stdout)
        # A known line never turns anything but a FAIL into a pass.
        self.assertIn("auth has no `regression-check: auth` line", self.rewalk(self.AC + self.KNOWN + self.PROJECTS).stdout)

    def test_the_known_line_needs_the_screenshot_of_the_step_it_cites(self):
        # Another step's screenshot does not stand in for the cited one's.
        write(self.work, {"qae-artifacts/qae/features/base/auth.md": "- step 1: signed in as the test user -> an error page\n"
                                                                    "- step 2: reloaded -> the same error page\n"})
        Path(self.work, "qae-artifacts", "qae", "features", "base", "auth-step-2.png").write_bytes(png(1280))
        result = self.rewalk(self.AC + self.FAILED + self.KNOWN + self.PROJECTS)
        self.assertEqual(result.returncode, 1)
        self.assertIn("auth is a FAIL", result.stdout)

    def test_a_known_line_does_not_stand_in_for_the_pull_requests_own_walk(self):
        # Zero walked steps on the pull request's site is a re-walk that did not happen, whatever main shows.
        self.walk_on_main()
        self.shot("auth").unlink()
        result = self.rewalk(self.AC + self.FAILED + self.KNOWN + self.PROJECTS)
        self.assertEqual(result.returncode, 1)
        self.assertIn("auth was not re-walked", result.stdout)
        self.assertNotIn("fails the same way on the base branch", result.stdout)

    def test_a_fail_the_base_branch_does_not_have_stays_a_finding(self):
        # The explorer walked the state on the base branch's site, it worked there, so it wrote no known line.
        self.walk_on_main(line="- step 1: signed in as the test user -> the dashboard")
        result = self.rewalk(self.AC + self.FAILED + self.PROJECTS)
        self.assertEqual(result.returncode, 1)
        self.assertIn("auth is a FAIL", result.stdout)
