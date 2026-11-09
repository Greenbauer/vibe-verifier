"""regression-test-fails-on-base on a Python test file that imports a name the fix adds beside names that already
exist. On the code before the fix the file does not load, so before the gate stood the new name in, none of its
tests ran there and the gate read "fails only because `<name>` is missing" even when the file's other tests fail
there through the old code (PR #318: `from dashboard.bot_runs import job_named, pulls_bot_jobs`)."""
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
        # It names PriceError and the module a new function sharing a method's name, but fails on the old discount
        # first: the failure's traceback holds no stand-in, so it is the bug shown.
        changed = {"shop/price.py": PY_FIX + "\n\nclass PriceError(ValueError):\n    pass\n\n\ndef rounded(x):\n    return x\n"}
        header = HEADER.replace("discount, member_price", "discount, PriceError, rounded")
        test = ("    def test_old_then_new(self):\n        self.assertEqual(self.discount(200, True), 180)\n"
                "        with self.assertRaises(PriceError):\n            raise PriceError('x')\n\n"
                "    def discount(self, total, member):\n        return discount(total, member)\n\n"
                "    def rounded(self):\n        return 1\n\n"
                "    def test_a_method_named_like_the_new_name(self):\n        self.assertEqual(self.rounded() + discount(200, True), 181)\n")
        for runner in ("unittest", "pytest"):
            with self.subTest(runner=runner):
                result = self.judge(runner, [test], None, changed, header=header)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertIn("test_old_then_new fails on the code before the fix: AssertionError: 200 != 180", result.stderr)
                self.assertIn("test_a_method_named_like_the_new_name fails on the code before the fix: AssertionError: 201 != 181",
                              result.stderr)

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


if __name__ == "__main__":
    unittest.main()
