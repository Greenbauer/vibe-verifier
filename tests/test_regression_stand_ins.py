"""regression-test-fails-on-base on a Python test file that imports a name the fix adds beside names that already
exist, or a module the fix adds. On the code before the fix the file does not load, so before the gate stood the new
name or module in, none of its tests ran there and the gate read "fails only because `<name>` is missing" even when
the file's other tests fail there through the old code (PR #318: `from dashboard.bot_runs import job_named,
pulls_bot_jobs`; PR #337: helpers moved to a new `gates/_proof_table.py`, imported by a test file another one imports)."""
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

from helpers import make_repo
from test_regression_test import PY_BUG, PY_FIX, PYTEST, findings, fix, tree_is_head

PY_NEW = PY_FIX + "\n\ndef member_price(total):\n    return discount(total, True)\n"
HEADER = "import unittest\n\nfrom shop.price import discount, member_price\n\n\nclass Price(unittest.TestCase):\n"
OLD_CODE_TEST = "    def test_members_get_ten_percent_off_over_100(self):\n        self.assertEqual(discount(200, True), 180)\n"
NEW_NAME_TEST = "    def test_member_price_is_the_members_discount(self):\n        self.assertEqual(member_price(200), 180)\n"
UNCHANGED_TEST = "    def test_non_members_pay_full_price(self):\n        self.assertEqual(discount(200, False), 200)\n"


class StandIns(unittest.TestCase):
    """unittest in a venv without pytest, and the fixture's pytest in one with it."""

    @classmethod
    def setUpClass(cls):
        cls.envs = tempfile.mkdtemp(prefix="vv-regression-stand-in-")
        cls.pythons = {"unittest": os.path.join(cls.envs, "bare", "bin", "python"),
                       "pytest": os.path.join(cls.envs, "pytest", "bin", "python")}
        subprocess.run([sys.executable, "-m", "venv", "--without-pip", os.path.join(cls.envs, "bare")], check=True, capture_output=True)
        subprocess.run([sys.executable, "-m", "venv", os.path.join(cls.envs, "pytest")], check=True, capture_output=True)
        subprocess.run([cls.pythons["pytest"], "-m", "pip", "install", "--quiet", "--disable-pip-version-check", "--require-hashes",
                        "--only-binary", ":all:", "-r", str(PYTEST)], check=True, capture_output=True, text=True)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.envs, ignore_errors=True)

    def judge(self, runner, tests, base=None, changed=None, top="", old_name="discount", header=HEADER):
        repo = make_repo(self, base or {"shop/__init__.py": "", "shop/price.py": PY_BUG})
        header = top + header.replace("import discount,", "import %s," % old_name)
        result = fix(repo, dict(changed or {"shop/price.py": PY_NEW}, **{"tests/test_price.py": header + "\n".join(tests)}),
                     "fix: members", "", "--python", self.pythons[runner], "--format", "json")
        tree_is_head(self, repo)
        return result

    def only_missing(self, result, name="member_price"):
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        [message] = [finding["message"] for finding in findings(result) if finding.get("path") == "tests/test_price.py"]
        self.assertTrue(message.startswith("fails on the code before the fix only because `%s` is missing (ImportError: "
                                           "cannot import name '%s' from 'shop.price'" % (name, name)), message)

    def test_a_test_through_the_old_code_beside_an_import_the_fix_adds_shows_the_bug(self):
        for runner, test_id in (("unittest", "test_price.Price.test_members_get_ten_percent_off_over_100"),
                                ("pytest", "tests/test_price.py::Price::test_members_get_ten_percent_off_over_100")):
            with self.subTest(runner=runner):
                result = self.judge(runner, [OLD_CODE_TEST, NEW_NAME_TEST])
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertIn("tests/test_price.py: %s fails on the code before the fix: AssertionError: 200 != 180" % test_id,
                              result.stderr)
                self.assertIn("tests/test_price.py: on the code before the fix `member_price` is not in `shop.price` yet, so "
                              "the file ran again with a stand-in for it", result.stderr)

    def test_a_file_whose_only_failure_there_is_the_new_name_still_does_not_show_the_bug(self):
        for runner in ("unittest", "pytest"):
            with self.subTest(runner=runner):
                self.only_missing(self.judge(runner, [UNCHANGED_TEST, NEW_NAME_TEST]))

    def test_any_use_of_the_new_name_fails_as_it_missing_whatever_error_the_use_raises(self):
        # issubclass, assertRaises and isinstance raise TypeError on an object that is not a class; a TypeError is
        # not "missing", so before the stand-in recorded its use, each read as the bug shown.
        uses = ["    def test_issubclass(self):\n        self.assertTrue(issubclass(PriceError, ValueError))\n",
                "    def test_raises(self):\n        with self.assertRaises(PriceError):\n            raise PriceError('x')\n",
                "    def test_isinstance(self):\n        self.assertFalse(isinstance(ValueError(), PriceError))\n",
                # The same through a helper: the test's own code never names it.
                "    def rejected(self):\n        with self.assertRaises(PriceError):\n            raise PriceError('x')\n\n"
                "    def test_through_a_helper(self):\n        self.rejected()\n"]
        changed = {"shop/price.py": PY_FIX + "\n\nclass PriceError(ValueError):\n    pass\n"}
        header = HEADER.replace("discount, member_price", "discount, PriceError")
        for runner in ("unittest", "pytest"):
            with self.subTest(runner=runner):
                self.only_missing(self.judge(runner, [UNCHANGED_TEST] + uses, None, changed, header=header), "PriceError")

    def test_a_test_that_fails_through_the_old_code_before_it_reaches_the_new_name_shows_the_bug(self):
        # Each fails on the old discount first. The new names appear on or around the failing line only as a later
        # statement, a method, a dict key, a message, a comment, a keyword or a local: none is the stand-in read,
        # so each is the bug shown.
        changed = {"shop/price.py": PY_FIX + "\n\nclass PriceError(ValueError):\n    pass\n\n\ndef rounded(x):\n    return x\n"
                                             "\n\ndef member(total):\n    return total\n"}
        header = HEADER.replace("discount, member_price", "discount, PriceError, member, rounded")
        old = "self.assertEqual(discount(200, True), 180"
        cases = {
            "test_old_then_new": "        %s)\n        with self.assertRaises(PriceError):\n            raise PriceError('x')\n" % old,
            "test_a_method_named_like_the_new_name": "        self.assertEqual(self.rounded() + discount(200, True), 181)\n\n"
                                                     "    def rounded(self):\n        return 1\n",
            "test_a_dict_key": "        self.assertEqual({'rounded': discount(200, True)}['rounded'], 180)\n",
            "test_a_message": "        %s, 'rounded builds on it')\n" % old,
            "test_a_comment": "        %s)  # PriceError builds on it\n" % old,
            "test_a_keyword": "        self.assertEqual(discount(200, member=True), 180)\n",
            "test_a_local": "        PriceError = discount(200, True)\n        self.assertEqual(PriceError, 180)\n",
        }
        for runner in ("unittest", "pytest"):
            for name, body in cases.items():
                with self.subTest(runner=runner, test=name):
                    result = self.judge(runner, ["    def %s(self):\n%s" % (name, body)], None, changed, header=header)
                    self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                    self.assertIn("%s fails on the code before the fix: AssertionError" % name, result.stderr)

    def test_top_level_code_that_fails_through_the_old_code_shows_the_bug(self):
        # A module frame's locals are every name the file imported, the stand-in among them.
        for runner in ("unittest", "pytest"):
            with self.subTest(runner=runner):
                result = self.judge(runner, [UNCHANGED_TEST], None, None,
                                    header=HEADER.replace("\n\nclass Price", "\nassert discount(200, True) == 180\n\n\nclass Price"))
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertIn("tests/test_price.py: its top-level code fails on the code before the fix: AssertionError", result.stderr)

    def test_the_module_loads_when_the_test_file_imports_it_after_the_file_s_own_setup(self):
        # The file sets SHOP_RATE before it imports shop.price, whose old code reads it once. A stand-in that loaded
        # shop.price ahead of the file read 1.0 and failed the unchanged test there, a false "bug shown".
        base = {"shop/__init__.py": "", "shop/price.py": "import os\n\nRATE = float(os.environ.get('SHOP_RATE', '1'))\n\n\n"
                                                         "def scaled(x):\n    return x * RATE\n"}
        top = "import os\n\nos.environ['SHOP_RATE'] = '2'\n"
        tests = ["    def test_scaled(self):\n        self.assertEqual(scaled(3), 6)\n",
                 "    def test_member_price(self):\n        self.assertEqual(member_price(3), 3)\n"]
        changed = {"shop/price.py": base["shop/price.py"] + "\n\ndef member_price(total):\n    return total\n"}
        for runner in ("unittest", "pytest"):
            with self.subTest(runner=runner):
                self.only_missing(self.judge(runner, tests, base, changed, top, "scaled"))

    def test_a_module_found_through_pytest_s_pythonpath_gets_its_stand_in(self):
        base = {"pytest.ini": "[pytest]\npythonpath = src\n", "src/shop/__init__.py": "", "src/shop/price.py": PY_BUG}
        result = self.judge("pytest", [OLD_CODE_TEST, NEW_NAME_TEST], base, {"src/shop/price.py": PY_NEW})
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("on the code before the fix `member_price` is not in `shop.price` yet, so the file ran again with a "
                      "stand-in for it", result.stderr)


    def test_a_test_through_the_old_code_beside_a_module_the_fix_adds_shows_the_bug(self):
        # PR #337's shape: tests/test_cents.py imports the module the fix adds (a helper moved to a file of its own) and
        # tests/test_price.py, which tests the old code, reads a constant from tests/test_cents.py as it loads.
        changed = {"shop/price.py": PY_FIX, "shop/rounding.py": "def cents(x):\n    return round(x, 2)\n",
                   "tests/test_cents.py": "import unittest\n\nimport shop.rounding\n\nPLACES = 2\n\n\nclass Cents(unittest.TestCase):\n"
                                          "    def test_cents(self):\n        self.assertEqual(shop.rounding.cents(1.234), 1.23)\n"}
        header = HEADER.replace("from shop.price import discount, member_price\n",
                                "import test_cents\nfrom shop.price import discount\n\nPLACES = test_cents.PLACES\n")
        for runner, test_id in (("unittest", "test_price.Price.test_members_get_ten_percent_off_over_100"),
                                ("pytest", "tests/test_price.py::Price::test_members_get_ten_percent_off_over_100")):
            with self.subTest(runner=runner):
                result = self.judge(runner, [OLD_CODE_TEST], None, changed, header=header)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertIn("tests/test_price.py: %s fails on the code before the fix: AssertionError: 200 != 180" % test_id,
                              result.stderr)
                for path in ("tests/test_cents.py", "tests/test_price.py"):
                    self.assertIn("%s: on the code before the fix `shop.rounding` does not exist yet, so the file ran again "
                                  "with a stand-in for it" % path, result.stderr)

    def test_a_file_whose_only_failure_there_is_a_module_the_fix_adds_still_does_not_show_the_bug(self):
        # Every use of the stand-in module, a name imported from it or one read off it, fails as the module missing.
        changed = {"shop/price.py": PY_FIX, "shop/rounding.py": "def cents(x):\n    return round(x, 2)\n\n\nPLACES = 2\n"}
        header = HEADER.replace("from shop.price import discount, member_price\n",
                                "import shop.rounding\nfrom shop.price import discount\nfrom shop.rounding import cents\n")
        uses = ["    def test_cents(self):\n        self.assertEqual(cents(1.234), 1.23)\n",
                "    def test_places(self):\n        self.assertIs(shop.rounding.PLACES, 2)\n"]
        for runner in ("unittest", "pytest"):
            with self.subTest(runner=runner):
                result = self.judge(runner, [UNCHANGED_TEST] + uses, None, changed, header=header)
                self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
                [message] = [finding["message"] for finding in findings(result) if finding.get("path") == "tests/test_price.py"]
                self.assertTrue(message.startswith("fails on the code before the fix only because `shop.rounding` is missing "
                                                   "(ModuleNotFoundError: No module named 'shop.rounding', in "), message)


if __name__ == "__main__":
    unittest.main()
