"""The feature-map gate, driven through its command line against a temp repository whose map is
written in the format docs/feature-map.md describes."""
import os
import sys
import unittest

from helpers import ROOT, commit, gate, make_repo, runner, write

sys.path.insert(0, str(ROOT / "gates"))
import _features  # noqa: E402

ROUTES = 'export const routes = [\n  "/login",\n  "/projects",\n];\nconst notARoute = "/elsewhere";\n'
ROUTE_REGEX = r'^\s+"(/[^"]*)",?$'
LOGIN = """# Sign-in

Email and password sign-in.

## Sub-features

- S1 Wrong-password message

## Surfaces

- route: `/login`
- source: `app/login.ts`

## Reach

Signed out, open `/login`.

## Verify

- `test/login.test.ts::rejects a wrong password with a message`
- `app/login.ts:1-2`

## Gotchas

- The reset link works once.
"""
PROJECTS = """# Projects

## Surfaces

- route: `/projects`
- source: `app/projects/**`

## Reach

Signed in, choose Projects in the sidebar.

## Verify

1. Sign in as the seeded test user and open `/projects`.
2. Create a project named "Roadmap" and see it in the list.

## Gotchas

- An empty list shows the create button.
"""
FILES = {
    "app/routes.ts": ROUTES,
    "app/login.ts": "export function login() {\n  return true\n}\n",
    "app/projects/list.ts": "export const list = []\n",
    "test/login.test.ts": "test('rejects a wrong password with a message', () => {})\n",
    "docs/features/README.md": "# Feature map\n\n- [Sign-in](login.md)\n- [Projects](projects.md)\n",
    "docs/features/login.md": LOGIN,
    "docs/features/projects.md": PROJECTS,
}
ROUTE_SURFACE = ("--surface", "route", "app/routes.ts", ROUTE_REGEX)


class FeatureMap(unittest.TestCase):
    def repo(self, changes=None):
        """FILES with `changes` applied; a path mapped to None is left out."""
        files = dict(FILES, **(changes or {}))
        return make_repo(self, {path: text for path, text in files.items() if text is not None})

    def test_a_map_that_owns_every_surface_passes(self):
        result = gate("feature-map", self.repo(), *ROUTE_SURFACE)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("feature-map: 2 feature(s) in docs/features/; surfaces checked: 2 route", result.stdout)

    def test_a_surface_no_feature_lists_is_owned_by_no_feature(self):
        repo = self.repo({"app/routes.ts": ROUTES.replace('  "/projects",\n', '  "/projects",\n  "/friends",\n')})
        result = gate("feature-map", repo, *ROUTE_SURFACE)
        self.assertEqual(result.returncode, 1)
        self.assertIn("app/routes.ts:4: route `/friends` is owned by no feature", result.stdout)

    def test_a_listed_surface_the_code_no_longer_declares_is_stale(self):
        repo = self.repo({"app/routes.ts": ROUTES.replace('  "/projects",\n', "")})
        result = gate("feature-map", repo, *ROUTE_SURFACE)
        self.assertEqual(result.returncode, 1)
        self.assertIn("docs/features/projects.md:5: lists route `/projects`, which the code no longer declares", result.stdout)

    def test_a_kind_no_surface_declares_is_a_finding_not_a_skip(self):
        repo = self.repo({"docs/features/login.md": LOGIN.replace("- source: `app/login.ts`", "- source: `app/login.ts`\n- page: `/login`")})
        result = gate("feature-map", repo, *ROUTE_SURFACE)
        self.assertEqual(result.returncode, 1)
        self.assertIn("lists a page, but no `--surface page` is declared, so nothing checks it", result.stdout)

    def test_a_source_glob_must_match_a_tracked_file(self):
        repo = self.repo({"docs/features/projects.md": PROJECTS.replace("app/projects/**", "app/trips/**")})
        result = gate("feature-map", repo, *ROUTE_SURFACE)
        self.assertEqual(result.returncode, 1)
        self.assertIn("docs/features/projects.md:6: source `app/trips/**` matches no tracked file", result.stdout)

    def test_a_feature_with_no_source_glob_can_never_be_selected(self):
        repo = self.repo({"docs/features/projects.md": PROJECTS.replace("- source: `app/projects/**`\n", "")})
        result = gate("feature-map", repo, *ROUTE_SURFACE)
        self.assertEqual(result.returncode, 1)
        self.assertIn("docs/features/projects.md: lists no `- source:` glob", result.stdout)

    def test_a_verify_anchor_must_resolve(self):
        broken = LOGIN.replace("rejects a wrong password with a message", "rejects a forged password outright")
        broken = broken.replace("app/login.ts:1-2", "app/login.ts:3-9")
        result = gate("feature-map", self.repo({"docs/features/login.md": broken + "\n## Verify\n\n- `test/gone.test.ts::was deleted last week`\n"}),
                      *ROUTE_SURFACE)
        self.assertEqual(result.returncode, 1)
        self.assertIn('the title "rejects a forged password outright" is not in test/login.test.ts', result.stdout)
        self.assertIn("app/login.ts has 3 lines, so app/login.ts:3-9 is outside it", result.stdout)
        self.assertIn("test/gone.test.ts is not in the tree", result.stdout)

    def test_a_malformed_bullet_is_a_finding_never_skipped(self):
        broken = LOGIN.replace("- `test/login.test.ts::rejects a wrong password with a message`",
                               "- test/login.test.ts::rejects a wrong password with a message\n- `test/login.test.ts::short`")
        broken = broken.replace("- route: `/login`", "- route: /login")
        result = gate("feature-map", self.repo({"docs/features/login.md": broken}), *ROUTE_SURFACE)
        self.assertEqual(result.returncode, 1)
        self.assertIn("docs/features/login.md:11: malformed Surfaces bullet", result.stdout)
        self.assertIn("docs/features/login.md:20: malformed Verify bullet", result.stdout)
        self.assertIn("docs/features/login.md:21: malformed Verify bullet", result.stdout)
        # The route bullet was malformed, so the route it meant to own is owned by nobody.
        self.assertIn("route `/login` is owned by no feature", result.stdout)

    def test_browser_steps_alone_verify_a_feature_and_an_empty_verify_does_not(self):
        self.assertEqual(gate("feature-map", self.repo(), *ROUTE_SURFACE).returncode, 0)  # projects has steps only
        empty = PROJECTS.split("## Verify")[0] + "## Verify\n\nSee the spec.\n"
        result = gate("feature-map", self.repo({"docs/features/projects.md": empty}), *ROUTE_SURFACE)
        self.assertEqual(result.returncode, 1)
        self.assertIn("docs/features/projects.md: Verify lists no anchor and no step", result.stdout)

    def test_missing_sections_and_a_file_name_that_is_not_one_word(self):
        repo = self.repo({"docs/features/projects.md": None, "docs/features/my projects.md": "# Projects\n\nNo sections.\n"})
        result = gate("feature-map", repo)
        self.assertEqual(result.returncode, 1)
        for message in ("no `## Surfaces` section", "no `## Verify` section", "the file name is the feature's id, so it must be one word"):
            self.assertIn("docs/features/my projects.md: %s" % message, result.stdout)

    def test_path_surfaces_are_the_files_a_glob_matches(self):
        # A repository with no API: its pages are files, owned by listing their paths.
        pages = {"content/about.md": "# About\n", "content/pricing.md": "# Pricing\n"}
        login = LOGIN.replace("- route: `/login`\n", "- page: `content/about.md`\n")
        repo = self.repo({"docs/features/login.md": login, "docs/features/projects.md": PROJECTS.replace("- route: `/projects`\n", ""), **pages})
        result = gate("feature-map", repo, "--surface", "page", "content/*.md")
        self.assertEqual(result.returncode, 1)
        self.assertIn("content/pricing.md: page `content/pricing.md` is owned by no feature", result.stdout)
        self.assertNotIn("content/about.md", result.stdout.replace("page `content/about.md`", ""))

    def test_two_surfaces_of_one_kind_add_up(self):
        repo = self.repo({"app/legacy.ts": 'route("/old-login")\n',
                          "docs/features/login.md": LOGIN.replace("- route: `/login`", "- route: `/login`\n- route: `/old-login`")})
        result = gate("feature-map", repo, *ROUTE_SURFACE, "--surface", "route", "app/legacy.ts", r'route\("([^"]+)"\)')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("surfaces checked: 3 route", result.stdout)

    def test_without_a_surface_the_pass_says_completeness_was_not_checked(self):
        repo = self.repo({"docs/features/login.md": LOGIN.replace("- route: `/login`\n", ""),
                          "docs/features/projects.md": PROJECTS.replace("- route: `/projects`\n", "")})
        result = gate("feature-map", repo)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("advisory: completeness was not checked", result.stdout)
        self.assertIn("::warning title=feature-map::completeness was not checked", gate("feature-map", repo, "--format", "github").stdout)
        manifest = os.path.join(repo, ".vibe-verifier")
        write(repo, {".vibe-verifier": "feature-map\n"})
        summary = runner("run", "--repo", repo, "--manifest", manifest)
        self.assertEqual(summary.returncode, 0, summary.stdout + summary.stderr)
        self.assertRegex(summary.stdout, r"feature-map\s+PASS \(completeness not checked\)")
        write(repo, {".vibe-verifier": "feature-map --surface page 'docs/features/*.md'\n"})
        full = runner("run", "--repo", repo, "--manifest", manifest)
        self.assertRegex(full.stdout, r"feature-map\s+FAIL\n")  # the pages are owned by no feature, and the label is bare

    def test_the_whole_map_is_judged_not_only_what_a_branch_changed(self):
        repo = self.repo({"app/routes.ts": ROUTES.replace('  "/projects",\n', '  "/projects",\n  "/friends",\n')})
        commit(repo, {"README.md": "unrelated\n"})
        self.assertEqual(gate("feature-map", repo, *ROUTE_SURFACE).returncode, 1)

    def test_soak_reports_and_never_masks_could_not_run(self):
        repo = self.repo({"app/routes.ts": ROUTES.replace('  "/projects",\n', '  "/projects",\n  "/friends",\n')})
        soaked = gate("feature-map", repo, *ROUTE_SURFACE, "--soak")
        self.assertEqual(soaked.returncode, 0)
        self.assertIn("reported only (--soak)", soaked.stdout)
        self.assertEqual(gate("feature-map", repo, "--surface", "route", "app/none.ts", ROUTE_REGEX, "--soak").returncode, 2)

    def test_what_cannot_be_read_cannot_run(self):
        repo = self.repo()
        cases = {
            ("--dir", "docs/missing"): "no feature files in docs/missing/",
            ("--dir", "../elsewhere"): "must be a path inside the repository",
            ("--surface", "route", "app/none.ts", ROUTE_REGEX): "matches no tracked file",
            ("--surface", "route", "app/routes.ts", r'"/[a-z]+"'): "exactly one capture group",
            ("--surface", "route", "app/routes.ts", r'"(/)([a-z]+)"'): "exactly one capture group",
            ("--surface", "route", "app/routes.ts", r'"(/nothing)"'): "found no surface",
            ("--surface", "route", "app/routes.ts", r'"(/[a-z'): "does not compile",
            ("--surface", "source", "app/*.ts"): "not `source`",
            ("--surface", "route"): "KIND GLOB [REGEX]",
        }
        for args, message in cases.items():
            result = gate("feature-map", repo, *args)
            self.assertEqual(result.returncode, 2, args)
            self.assertIn(message, result.stderr, args)
        only_index = make_repo(self, {"docs/features/README.md": "# Feature map\n"})
        self.assertIn("no feature files", gate("feature-map", only_index).stderr)


class Parse(unittest.TestCase):
    """How one feature file reads, independent of any repository."""

    def test_the_sections_the_map_reads_and_the_ones_it_leaves_alone(self):
        feature = _features.parse(LOGIN, "login", "docs/features/login.md")
        self.assertEqual(feature.sections, {"sub-features", "surfaces", "reach", "verify", "gotchas"})
        self.assertEqual(feature.sources, [("app/login.ts", 12)])
        self.assertEqual(feature.surfaces, [("route", "/login", 11)])
        self.assertEqual([(anchor.path, anchor.title, anchor.start, anchor.end) for anchor, _ in feature.anchors],
                         [("test/login.test.ts", "rejects a wrong password with a message", 0, 0), ("app/login.ts", "", 1, 2)])
        self.assertEqual((feature.steps, feature.malformed), (0, []))  # Sub-features and Gotchas bullets are not read

    def test_headings_match_in_any_case_and_steps_and_nested_bullets_stay_prose(self):
        text = ("## SURFACES\n\n- source: `a/**`\n\n## verify\n\n1. Open the page.\n   - a nested note\n"
                "2) Click save.\n\n### Detail\n\n- `t.test.ts::a long enough title`\n")
        feature = _features.parse(text, "a", "a.md")
        self.assertEqual((feature.steps, len(feature.anchors), feature.malformed), (2, 1, []))


if __name__ == "__main__":
    unittest.main()
