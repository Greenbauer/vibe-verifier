"""The feature-map gate, driven through its command line against a temp repository whose map is
written in the format docs/feature-map.md describes."""
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

from helpers import ROOT, clean_env, commit, gate, git, make_repo, runner, write

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


MAP = {path: text for path, text in FILES.items() if path.startswith("docs/features/")}
CODE = {path: text for path, text in FILES.items() if path not in MAP}
FRIENDS = ROUTES.replace('  "/projects",\n', '  "/projects",\n  "/friends",\n')
ABSENT = "feature-map could not run: no feature files in docs/features/ (one <id>.md per feature, besides the README.md index)"
REMOVED = (r"^feature-map could not run: the feature map was removed: docs/features/ has no feature file at the head, "
           r"and the merge base [0-9a-f]{12} has 2\. Restore it\. To stop keeping a map, first merge a change that removes "
           r"what reads it \(the feature-map line, the --features option\), then delete the map\n$")


def commit_on(repo, day, files, message):
    """commit() dated that day of January 2030, so which of two merge bases git names first does not vary."""
    write(repo, files)
    git(repo, "add", "-A")
    stamp = "2030-01-%02dT00:00:00Z" % day
    subprocess.run(["git", "-C", repo, "commit", "-q", "-m", message], check=True, capture_output=True,
                   env=clean_env({"GIT_AUTHOR_DATE": stamp, "GIT_COMMITTER_DATE": stamp}))


def drop_the_map(repo):
    git(repo, "rm", "-q", "docs/features/login.md", "docs/features/projects.md")  # the index alone is no map
    git(repo, "commit", "-q", "-m", "drop the map")


class NoMapAtTheHead(unittest.TestCase):
    """A head with no feature file is judged by what the base says of it. A branch cut before the map
    landed passes and says it was not checked; one that removed the map fails; and where nothing
    tells the two apart the gate cannot run, as before."""

    def predating(self):
        """A branch cut from main before main got its map, with a route of its own that no feature lists."""
        repo = make_repo(self, CODE)
        git(repo, "checkout", "-q", "-b", "feat")
        commit(repo, {"app/routes.ts": FRIENDS}, "feature work")
        git(repo, "checkout", "-q", "main")
        commit(repo, MAP, "add the feature map")
        git(repo, "checkout", "-q", "feat")
        return repo

    def test_a_branch_that_predates_the_map_passes_and_says_it_was_not_checked(self):
        repo = self.predating()
        result = gate("feature-map", repo, "--base-ref", "main", *ROUTE_SURFACE)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertRegex(result.stdout, r"^feature-map: advisory: not checked: this branch has no feature map because it "
                                        r"predates it \(docs/features/ has no feature file here or at the merge base "
                                        r"[0-9a-f]{12}, and main has 2\)\. The map is checked once the branch takes main\n"
                                        r"feature-map: 1 advisory finding\(s\), not blocking\n$")
        annotated = gate("feature-map", repo, "--base-ref", "main", "--format", "github", *ROUTE_SURFACE)
        self.assertIn("::warning title=feature-map::not checked: this branch has no feature map", annotated.stdout)
        # The fix-forward is real: once the branch takes main it has the map, and its unowned route is found.
        git(repo, "merge", "-q", "--no-edit", "main")
        taken = gate("feature-map", repo, "--base-ref", "main", *ROUTE_SURFACE)
        self.assertEqual(taken.returncode, 1, taken.stdout + taken.stderr)
        self.assertIn("feature-map: 2 feature(s) in docs/features/; surfaces checked: 3 route", taken.stdout)
        self.assertIn("app/routes.ts:4: route `/friends` is owned by no feature", taken.stdout)

    def test_a_pull_request_opened_before_the_map_landed_is_not_failed_for_it(self):
        # Seen on a consumer (2026-10-08): the gate joined its gate list right after its map merged, and an open
        # pull request that had not taken main since answered `could not run`, exit 2, red through --soak too.
        # As a CI run has it: the pull request's head checked out by SHA, the base as origin/main, the gate
        # list a file outside the repository (a wrapper's), and no local main.
        clone = tempfile.mkdtemp(prefix="vv-clone-")
        self.addCleanup(shutil.rmtree, clone, True)
        git(self.predating(), "clone", "-q", ".", clone)
        git(clone, "checkout", "-q", "--detach", "origin/feat")
        entries = os.path.join(clone, ".git", "entries")
        for line in ("feature-map --surface route app/routes.ts '%s'" % ROUTE_REGEX, "feature-map --soak"):
            write(clone, {".git/entries": line + "\n"})
            result = runner("run", "--repo", clone, "--manifest", entries, "--format", "github", env={"GITHUB_BASE_REF": "main"})
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertNotIn("could not run", result.stdout + result.stderr)
            self.assertIn("The map is checked once the branch takes origin/main", result.stdout)
            # Not a bare PASS: the summary says the map was not checked.
            self.assertRegex(result.stdout, r"feature-map\s+PASS( \(soak\))? \(not checked: the branch predates the feature map\)\n")

    def test_a_pull_request_that_removes_the_map_cannot_run_and_soak_does_not_hide_it(self):
        # As before this rule, and for its reason: a gate with no map judged nothing, here or on the base once
        # this merged. What is new is that the message says who removed it.
        repo = make_repo(self, FILES)
        git(repo, "checkout", "-q", "-b", "feat")
        drop_the_map(repo)
        for extra in ((), ("--soak",)):
            result = gate("feature-map", repo, "--base-ref", "main", *ROUTE_SURFACE, *extra)
            self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
            self.assertRegex(result.stderr, REMOVED)
        write(repo, {".vibe-verifier": "feature-map --soak\n"})
        summary = runner("run", "--repo", repo, "--manifest", os.path.join(repo, ".vibe-verifier"), "--base-ref", "main")
        self.assertEqual(summary.returncode, 2, summary.stdout + summary.stderr)
        self.assertRegex(summary.stdout, r"feature-map\s+COULD NOT RUN\n")

    def test_a_map_only_one_of_two_merge_bases_has_was_removed(self):
        # After a criss-cross merge the base and the head have two merge bases and plain `git merge-base`
        # names one. Here the newer of the two has no map, and the other is the commit that brought the map
        # this branch took and then deleted. Read as one merge base, that passed as a branch predating the map.
        repo = make_repo(self, CODE)
        git(repo, "checkout", "-q", "-b", "lower")
        commit_on(repo, 3, {"app/lower.ts": "export const lower = 1\n"}, "lower")
        git(repo, "checkout", "-q", "main")
        commit_on(repo, 2, MAP, "add the feature map")
        git(repo, "checkout", "-q", "-b", "feat", "lower")
        git(repo, "merge", "-q", "--no-ff", "--no-edit", "main")
        git(repo, "checkout", "-q", "main")
        git(repo, "merge", "-q", "--no-ff", "--no-edit", "lower")
        git(repo, "checkout", "-q", "feat")
        drop_the_map(repo)
        bases = subprocess.run(["git", "-C", repo, "merge-base", "--all", "main", "HEAD"], capture_output=True, text=True)
        self.assertEqual(len(bases.stdout.split()), 2, bases.stdout + bases.stderr)
        result = gate("feature-map", repo, "--base-ref", "main")
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertRegex(result.stderr, REMOVED)

    def test_a_named_base_the_checkout_lacks_is_never_replaced_by_main(self):
        # release and main each got a map of their own, and the pull request targets release and deletes its
        # map. In a checkout without origin/release the base falls through to origin/main, whose map this
        # branch does predate. That would be a guess, so the gate cannot run.
        origin = make_repo(self, CODE)
        git(origin, "checkout", "-q", "-b", "release")
        commit(origin, MAP, "release gets a map")
        git(origin, "checkout", "-q", "-b", "feat")
        drop_the_map(origin)
        git(origin, "checkout", "-q", "main")
        commit(origin, MAP, "main gets a map")
        clone = tempfile.mkdtemp(prefix="vv-clone-")
        self.addCleanup(shutil.rmtree, clone, True)
        git(origin, "clone", "-q", "--branch", "feat", ".", clone)
        fetched = gate("feature-map", clone, env={"GITHUB_BASE_REF": "release"})
        self.assertEqual(fetched.returncode, 2, fetched.stdout + fetched.stderr)
        self.assertRegex(fetched.stderr, REMOVED)
        git(clone, "update-ref", "-d", "refs/remotes/origin/release")
        lacking = gate("feature-map", clone, env={"GITHUB_BASE_REF": "release"})
        self.assertEqual(lacking.returncode, 2, lacking.stdout + lacking.stderr)
        self.assertEqual(lacking.stderr, ABSENT + ", and the base this run names, release, is not in this checkout to tell "
                                         "whether the branch predates the map (fetch it: fetch-depth: 0)\n")

    def test_a_pull_request_that_introduces_the_map_is_judged_on_its_own_map(self):
        repo = make_repo(self, CODE)
        git(repo, "checkout", "-q", "-b", "feat")
        commit(repo, MAP, "add the feature map")
        result = gate("feature-map", repo, "--base-ref", "main", *ROUTE_SURFACE)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(result.stdout, "feature-map: 2 feature(s) in docs/features/; surfaces checked: 2 route\n")
        commit(repo, {"app/routes.ts": FRIENDS})
        self.assertEqual(gate("feature-map", repo, "--base-ref", "main", *ROUTE_SURFACE).returncode, 1)

    def test_with_nothing_to_tell_them_apart_a_missing_map_cannot_run(self):
        # No base at all: one commit on a branch that is neither main nor master.
        alone = make_repo(self, CODE, initial_branch="work")
        for extra in ((), ("--soak",)):
            result = gate("feature-map", alone, *extra)
            self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
            self.assertIn(ABSENT + ", and no base to tell whether the branch predates the map: no base ref to compare against",
                          result.stderr)
        # A named base that does not resolve.
        named = gate("feature-map", self.predating(), "--base-ref", "gone")
        self.assertEqual(named.returncode, 2)
        self.assertIn(ABSENT + ", and no base to tell whether the branch predates the map: --base-ref 'gone' does not resolve",
                      named.stderr)
        # A shallow clone: the base and the head are both there, and their merge base is not.
        shallow = tempfile.mkdtemp(prefix="vv-shallow-")
        self.addCleanup(shutil.rmtree, shallow, True)
        origin = self.predating()
        git(origin, "clone", "-q", "--depth", "1", "--branch", "feat", "file://" + origin, shallow)
        git(shallow, "fetch", "-q", "--depth", "1", "origin", "main:refs/remotes/origin/main")
        cut = gate("feature-map", shallow, env={"GITHUB_BASE_REF": "main"})
        self.assertEqual(cut.returncode, 2, cut.stdout + cut.stderr)
        self.assertIn(ABSENT + ", and origin/main and HEAD have no merge base to tell whether the branch predates the map "
                      "(fetch history: fetch-depth: 0)", cut.stderr)
        # A base with no map either: there is no map for the branch to predate, so the subscription has none.
        mapless = make_repo(self, CODE)
        git(mapless, "checkout", "-q", "-b", "feat")
        commit(mapless, {"app/routes.ts": FRIENDS})
        nowhere = gate("feature-map", mapless, "--base-ref", "main")
        self.assertEqual(nowhere.returncode, 2, nowhere.stdout + nowhere.stderr)
        self.assertEqual(nowhere.stderr, ABSENT + "\n")


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
