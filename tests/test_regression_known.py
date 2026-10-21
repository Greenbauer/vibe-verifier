"""A re-walk FAIL the base branch has too (docs/feature-map.md, "A FAIL that main has too"): the explorer walks
the failed state once more on a site built from the base branch, and the gate turns that FAIL into a warning only
with that walk on record."""
import os
import shutil
import unittest
from pathlib import Path

from helpers import ROOT, commit, gate, git, png, write
from test_features import RegressionWalk

PROMPT = ROOT / "harnesses" / "qae" / "prompt.md"


class KnownOnMain(RegressionWalk):
    def test_the_prompt_walks_only_the_failed_state_on_the_base_branch_and_names_the_line(self):
        prompt = " ".join(PROMPT.read_text().split())
        for phrase in ("If qae-inputs/base-site.md exists", "walk that same state once more on that site, the same way",
                       "never a state that passed here, and never a criterion",
                       "log those steps in qae-artifacts/qae/features/base/<id>.md with screenshots "
                       "qae-artifacts/qae/features/base/<id>-step-k.png",
                       # the anchor is the step line after its "- ": a consumer's trial run wrote "::- step 4: ...",
                       # which known_on_main refuses
                       "regression-known: <id> -- <what the base branch's site shows> (qae/features/base/<id>.md::<the step line text, exactly as written after \"- \">)",
                       "regression-check: <id> -- FAIL -- <what broke> (qae/features/<id>.md::<the step line text, exactly as written after \"- \">)",
                       'The text after :: starts with "step k:" and must be copied verbatim from the step log line'):
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

    def test_the_known_line_must_cite_a_step_line_of_this_features_base_walk(self):
        # Another feature's base-branch walk does not count, nor the cited text anywhere but on its step line.
        self.walk_on_main(fid="projects")
        result = self.rewalk(self.AC + self.FAILED + self.KNOWN.replace("base/auth.md", "base/projects.md") + self.PROJECTS)
        self.assertIn("auth is a FAIL", result.stdout)
        write(self.work, {"qae-artifacts/qae/features/base/auth.md": "- step 1: reloaded -> fine\n"
                                                                    "note: step 1: signed in as the test user -> an error page\n"})
        Path(self.work, "qae-artifacts", "qae", "features", "base", "auth-step-1.png").write_bytes(png(1280))
        result = self.rewalk(self.AC + self.FAILED + self.KNOWN + self.PROJECTS)
        self.assertEqual(result.returncode, 1)
        self.assertIn("auth is a FAIL", result.stdout)

    def test_an_anchor_into_the_pull_requests_own_walk_never_counts_even_with_the_same_text(self):
        same = "- step 1: signed in as the test user -> the dashboard"
        self.walk_on_main(line=same)
        own = "regression-known: auth -- same (qae/features/auth.md::step 1: signed in as the test user -> the dashboard)\n"
        result = self.rewalk(self.AC + self.FAILED + own + self.PROJECTS)
        self.assertEqual(result.returncode, 1)
        self.assertIn("auth is a FAIL", result.stdout)
        cased = self.KNOWN.replace("::step 1:", "::Step 1:")
        self.walk_on_main()
        self.assertEqual(self.rewalk(self.AC + self.FAILED + cased + self.PROJECTS).returncode, 0)

    def test_a_base_walk_committed_to_the_repository_is_not_evidence(self):
        commit(self.repo, {"qae/features/base/auth.md": "- step 1: signed in as the test user -> an error page\n"})
        # The screenshot is in the run's evidence; only the step log is the repository's, which once counted.
        Path(self.work, "qae-artifacts", "qae", "features", "base").mkdir(parents=True, exist_ok=True)
        Path(self.work, "qae-artifacts", "qae", "features", "base", "auth-step-1.png").write_bytes(png(1280))
        result = self.rewalk(self.AC + self.FAILED + self.KNOWN + self.PROJECTS)
        self.assertEqual(result.returncode, 1)
        self.assertIn("auth is a FAIL", result.stdout)

    def test_one_features_known_line_cannot_cite_anothers_base_walk(self):
        self.walk_on_main()
        failed = self.PROJECTS.replace("PASS -- listed", "FAIL -- empty list")
        borrowed = self.KNOWN.replace("regression-known: auth", "regression-known: projects")
        result = self.rewalk(self.AC + self.AUTH + failed + borrowed)
        self.assertEqual(result.returncode, 1)
        self.assertIn("projects is a FAIL", result.stdout)

    def test_the_cited_step_must_be_a_step_of_the_log_with_text_and_a_non_empty_screenshot(self):
        failing = self.AC + self.FAILED + self.KNOWN + self.PROJECTS
        # The cited step is not a `- step 1:` line (only step 2 is), though its screenshot is there.
        write(self.work, {"qae-artifacts/qae/features/base/auth.md": "- step 2: signed in as the test user -> an error page\n"
                                                                    "step 1: signed in as the test user -> an error page\n"})
        Path(self.work, "qae-artifacts", "qae", "features", "base", "auth-step-1.png").write_bytes(png(1280))
        self.assertEqual(self.rewalk(failing).returncode, 1)
        # An empty screenshot, and a cited step with no text.
        self.walk_on_main()
        Path(self.work, "qae-artifacts", "qae", "features", "base", "auth-step-1.png").write_bytes(b"")
        self.assertEqual(self.rewalk(failing).returncode, 1)
        write(self.work, {"qae-artifacts/qae/features/base/auth.md": "- step 10: signed in as the test user -> an error page\n"})
        Path(self.work, "qae-artifacts", "qae", "features", "base", "auth-step-10.png").write_bytes(png(1280))
        bare = "regression-known: auth -- same (qae/features/base/auth.md::step 10:)\n"
        self.assertEqual(self.rewalk(self.AC + self.FAILED + bare + self.PROJECTS).returncode, 1)

    def test_a_known_line_never_rescues_a_pass_without_anchor_or_two_check_lines(self):
        self.walk_on_main()
        dangling = self.AUTH.replace("-> the dashboard)", "-> nowhere at all)")
        for lines in (dangling, self.FAILED + self.FAILED):
            result = self.rewalk(self.AC + lines + self.KNOWN + self.PROJECTS)
            self.assertEqual(result.returncode, 1, lines)
            self.assertNotIn("fails the same way on the base branch", result.stdout)

    def test_the_known_line_must_name_this_features_base_log_when_both_have_one(self):
        # Both features walked the base branch's site; auth's line cites projects' log, which says the same thing.
        self.walk_on_main(fid="auth", line="- step 1: reloaded -> the dashboard")
        self.walk_on_main(fid="projects")
        borrowed = self.KNOWN.replace("base/auth.md", "base/projects.md")
        result = self.rewalk(self.AC + self.FAILED + borrowed + self.PROJECTS)
        self.assertEqual(result.returncode, 1)
        self.assertIn("auth is a FAIL", result.stdout)

    def test_the_cited_text_must_be_on_the_cited_step_not_another(self):
        write(self.work, {"qae-artifacts/qae/features/base/auth.md": "- step 1: reloaded -> the dashboard\n"
                                                                    "- step 2: retried; step 1: signed in as the test user -> an error page\n"})
        for k in (1, 2):
            Path(self.work, "qae-artifacts", "qae", "features", "base", "auth-step-%d.png" % k).write_bytes(png(1280))
        result = self.rewalk(self.AC + self.FAILED + self.KNOWN + self.PROJECTS)
        self.assertEqual(result.returncode, 1)
        self.assertIn("auth is a FAIL", result.stdout)

    def test_a_cited_step_with_no_text_after_it_never_counts(self):
        # `step 10: .` keeps the title `step 10:`, which every `- step 10:` line holds, whatever main showed.
        write(self.work, {"qae-artifacts/qae/features/base/auth.md": "- step 10: signed in as the test user -> the dashboard\n"})
        Path(self.work, "qae-artifacts", "qae", "features", "base", "auth-step-10.png").write_bytes(png(1280))
        bare = "regression-known: auth -- same (qae/features/base/auth.md::step 10: .)\n"
        self.assertEqual(self.rewalk(self.AC + self.FAILED + bare + self.PROJECTS).returncode, 1)

    def test_the_anchor_must_name_this_features_base_log_even_when_its_own_log_has_the_step(self):
        self.walk_on_main()
        for path in ("base/projects.md", "base/nowhere.md"):
            result = self.rewalk(self.AC + self.FAILED + self.KNOWN.replace("base/auth.md", path) + self.PROJECTS)
            self.assertEqual(result.returncode, 1, path)

    def test_only_a_fail_is_downgraded_not_another_word(self):
        self.walk_on_main()
        broken = self.FAILED.replace("FAIL -- an error page", "BROKEN -- an error page")
        self.assertEqual(self.rewalk(self.AC + broken + self.KNOWN + self.PROJECTS).returncode, 1)

    def test_case_never_decides(self):
        # A step line written in capitals, and a line citing two anchors of which the base walk's is the second.
        self.walk_on_main(line="- Step 1: Signed in as the test user -> An error page")
        self.assertEqual(self.rewalk(self.AC + self.FAILED + self.KNOWN + self.PROJECTS).returncode, 0)
        self.walk_on_main()
        two = self.KNOWN.replace("(qae/features/base/auth.md",
                                 "(qae/features/auth.md::step 1: signed in as the test user -> the dashboard; qae/features/base/auth.md")
        self.assertEqual(self.rewalk(self.AC + self.FAILED + two + self.PROJECTS).returncode, 0)

    def test_a_feature_id_with_capitals_matches_its_lines_in_any_case(self):
        commit(self.repo, {"docs/features/Reports.md": "# Reports\n\n## Surfaces\n\n- source: `app/reports/**`\n\n## Reach\n\n"
                                                        "Open it.\n\n## Verify\n\n- `test/app.test.ts::the app starts and answers`\n",
                           "app/reports/list.ts": "1\n"})
        write(self.work, {"changed-files": "app/reports/list.ts\n",
                          "qae-artifacts/qae/features/Reports.md": "- step 1: opened reports -> a blank page\n"})
        Path(self.work, "qae-artifacts", "qae", "features", "Reports-step-1.png").write_bytes(png(1280))
        self.walk_on_main(fid="Reports", line="- step 1: opened reports -> the same blank page")
        lines = ("regression-check: reports -- FAIL -- blank (qae/features/Reports.md::step 1: opened reports -> a blank page)\n"
                 "regression-known: reports -- same (qae/features/base/Reports.md::step 1: opened reports -> the same blank page)\n")
        result = self.rewalk(self.AC + lines)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("advisory: Reports fails the same way on the base branch", result.stdout)

    def test_an_anchor_path_must_be_the_log_itself_not_one_ending_like_it(self):
        self.walk_on_main()
        longer = self.KNOWN.replace("(qae/features/base/auth.md", "(qae-artifacts/qae/features/base/auth.md")
        self.assertEqual(self.rewalk(self.AC + self.FAILED + longer + self.PROJECTS).returncode, 1)

    def evidence_in_checkout(self):
        """The run's evidence copied into the checkout, where the verify job keeps it; returns the directory."""
        evidence = Path(self.repo, "qae-artifacts")
        shutil.copytree(Path(self.work, "qae-artifacts"), evidence)
        return evidence

    def rewalk_in(self, evidence, verdict):
        write(self.work, {"verdict.md": verdict, "pr-body.md": self.BODY})
        return gate("acceptance-verdict", self.repo, "--criteria", os.path.join(self.work, "pr-body.md"),
                    "--verdict", os.path.join(self.work, "verdict.md"), "--artifacts", str(evidence),
                    "--features", "docs/features", "--changed-files", os.path.join(self.work, "changed-files"))

    def test_a_base_walk_the_pull_request_commits_into_the_checkout_is_not_evidence(self):
        # Each half alone: a committed log with the run's screenshot, and the run's log with a committed screenshot.
        # Refused with all the evidence (gates/_contract.py, planted), tests/test_qae_planted_evidence.py has the rest.
        for committed in ("auth.md", "auth-step-1.png"):
            with self.subTest(committed=committed):
                self.setUp()
                evidence = self.evidence_in_checkout()
                base = Path(evidence, "qae", "features", "base")
                base.mkdir(parents=True, exist_ok=True)
                Path(base, "auth.md").write_text("- step 1: signed in as the test user -> an error page\n")
                Path(base, "auth-step-1.png").write_bytes(png(1280))
                git(self.repo, "add", "-f", str(Path(base, committed)))
                git(self.repo, "commit", "-q", "-m", "evidence")
                result = self.rewalk_in(evidence, self.AC + self.FAILED + self.KNOWN + self.PROJECTS)
                self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
                self.assertIn("is not only the run's own, so none of it is judged", result.stdout)

    def test_a_link_into_the_pull_requests_own_walk_is_not_a_base_walk(self):
        # The base log and screenshot as links to the pull request's own walk, and the base directory as a link
        # to the feature directory: either way the known line cites the pull request's own step text.
        own = "regression-known: auth -- same (qae/features/base/auth.md::step 1: signed in as the test user -> the dashboard)\n"
        for shape in ("files", "directory"):
            with self.subTest(shape=shape):
                self.setUp()
                evidence = self.evidence_in_checkout()
                features = Path(evidence, "qae", "features")
                if shape == "files":
                    Path(features, "base").mkdir()
                    os.symlink("../auth.md", Path(features, "base", "auth.md"))
                    os.symlink("../auth-step-1.png", Path(features, "base", "auth-step-1.png"))
                else:
                    os.symlink(".", Path(features, "base"))
                result = self.rewalk_in(evidence, self.AC + self.FAILED + own + self.PROJECTS)
                self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
                self.assertIn("is not only the run's own, so none of it is judged", result.stdout)

