"""The coverage-not-lost gate: the real project Vitest with coverage-v8 (npm ci from tests/fixtures/coverage-not-lost),
the real pinned coverage.py around a real pytest and unittest (a venv from tests/requirements-pytest.txt), and a
stand-in Vitest for the version rule."""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from helpers import ROOT, clean_env, commit, gate, git, install_fixture, make_repo

FIXTURE = ROOT / "tests" / "fixtures" / "coverage-not-lost"
PYTEST = ROOT / "tests" / "requirements-pytest.txt"
IGNORE = {".gitignore": "/node_modules\n"}

PRICE = """export function discount(total: number, member: boolean): number {
  if (member && total > 100) {
    return total * 0.9;
  }
  return total;
}
export function rounded(value: number): number {
  return Math.round(value * 100) / 100;
}
"""
MEMBER_TEST = """import { expect, it } from "vitest";
import { discount, rounded } from "./price";
it("gives members 10% off over 100", () => { expect(rounded(discount(200, true))).toBe(180); });
"""
PLAIN_TEST = """import { expect, it } from "vitest";
import { discount } from "./price";
it("charges everyone else full price", () => { expect(discount(50, false)).toBe(50); });
"""
# The same module after a refactor: an untested function added above (every line below it moves), and
# rounded() moved to money.ts. Read line for line, the merge base's covered lines would land on untested ones.
PRICE_REFACTORED = """export function unused(): number {
  return 0;
}
export function discount(total: number, member: boolean): number {
  if (member && total > 100) {
    return total * 0.9;
  }
  return total;
}
export { rounded } from "./money";
"""
MONEY = "export function rounded(value: number): number {\n  return Math.round(value * 100) / 100;\n}\n"
# Covers line 3 of settle.ts in every run but the second one the gate makes (the first at the head), counted in a
# file outside the repository: coverage that changes from run to run.
FLAKY_SOURCE = """export function settle(fast: boolean): string {
  if (fast) {
    return "fast";
  }
  return "slow";
}
"""
FLAKY_TEST = """import { readFileSync, writeFileSync } from "node:fs";
import { expect, it } from "vitest";
import { settle } from "./settle";
const counter = process.env.VV_RUN_COUNTER as string;
const run = Number(readFileSync(counter, "utf8"));
writeFileSync(counter, String(run + 1));
it("settles", () => { expect(settle(run !== 1)).toBe(run !== 1 ? "fast" : "slow"); });
"""
FAILING_TEST = """import { expect, it } from "vitest";
import { discount } from "./price";
it("charges everyone else full price", () => { expect(discount(50, false)).toBe(51); });
"""
THRESHOLD_CONFIG = """import { defineConfig } from "vitest/config";
export default defineConfig({ test: { coverage: { provider: "istanbul", reporter: ["text"], thresholds: { lines: 100 } } } });
"""
HANGING_TEST = """import { it } from "vitest";
it("never settles", () => new Promise(() => {}), 600000);
"""


def findings(result):
    return json.loads(result.stdout)["findings"]


class RealVitest(unittest.TestCase):
    """The fixture's own Vitest 4.1.11 and its own @vitest/coverage-v8 4.1.11."""

    @classmethod
    def setUpClass(cls):
        cls.deps = tempfile.mkdtemp(prefix="vv-coverage-deps-")
        for name in ("package.json", "package-lock.json"):
            shutil.copy(FIXTURE / name, cls.deps)
        install_fixture(cls.deps)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.deps, ignore_errors=True)

    def project(self, base, head, provider=True):
        """A repository whose node_modules is the fixture's, or (provider=False) holds only its vitest."""
        repo = make_repo(self, dict(IGNORE, **{"package.json": '{"type": "module"}\n'}, **base))
        if provider:
            os.symlink(os.path.join(self.deps, "node_modules"), os.path.join(repo, "node_modules"))
        else:
            os.makedirs(os.path.join(repo, "node_modules"))
            os.symlink(os.path.join(self.deps, "node_modules", "vitest"), os.path.join(repo, "node_modules", "vitest"))
        for path, text in head.items():
            if text is None:
                git(repo, "rm", "-q", path)
        commit(repo, {path: text for path, text in head.items() if text is not None})
        return repo

    def run_gate(self, repo, *args, env=None):
        return gate("coverage-not-lost", repo, "--base-ref", "HEAD~1", "--format", "json", *args, env=env)

    def assertNoWorktreeLeft(self, repo):
        listed = subprocess.run(["git", "-C", repo, "worktree", "list", "--porcelain"], capture_output=True, text=True).stdout
        self.assertEqual(listed.count("worktree "), 1, listed)

    def test_a_deleted_test_that_was_the_only_cover_flags_its_lines(self):
        repo = self.project({"src/price.ts": PRICE, "src/price.test.ts": MEMBER_TEST, "src/plain.test.ts": PLAIN_TEST},
                            {"src/price.test.ts": None})
        result = self.run_gate(repo)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertEqual([(f["path"], f["line"], f["advisory"]) for f in findings(result)],
                         [("src/price.ts", 3, False), ("src/price.ts", 8, False)])
        first = findings(result)[0]["message"]
        self.assertIn("Line 3 (`return total * 0.9;`) ran under the project's tests at the merge base and does not at the head", first)
        self.assertIn("Run at the merge base by the test files this pull request deleted or changed: src/price.test.ts (deleted).", first)
        self.assertIn("`coverage-lost: <reason>`", first)
        self.assertIn("measuring with the project's own @vitest/coverage-v8 4.1.11", result.stderr)
        self.assertRegex(result.stderr, r"the first runs found 2 that no longer run at the head, and the second runs agree on 2")
        self.assertNoWorktreeLeft(repo)

    def test_a_moved_test_does_not_flag(self):
        # The project's own 100% line threshold, which this suite misses, is not the gate's: the runs still pass.
        repo = self.project({"src/price.ts": PRICE, "src/price.test.ts": MEMBER_TEST, "vitest.config.ts": THRESHOLD_CONFIG},
                            {"src/price.test.ts": None, "src/pricing/price.test.ts": MEMBER_TEST.replace('"./price"', '"../price"')})
        result = self.run_gate(repo)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(findings(result), [])
        self.assertIn("every one the pull request left unchanged still runs at the head", result.stderr)

    def test_a_refactor_that_moves_code_does_not_flag(self):
        repo = self.project({"src/price.ts": PRICE, "src/price.test.ts": MEMBER_TEST, "src/plain.test.ts": PLAIN_TEST},
                            {"src/price.ts": PRICE_REFACTORED, "src/money.ts": MONEY})
        result = self.run_gate(repo)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(findings(result), [])

    def test_coverage_that_changes_between_runs_is_not_reported(self):
        counter = tempfile.NamedTemporaryFile("w", delete=False, suffix=".count")
        counter.write("0")
        counter.close()
        self.addCleanup(os.unlink, counter.name)
        repo = self.project({"src/settle.ts": FLAKY_SOURCE, "src/settle.test.ts": FLAKY_TEST, "README.md": "a\n"}, {"README.md": "b\n"})
        result = self.run_gate(repo, env={"VV_RUN_COUNTER": counter.name})
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(findings(result), [])
        self.assertIn("the first runs found 1 that no longer run at the head, and the second runs agree on 0", result.stderr)
        self.assertIn("not reported, because the two runs disagree (coverage that changes from run to run): src/settle.ts:3",
                      result.stderr)
        self.assertEqual(Path(counter.name).read_text(), "4")  # merge base, head, merge base, head

    def test_a_suite_that_fails_at_either_side_cannot_run_even_under_soak(self):
        for side, base, head in (("merge base", {"src/plain.test.ts": FAILING_TEST}, {"src/plain.test.ts": PLAIN_TEST}),
                                 ("head", {"src/plain.test.ts": PLAIN_TEST}, {"src/plain.test.ts": FAILING_TEST})):
            with self.subTest(side=side):
                repo = self.project(dict({"src/price.ts": PRICE}, **base), head)
                result = self.run_gate(repo, "--soak")
                self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
                self.assertRegex(result.stderr, r"the suite of \. fails at the %s(?: [0-9a-f]{12})? \(vitest exited 1; its failures "
                                                r"are above\)" % side)
                self.assertIn("plain.test.ts > charges everyone else full price: AssertionError: expected 50 to be 51", result.stderr)
                self.assertEqual("a merge base that imports one this pull request removed fails here" in result.stderr,
                                 side == "merge base")
                self.assertNoWorktreeLeft(repo)

    def test_a_coverage_lost_line_in_the_body_marks_the_loss_intended(self):
        repo = self.project({"src/price.ts": PRICE, "src/price.test.ts": MEMBER_TEST, "src/plain.test.ts": PLAIN_TEST},
                            {"src/price.test.ts": None})
        body = Path(tempfile.mkdtemp(prefix="vv-coverage-body-"), "body.md")
        self.addCleanup(shutil.rmtree, body.parent, True)
        body.write_text("## Summary\n\nRetires the member discount.\n\n- coverage-lost: the member discount is retired next week\n")
        result = self.run_gate(repo, "--body", str(body))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual([(f["line"], f["advisory"]) for f in findings(result)], [(3, True), (8, True)])
        self.assertTrue(findings(result)[0]["message"].startswith(
            "intended, the pull request body says `coverage-lost: the member discount is retired next week`: Line 3"))
        body.write_text("## Summary\n\ncoverage-lost:\nno reason on the line above, so none is given\n")
        result = self.run_gate(repo, "--body", str(body))
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertEqual({f["advisory"] for f in findings(result)}, {False})
        missing = self.run_gate(repo, "--body", str(body) + ".missing")
        self.assertEqual(missing.returncode, 2, missing.stdout + missing.stderr)
        self.assertIn("--body cannot be read", missing.stderr)

    def test_two_projects_are_one_suite_and_findings_name_repository_paths(self):
        web = {"web/src/price.ts": PRICE, "web/src/price.test.ts": MEMBER_TEST}
        api = {"api/src/price.ts": PRICE, "api/src/plain.test.ts": PLAIN_TEST}
        repo = self.project(dict(web, **api), {"web/src/price.test.ts": None})
        result = self.run_gate(repo, "--project", "web", "--project", "api")
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        # web's only test is gone, so nothing of web runs at the head: lines 2-3 and 8 ran at the merge base.
        self.assertEqual([(f["path"], f["line"]) for f in findings(result)], [("web/src/price.ts", 2), ("web/src/price.ts", 8)])
        self.assertIn("Lines 2-3 (`if (member && total > 100) {`)", findings(result)[0]["message"])

    def test_without_a_provider_of_its_own_it_measures_with_the_catalogs(self):
        repo = self.project({"src/price.ts": PRICE, "src/price.test.ts": MEMBER_TEST, "src/plain.test.ts": PLAIN_TEST},
                            {"src/price.test.ts": None}, provider=False)
        result = self.run_gate(repo)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertEqual([(f["path"], f["line"]) for f in findings(result)], [("src/price.ts", 3), ("src/price.ts", 8)])
        self.assertIn("measuring with the catalog's @vitest/coverage-v8 4.1.11", result.stderr)

    def test_a_run_past_the_timeout_cannot_run(self):
        repo = self.project({"src/price.ts": PRICE, "src/hang.test.ts": HANGING_TEST, "README.md": "a\n"}, {"README.md": "b\n"})
        result = self.run_gate(repo, "--timeout", "8")
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn("vitest did not finish within --timeout 8s; no verdict on any file. Raise --timeout on the base manifest",
                      result.stderr)
        self.assertNoWorktreeLeft(repo)


class VitestVersions(unittest.TestCase):
    """A stand-in Vitest: the provider must be of exactly its version, and one is refused before anything runs."""

    def test_a_vitest_with_no_provider_of_its_version_cannot_run(self):
        repo = make_repo(self, dict(IGNORE, **{"src/a.ts": "export const a = 1;\n"}))
        for name, version in (("vitest", "4.1.2"), ("@vitest/coverage-v8", "4.1.0")):
            Path(repo, "node_modules", name).mkdir(parents=True)
            Path(repo, "node_modules", name, "package.json").write_text(json.dumps({"name": name, "version": version}))
        commit(repo, {"src/a.ts": "export const a = 2;\n"})
        result = gate("coverage-not-lost", repo, "--base-ref", "HEAD~1", "--soak")
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn("vitest 4.1.2 has no coverage provider of its version: the project's own @vitest/coverage-v8 is 4.1.0, "
                      "and the catalog pins one only for vitest 2.1.9, 3.2.7, 4.0.18, 4.1.11, 5.0.3. "
                      "Add @vitest/coverage-v8 4.1.2 to the project's devDependencies", result.stderr)


PY_PRICE = "def discount(total, member):\n    if member and total > 100:\n        return total * 0.9\n    return total\n"
PY_MEMBER = "from shop.price import discount\n\n\ndef test_member():\n    assert discount(200, True) == 180\n"
PY_PLAIN = ("import unittest\n\nfrom shop.price import discount\n\n\nclass Plain(unittest.TestCase):\n"
            "    def test_plain(self):\n        self.assertEqual(discount(50, False), 50)\n")
PY_MEMBER_CASE = ("import unittest\n\nfrom shop.price import discount\n\n\nclass Member(unittest.TestCase):\n"
                  "    def test_member(self):\n        self.assertEqual(discount(200, True), 180)\n")
# A command line no test imports: the only test of it runs it as a child process. testimony() is named so that
# coverage.py's test_function context would take it for a test, were it recorded in the child.
PY_APP = ('def greeting(name):\n    return "hello " + name\n\n\n'
          'def testimony(name):\n    return greeting(name) + ", said the witness"\n\n\nprint(testimony("world"))\n')
PY_APP_TEST = ("import subprocess\nimport sys\nimport unittest\n\n\nclass App(unittest.TestCase):\n"
               "    def test_prints(self):\n"
               '        out = subprocess.run([sys.executable, "app.py"], capture_output=True, text=True, check=True).stdout\n'
               '        self.assertEqual(out, "hello world, said the witness\\n")\n')
# A test that writes a script to a directory of its own, runs it as a child and removes the directory: its source is
# gone by the time the report is read.
PY_TEMP_SCRIPT_TEST = ("import shutil\nimport subprocess\nimport sys\nimport tempfile\nimport unittest\nfrom pathlib import Path\n\n\n"
                       "class Script(unittest.TestCase):\n    def test_script(self):\n        where = tempfile.mkdtemp()\n"
                       '        Path(where, "script.py").write_text("print(6 * 7)\\n")\n'
                       '        out = subprocess.run([sys.executable, "script.py"], cwd=where, capture_output=True, text=True).stdout\n'
                       "        shutil.rmtree(where)\n"
                       '        self.assertEqual(out, "42\\n")\n')
# A test of a package installed in the project's venv, as a dependency is.
PY_VENDORED_TEST = ("import unittest\n\nfrom shop.price import discount\nfrom vendored import double\n\n\n"
                    "class Member(unittest.TestCase):\n    def test_member(self):\n"
                    "        self.assertEqual(double(discount(200, True)), 360)\n")
PY_SITE_TEST = ('import sys\nimport unittest\n\n\nclass Site(unittest.TestCase):\n    def test_site(self):\n'
                '        self.assertTrue(getattr(sys, "vv_project_sitecustomize", False))\n')


class RealCoveragePy(unittest.TestCase):
    """The pinned coverage.py in the project's own interpreter, around pytest 9 and unittest."""

    @classmethod
    def setUpClass(cls):
        cls.env = tempfile.mkdtemp(prefix="vv-coverage-py-env-")
        subprocess.run([sys.executable, "-m", "venv", cls.env], check=True, capture_output=True)
        cls.python = os.path.join(cls.env, "bin", "python")
        subprocess.run([cls.python, "-m", "pip", "install", "--quiet", "--disable-pip-version-check", "--require-hashes",
                        "--only-binary", ":all:", "-r", str(PYTEST)], check=True, capture_output=True, text=True)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.env, ignore_errors=True)

    def run_gate(self, repo, *args):
        return gate("coverage-not-lost", repo, "--base-ref", "HEAD~1", "--python", self.python, "--format", "json", *args)

    def project(self, base, head):
        repo = make_repo(self, dict({"shop/__init__.py": "", "shop/price.py": PY_PRICE}, **base))
        for path, text in head.items():
            if text is None:
                git(repo, "rm", "-q", path)
        commit(repo, {path: text for path, text in head.items() if text is not None})
        return repo

    def test_pytest_names_the_test_that_ran_a_lost_line(self):
        repo = self.project({"tests/test_member.py": PY_MEMBER, "tests/test_plain.py": PY_PLAIN}, {"tests/test_member.py": None})
        result = self.run_gate(repo, "--runner", "pytest")
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        (finding,) = findings(result)
        self.assertEqual((finding["path"], finding["line"]), ("shop/price.py", 3))
        self.assertIn("Line 3 (`return total * 0.9`) ran under the project's tests at the merge base", finding["message"])
        self.assertIn("Run at the merge base by test_member.test_member.", finding["message"])

    def test_pytest_a_moved_test_does_not_flag(self):
        repo = self.project({"tests/test_member.py": PY_MEMBER, "tests/test_plain.py": PY_PLAIN},
                            {"tests/test_member.py": None, "tests/pricing/test_member.py": PY_MEMBER})
        result = self.run_gate(repo, "--runner", "pytest")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_unittest_flags_a_deleted_test_and_a_red_head_cannot_run(self):
        repo = self.project({"tests/test_member.py": PY_MEMBER_CASE, "tests/test_plain.py": PY_PLAIN}, {"tests/test_member.py": None})
        result = self.run_gate(repo, "--runner", "unittest", "--args", "discover -s tests")
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertEqual([(f["path"], f["line"]) for f in findings(result)], [("shop/price.py", 3)])
        self.assertIn("Run at the merge base by test_member.Member.test_member.", findings(result)[0]["message"])
        commit(repo, {"tests/test_plain.py": PY_PLAIN.replace("50)", "51)")})
        red = self.run_gate(repo, "--runner", "unittest", "--args", "discover -s tests", "--soak")
        self.assertEqual(red.returncode, 2, red.stdout + red.stderr)
        self.assertIn("the suite of . fails at the head (unittest exited 1; its failures are above)", red.stderr)
        self.assertIn("AssertionError: 50 != 51", red.stderr)

    def test_a_line_only_a_child_process_ran_is_measured(self):
        repo = self.project({"app.py": PY_APP, "tests/test_app.py": PY_APP_TEST, "tests/test_plain.py": PY_PLAIN},
                            {"tests/test_app.py": None})
        result = self.run_gate(repo, "--runner", "unittest", "--args", "discover -s tests")
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertEqual([(f["path"], f["line"]) for f in findings(result)], [("app.py", 1), ("app.py", 5), ("app.py", 9)])
        self.assertIn("Lines 5-6 (`def testimony(name):`) ran under the project's tests at the merge base and does not at "
                      "the head", findings(result)[1]["message"])
        # The child names no test function: none is on its stack, and testimony() is not one.
        self.assertNotIn("Run at the merge base by", "".join(f["message"] for f in findings(result)))
        # Each run says what it measured, by top directory, and how long it took: app.py's 5 lines only at the merge base.
        self.assertRegex(result.stderr, r"the run at the merge base [0-9a-f]{12}: 8 line\(s\) ran \(5 at the root, 3 in shop/\), "
                                        r"in \d+ seconds")
        self.assertRegex(result.stderr, r"the run at the head: 3 line\(s\) ran \(3 in shop/\), in \d+ seconds")

    def test_without_subprocess_measurement_a_line_only_a_child_ran_is_missed(self):
        # The test above, with coverage.py's subprocess support switched off in a copy of the gate: it passes.
        copy = tempfile.mkdtemp(prefix="vv-coverage-copy-")
        self.addCleanup(shutil.rmtree, copy, True)
        shutil.copytree(ROOT / "gates", os.path.join(copy, "gates"))
        shutil.copytree(ROOT / "tools" / "coverage-not-lost-python", os.path.join(copy, "tools", "coverage-not-lost-python"))
        runs = Path(copy, "gates", "_coverage_runs.py")
        source = runs.read_text()
        self.assertEqual(source.count("patch = subprocess\\n"), 1)
        runs.write_text(source.replace("patch = subprocess\\n", ""))
        repo = self.project({"app.py": PY_APP, "tests/test_app.py": PY_APP_TEST, "tests/test_plain.py": PY_PLAIN},
                            {"tests/test_app.py": None})
        result = subprocess.run([sys.executable, os.path.join(copy, "gates", "coverage_not_lost.py"), "--repo", repo,
                                 "--base-ref", "HEAD~1", "--python", self.python, "--format", "json", "--runner", "unittest",
                                 "--args", "discover -s tests"], capture_output=True, text=True, env=clean_env())
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(findings(result), [])
        self.assertIn("every one the pull request left unchanged still runs at the head", result.stderr)

    def test_a_script_a_test_ran_from_a_directory_it_removed_does_not_stop_the_report(self):
        # Only the checkout and the working copy are reported: a file outside them, gone before the report is read,
        # stopped the report ("No source for code") when every file a child ran was.
        repo = self.project({"tests/test_member.py": PY_MEMBER_CASE, "tests/test_script.py": PY_TEMP_SCRIPT_TEST, "README.md": "a\n"},
                            {"README.md": "b\n"})
        result = self.run_gate(repo, "--runner", "unittest", "--args", "discover -s tests")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertRegex(result.stderr, r"the run at the head: 3 line\(s\) ran \(3 in shop/\), in \d+ seconds")
        self.assertNotIn("the lines were lost", result.stderr)  # named only when a line was lost

    def test_a_venv_in_the_working_copy_is_not_taken_for_its_code(self):
        # --python .venv/bin/python, with a package installed in that venv that the tests run: coverage.py skips
        # installed packages, but not under [run] include, which measured it and refused it as the working copy's.
        repo = self.project({".gitignore": "/.venv/\n", "tests/test_member.py": PY_VENDORED_TEST, "tests/test_plain.py": PY_PLAIN},
                            {"tests/test_member.py": None})
        venv = os.path.join(repo, ".venv")
        subprocess.run([self.python, "-m", "venv", "--without-pip", venv], check=True, capture_output=True)
        site = subprocess.run([os.path.join(venv, "bin", "python"), "-c", "import sysconfig; print(sysconfig.get_path('purelib'))"],
                              check=True, capture_output=True, text=True).stdout.strip()
        Path(site, "vendored.py").write_text("def double(value):\n    return value * 2\n")
        result = gate("coverage-not-lost", repo, "--base-ref", "HEAD~1", "--python", ".venv/bin/python", "--format", "json",
                      "--runner", "unittest", "--args", "discover -s tests")
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertEqual([(f["path"], f["line"]) for f in findings(result)], [("shop/price.py", 3)])

    def test_a_loss_names_the_commits_it_came_in_with(self):
        # The nightly run's shape: --base-ref is the last night's commit, and two pull requests were merged since.
        repo = make_repo(self, {"shop/__init__.py": "", "shop/price.py": PY_PRICE, "tests/test_member.py": PY_MEMBER_CASE,
                                "tests/test_plain.py": PY_PLAIN})
        night = subprocess.run(["git", "-C", repo, "rev-parse", "HEAD"], capture_output=True, text=True, check=True).stdout.strip()
        commit(repo, {"README.md": "a\n"}, "docs: a readme (#7)")
        git(repo, "rm", "-q", "tests/test_member.py")
        commit(repo, {}, "test: one test fewer (#8)")
        result = gate("coverage-not-lost", repo, "--base-ref", night, "--python", self.python, "--format", "json",
                      "--runner", "unittest", "--args", "discover -s tests")
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertEqual([(f["path"], f["line"]) for f in findings(result)], [("shop/price.py", 3)])
        self.assertRegex(result.stderr, r"the lines were lost in the 2 commit\(s\) after the merge base %s: [0-9a-f]{7,} "
                                        r"test: one test fewer \(#8\); [0-9a-f]{7,} docs: a readme \(#7\)\n" % night[:12])

    def test_the_interpreters_own_sitecustomize_still_runs(self):
        # The gate's sitecustomize comes first on the suite's PYTHONPATH; the project's, after it, runs too.
        site = tempfile.mkdtemp(prefix="vv-coverage-site-")
        self.addCleanup(shutil.rmtree, site, True)
        # It marks the process, not the environment: the gate's own process imports it too, and a child inherits that.
        Path(site, "sitecustomize.py").write_text("import sys\nsys.vv_project_sitecustomize = True\n")
        repo = self.project({"tests/test_member.py": PY_MEMBER_CASE, "tests/test_site.py": PY_SITE_TEST},
                            {"shop/price.py": PY_PRICE + "\n"})
        result = gate("coverage-not-lost", repo, "--base-ref", "HEAD~1", "--python", self.python, "--format", "json",
                      "--runner", "unittest", "--args", "discover -s tests", env={"PYTHONPATH": site})
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_no_test_at_either_side_cannot_run(self):
        repo = self.project({"tests/helpers.py": "X = 1\n"}, {"shop/price.py": PY_PRICE + "\n"})
        result = self.run_gate(repo, "--runner", "pytest")
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn("no test ran at the merge base or at the head", result.stderr)


class RepositoryPaths(unittest.TestCase):
    """gates/_coverage_runs.py names a measured file by its path in the repository, and refuses one read from the working copy."""

    @classmethod
    def setUpClass(cls):
        sys.path.insert(0, str(ROOT / "gates"))
        import _coverage_runs
        cls.module = _coverage_runs

    @classmethod
    def tearDownClass(cls):
        sys.path.remove(str(ROOT / "gates"))

    def test_a_file_is_named_by_its_path_in_the_checkout(self):
        scratch = tempfile.mkdtemp(prefix="vv-coverage-paths-")
        self.addCleanup(shutil.rmtree, scratch, True)
        repo, tree = os.path.join(scratch, "repo"), os.path.join(scratch, "tree")
        os.makedirs(os.path.join(tree, "web"))
        os.makedirs(repo)
        cwd = os.path.join(tree, "web")
        self.assertEqual(self.module.repository_path("src/a.ts", cwd, tree, repo), "web/src/a.ts")
        self.assertEqual(self.module.repository_path(os.path.join(tree, "lib", "b.py"), cwd, tree, repo), "lib/b.py")
        self.assertIsNone(self.module.repository_path("/elsewhere/c.py", cwd, tree, repo))
        with self.assertRaisesRegex(self.module.CannotRun, r"ran shop/price.py from the working copy instead \(an editable install"):
            self.module.repository_path(os.path.join(repo, "shop", "price.py"), cwd, tree, repo)


if __name__ == "__main__":
    unittest.main()
