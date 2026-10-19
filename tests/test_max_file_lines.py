"""The file-size ratchet: lines and tokens (bytes / 4), judged against the merge base or, with
--staged, the index against HEAD."""
import json
import unittest

from helpers import commit, gate, git, make_repo, write


def branch_with(test, base_files, head_files):
    repo = make_repo(test, base_files)
    commit(repo, head_files, "change")
    return repo


def long(n):
    return "".join("export const v%d = %d;\n" % (i, i) for i in range(n))


def wide(tokens, lines=10):
    """A file of `lines` lines and exactly `tokens` tokens: few lines, so only the token limit can catch it."""
    body = "".join("// %s\n" % ("x" * (tokens * 4 // lines - 4)) for _ in range(lines))
    return body + "/" * (tokens * 4 - len(body))


class MaxFileLines(unittest.TestCase):
    def test_a_new_file_over_the_limit_fails(self):
        repo = branch_with(self, {"README.md": "x\n"}, {"src/big.ts": long(501)})
        result = gate("max-file-lines", repo, "--base-ref", "HEAD~1")
        self.assertEqual(result.returncode, 1)
        self.assertIn("src/big.ts: 501 lines, over the 500 allowed (new file)", result.stdout)

    def test_a_long_file_that_shrank_or_was_untouched_passes_and_one_that_grew_fails(self):
        repo = branch_with(self, {"src/big.ts": long(600), "src/other.ts": long(700)}, {"src/big.ts": long(590)})
        self.assertEqual(gate("max-file-lines", repo, "--base-ref", "HEAD~1").returncode, 0)
        commit(repo, {"src/big.ts": long(601)}, "grow")
        result = gate("max-file-lines", repo, "--base-ref", "HEAD~2")
        self.assertEqual(result.returncode, 1)
        self.assertIn("601 lines, over the 500 allowed (600 at the base)", result.stdout)
        self.assertNotIn("other.ts", result.stdout)

    def test_the_base_is_the_merge_base_not_the_base_branchs_tip(self):
        # The branch changes one line of a 600-line file. Meanwhile main shrinks it to 550. Against main's tip the
        # branch would look as if it had grown the file by 50 lines; against where it started, it grew nothing.
        repo = make_repo(self, {"src/big.ts": long(600)})
        git(repo, "checkout", "-q", "-b", "feat/x")
        commit(repo, {"src/big.ts": long(599) + "export const last = 0;\n"}, "touch")
        git(repo, "checkout", "-q", "main")
        commit(repo, {"src/big.ts": long(550)}, "shrink on main")
        git(repo, "checkout", "-q", "feat/x")
        result = gate("max-file-lines", repo, "--base-ref", "main")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        # Whether the file existed is asked of the merge base too: main deleting it does not make it new here.
        git(repo, "checkout", "-q", "main")
        git(repo, "rm", "-q", "src/big.ts")
        git(repo, "commit", "-q", "-m", "delete it on main")
        git(repo, "checkout", "-q", "feat/x")
        result = gate("max-file-lines", repo, "--base-ref", "main")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        # And real growth on the branch is still measured from where it started.
        commit(repo, {"src/big.ts": long(610)}, "grow")
        result = gate("max-file-lines", repo, "--base-ref", "main")
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("610 lines, over the 500 allowed (600 at the base)", result.stdout)

    def test_python_files_count_by_default(self):
        repo = branch_with(self, {"README.md": "x\n"}, {"pkg/big.py": "x = 1\n" * 501})
        result = gate("max-file-lines", repo, "--base-ref", "HEAD~1")
        self.assertEqual(result.returncode, 1)
        self.assertIn("pkg/big.py: 501 lines, over the 500 allowed (new file)", result.stdout)

    def test_all_and_max(self):
        repo = branch_with(self, {"src/big.ts": long(120)}, {"README.md": "y\n"})
        self.assertEqual(gate("max-file-lines", repo, "--base-ref", "HEAD~1", "--max", "100").returncode, 0)
        result = gate("max-file-lines", repo, "--all", "--max", "100")
        self.assertEqual(result.returncode, 1)
        self.assertIn("src/big.ts: 120 lines", result.stdout)

    def test_a_non_ascii_path_is_still_judged(self):
        # git quotes such a path unless asked for -z; a quoted path matched nothing and was skipped.
        repo = make_repo(self, {"README.md": "x\n"})
        commit(repo, {"src/café.ts": "x\n" * 600}, "add")
        result = gate("max-file-lines", repo, "--base-ref", "HEAD~1", "--format", "json")
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertEqual([f["path"] for f in json.loads(result.stdout)["findings"]], ["src/café.ts"])

    def test_moving_a_long_file_is_not_a_finding_but_growing_it_is(self):
        text = "x\n" * 600
        repo = make_repo(self, {"src/old.ts": text})
        git(repo, "mv", "src/old.ts", "src/new.ts")
        git(repo, "commit", "-q", "-m", "move")
        self.assertEqual(gate("max-file-lines", repo, "--base-ref", "HEAD~1").returncode, 0)
        commit(repo, {"src/new.ts": text + "y\n"}, "grow")
        result = gate("max-file-lines", repo, "--base-ref", "HEAD~2")
        self.assertEqual(result.returncode, 1)
        self.assertIn("600 at the base", result.stdout)


class MaxTokens(unittest.TestCase):
    """Lines miss a file of few, very long lines, and joining lines would game them; bytes / 4 does not."""

    def test_a_new_file_of_few_lines_over_the_token_limit_fails_and_says_how_to_split(self):
        repo = branch_with(self, {"README.md": "x\n"}, {"src/wide.ts": wide(5001)})
        result = gate("max-file-lines", repo, "--base-ref", "HEAD~1", "--format", "json")
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        [finding] = json.loads(result.stdout)["findings"]
        self.assertEqual(finding["path"], "src/wide.ts")
        self.assertIn("5001 tokens (bytes / 4), over the 5000 allowed (new file)", finding["message"])
        self.assertNotIn("lines, over", finding["message"])
        self.assertIn("Split it: move one self-contained part", finding["message"])
        self.assertIn("exclude it instead: add --exclude", finding["message"])

    def test_exactly_at_the_limit_passes(self):
        repo = branch_with(self, {"README.md": "x\n"}, {"src/wide.ts": wide(5000)})
        self.assertEqual(gate("max-file-lines", repo, "--base-ref", "HEAD~1").returncode, 0)

    def test_an_over_limit_file_may_shrink_but_not_grow(self):
        repo = branch_with(self, {"src/wide.ts": wide(6000)}, {"src/wide.ts": wide(5900)})
        self.assertEqual(gate("max-file-lines", repo, "--base-ref", "HEAD~1").returncode, 0)
        # Same line count, more bytes: the line measure did not grow, the token measure did.
        commit(repo, {"src/wide.ts": wide(6100)}, "grow")
        result = gate("max-file-lines", repo, "--base-ref", "HEAD~2")
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("6100 tokens (bytes / 4), over the 5000 allowed (6000 at the base)", result.stdout)

    def test_a_file_over_both_limits_names_both_in_one_finding(self):
        repo = branch_with(self, {"README.md": "x\n"}, {"src/big.ts": long(1000)})
        result = gate("max-file-lines", repo, "--base-ref", "HEAD~1", "--format", "json")
        [finding] = json.loads(result.stdout)["findings"]
        self.assertRegex(finding["message"], r"^1000 lines, over the 500 allowed \(new file\); \d+ tokens \(bytes / 4\)")

    def test_files_not_written_by_hand_are_not_judged_and_exclude_drops_more(self):
        # Lockfiles are not source; node_modules and *.d.ts never count; generated source is --exclude'd.
        repo = branch_with(self, {"README.md": "x\n"}, {
            "package-lock.json": wide(9000), "pnpm-lock.yaml": wide(9000), "node_modules/x/index.js": wide(9000),
            "src/types.d.ts": wide(9000), "src/gen/client.ts": wide(9000), "src/app.ts": wide(9000)})
        result = gate("max-file-lines", repo, "--base-ref", "HEAD~1", "--format", "json")
        self.assertEqual([f["path"] for f in json.loads(result.stdout)["findings"]], ["src/app.ts", "src/gen/client.ts"])
        result = gate("max-file-lines", repo, "--base-ref", "HEAD~1", "--exclude", "src/gen/**", "--format", "json")
        self.assertEqual([f["path"] for f in json.loads(result.stdout)["findings"]], ["src/app.ts"])

    def test_max_tokens_sets_the_limit_and_zero_turns_it_off(self):
        repo = branch_with(self, {"README.md": "x\n"}, {"src/wide.ts": wide(3000)})
        self.assertEqual(gate("max-file-lines", repo, "--base-ref", "HEAD~1").returncode, 0)
        self.assertEqual(gate("max-file-lines", repo, "--base-ref", "HEAD~1", "--max-tokens", "2000").returncode, 1)
        self.assertEqual(gate("max-file-lines", repo, "--base-ref", "HEAD~1", "--max-tokens", "0").returncode, 0)
        result = gate("max-file-lines", repo, "--all", "--max-tokens", "2000")
        self.assertIn("src/wide.ts: 3000 tokens (bytes / 4), over the 2000 allowed. Split it", result.stdout)


class Staged(unittest.TestCase):
    """--staged is the pre-commit form: what is in the index, judged against HEAD."""

    def test_a_staged_new_file_over_the_limit_fails_and_an_unstaged_one_is_not_judged(self):
        repo = make_repo(self, {"README.md": "x\n"})
        write(repo, {"src/staged.ts": wide(5001), "src/loose.ts": wide(9000)})
        git(repo, "add", "src/staged.ts")
        result = gate("max-file-lines", repo, "--staged")
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("src/staged.ts: 5001 tokens (bytes / 4), over the 5000 allowed (new file)", result.stdout)
        self.assertNotIn("loose.ts", result.stdout)

    def test_the_staged_content_is_judged_not_the_working_tree(self):
        repo = make_repo(self, {"src/a.ts": "x\n"})
        write(repo, {"src/a.ts": "y\n"})
        git(repo, "add", "src/a.ts")
        write(repo, {"src/a.ts": wide(9000)})  # edited after staging: not what the commit records
        self.assertEqual(gate("max-file-lines", repo, "--staged").returncode, 0)

    def test_a_staged_file_is_compared_with_head_not_the_base_branch(self):
        repo = make_repo(self, {"src/big.ts": long(600)})
        git(repo, "checkout", "-q", "-b", "feat/x")
        commit(repo, {"src/big.ts": long(650)}, "grew on the branch")
        write(repo, {"src/big.ts": long(640)})
        git(repo, "add", "src/big.ts")
        self.assertEqual(gate("max-file-lines", repo, "--staged").returncode, 0)
        write(repo, {"src/big.ts": long(660)})
        git(repo, "add", "src/big.ts")
        result = gate("max-file-lines", repo, "--staged")
        self.assertEqual(result.returncode, 1)
        self.assertIn("660 lines, over the 500 allowed (650 at the base)", result.stdout)

    def test_a_staged_move_is_compared_with_its_old_path(self):
        repo = make_repo(self, {"src/old.ts": "x\n" * 600})
        git(repo, "mv", "src/old.ts", "src/new.ts")
        self.assertEqual(gate("max-file-lines", repo, "--staged").returncode, 0)

    def test_the_first_commit_of_a_repository_is_judged_as_all_new(self):
        repo = make_repo(self, {"README.md": "x\n"})
        git(repo, "checkout", "-q", "--orphan", "fresh")
        git(repo, "rm", "-rq", "--cached", ".")
        write(repo, {"src/big.ts": long(501)})
        git(repo, "add", "src/big.ts")
        result = gate("max-file-lines", repo, "--staged")
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("501 lines, over the 500 allowed (new file)", result.stdout)

    def test_staged_and_all_cannot_be_combined(self):
        repo = make_repo(self, {"README.md": "x\n"})
        self.assertEqual(gate("max-file-lines", repo, "--staged", "--all").returncode, 2)


if __name__ == "__main__":
    unittest.main()
