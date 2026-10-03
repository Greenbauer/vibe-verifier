"""The research scripts: SZZ outcome linking through its command line, and the mutation operators."""
import ast
import json
import os
import subprocess
import sys
import tempfile
import unittest

from helpers import ROOT, clean_env, commit, make_repo, write

sys.path.insert(0, str(ROOT / "research"))
import mutation_score  # noqa: E402


def head(repo):
    return subprocess.run(["git", "-C", repo, "rev-parse", "HEAD"], capture_output=True, text=True, check=True).stdout.strip()


def pr(number, title, sha, day, files):
    stamp = "2026-09-%02dT12:00:00Z" % day
    return {"number": number, "title": title, "headRefName": "b%d" % number, "author": {"login": "bot"},
            "createdAt": stamp, "mergedAt": stamp, "mergeCommit": {"oid": sha},
            "prCommits": {"nodes": [{"commit": {"oid": sha}}]}, "files": {"nodes": [{"path": f} for f in files]},
            "additions": 1, "deletions": 1, "changedFiles": len(files), "commits": {"totalCount": 1},
            "reviewThreads": {"totalCount": 0}}


class PrOutcomes(unittest.TestCase):
    def setUp(self):
        self.repo = make_repo(self, {"app.js": "a\nb\nc\n"})
        commit(self.repo, {"app.js": "a\nb-with-a-bug\nc\n", "feature.js": "x\n"}, "feat: the feature")
        feature = head(self.repo)
        commit(self.repo, {"other.js": "y\n"}, "feat: unrelated")
        unrelated = head(self.repo)
        commit(self.repo, {"app.js": "a\nb\nc\n"}, "fix: the bug")
        fix = head(self.repo)
        commit(self.repo, {"other.js": "y\nz\n"}, "fix: add a missing line")
        addition = head(self.repo)
        self.prs = [pr(1, "feat: the feature", feature, 1, ["app.js", "feature.js"]),
                    pr(2, "feat: unrelated", unrelated, 2, ["other.js"]),
                    pr(3, "fix: the bug", fix, 4, ["app.js"]),
                    pr(4, "fix: add a missing line", addition, 5, ["other.js"])]
        self.inputs = tempfile.mkdtemp(prefix="vv-prs-")
        write(self.inputs, {"prs.jsonl": "".join(json.dumps(p) + "\n" for p in self.prs)})

    def outcomes(self, *args):
        done = subprocess.run([sys.executable, str(ROOT / "research" / "pr_outcomes.py"), "--repo", self.repo,
                               "--prs", os.path.join(self.inputs, "prs.jsonl"), "--format", "json", *args],
                              capture_output=True, text=True, env=clean_env())
        self.assertEqual(done.returncode, 0, done.stderr)
        return {row["number"]: row for row in json.loads(done.stdout)["prs"]}

    def test_a_fix_links_to_the_pr_whose_line_it_changed(self):
        rows = self.outcomes("--window-days", "14")
        self.assertEqual(rows[1]["fixed_by"], [3])
        self.assertEqual(rows[2]["fixed_by"], [])

    def test_a_fix_outside_the_window_links_nothing(self):
        self.assertEqual(self.outcomes("--window-days", "2")[1]["fixed_by"], [])

    def test_a_fix_that_only_adds_lines_is_unattributed(self):
        rows = self.outcomes()
        self.assertEqual(rows[4]["unattributed_fix_hunks"], 1)
        self.assertEqual(rows[2]["fixed_by"], [])


class MutationOperators(unittest.TestCase):
    def mutants(self, source):
        tree = ast.parse(source)
        return {description: ast.unparse(mutation_score.Mutate(index).apply(ast.parse(source)))
                for index, _, description in mutation_score.mutation_sites(tree)}

    def test_each_operator_changes_one_thing(self):
        found = self.mutants("def f(a, b):\n    if a == b and not a:\n        return a + 1\n")
        self.assertEqual(found, {
            "And -> Or": "def f(a, b):\n    if a == b or not a:\n        return a + 1",
            "Eq -> NotEq": "def f(a, b):\n    if a != b and (not a):\n        return a + 1",
            "drop not": "def f(a, b):\n    if a == b and a:\n        return a + 1",
            "return None": "def f(a, b):\n    if a == b and (not a):\n        return None",
            "Add -> Sub": "def f(a, b):\n    if a == b and (not a):\n        return a - 1",
            "1 -> 2": "def f(a, b):\n    if a == b and (not a):\n        return a + 2",
        })

    def test_returning_none_is_not_a_mutant(self):
        self.assertEqual(self.mutants("def f():\n    return None\n"), {})


if __name__ == "__main__":
    unittest.main()
