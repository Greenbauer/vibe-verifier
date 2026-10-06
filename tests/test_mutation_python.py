"""The changed-code-mutation gate on Python files: the real pinned mutmut (a venv from
tools/changed-code-mutation-python) against a fixture project's own pytest (a venv from
tests/fixtures/changed-code-mutation-python)."""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

from helpers import ROOT, commit, gate, make_repo

FIXTURE = ROOT / "tests" / "fixtures" / "changed-code-mutation-python" / "requirements.txt"

PRICE_BEFORE = "def discount(total, is_member):\n    return total\n"
PRICE_AFTER = """def discount(total, is_member):
    if is_member and total > 100:
        return total * 0.9
    return total
"""
# Copies the logic instead of importing it: passes whatever shop/price.py does.
COPYING_TEST = """def discount(total, is_member):
    if is_member and total > 100:
        return total * 0.9
    return total


def test_members_get_ten_percent_off_over_100():
    assert discount(200, True) == 180
"""
IMPORTING_TEST = """from shop.price import discount


def test_members_get_ten_percent_off_over_100():
    assert discount(200, True) == 180


def test_non_members_pay_full_price():
    assert discount(200, False) == 200


def test_members_pay_full_price_at_exactly_100():
    assert discount(100, True) == 100


def test_members_get_ten_percent_off_at_101():
    assert discount(101, True) == 90.9
"""
UNTESTED = "def untouched(x):\n    return x + 1\n"


def findings(result):
    return json.loads(result.stdout)["findings"]


class RealMutmut(unittest.TestCase):
    """The pinned mutmut running the fixture's own pytest 9."""

    @classmethod
    def setUpClass(cls):
        cls.env = tempfile.mkdtemp(prefix="vv-mutation-py-env-")
        subprocess.run([sys.executable, "-m", "venv", cls.env], check=True, capture_output=True)
        cls.python = os.path.join(cls.env, "bin", "python")
        subprocess.run([cls.python, "-m", "pip", "install", "--quiet", "--disable-pip-version-check", "--require-hashes",
                        "--only-binary", ":all:", "-r", str(FIXTURE)], check=True, capture_output=True, text=True)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.env, ignore_errors=True)

    def project(self, base, head):
        repo = make_repo(self, dict({"shop/__init__.py": ""}, **base))
        commit(repo, head)
        return repo

    def run_gate(self, repo, *args):
        return gate("changed-code-mutation", repo, "--base-ref", "HEAD~1", "--python", self.python, "--format", "json", *args)

    def test_a_test_that_copies_the_logic_leaves_the_changed_lines_unguarded(self):
        repo = self.project({"shop/price.py": PRICE_BEFORE, "tests/test_price.py": COPYING_TEST}, {"shop/price.py": PRICE_AFTER})
        result = self.run_gate(repo)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        summary, *mutants = findings(result)
        self.assertIsNone(summary["path"])
        self.assertIn("mutation score on the changed lines is 0%", summary["message"])
        self.assertTrue(mutants)
        self.assertEqual({m["path"] for m in mutants}, {"shop/price.py"})
        self.assertTrue(all(m["line"] in (2, 3) for m in mutants), mutants)  # never line 4, which the PR did not change
        self.assertTrue(all("not covered: no test runs this code" in m["message"] for m in mutants), mutants)

    def test_tests_through_the_real_code_kill_every_mutant(self):
        repo = self.project({"shop/price.py": PRICE_BEFORE, "tests/test_price.py": IMPORTING_TEST}, {"shop/price.py": PRICE_AFTER})
        result = self.run_gate(repo)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertRegex(result.stderr, r"(\d+) of \1 mutant\(s\) on the changed lines killed: score 100%")

    def test_a_survivor_names_the_line_and_the_change(self):
        weak = IMPORTING_TEST.split("\n\n\ndef test_non_members")[0] + "\n"  # only the member-over-100 case
        repo = self.project({"shop/price.py": PRICE_BEFORE, "tests/test_price.py": weak}, {"shop/price.py": PRICE_AFTER})
        result = self.run_gate(repo, "--break", "100")
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        survivors = [m for m in findings(result)[1:] if "survived" in m["message"]]
        self.assertTrue(survivors)
        self.assertTrue(any(m["line"] == 2 and "`if is_member and total > 100:` ->" in m["message"] for m in survivors), survivors)

    def test_only_the_changed_lines_are_mutated(self):
        repo = self.project({"shop/price.py": UNTESTED + "\n\n" + PRICE_BEFORE, "tests/test_price.py": IMPORTING_TEST},
                            {"shop/price.py": UNTESTED + "\n\n" + PRICE_AFTER})
        result = self.run_gate(repo)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)  # `untouched` has no test, but no changed line
        self.assertNotIn("shop/price.py:2:", result.stderr)

    def test_a_method_is_placed_at_its_own_line(self):
        before = "class Till:\n    def ring(self, total):\n        return total\n"
        after = "class Till:\n    def ring(self, total):\n        if total > 50:\n            return total - 5\n        return total\n"
        test = "from shop.till import Till\n\n\ndef test_ring():\n    assert Till().ring(10) == 10\n"
        repo = self.project({"shop/till.py": before, "tests/test_till.py": test}, {"shop/till.py": after})
        result = self.run_gate(repo, "--break", "100")
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        lines = {m["line"] for m in findings(result)[1:]}
        self.assertTrue(lines and lines <= {3, 4}, lines)

    def test_decorated_functions_and_pragmas_are_left_out_and_said(self):
        after = ("import functools\n\n\n@functools.lru_cache\ndef rate(code):\n    return 3 if code == 'a' else 4\n\n\n"
                 "def fee(amount):\n    return amount * 2  # pragma: no mutate\n")
        repo = self.project({"shop/rates.py": "x = 1\n", "tests/test_rates.py": "def test_nothing():\n    pass\n"},
                            {"shop/rates.py": after})
        result = self.run_gate(repo)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("shop/rates.py:4: left out: mutmut does not mutate rate", result.stderr)
        self.assertIn("shop/rates.py:10: left out by its `# pragma: no mutate` comment", result.stderr)
        self.assertIn("no mutant on the changed lines to score", result.stderr)

    def test_the_projects_own_mutmut_table_is_replaced(self):
        hostile = '[project]\nname = "shop"\n\n[tool.mutmut]\nsource_paths = ["elsewhere/"]\nonly_mutate = ["nothing"]\n'
        repo = self.project({"pyproject.toml": hostile, "shop/price.py": PRICE_BEFORE, "tests/test_price.py": COPYING_TEST},
                            {"shop/price.py": PRICE_AFTER})
        result = self.run_gate(repo)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)

    def test_failing_tests_cannot_run(self):
        repo = self.project({"shop/price.py": PRICE_BEFORE, "tests/test_price.py": "def test_broken():\n    assert False\n"},
                            {"shop/price.py": PRICE_AFTER})
        result = self.run_gate(repo)
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn("the project's tests fail without any mutant (pytest exited 1", result.stderr)

    def test_without_pytest_it_cannot_run(self):
        repo = self.project({"shop/price.py": PRICE_BEFORE}, {"shop/price.py": PRICE_AFTER})
        bare = tempfile.mkdtemp(prefix="vv-bare-")
        self.addCleanup(shutil.rmtree, bare, True)
        subprocess.run([sys.executable, "-m", "venv", "--without-pip", bare], check=True)
        result = gate("changed-code-mutation", repo, "--base-ref", "HEAD~1", "--python", os.path.join(bare, "bin", "python"))
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn("no pytest is installed for", result.stderr)

    def test_a_python_only_change_never_needs_vitest(self):
        repo = self.project({"shop/price.py": PRICE_BEFORE, "tests/test_price.py": IMPORTING_TEST}, {"shop/price.py": PRICE_AFTER})
        self.assertNotIn("vitest", self.run_gate(repo).stderr)


class Helpers(unittest.TestCase):
    """gates/_mutation_python.py's pure parts: the config it writes and the functions it maps."""

    @classmethod
    def setUpClass(cls):
        sys.path.insert(0, str(ROOT / "gates"))
        import _mutation_python
        cls.module = _mutation_python

    @classmethod
    def tearDownClass(cls):
        sys.path.remove(str(ROOT / "gates"))

    def test_the_gates_table_replaces_the_projects_and_keeps_the_rest(self):
        workspace = tempfile.mkdtemp(prefix="vv-config-")
        self.addCleanup(shutil.rmtree, workspace, True)
        with open(os.path.join(workspace, "pyproject.toml"), "w") as handle:
            handle.write('[project]\nname = "shop"\n\n[tool.mutmut]\nonly_mutate = ["x"]\n\n[ tool.mutmut.extra ]\nk = 1\n\n'
                         '[tool.pytest.ini_options]\naddopts = "-q"\n')
        self.module.write_config(workspace, ["app/[id]/view.py", "lib/a.py"])
        with open(os.path.join(workspace, "pyproject.toml")) as handle:
            text = handle.read()
        self.assertEqual(text.count("[tool.mutmut]"), 1)
        self.assertNotIn("extra", text)
        self.assertIn('[tool.pytest.ini_options]\naddopts = "-q"', text)
        self.assertIn('[project]\nname = "shop"', text)
        self.assertIn('source_paths = ["app", "lib"]', text)
        self.assertIn('only_mutate = ["app/[[]id]/view.py", "lib/a.py"]', text)

    def test_functions_names_what_mutmut_mutates_and_lists_what_it_skips(self):
        source = ("def a():\n    pass\n\n\nclass K:\n    def m(self):\n        pass\n\n    @property\n    def p(self):\n"
                  "        return 1\n\n    class N:\n        def deep(self):\n            pass\n\n\n@cache\ndef d():\n    pass\n")
        mutated, skipped = self.module.functions(source)
        self.assertEqual(mutated, {"x_a": 1, "x\u01c1K\u01c1m": 6})
        self.assertEqual(skipped, [("K.p", 9, 11), ("K.N", 13, 15), ("d", 18, 20)])


if __name__ == "__main__":
    unittest.main()
