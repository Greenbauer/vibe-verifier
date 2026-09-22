import json
import os
import tempfile
import unittest

from helpers import commit, make_repo, runner

BAD_PKG = json.dumps({"dependencies": {"typescript": "5"}})


class Runner(unittest.TestCase):
    def manifest(self, repo, text):
        path = os.path.join(repo, ".vibe-verifier")
        with open(path, "w") as handle:
            handle.write(text)
        return path

    def run_in(self, repo, text, **kwargs):
        return runner("run", "--repo", repo, "--manifest", self.manifest(repo, text), **kwargs)

    def test_one_failing_gate_fails_the_run_and_every_gate_still_runs(self):
        repo = make_repo(self, {"package.json": BAD_PKG})
        result = self.run_in(repo, "build-tools-in-devdependencies\nno-duplicate-package-json-keys\n")
        self.assertEqual(result.returncode, 1)
        self.assertRegex(result.stdout, r"build-tools-in-devdependencies\s+FAIL")
        self.assertRegex(result.stdout, r"no-duplicate-package-json-keys\s+PASS")

    def test_all_pass(self):
        self.assertEqual(self.run_in(make_repo(self, {"a.txt": "x"}), "no-duplicate-package-json-keys\n").returncode, 0)

    def test_comments_blank_lines_and_quoted_args(self):
        repo = make_repo(self, {"a.txt": "x"})
        text = "# subscribed gates\n\nbranch-name-length --max 60  # budget\nnew-source-has-test --source 'lib/**/*.ts'\n"
        self.assertEqual(self.run_in(repo, text).returncode, 0)

    def test_soak_is_labelled(self):
        result = self.run_in(make_repo(self, {"package.json": BAD_PKG}), "build-tools-in-devdependencies --soak\n")
        self.assertEqual(result.returncode, 0)
        self.assertRegex(result.stdout, r"PASS \(soak\)")

    def test_verifying_nothing_is_not_a_pass(self):
        repo = make_repo(self, {"a.txt": "x"})
        self.assertEqual(runner("run", "--repo", repo, "--manifest", os.path.join(repo, "absent")).returncode, 2)
        self.assertEqual(self.run_in(repo, "# nothing subscribed\n").returncode, 2)
        self.assertEqual(self.run_in(repo, "no-such-gate\n").returncode, 2)

    def test_could_not_run_outranks_violations(self):
        repo = make_repo(self, {"package.json": BAD_PKG})
        result = self.run_in(repo, "build-tools-in-devdependencies\nbranch-name-length\n")  # second one lacks --max
        self.assertEqual(result.returncode, 2)
        self.assertRegex(result.stdout, r"branch-name-length\s+COULD NOT RUN")

    def test_github_format_groups_and_writes_the_step_summary(self):
        repo = make_repo(self, {"package.json": BAD_PKG})
        summary = tempfile.NamedTemporaryFile(delete=False)
        summary.close()
        self.addCleanup(os.unlink, summary.name)
        result = self.run_in(repo, "build-tools-in-devdependencies\n", env={"GITHUB_STEP_SUMMARY": summary.name})
        self.assertNotIn("::group::", result.stdout)  # text format by default
        grouped = runner("run", "--repo", repo, "--manifest", os.path.join(repo, ".vibe-verifier"), "--format", "github",
                         env={"GITHUB_STEP_SUMMARY": summary.name})
        self.assertIn("::group::build-tools-in-devdependencies", grouped.stdout)
        self.assertIn("::error file=package.json", grouped.stdout)
        with open(summary.name) as handle:
            self.assertIn("| `build-tools-in-devdependencies` | FAIL |", handle.read())

    def test_list(self):
        self.assertEqual(runner("list").stdout.split(),
                         ["acceptance-verdict", "actionlint", "branch-name-length", "build-tools-in-devdependencies", "cognitive-complexity",
                          "gitleaks", "max-file-lines", "new-source-has-test", "no-duplicate-package-json-keys", "qae-artifacts",
                          "review-receipt", "zizmor"])


if __name__ == "__main__":
    unittest.main()


class ManifestFromBase(unittest.TestCase):
    """On a pull request the manifest is judged from the base: a gate the branch removed or
    weakened still runs as it was; a gate it added runs too; with no base manifest the head's stands."""
    GOOD_PKG = json.dumps({"devDependencies": {"typescript": "5"}})
    BASE = {"package.json": GOOD_PKG, ".vibe-verifier": "build-tools-in-devdependencies\n"}

    def judged(self, repo):
        return runner("run", "--repo", repo, "--manifest", os.path.join(repo, ".vibe-verifier"), "--base-ref", "HEAD~1")

    def test_a_removed_gate_still_runs(self):
        repo = make_repo(self, self.BASE)
        commit(repo, {"package.json": BAD_PKG, ".vibe-verifier": "branch-name-length --max 60\n"})
        result = self.judged(repo)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertRegex(result.stdout, r"build-tools-in-devdependencies\s+FAIL")
        self.assertIn("judged by the manifest at", result.stdout)

    def test_soak_appended_on_the_branch_does_not_soften_the_gate(self):
        repo = make_repo(self, self.BASE)
        commit(repo, {"package.json": BAD_PKG, ".vibe-verifier": "build-tools-in-devdependencies --soak\n"})
        result = self.judged(repo)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertNotIn("(soak)", result.stdout)

    def test_a_gate_the_branch_adds_runs_now(self):
        repo = make_repo(self, {"package.json": BAD_PKG, ".vibe-verifier": "branch-name-length --max 60\n"})
        commit(repo, {".vibe-verifier": "branch-name-length --max 60\nbuild-tools-in-devdependencies\n"})
        result = self.judged(repo)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertRegex(result.stdout, r"build-tools-in-devdependencies\s+FAIL")

    def test_no_manifest_at_the_base_means_the_head_manifest_stands(self):
        repo = make_repo(self, {"package.json": BAD_PKG})
        commit(repo, {".vibe-verifier": "branch-name-length --max 60\n"})
        result = self.judged(repo)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertNotIn("judged by the manifest at", result.stdout)

    def test_an_unchanged_manifest_says_nothing(self):
        repo = make_repo(self, self.BASE)
        commit(repo, {"README.md": "x\n"})
        result = self.judged(repo)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertNotIn("judged by the manifest at", result.stdout)


class Tool(unittest.TestCase):
    """`tool <name>` installs a toolchain under tools/ from its lockfile and prints where."""

    def test_the_qae_browser_toolchain_installs_from_its_lockfile(self):
        result = runner("tool", "qae-browser")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        where = result.stdout.strip()
        self.assertTrue(os.path.isfile(os.path.join(where, "node_modules", "@playwright", "mcp", "cli.js")), where)
        self.assertTrue(os.path.isfile(os.path.join(where, "node_modules", ".bin", "playwright")), where)

    def test_an_unknown_toolchain_is_exit_2(self):
        result = runner("tool", "nope")
        self.assertEqual(result.returncode, 2)
        self.assertIn("no toolchain named 'nope'", result.stderr)
