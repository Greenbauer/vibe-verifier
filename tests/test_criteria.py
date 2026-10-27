"""`vibe-verifier criteria`: the one-line answer a harness keys its skip on, from the PR body and,
when given, its changed paths."""
import os
import subprocess
import tempfile
import unittest

from helpers import ROOT, clean_env, runner
from test_qae_annotations import action_script

BODY = "## Why\n\nx\n\n## Acceptance criteria\n\n%s\n"


class Criteria(unittest.TestCase):
    def doc(self, text):
        handle, path = tempfile.mkstemp(prefix="vv-body-", suffix=".md")
        os.write(handle, text.encode()); os.close(handle)
        self.addCleanup(os.remove, path)
        return path

    def test_declared_none(self):
        result = runner("criteria", self.doc(BODY % "- None: a CI step; no rendered surface."))
        self.assertEqual((result.returncode, result.stdout), (0, "none: a CI step; no rendered surface.\n"))

    def test_counts_criteria(self):
        result = runner("criteria", self.doc(BODY % "- AC one\n- AC two\n- AC three"))
        self.assertEqual((result.returncode, result.stdout), (0, "criteria: 3\n"))

    def test_a_bare_none_or_an_absent_section_is_zero_not_declared(self):
        self.assertEqual(runner("criteria", self.doc(BODY % "- None")).stdout, "criteria: 1\n")
        self.assertEqual(runner("criteria", self.doc("## Why\n\nno section\n")).stdout, "criteria: 0\n")

    def test_a_near_miss_heading_fails_before_any_browser_run(self):
        # a consumer PR, 2026-10-14: `criteria: 0` let the run go on to fail with "none found".
        result = runner("criteria", self.doc("## Acceptance criteria (check at 1280 px)\n\n- AC one\n"))
        self.assertEqual((result.returncode, result.stdout), (1, ""))
        self.assertIn("::error::no acceptance criteria found", result.stderr)
        self.assertIn("`## Acceptance criteria (check at 1280 px)` is not that heading", result.stderr)

    def test_a_criterion_judged_against_main_or_production_fails_before_any_browser_run(self):
        # a consumer PR, 2026-10-14: the explorer walked these for half an hour, then failed them
        # for want of a main baseline it is never given.
        for wording, phrase in (("The Orders page opens with the same rows as on main.", "as on main"),
                                ("The Invoices pages open as on main", "as on main"),
                                ("Totals match the main branch [as: viewer]", "match the main branch"),
                                ("Works the same as in production", "as in production")):
            result = runner("criteria", self.doc(BODY % ("- A page loads\n- " + wording)))
            self.assertEqual((result.returncode, result.stdout), (1, ""), wording)
            self.assertIn("::error::AC2 compares with main or production (`%s`)" % phrase, result.stderr)

    def test_a_role_the_preview_has_no_login_for_fails_before_any_browser_run(self):
        # consumer PRs, 2026-10: 41 criteria named a role the site step gave no login (a planner where only
        # a viewer signs in, "Customer B"); each failed only after a full walk.
        body = self.doc(BODY % "- Lists orders [as: admin, Read-only]\n- Approves a plan [as: planner]")
        result = runner("criteria", body, "--roles", "admin, read-only, rep")
        self.assertEqual((result.returncode, result.stdout), (1, ""))
        self.assertEqual(result.stderr.count("::error::"), 1)
        self.assertIn("::error::AC2 names [as: planner], and this preview has a login only for admin, read-only, rep",
                      result.stderr)

    def test_a_ticket_criterion_naming_a_role_with_no_login_fails_too(self):
        ticket = self.doc("- Approves a plan [as: planner]\n")
        result = runner("criteria", self.doc(BODY % "- Lists orders"), "--ticket", ticket, "--roles", "viewer")
        self.assertEqual(result.returncode, 1)
        self.assertIn("::error::TC1 names [as: planner], and this preview has a login only for viewer", result.stderr)

    def test_roles_are_checked_only_when_the_harness_names_them(self):
        body = self.doc(BODY % "- Approves a plan [as: planner]")
        self.assertEqual(runner("criteria", body).stdout, "criteria: 1\nAC1 roles: planner\n")
        self.assertEqual(runner("criteria", body, "--roles", "Planner").returncode, 0)

    def test_the_action_passes_its_roles_on(self):
        body, output = self.doc(BODY % "- Approves a plan [as: planner]"), self.doc("")
        env = {"GITHUB_ACTION_PATH": str(ROOT / "actions" / "criteria"), "GITHUB_OUTPUT": output, "VV_PATH": body,
               "VV_CHANGED": "", "VV_TICKET": "", "VV_MAX_SHARDS": "1", "VV_MANIFEST": "", "VV_ENTRIES": ""}
        script = ["bash", "-e", "-c", action_script("criteria")]
        refused = subprocess.run(script, capture_output=True, text=True, env=clean_env(dict(env, VV_ROLES="viewer")))
        self.assertNotEqual(refused.returncode, 0)
        self.assertIn("::error::AC1 names [as: planner]", refused.stderr)
        self.assertEqual(subprocess.run(script, capture_output=True, text=True, env=clean_env(env)).returncode, 0)

    def test_main_or_production_as_words_on_a_page_is_not_a_comparison(self):
        for wording in ("Clicking Home returns to the main page", "The main menu opens",
                        "The production-ready badge shows", "Opens from the main navigation",
                        "An alert shown before the refresh is the state copied from production"):
            result = runner("criteria", self.doc(BODY % ("- " + wording)))
            self.assertEqual((result.returncode, result.stdout), (0, "criteria: 1\n"), wording)

    def changed(self, *paths):
        return self.doc("".join(path + "\n" for path in paths))

    def test_a_pull_request_that_changes_only_unrendered_paths_needs_no_check(self):
        # The paths a site never serves: CI configuration, the catalog's manifests, and documentation.
        paths = (".github/workflows/ci.yml", ".github/CODEOWNERS", "docs/adr/0001-runners.md", "docs/diagram.png",
                 "README.md", "packages/web/README.md", "CLAUDE.md", "AGENTS.md", "CHANGELOG.md", "CONTRIBUTING.md",
                 "SECURITY.md", "LICENSE", "LICENSE.md", "CODEOWNERS", ".gitignore", "app/.gitignore",
                 ".gitattributes", ".editorconfig", ".vibe-verifier", ".vibe-verifier-qae", ".vibe-verifier-review")
        result = runner("criteria", self.doc("## Why\n\ndocs\n"), "--changed-files", self.changed(*paths))
        self.assertEqual((result.returncode, result.stdout),
                         (0, "none: every changed file (%d) is CI configuration or documentation no site renders\n" % len(paths)))

    def test_one_rendered_path_needs_a_check(self):
        # A page, a component, a stylesheet, a dependency, a markdown page the site may render.
        body = self.doc("## Why\n\nx\n")
        for path in ("app/page.tsx", "styles/site.css", "package.json", "package-lock.json", "content/blog/post.md",
                     "public/robots.txt", "next.config.js", "documents/terms.md"):
            result = runner("criteria", body, "--changed-files", self.changed("README.md", path))
            self.assertEqual(result.stdout, "criteria: 0\n", path)

    def test_a_page_renamed_into_docs_still_needs_a_check(self):
        # The workflow lists a rename's old name too, so the page leaving the site counts.
        result = runner("criteria", self.doc("## Why\n\nx\n"), "--changed-files", self.changed("docs/about.tsx", "app/about.tsx"))
        self.assertEqual(result.stdout, "criteria: 0\n")

    def test_a_body_with_no_headings_lists_no_criteria(self):
        # A short CI PR body: prose and bullets, no headings. Read as a plain list every line would be a
        # criterion, so it was never exempt (the first consumer PR, 2026-10-05).
        body = self.doc("Moves the QAE pins.\n\n- Both jobs read the changed paths\n- One review comment\n")
        result = runner("criteria", body, "--changed-files", self.changed(".github/workflows/qae-explore.yml"))
        self.assertEqual(result.stdout, "none: every changed file (1) is CI configuration or documentation no site renders\n")
        # On a page it still lists none, so nothing is explored and the verdict gate refuses it for that:
        # counted as three criteria, it paid for an exploration of nothing first.
        self.assertEqual(runner("criteria", body, "--changed-files", self.changed("app/page.tsx")).stdout, "criteria: 0\n")

    def test_criteria_win_over_paths(self):
        # A pull request that lists a criterion is explored whatever it touches.
        result = runner("criteria", self.doc(BODY % "- The docs link in the footer opens /docs"),
                        "--changed-files", self.changed("README.md"))
        self.assertEqual(result.stdout, "criteria: 1\n")

    def test_a_declared_none_keeps_its_own_reason(self):
        result = runner("criteria", self.doc(BODY % "- None: a pin bump."), "--changed-files", self.changed("app/page.tsx"))
        self.assertEqual(result.stdout, "none: a pin bump.\n")

    def test_a_path_with_a_space_is_one_path(self):
        result = runner("criteria", self.doc("## Why\n\nx\n"), "--changed-files", self.changed("docs/My Guide.md", "README.md"))
        self.assertEqual(result.stdout, "none: every changed file (2) is CI configuration or documentation no site renders\n")

    def test_an_empty_path_list_exempts_nothing(self):
        result = runner("criteria", self.doc("## Why\n\nx\n"), "--changed-files", self.changed())
        self.assertEqual(result.stdout, "criteria: 0\n")

    def test_unreadable_changed_files_cannot_run(self):
        result = runner("criteria", self.doc("## Why\n\nx\n"), "--changed-files", "/nonexistent/changed-files")
        self.assertEqual(result.returncode, 2)
        self.assertIn("could not read", result.stderr)

    def test_unreadable_file_cannot_run(self):
        result = runner("criteria", "/nonexistent/pr-body.md")
        self.assertEqual(result.returncode, 2)
        self.assertIn("could not read", result.stderr)


if __name__ == "__main__":
    unittest.main()
