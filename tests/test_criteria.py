"""`vibe-verifier criteria`: the one-line answer a harness keys its skip on, from the PR body and,
when given, its changed paths."""
import os
import tempfile
import unittest

from helpers import runner

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

    def test_criteria_win_over_paths(self):
        # A pull request that lists a criterion is explored whatever it touches.
        result = runner("criteria", self.doc(BODY % "- The docs link in the footer opens /docs"),
                        "--changed-files", self.changed("README.md"))
        self.assertEqual(result.stdout, "criteria: 1\n")

    def test_a_declared_none_keeps_its_own_reason(self):
        result = runner("criteria", self.doc(BODY % "- None: a pin bump."), "--changed-files", self.changed("app/page.tsx"))
        self.assertEqual(result.stdout, "none: a pin bump.\n")

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
