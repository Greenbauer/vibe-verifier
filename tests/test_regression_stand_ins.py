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

    def judge(self, runner, tests):
        repo = make_repo(self, {"shop/__init__.py": "", "shop/price.py": PY_BUG})
        result = fix(repo, {"shop/price.py": PY_NEW, "tests/test_price.py": HEADER + "\n".join(tests)}, "fix: members", "",
                     "--python", self.pythons[runner], "--format", "json")
        tree_is_head(self, repo)
        return result

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
                result = self.judge(runner, [UNCHANGED_TEST, NEW_NAME_TEST])
                self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
                [message] = [finding["message"] for finding in findings(result) if finding.get("path") == "tests/test_price.py"]
                self.assertTrue(message.startswith("fails on the code before the fix only because `member_price` is missing "
                                                   "(ImportError: cannot import name 'member_price' from 'shop.price'"), message)


if __name__ == "__main__":
    unittest.main()
