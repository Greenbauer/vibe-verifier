"""The typescript pack's suppression rules through the real repo-rules gate."""

import unittest

from helpers import commit, gate, make_repo


def new_file_findings(test, path, source):
    repo = make_repo(test, {"README.md": "x\n"})
    commit(repo, {path: source})
    return gate("repo-rules", repo, "--base-ref", "HEAD~1", "--pack", "typescript")


class TypescriptPack(unittest.TestCase):
    def test_a_new_type_check_suppression_blocks(self):
        result = new_file_findings(self, "src/a.ts", "// @ts-ignore\nload(x);\n")
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("no-type-check-suppression", result.stdout)

    def test_a_silenced_hooks_rule_blocks(self):
        source = "useEffect(() => {\n  load(id);\n  // eslint-disable-next-line react-hooks/exhaustive-deps\n}, []);\n"
        result = new_file_findings(self, "src/b.tsx", source)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("no-correctness-eslint-disable", result.stdout)

    def test_an_unchecked_external_data_cast_is_reported_but_does_not_block(self):
        result = new_file_findings(self, "src/c.ts", "export const order = (await response.json()) as Order;\n")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("no-unchecked-external-data-cast", result.stdout)


if __name__ == "__main__":
    unittest.main()
