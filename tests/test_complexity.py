"""The cognitive-complexity ratchet, on the real pinned toolchain (npm ci from the lockfile into
the tool cache), and its file-length sibling."""
import json
import os
import unittest

from helpers import commit, gate, git, make_repo

TANGLED = """export function tangled(a: number, b: number, c: number[]): number {
  let total = 0;
  for (const x of c) {
    if (x > a) {
      if (x > b) { total += x; } else if (x === b) { total -= 1; } else {
        for (let i = 0; i < x; i++) {
          if (i % 2 === 0 && i % 3 === 0) { total += i; } else if (i % 5 === 0) { total -= i; }
        }
      }
    } else {
      while (total > 100) { total = total / 2; if (total < 10) { break; } }
    }
  }
  return total;
}
"""
SIMPLE = "export const simple = (n: number) => n + 1;\n"


def branch_with(test, base_files, head_files):
    repo = make_repo(test, base_files)
    commit(repo, head_files, "change")
    return repo


class CognitiveComplexity(unittest.TestCase):
    def test_a_new_file_with_an_over_limit_function_fails_at_its_line(self):
        repo = branch_with(self, {"README.md": "x\n"}, {"src/a.ts": TANGLED + SIMPLE})
        result = gate("cognitive-complexity", repo, "--base-ref", "HEAD~1", "--format", "json")
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        findings = json.loads(result.stdout)["findings"]
        self.assertEqual([(f["path"], f["line"]) for f in findings], [("src/a.ts", 1)])
        self.assertIn("Cognitive Complexity from 27 to the 15 allowed", findings[0]["message"])
        self.assertIn("0 at the base, 1 now", findings[0]["message"])

    def test_an_over_limit_function_already_at_the_base_does_not_block_a_touch(self):
        repo = branch_with(self, {"src/a.ts": TANGLED}, {"src/a.ts": TANGLED + SIMPLE})
        result = gate("cognitive-complexity", repo, "--base-ref", "HEAD~1")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_adding_a_second_over_limit_function_to_such_a_file_fails(self):
        second = TANGLED.replace("function tangled", "function tangled2")
        repo = branch_with(self, {"src/a.ts": TANGLED}, {"src/a.ts": TANGLED + second})
        result = gate("cognitive-complexity", repo, "--base-ref", "HEAD~1")
        self.assertEqual(result.returncode, 1)
        self.assertIn("1 at the base, 2 now", result.stdout)

    def test_untouched_files_and_tests_are_not_measured_unless_all(self):
        repo = branch_with(self, {"src/a.ts": TANGLED, "src/a.test.ts": TANGLED}, {"README.md": "y\n"})
        self.assertEqual(gate("cognitive-complexity", repo, "--base-ref", "HEAD~1").returncode, 0)
        result = gate("cognitive-complexity", repo, "--all", "--format", "json")
        self.assertEqual(result.returncode, 1)
        self.assertEqual([f["path"] for f in json.loads(result.stdout)["findings"]], ["src/a.ts"])

    def test_max_is_the_limit(self):
        repo = branch_with(self, {"README.md": "x\n"}, {"src/a.ts": TANGLED})
        self.assertEqual(gate("cognitive-complexity", repo, "--base-ref", "HEAD~1", "--max", "30").returncode, 0)

    def test_a_file_that_does_not_parse_cannot_run(self):
        repo = branch_with(self, {"README.md": "x\n"}, {"src/a.ts": "export function (\n"})
        result = gate("cognitive-complexity", repo, "--base-ref", "HEAD~1")
        self.assertEqual(result.returncode, 2)
        self.assertIn("could not parse src/a.ts", result.stderr)

    def test_without_node_it_cannot_run(self):
        repo = branch_with(self, {"README.md": "x\n"}, {"src/a.ts": TANGLED})
        result = gate("cognitive-complexity", repo, "--base-ref", "HEAD~1",
                      env={"PATH": "/usr/bin:/bin", "VIBE_VERIFIER_TOOLS": os.path.join(repo, "empty-cache")})
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn("needs node and npm", result.stderr)


class MaxFileLines(unittest.TestCase):
    def long(self, n):
        return "".join("export const v%d = %d;\n" % (i, i) for i in range(n))

    def test_a_new_file_over_the_limit_fails(self):
        repo = branch_with(self, {"README.md": "x\n"}, {"src/big.ts": self.long(501)})
        result = gate("max-file-lines", repo, "--base-ref", "HEAD~1")
        self.assertEqual(result.returncode, 1)
        self.assertIn("src/big.ts: 501 lines, over the 500 allowed (new file)", result.stdout)

    def test_a_long_file_that_shrank_or_was_untouched_passes_and_one_that_grew_fails(self):
        repo = branch_with(self, {"src/big.ts": self.long(600), "src/other.ts": self.long(700)}, {"src/big.ts": self.long(590)})
        self.assertEqual(gate("max-file-lines", repo, "--base-ref", "HEAD~1").returncode, 0)
        commit(repo, {"src/big.ts": self.long(601)}, "grow")
        result = gate("max-file-lines", repo, "--base-ref", "HEAD~2")
        self.assertEqual(result.returncode, 1)
        self.assertIn("601 lines, over the 500 allowed (600 at the base)", result.stdout)
        self.assertNotIn("other.ts", result.stdout)

    def test_all_and_max(self):
        repo = branch_with(self, {"src/big.ts": self.long(120)}, {"README.md": "y\n"})
        self.assertEqual(gate("max-file-lines", repo, "--base-ref", "HEAD~1", "--max", "100").returncode, 0)
        result = gate("max-file-lines", repo, "--all", "--max", "100")
        self.assertEqual(result.returncode, 1)
        self.assertIn("src/big.ts: 120 lines", result.stdout)


class Renames(unittest.TestCase):
    """A moved file is compared with itself at its old path, never counted as new."""

    def test_moving_a_tangled_file_is_not_a_finding_but_growing_it_is(self):
        # Enough unchanged lines that the grown file is still, to git, the same file moved (-M is 50%).
        padding = "".join("export const c%d = %d;\n" % (i, i) for i in range(40))
        repo = make_repo(self, {"src/old.ts": TANGLED + padding})
        git(repo, "mv", "src/old.ts", "src/new.ts")
        git(repo, "commit", "-q", "-m", "move")
        self.assertEqual(gate("cognitive-complexity", repo, "--base-ref", "HEAD~1").returncode, 0)
        commit(repo, {"src/new.ts": TANGLED + padding + TANGLED.replace("function tangled", "function tangled2")}, "grow")
        result = gate("cognitive-complexity", repo, "--base-ref", "HEAD~2")
        self.assertEqual(result.returncode, 1)
        self.assertIn("1 at the base, 2 now", result.stdout)

    def test_a_non_ascii_path_is_still_judged(self):
        # git quotes such a path unless asked for -z; a quoted path matched nothing and was skipped.
        repo = make_repo(self, {"README.md": "x\n"})
        commit(repo, {"src/caf\u00e9.ts": "x\n" * 600}, "add")
        result = gate("max-file-lines", repo, "--base-ref", "HEAD~1", "--format", "json")
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertEqual([f["path"] for f in json.loads(result.stdout)["findings"]], ["src/caf\u00e9.ts"])

    def test_moving_a_long_file_is_not_a_finding_but_growing_it_is(self):
        long = "x\n" * 600
        repo = make_repo(self, {"src/old.ts": long})
        git(repo, "mv", "src/old.ts", "src/new.ts")
        git(repo, "commit", "-q", "-m", "move")
        self.assertEqual(gate("max-file-lines", repo, "--base-ref", "HEAD~1").returncode, 0)
        commit(repo, {"src/new.ts": long + "y\n"}, "grow")
        result = gate("max-file-lines", repo, "--base-ref", "HEAD~2")
        self.assertEqual(result.returncode, 1)
        self.assertIn("600 at the base", result.stdout)


if __name__ == "__main__":
    unittest.main()
