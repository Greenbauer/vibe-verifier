"""The bandit ratchet, on the real pinned Bandit (a venv from tools/bandit/requirements.txt)."""
import json
import unittest

from helpers import commit, gate, git, make_repo

SHELL = "import subprocess\n\n\ndef run(cmd):\n    return subprocess.call(cmd, shell=True)\n"
SECOND = "\n\ndef run_again(cmd):\n    return subprocess.call(cmd, shell=True)\n"
SAFE = "def add(a, b):\n    return a + b\n"


def branch_with(test, base_files, head_files):
    repo = make_repo(test, base_files)
    commit(repo, head_files, "change")
    return repo


def findings(result):
    return json.loads(result.stdout)["findings"]


class Bandit(unittest.TestCase):
    def run_gate(self, repo, *args):
        return gate("bandit", repo, "--base-ref", "HEAD~1", "--format", "json", *args)

    def test_a_new_file_with_a_result_fails_at_its_line(self):
        result = self.run_gate(branch_with(self, {"README.md": "x\n"}, {"pkg/a.py": SHELL}))
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        [finding] = findings(result)
        self.assertEqual((finding["path"], finding["line"]), ("pkg/a.py", 5))
        self.assertIn("B602 subprocess call with shell=True", finding["message"])
        self.assertIn("B602 results in this file: 0 at the base, 1 now", finding["message"])

    def test_a_result_already_at_the_base_does_not_block_a_touch_but_another_one_does(self):
        repo = branch_with(self, {"pkg/a.py": SHELL}, {"pkg/a.py": SHELL + "\n\n" + SAFE})
        self.assertEqual(self.run_gate(repo).returncode, 0)
        commit(repo, {"pkg/a.py": SHELL + SECOND}, "second")
        result = self.run_gate(repo)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertEqual([f["line"] for f in findings(result)], [5, 9])
        self.assertIn("1 at the base, 2 now", findings(result)[0]["message"])

    def test_a_line_marked_nosec_is_not_a_result(self):
        marked = SHELL.replace("shell=True)", "shell=True)  # nosec B602")
        self.assertEqual(self.run_gate(branch_with(self, {"README.md": "x\n"}, {"pkg/a.py": marked})).returncode, 0)

    def test_severity_sets_what_counts(self):
        repo = branch_with(self, {"README.md": "x\n"}, {"pkg/a.py": "import subprocess  # B404 is low severity\n"})
        self.assertEqual(self.run_gate(repo).returncode, 0)
        result = self.run_gate(repo, "--severity", "low")
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("B404", findings(result)[0]["message"])

    def test_tests_are_not_read(self):
        repo = branch_with(self, {"README.md": "x\n"}, {"tests/test_a.py": SHELL, "pkg/a_test.py": SHELL, "conftest.py": SHELL})
        self.assertEqual(self.run_gate(repo).returncode, 0)

    def test_moving_a_file_with_a_result_is_not_a_finding(self):
        padding = "".join("C%d = %d\n" % (i, i) for i in range(20))
        repo = make_repo(self, {"pkg/old.py": SHELL + padding})
        git(repo, "mv", "pkg/old.py", "pkg/new.py")
        git(repo, "commit", "-q", "-m", "move")
        self.assertEqual(self.run_gate(repo).returncode, 0)

    def test_all_reports_every_result(self):
        repo = branch_with(self, {"pkg/a.py": SHELL}, {"README.md": "y\n"})
        self.assertEqual(self.run_gate(repo).returncode, 0)
        result = gate("bandit", repo, "--all", "--format", "json")
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertEqual([(f["path"], f["line"]) for f in findings(result)], [("pkg/a.py", 5)])

    def test_a_file_bandit_cannot_parse_cannot_run(self):
        result = self.run_gate(branch_with(self, {"README.md": "x\n"}, {"pkg/a.py": "def broken(:\n"}))
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn("bandit could not read pkg/a.py", result.stderr)


if __name__ == "__main__":
    unittest.main()
