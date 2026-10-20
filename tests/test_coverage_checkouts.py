"""gates/_coverage_checkouts.py on its own: coverage-not-lost's two checkouts, each with the working copy's ignored files
linked in and its node_modules mirrored entry by entry, and nothing left behind. The gate end to end is
tests/test_coverage_not_lost.py."""
import os
import subprocess
import sys
import unittest
from pathlib import Path

from helpers import ROOT, commit, make_repo

sys.path.insert(0, str(ROOT / "gates"))
from _coverage_checkouts import checkouts  # noqa: E402  (gates/ is not a package)


class Checkouts(unittest.TestCase):
    def test_each_side_gets_the_working_copys_ignored_files_and_dependencies_and_is_removed_after(self):
        repo = make_repo(self, {".gitignore": "node_modules/\n.env.local\n", "src/a.js": "export const a = 1;\n"})
        base = subprocess.run(["git", "-C", repo, "rev-parse", "HEAD"], capture_output=True, text=True, check=True).stdout.strip()
        commit(repo, {"src/a.js": "export const a = 2;\n"})
        modules = Path(repo, "node_modules")
        (modules / "@scope" / "lib").mkdir(parents=True)
        (modules / "vitest").mkdir()
        (modules / ".vite").mkdir()  # a cache a run writes: each checkout gets its own
        Path(repo, "packages", "shared").mkdir(parents=True)
        os.symlink("../packages/shared", modules / "shared")  # a workspace package, linked by a relative path
        Path(repo, ".env.local").write_text("SESSION_SECRET=local-only\n")
        with checkouts(repo, base) as (scratch, trees):
            for side, text in (("base", "export const a = 1;\n"), ("head", "export const a = 2;\n")):
                tree = Path(trees[side])
                self.assertEqual((tree / "src" / "a.js").read_text(), text)
                self.assertEqual(os.readlink(tree / ".env.local"), os.path.join(repo, ".env.local"))
                self.assertEqual(os.readlink(tree / "node_modules" / "vitest"), str(modules / "vitest"))
                self.assertEqual(os.readlink(tree / "node_modules" / "shared"), "../packages/shared")  # resolves in this side
                self.assertTrue((tree / "node_modules" / "@scope").is_dir() and not (tree / "node_modules" / "@scope").is_symlink())
                self.assertEqual(os.readlink(tree / "node_modules" / "@scope" / "lib"), str(modules / "@scope" / "lib"))
                self.assertFalse(os.path.lexists(tree / "node_modules" / ".vite"))
        self.assertFalse(os.path.exists(scratch))
        listed = subprocess.run(["git", "-C", repo, "worktree", "list", "--porcelain"], capture_output=True, text=True).stdout
        self.assertEqual(listed.count("worktree "), 1, listed)
        self.assertTrue((modules / "vitest").is_dir())  # links were removed, never followed


if __name__ == "__main__":
    unittest.main()
