"""coverage-not-lost and changed-code-mutation under gates/_local_guard.py, both ways, for each runner each starts:
coverage-not-lost's Vitest (with its coverage provider) and unittest under coverage.py, and changed-code-mutation's
StrykerJS (the fixture's Vitest 4.1.11) and mutmut (a venv with the fixture's pytest). The fixtures are
tests/test_local_guard_gates.py's: each test reaches a server of its own on this machine, and the guarded ones also try
an outside address and start a child that tries another, and expect both refused."""
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from helpers import ROOT, commit, gate, git, install_fixture, make_repo
from test_local_guard_gates import (FIXTURE, IN_DOTENV, IN_ENV, JS_GUARDED, JS_OUTSIDE, JS_PRICE_FIX, OUTSIDE, PY_GUARDED,
                                    PY_OUTSIDE, PY_PRICE_FIX, UNITTEST, VITEST, marker)

PYTEST = ROOT / "tests" / "fixtures" / "changed-code-mutation-python" / "requirements.txt"
JS_PRICE_BEFORE = "export function discount(total, member) {\n  return total;\n}\n"
PY_PRICE_BEFORE = "def discount(total, member):\n    return total\n"
# More checks for the mutation runs, so each mutant on the changed lines is killed.
JS_MORE = """it("charges non-members and exactly 100 full price", () => {
  expect(discount(200, false)).toBe(200);
  expect(discount(100, true)).toBe(100);
  expect(discount(101, true)).toBeCloseTo(90.9);
});
"""
PYTEST_TEST = """from shop.guarded import child, here, outside
from shop.price import discount


def test_members_get_ten_percent_off_over_100():
    assert here() == "ok"
OUTSIDE    assert discount(200, True) == 180


def test_non_members_and_exactly_100_pay_full_price():
    assert discount(200, False) == 200
    assert discount(100, True) == 100
    assert discount(101, True) == 90.9
"""
PYTEST_OUTSIDE = '    assert outside() == "refused"\n    assert child() == "refused"\n'


def setUpModule():
    global DEPS, VENV
    DEPS = tempfile.mkdtemp(prefix="vv-guard-deps-")
    for name in ("package.json", "package-lock.json"):
        shutil.copy(FIXTURE / name, DEPS)
    install_fixture(DEPS)
    VENV = tempfile.mkdtemp(prefix="vv-guard-venv-")
    subprocess.run([sys.executable, "-m", "venv", VENV], check=True, capture_output=True)
    subprocess.run([os.path.join(VENV, "bin", "python"), "-m", "pip", "install", "--quiet", "--disable-pip-version-check",
                    "--require-hashes", "--only-binary", ":all:", "-r", str(PYTEST)], check=True, capture_output=True)


def tearDownModule():
    shutil.rmtree(DEPS, ignore_errors=True)
    shutil.rmtree(VENV, ignore_errors=True)


def project(test, kind, outside, mutation):
    """A repository whose head changes the price (mutation) or only a README (coverage), with one runner's guarded
    test; `outside` is "" (only this machine), "tolerated" (expects both refused) or "needed" (expects to reach out)."""
    js, before = kind in ("vitest", "stryker"), (JS_PRICE_BEFORE if kind == "stryker" else PY_PRICE_BEFORE) if mutation else None
    calls = {"": "", "tolerated": JS_OUTSIDE if js else (PYTEST_OUTSIDE if kind == "mutmut" else PY_OUTSIDE),
             "needed": '  expect(await outside()).toBe("reached");\n' if js else
             ('    assert outside() == "reached"\n' if kind == "mutmut" else '        self.assertEqual(outside(), "reached")\n')}[outside]
    if js:
        files = {".gitignore": "node_modules\n.env.local\n", "package.json": '{"type": "module"}\n', "src/guarded.mjs": JS_GUARDED,
                 "src/price.mjs": before or JS_PRICE_FIX, "src/price.test.mjs": VITEST.replace("OUTSIDE", calls) + JS_MORE}
    else:
        files = {".gitignore": ".env.local\n", "shop/__init__.py": "", "shop/guarded.py": PY_GUARDED, "shop/price.py": before or PY_PRICE_FIX,
                 "tests/test_price.py": (PYTEST_TEST if kind == "mutmut" else UNITTEST).replace("OUTSIDE", calls)}
    repo = make_repo(test, dict(files, **{"README.md": "a\n"}))
    if js:
        Path(repo, "node_modules").symlink_to(Path(DEPS, "node_modules"))
    commit(repo, {"src/price.mjs": JS_PRICE_FIX} if kind == "stryker" else {"shop/price.py": PY_PRICE_FIX} if mutation else {"README.md": "b\n"})
    return repo


def run(kind, repo, *args, env=None):
    if kind in ("vitest", "unittest"):
        runner = ["--runner", "unittest", "--args", "discover -s tests", "--python", sys.executable] if kind == "unittest" else []
        return gate("coverage-not-lost", repo, "--base-ref", "HEAD~1", *runner, *args, env=env)
    python = ["--python", os.path.join(VENV, "bin", "python")] if kind == "mutmut" else []
    return gate("changed-code-mutation", repo, "--base-ref", "HEAD~1", *python, *args, env=env)


class GuardedRuns(unittest.TestCase):
    KINDS = (("vitest", False), ("unittest", False), ("stryker", True), ("mutmut", True))

    def test_a_run_that_reaches_only_this_machine_is_judged_as_before(self):
        for kind, mutation in self.KINDS:
            with self.subTest(runner=kind):
                ran = marker(self)
                result = run(kind, project(self, kind, "", mutation), "--local-env", "SESSION_SECRET",
                             env={"SESSION_SECRET": "local-only", "VV_RAN": ran})
                self.assertTrue(Path(ran).exists())  # the marker works: a test process loaded the module
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertIn("mutant(s) on the changed lines killed: score" if mutation else "still runs at the head", result.stderr)
                self.assertNotIn("refused", result.stderr)

    def test_a_coverage_run_that_passed_with_its_connections_refused_says_so(self):
        for kind in ("vitest", "unittest"):
            with self.subTest(runner=kind):
                result = run(kind, project(self, kind, "tolerated", False))
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                for side in ("base", "head"):
                    self.assertIn("coverage-not-lost: the %s run in the %s checkout tried to reach %s, which the gate refused; "
                                  "its tests passed all the same" % (kind, side, OUTSIDE), result.stderr)

    def test_a_failure_in_a_run_that_reached_out_cannot_be_judged(self):
        # coverage-not-lost: a suite that fails after a refusal. changed-code-mutation: mutants killed in a run that
        # refused connections, by tests that expected the refusals.
        for kind, mutation, outside, said in (
                ("vitest", False, "needed", r"price.test.mjs > gives members 10% off over 100: .* failed in the vitest run in the base checkout, which tried to reach 192.0.2.1:80"),
                ("unittest", False, "needed", r"the suite \(unittest exited 1\) failed in the unittest run in the base checkout, which tried to reach 192.0.2.1:80"),
                ("stryker", True, "tolerated", r"the tests that killed \d+ mutant\(s\) failed in the StrykerJS run, which tried to reach " + OUTSIDE),
                ("mutmut", True, "tolerated", r"the tests that killed \d+ mutant\(s\) failed in the mutmut run, which tried to reach " + OUTSIDE)):
            with self.subTest(runner=kind):
                result = run(kind, project(self, kind, outside, mutation), "--soak")
                self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
                self.assertRegex(result.stderr, said + r"(, [^:]+)?: the gate refused it, because a gate's test runs reach only "
                                                       r"this machine, so the failure cannot be told from the refusal")

    def test_a_dotenv_file_only_the_merge_base_has_is_checked_in_the_checkout_that_loads_it(self):
        # The pull request deletes a committed .env file: the working copy has none, but the run at the merge base
        # runs in a checkout that does.
        repo = make_repo(self, {".env.production": "DATABASE_URL=postgresql://app:pw-at-base@db.example.com:5432/app\n",
                                "shop/__init__.py": "", "shop/guarded.py": PY_GUARDED, "shop/price.py": PY_PRICE_FIX,
                                "tests/test_price.py": UNITTEST.replace("OUTSIDE", "")})
        git(repo, "rm", "-q", ".env.production")
        commit(repo, {})
        result = run("unittest", repo, "--soak")
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn("/base/.env.production: DATABASE_URL holds a connection string for db.example.com", result.stderr)
        self.assertNotIn("pw-at-base", result.stdout + result.stderr)

    def test_a_credential_for_anywhere_else_stops_each_runner_before_it_starts(self):
        for kind, mutation in self.KINDS:
            for where in ("the environment", "a .env file"):
                with self.subTest(runner=kind, credential=where):
                    repo = project(self, kind, "", mutation)
                    if where == "a .env file":
                        Path(repo, IN_DOTENV[0]).parent.mkdir(parents=True)
                        Path(repo, IN_DOTENV[0]).write_text(IN_DOTENV[1])
                    ran = marker(self)
                    result = run(kind, repo, "--soak", env=dict(IN_ENV if where == "the environment" else {}, VV_RAN=ran))
                    self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
                    self.assertFalse(Path(ran).exists())  # no test process started
                    self.assertIn("would run the pull request's code with a credential for somewhere other than this machine "
                                  "in what it inherits, so this gate does not start it", result.stderr)
                    self.assertIn("the environment: RESEND_API_KEY is named as a credential" if where == "the environment" else
                                  "apps/web/.env.local: DATABASE_URL holds a connection string for db.example.com", result.stderr)
                    for value in ("re_value_in_env", "pw-in-dotenv"):
                        self.assertNotIn(value, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
