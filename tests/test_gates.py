import json
import unittest

from helpers import commit, gate, git, make_repo

PKG = '{"name": "app", "dependencies": {"react": "19.0.0"}}\n'


class Contract(unittest.TestCase):
    """Exit codes, --soak, formats and base-ref resolution, exercised through a real gate."""

    def untested_source_on_branch(self):
        repo = make_repo(self, {"package.json": PKG})
        git(repo, "checkout", "-q", "-b", "feat/x")
        commit(repo, {"lib/a.ts": "export const a = 1\n"})
        return repo

    def test_violation_exits_1(self):
        self.assertEqual(gate("new-source-has-test", self.untested_source_on_branch()).returncode, 1)

    def test_soak_reports_but_exits_0(self):
        result = gate("new-source-has-test", self.untested_source_on_branch(), "--soak")
        self.assertEqual(result.returncode, 0)
        self.assertIn("lib/a.ts", result.stdout)
        self.assertIn("reported only", result.stdout)

    def test_soak_never_masks_could_not_run(self):
        repo = make_repo(self, {"a.txt": "x"}, initial_branch="trunk")
        self.assertEqual(gate("new-source-has-test", repo, "--soak").returncode, 2)

    def test_no_base_ref_exits_2_with_the_fix(self):
        repo = make_repo(self, {"a.txt": "x"}, initial_branch="trunk")
        result = gate("new-source-has-test", repo)
        self.assertEqual(result.returncode, 2)
        self.assertIn("fetch-depth", result.stderr)

    def test_explicit_base_ref_must_resolve(self):
        result = gate("new-source-has-test", self.untested_source_on_branch(), "--base-ref", "nope")
        self.assertEqual(result.returncode, 2)

    def test_falls_back_to_previous_commit(self):
        repo = make_repo(self, {"a.txt": "x"}, initial_branch="trunk")
        commit(repo, {"lib/a.ts": "export const a = 1\n"})
        self.assertEqual(gate("new-source-has-test", repo).returncode, 1)

    def test_github_format_annotates_the_file(self):
        repo = self.untested_source_on_branch()
        self.assertIn("::error file=lib/a.ts,title=new-source-has-test::", gate("new-source-has-test", repo, "--format", "github").stdout)
        self.assertIn("::warning file=lib/a.ts", gate("new-source-has-test", repo, "--format", "github", "--soak").stdout)

    def test_json_format(self):
        payload = json.loads(gate("new-source-has-test", self.untested_source_on_branch(), "--format", "json").stdout)
        self.assertEqual(payload["status"], "violations")
        self.assertEqual(payload["findings"][0]["path"], "lib/a.ts")

    def test_bad_usage_exits_2(self):
        self.assertEqual(gate("branch-name-length", make_repo(self, {"a.txt": "x"})).returncode, 2)


class NoDuplicatePackageJsonKeys(unittest.TestCase):
    def test_duplicate_top_level_key_with_line(self):
        repo = make_repo(self, {"package.json": '{\n  "scripts": {},\n  "name": "a",\n  "scripts": {}\n}\n'})
        result = gate("no-duplicate-package-json-keys", repo)
        self.assertEqual(result.returncode, 1)
        self.assertIn('package.json:4: duplicate key "scripts"', result.stdout)

    def test_duplicate_nested_key(self):
        repo = make_repo(self, {"web/package.json": '{"dependencies": {"a": "1", "a": "2"}}'})
        self.assertEqual(gate("no-duplicate-package-json-keys", repo).returncode, 1)

    def test_invalid_json_is_a_violation(self):
        repo = make_repo(self, {"package.json": "{not json"})
        result = gate("no-duplicate-package-json-keys", repo)
        self.assertEqual(result.returncode, 1)
        self.assertIn("not valid JSON", result.stdout)

    def test_clean_and_absent(self):
        self.assertEqual(gate("no-duplicate-package-json-keys", make_repo(self, {"package.json": PKG})).returncode, 0)
        self.assertEqual(gate("no-duplicate-package-json-keys", make_repo(self, {"a.txt": "x"})).returncode, 0)


class BuildToolsInDevDependencies(unittest.TestCase):
    def run_with(self, manifest):
        return gate("build-tools-in-devdependencies", make_repo(self, {"package.json": json.dumps(manifest)}))

    def test_build_tool_in_dependencies(self):
        result = self.run_with({"dependencies": {"react": "19", "typescript": "5"}})
        self.assertEqual(result.returncode, 1)
        self.assertIn('"typescript"', result.stdout)
        self.assertNotIn('"react"', result.stdout)

    def test_type_package_prefix(self):
        self.assertEqual(self.run_with({"dependencies": {"@types/node": "22"}}).returncode, 1)

    def test_dev_dependencies_are_fine(self):
        self.assertEqual(self.run_with({"dependencies": {"react": "19"}, "devDependencies": {"typescript": "5"}}).returncode, 0)

    def test_override_needs_a_reason(self):
        allowed = {"dependencies": {"typescript": "5"}, "vibeVerifier": {"allowInDependencies": {"typescript": "runtime transpile"}}}
        self.assertEqual(self.run_with(allowed).returncode, 0)
        allowed["vibeVerifier"]["allowInDependencies"]["typescript"] = "  "
        result = self.run_with(allowed)
        self.assertEqual(result.returncode, 1)
        self.assertIn("no reason", result.stdout)


class NewSourceHasTest(unittest.TestCase):
    def on_branch(self, base_files, branch_files):
        repo = make_repo(self, dict({"package.json": PKG}, **base_files))
        git(repo, "checkout", "-q", "-b", "feat/x")
        commit(repo, branch_files)
        return repo

    def test_sibling_test_satisfies(self):
        repo = self.on_branch({}, {"app/lib/a.js": "x", "app/lib/a.test.js": "t"})
        self.assertEqual(gate("new-source-has-test", repo).returncode, 0)

    def test_tests_directory_satisfies(self):
        repo = self.on_branch({}, {"lib/a.ts": "x", "lib/__tests__/a.test.ts": "t"})
        self.assertEqual(gate("new-source-has-test", repo).returncode, 0)

    def test_a_test_that_predates_the_pr_counts(self):
        repo = self.on_branch({"lib/a.test.ts": "t"}, {"lib/a.ts": "x"})
        self.assertEqual(gate("new-source-has-test", repo).returncode, 0)

    def test_editing_an_existing_file_never_trips(self):
        repo = self.on_branch({"lib/a.ts": "x"}, {"lib/a.ts": "changed"})
        self.assertEqual(gate("new-source-has-test", repo).returncode, 0)

    def test_barrels_and_config_are_excluded(self):
        repo = self.on_branch({}, {"lib/index.ts": "x", "next.config.mjs": "x", "lib/a.d.ts": "x"})
        self.assertEqual(gate("new-source-has-test", repo).returncode, 0)

    def test_source_narrows_and_exclude_removes(self):
        repo = self.on_branch({}, {"scripts/tool.ts": "x", "lib/a.ts": "x"})
        narrowed = gate("new-source-has-test", repo, "--source", "lib/**/*.ts")
        self.assertEqual(narrowed.returncode, 1)
        self.assertNotIn("scripts/tool.ts", narrowed.stdout)
        self.assertEqual(gate("new-source-has-test", repo, "--source", "lib/**/*.ts", "--exclude", "lib/a.ts").returncode, 0)

    def test_root_level_file(self):
        self.assertEqual(gate("new-source-has-test", self.on_branch({}, {"middleware.ts": "x"})).returncode, 1)

    def test_a_test_anywhere_that_imports_it_by_relative_path_counts(self):
        # a layout seen in practice: one central lib/__tests__/ named by topic, not by source file.
        repo = self.on_branch({}, {
            "lib/email/templates.ts": "x",
            "lib/__tests__/email-templates.test.ts": "import { render } from '../email/templates'\n",
        })
        self.assertEqual(gate("new-source-has-test", repo).returncode, 0)

    def test_require_with_an_extension_counts(self):
        repo = self.on_branch({}, {"lib/a.ts": "x", "tests/a.spec.js": "const a = require('../lib/a.js')\n"})
        self.assertEqual(gate("new-source-has-test", repo).returncode, 0)

    def test_a_test_importing_a_different_module_does_not_count(self):
        repo = self.on_branch({}, {
            "lib/a.ts": "x", "lib/b.ts": "y",
            "lib/__tests__/b.test.ts": "import '../b'\n",
        })
        result = gate("new-source-has-test", repo)
        self.assertEqual(result.returncode, 1)
        self.assertIn("lib/a.ts", result.stdout)
        self.assertNotIn("lib/b.ts", result.stdout)


class BranchNameLength(unittest.TestCase):
    def test_over_and_under_budget(self):
        repo = make_repo(self, {"a.txt": "x"})
        over = gate("branch-name-length", repo, "--max", "10", "--branch", "feat/a_long_branch")
        self.assertEqual(over.returncode, 1)
        self.assertIn('"feat-a-long-branch" is 18 characters, budget is 10', over.stdout)
        self.assertEqual(gate("branch-name-length", repo, "--max", "18", "--branch", "feat/a_long_branch").returncode, 0)

    def test_reads_the_checked_out_branch_then_the_pr_head(self):
        repo = make_repo(self, {"a.txt": "x"})
        self.assertEqual(gate("branch-name-length", repo, "--max", "4").returncode, 0)  # "main"
        self.assertEqual(gate("branch-name-length", repo, "--max", "4", env={"GITHUB_HEAD_REF": "feat/too-long"}).returncode, 1)

    def test_detached_head_could_not_run(self):
        repo = make_repo(self, {"a.txt": "x"})
        git(repo, "checkout", "-q", "--detach")
        self.assertEqual(gate("branch-name-length", repo, "--max", "10").returncode, 2)


if __name__ == "__main__":
    unittest.main()
