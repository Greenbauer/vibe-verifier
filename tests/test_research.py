"""The research scripts: SZZ outcome linking through its command line, and the mutation operators."""
import ast
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from helpers import ROOT, clean_env, commit, make_repo, write

sys.path.insert(0, str(ROOT / "research"))
import mutation_score  # noqa: E402
import pr_outcomes  # noqa: E402
import gate_backtest  # noqa: E402
import test_oracles  # noqa: E402
import fetch_prs  # noqa: E402


def head(repo):
    return subprocess.run(["git", "-C", repo, "rev-parse", "HEAD"], capture_output=True, text=True, check=True).stdout.strip()


def pr(number, title, sha, day, files):
    stamp = "2026-09-%02dT12:00:00Z" % day
    return {"number": number, "title": title, "headRefName": "b%d" % number, "author": {"login": "bot"},
            "createdAt": stamp, "mergedAt": stamp, "mergeCommit": {"oid": sha},
            "prCommits": {"nodes": [{"commit": {"oid": sha}}]}, "files": {"nodes": [{"path": f} for f in files]},
            "additions": 1, "deletions": 1, "changedFiles": len(files), "commits": {"totalCount": 1},
            "reviewThreads": {"totalCount": 0}}


class FetchMetadata(unittest.TestCase):
    def test_nested_commit_and_file_connections_are_completed(self):
        record = pr(1, "feat", "a", 1, ["one.py"])
        record["changedFiles"] = record["commits"]["totalCount"] = 2
        files = [{"path": "one.py"}, {"path": "two.py"}]
        commits = [{"commit": {"oid": "a"}}, {"commit": {"oid": "b"}}]
        with patch.object(fetch_prs, "query", side_effect=[files, commits]) as fetch:
            completed = fetch_prs.complete_node("owner", "repo", record)
        self.assertEqual(completed["files"]["nodes"], files)
        self.assertEqual(completed["prCommits"]["nodes"], commits)
        self.assertEqual(fetch.call_count, 2)
        self.assertIn("after:$endCursor", fetch.call_args.args[2])

    def test_complete_metadata_needs_no_extra_requests(self):
        with patch.object(fetch_prs, "query") as fetch:
            fetch_prs.complete_node("owner", "repo", pr(1, "feat", "a", 1, ["one.py"]))
        fetch.assert_not_called()

    def test_incomplete_nested_response_refuses_a_partial_snapshot(self):
        record = pr(1, "feat", "a", 1, ["one.py"])
        record["commits"]["totalCount"] = 2
        with patch.object(fetch_prs, "query", return_value=[]):
            with self.assertRaisesRegex(ValueError, "incomplete"):
                fetch_prs.complete_node("owner", "repo", record)


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
        self.addCleanup(shutil.rmtree, self.inputs, True)
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

    def test_new_file_in_a_fix_is_counted_as_unattributed(self):
        commit(self.repo, {"missing.js": "export const missing = 1\n"}, "fix: missing module")
        hunks, count = pr_outcomes.changed_old_lines(self.repo, "HEAD^", "HEAD")
        self.assertEqual(hunks, [])
        self.assertEqual(count, 1)

    def test_removed_sql_comments_do_not_replace_the_diff_path(self):
        commit(self.repo, {"query.sql": "-- old comment\nSELECT 1;\n"}, "query")
        commit(self.repo, {"query.sql": "-- new comment\nSELECT 2;\n"}, "fix: query")
        hunks, _ = pr_outcomes.changed_old_lines(self.repo, "HEAD^", "HEAD")
        self.assertEqual(hunks, [("query.sql", 1, 2)])
        self.assertTrue(pr_outcomes.blamed_commits(self.repo, "HEAD^", hunks))

    def test_observation_cutoff_excludes_future_fixes(self):
        rows = self.outcomes("--observed-at", "2026-09-03T00:00:00Z")
        self.assertEqual(set(rows), {1, 2})
        self.assertEqual(rows[1]["fixed_by"], [])

    def test_incomplete_followup_is_not_counted_as_clean(self):
        rows = list(self.outcomes("--observed-at", "2026-09-16T00:00:00Z").values())
        report = pr_outcomes.summary(rows)
        self.assertEqual(report["eligible_changes"], 1)
        self.assertEqual(report["right_censored_changes"], 3)
        self.assertEqual(report["later_fix_touch_rate"], 1.0)
        self.assertEqual(report["median_commits"], 1)

    def test_no_mature_changes_has_no_touch_rate(self):
        rows = list(self.outcomes("--observed-at", "2026-09-06T00:00:00Z").values())
        self.assertIsNone(pr_outcomes.summary(rows)["later_fix_touch_rate"])

    def test_partial_file_list_is_not_proof_of_docs_only(self):
        record = pr(5, "docs: also change code", "unused", 5, ["README.md"])
        record["changedFiles"] = 101
        self.assertEqual(pr_outcomes.kind_of(record), "change")

    def test_truncated_commit_list_refuses_a_biased_score(self):
        self.prs[0]["commits"]["totalCount"] = 101
        with self.assertRaisesRegex(ValueError, "truncated commit metadata"):
            pr_outcomes.outcomes(self.repo, self.prs, 14)

    def test_other_target_branches_do_not_own_mainline_changes(self):
        for record in self.prs:
            record["baseRefName"] = "main"
        self.prs[0]["baseRefName"] = "feature"
        rows = pr_outcomes.outcomes(self.repo, self.prs, 14, base_branch="main")
        self.assertNotIn(1, [r["number"] for r in rows])
        self.assertEqual(next(r for r in rows if r["number"] == 3)["fixed_by"], [])


JS_TESTS = """import { expect, it } from 'vitest'
it('formats a total', () => {
  expect(total([1, 2])).toBe(3)
})
it('returns something', () => {
  expect(total([])).toBeDefined()
})
it('runs', () => {
  total([1])
})
"""
PY_TESTS = """import unittest


class Totals(unittest.TestCase):
    def test_sum(self):
        self.assertEqual(total([1, 2]), 3)

    def test_present(self):
        self.assertIsNotNone(total([]))

    def test_runs(self):
        total([1])
"""


def run_script(name, *args):
    done = subprocess.run([sys.executable, str(ROOT / "research" / name), *args],
                          capture_output=True, text=True, env=clean_env())
    return done


def outcomes_file(test, prs):
    directory = tempfile.mkdtemp(prefix="vv-out-")
    test.addCleanup(shutil.rmtree, directory, True)
    write(directory, {"outcomes.json": json.dumps({"prs": prs})})
    return os.path.join(directory, "outcomes.json")


class TestOracles(unittest.TestCase):
    def test_added_header_like_text_does_not_change_test_path(self):
        repo = make_repo(self, {"tests/test_value.py": "def test_value():\n    assert 1\n"})
        commit(repo, {"tests/test_value.py": 'def test_value():\n    """\n++ b/fake.py\n    """\n    assert 2\n'})
        lines = test_oracles.changed_lines(repo, "HEAD^", "HEAD")
        self.assertEqual(set(lines), {"tests/test_value.py"})

    def test_each_touched_case_is_graded_strong_weak_or_none(self):
        repo = make_repo(self, {"src/total.ts": "export const total = (xs) => xs.length\n",
                                "tests/test_old.py": "def test_untouched():\n    pass\n"})
        commit(repo, {"tests/total.test.ts": JS_TESTS, "tests/test_total.py": PY_TESTS}, "add tests")
        sha = head(repo)
        path = outcomes_file(self, [{"number": 7, "kind": "change", "merge_commit": sha, "fixed_by": []}])
        done = run_script("test_oracles.py", "--repo", repo, "--outcomes", path, "--format", "json")
        self.assertEqual(done.returncode, 0, done.stderr)
        graded = {c["name"]: c["strength"] for c in json.loads(done.stdout)["prs"][0]["cases"]}
        self.assertNotIn("later_fixed_by_group", json.loads(done.stdout)["summary"])
        self.assertEqual(graded, {"formats a total": "strong", "returns something": "weak", "runs": "none",
                                  "test_sum": "strong", "test_present": "weak", "test_runs": "none"})


class SizeBacktest(unittest.TestCase):
    def test_a_threshold_reports_what_it_flags_and_catches(self):
        def sized(number, lines, fixed):
            return {"number": number, "kind": "change", "additions": lines, "deletions": 0, "files": 1,
                    "fixed_by": [99] if fixed else [], "has_full_window": True}
        path = outcomes_file(self, [sized(1, 50, True), sized(2, 150, True), sized(3, 300, False)])
        done = run_script("size_backtest.py", "--outcomes", path)
        self.assertEqual(done.returncode, 0, done.stderr)
        line = next(l for l in done.stdout.splitlines() if l.startswith("lines > 100"))
        self.assertEqual(line.split()[3:], ["flagged=2", "linked", "among", "flagged=1", "(0.50)",
                                            "selected", "1", "of", "2", "linked", "sources"])

    def test_size_comparison_excludes_immature_prs(self):
        path = outcomes_file(self, [{"number": 1, "kind": "change", "additions": 999,
                                     "deletions": 0, "files": 4, "fixed_by": [], "has_full_window": False}])
        done = run_script("size_backtest.py", "--outcomes", path)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn("no PRs", done.stdout)
        self.assertNotIn("flagged=1", done.stdout)


class GateBacktest(unittest.TestCase):
    def test_real_gate_retains_violation_evidence(self):
        repo = make_repo(self, {"package.json": '{"name":"a"}\n'})
        commit(repo, {"package.json": '{"name":"a","name":"b"}\n'})
        result = gate_backtest.run_gate("no-duplicate-package-json-keys", [], repo, "HEAD^")
        self.assertEqual(result["status"], "violations")
        self.assertEqual(result["returncode"], 1)
        self.assertIn("duplicate", result["stdout"].lower())

    def test_missing_gate_is_not_a_pass_and_keeps_diagnostic(self):
        result = gate_backtest.run_gate("no-such-gate", [], ".", "HEAD^")
        self.assertEqual(result["status"], "cannot-run")
        self.assertIn("no_such_gate.py", result["stderr"])

    def test_timeout_is_unmeasured_instead_of_losing_the_run(self):
        with patch.object(gate_backtest.subprocess, "run", side_effect=subprocess.TimeoutExpired("gate", 600)):
            result = gate_backtest.run_gate("gitleaks", [], ".", "HEAD^")
        self.assertEqual(result["status"], "cannot-run")
        self.assertIn("timed out", result["stderr"])

    def test_violation_without_later_fix_is_not_labeled_false_alarm(self):
        table = gate_backtest.confusion([{"number": 1, "fixed_by": []}],
                                      {1: {"gate": {"status": "violations"}}}, "gate")
        self.assertEqual(table["flagged_without_link"], 1)
        self.assertNotIn("false_alarm", table)


class BacktestDriver(unittest.TestCase):
    def test_batch_completes_with_more_than_one_page_of_commits(self):
        repo = make_repo(self, {"package.json": '{"name":"fixture"}\n'})
        parent = head(repo)
        stream = []
        for number in range(1, 102):
            body = json.dumps({"name": "fixture", "version": str(number)}) + "\n"
            stream.append(f"commit refs/heads/main\nmark :{number}\n"
                          f"committer Test <test@example.com> {number} +0000\n"
                          f"data 7\nchange\nfrom {parent if number == 1 else ':' + str(number - 1)}\n"
                          f"M 100644 inline package.json\ndata {len(body)}\n{body}\n")
        subprocess.run(["git", "-C", repo, "fast-import", "--quiet"], input="".join(stream),
                       text=True, capture_output=True, check=True)
        shas = subprocess.run(["git", "-C", repo, "rev-list", "--reverse", f"{parent}..HEAD"],
                              text=True, capture_output=True, check=True).stdout.splitlines()
        self.assertEqual(len(shas), 101)
        record = pr(1, "feat: fixture", shas[-1], 2, ["package.json"])
        record["baseRefName"] = "main"
        record["commits"]["totalCount"] = 101
        nodes = [{"commit": {"oid": sha}} for sha in shas]
        record["prCommits"]["nodes"] = nodes[:100]
        with tempfile.TemporaryDirectory() as directory:
            write(directory, {"record.json": json.dumps(record), "commits.json": json.dumps(nodes),
                              "manifest": "no-duplicate-package-json-keys\n"})
            fake = Path(directory) / "gh"
            fake.write_text(f"#!{sys.executable}\n" + '''import json, os, pathlib, subprocess, sys
root = pathlib.Path(__file__).parent
if sys.argv[1:3] == ['repo', 'clone']:
    subprocess.run(['git', 'clone', '-q', os.environ['RESEARCH_FIXTURE_REPO'], sys.argv[4]], check=True)
elif sys.argv[1:3] == ['api', 'graphql']:
    selector = sys.argv[sys.argv.index('--jq') + 1]
    if 'pullRequests' in selector:
        print((root / 'record.json').read_text())
    else:
        assert selector == '.data.repository.pullRequest.commits.nodes[]'
        assert '--paginate' in sys.argv
        assert any('after:$endCursor' in arg for arg in sys.argv)
        for node in json.loads((root / 'commits.json').read_text()):
            print(json.dumps(node))
else:
    assert sys.argv[1:3] == ['api', 'repos/example/project']
    print('main')
''')
            fake.chmod(0o755)
            env = clean_env({"PATH": directory + os.pathsep + os.environ["PATH"],
                             "RESEARCH_FIXTURE_REPO": repo, "OBSERVED_AT": "2026-10-04T00:00:00Z"})
            output = Path(directory) / "output"
            done = subprocess.run(["bash", str(ROOT / "research/backtest_repos.sh"), str(output),
                                   "example/project=" + str(Path(directory) / "manifest")],
                                  capture_output=True, text=True, env=env)
            self.assertEqual(done.returncode, 0, done.stderr)
            result = output / "example__project"
            outcomes = json.loads((result / "outcomes.json").read_text())
            self.assertEqual(outcomes["prs"][0]["commits"], 101)
            self.assertTrue(outcomes["prs"][0]["has_full_window"])
            gates = json.loads((result / "gates.json").read_text())
            self.assertEqual(gates["per_pr"]["1"]["no-duplicate-package-json-keys"]["status"], "pass")
            self.assertTrue((result / "oracles.json").is_file())

    def test_output_inside_the_public_repository_is_refused_before_anything_is_written(self):
        inside = ROOT / "research-output-should-not-exist"
        done = subprocess.run(["bash", str(ROOT / "research" / "backtest_repos.sh"), str(inside), "Greenbauer/vibe-verifier"],
                              capture_output=True, text=True, env=clean_env())
        self.assertEqual(done.returncode, 2)
        self.assertIn("outside this public repository", done.stderr)
        self.assertFalse(inside.exists())

    def test_symlink_into_public_repository_is_refused(self):
        with tempfile.TemporaryDirectory() as directory:
            link = os.path.join(directory, "linked-output")
            os.symlink(ROOT, link)
            done = subprocess.run(["bash", str(ROOT / "research" / "backtest_repos.sh"), link,
                                   "example/project"], capture_output=True, text=True, env=clean_env())
            self.assertEqual(done.returncode, 2)
            self.assertIn("outside this public repository", done.stderr)


class MutationOperators(unittest.TestCase):
    def test_absolute_and_parent_targets_never_touch_external_files(self):
        with tempfile.TemporaryDirectory() as directory:
            outside = Path(directory) / "outside.py"
            outside.write_text("VALUE = 1\n")
            for target in (str(outside), "../outside.py"):
                with self.assertRaisesRegex(ValueError, "repository-relative"):
                    mutation_score.score(target, "test_value.py", 1)
            self.assertEqual(outside.read_text(), "VALUE = 1\n")

    def test_tracked_symlink_cannot_escape_scratch(self):
        with tempfile.TemporaryDirectory() as directory:
            outside = Path(directory) / "outside.py"
            outside.write_text("VALUE = 1\n")
            repo = make_repo(self, {"README.md": "fixture\n"})
            os.symlink(outside, Path(repo) / "linked.py")
            subprocess.run(["git", "-C", repo, "add", "linked.py"], check=True)
            subprocess.run(["git", "-C", repo, "commit", "-qm", "link fixture"], check=True, env=clean_env())
            with patch.object(mutation_score, "ROOT", Path(repo)):
                with self.assertRaisesRegex(ValueError, "inside the scratch"):
                    mutation_score.score("linked.py", "test_value.py", 1)
            self.assertEqual(outside.read_text(), "VALUE = 1\n")

    def test_source_and_tests_use_head_and_leave_dirty_work_unchanged(self):
        repo = make_repo(self, {"value.py": "VALUE = 1\n",
                               "tests/test_value.py": "import unittest\nfrom value import VALUE\nclass Value(unittest.TestCase):\n    def test_value(self):\n        self.assertEqual(VALUE, 1)\n"})
        write(repo, {"value.py": "VALUE = 99\n"})
        with patch.object(mutation_score, "ROOT", Path(repo)):
            result = mutation_score.score("value.py", "test_value.py", 1)
        self.assertEqual(result["killed"], 1)
        self.assertEqual(result["score"], 1.0)
        self.assertEqual(Path(repo, "value.py").read_text(), "VALUE = 99\n")

    def test_empty_test_selection_is_unmeasured(self):
        with tempfile.TemporaryDirectory() as directory:
            os.mkdir(os.path.join(directory, "tests"))
            self.assertEqual(mutation_score.run_tests(directory, "missing.py"), "cannot-run")

    def test_test_timeout_is_not_a_killed_mutant(self):
        with patch.object(mutation_score.subprocess, "run", side_effect=subprocess.TimeoutExpired("test", 300)):
            self.assertEqual(mutation_score.run_tests(".", "test_x.py"), "cannot-run")

    def test_real_passing_and_failing_assertions(self):
        with tempfile.TemporaryDirectory() as directory:
            write(directory, {"tests/test_value.py": "import unittest\nclass Value(unittest.TestCase):\n    def test_value(self):\n        self.assertEqual(1, 1)\n"})
            self.assertEqual(mutation_score.run_tests(directory, "test_value.py"), "survived")
            write(directory, {"tests/test_value.py": "import unittest\nclass Value(unittest.TestCase):\n    def test_value(self):\n        self.assertEqual(1, 2)\n"})
            self.assertEqual(mutation_score.run_tests(directory, "test_value.py"), "killed")

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
