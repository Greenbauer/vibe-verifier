"""The typescript pack's suppression rules, and the .jsx mapping of both TSX packs, through the real repo-rules gate."""

import unittest

from helpers import commit, gate, make_repo

IGNORE = "// @ts-ignore\nload(x);\n"


def judge(test, base, head, pack="typescript"):
    """The gate's result for a pull request that turns the files in base into those in head."""
    repo = make_repo(test, dict(base, **{"README.md": "x\n"}))
    commit(repo, head)
    return gate("repo-rules", repo, "--base-ref", "HEAD~1", "--pack", pack)


class TypescriptPack(unittest.TestCase):
    def assertBlocks(self, result, rule):
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn(rule, result.stdout)

    def assertPasses(self, result):
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_a_new_type_check_suppression_blocks_in_every_mapped_extension(self):
        for extension in ("ts", "tsx", "mts", "cts", "js", "mjs", "cjs", "jsx"):
            with self.subTest(extension=extension):
                self.assertBlocks(judge(self, {}, {"src/a.%s" % extension: IGNORE}), "no-type-check-suppression")
        self.assertBlocks(judge(self, {}, {"scripts/b.mjs": "// @ts-nocheck\nexport const b = 1;\n"}),
                          "no-type-check-suppression")

    def test_ts_expect_error_and_prose_naming_a_directive_pass(self):
        self.assertPasses(judge(self, {}, {"src/a.ts": "// @ts-expect-error the types say string\nconfigure(3);\n"}))
        self.assertPasses(judge(self, {}, {"src/b.ts": "// Its preprocessor adds `// @ts-nocheck` to each copy.\n"
                                                       "export const b = 1;\n"}))

    def test_a_silenced_hooks_rule_or_a_bare_disable_blocks(self):
        hooks = "useEffect(() => {\n  load(id);\n  // eslint-disable-next-line react-hooks/exhaustive-deps\n}, []);\n"
        self.assertBlocks(judge(self, {}, {"src/a.tsx": hooks}), "no-correctness-eslint-disable")
        self.assertBlocks(judge(self, {}, {"src/a.jsx": hooks}), "no-correctness-eslint-disable")
        self.assertBlocks(judge(self, {}, {"src/b.ts": "// eslint-disable-next-line -- legacy\nrisky();\n"}),
                          "no-correctness-eslint-disable")

    def test_a_disable_naming_a_style_rule_passes(self):
        self.assertPasses(judge(self, {}, {"src/a.ts": "// eslint-disable-next-line no-console -- CLI output\n"
                                                       "console.log(report);\n"}))

    def test_a_suppression_already_at_the_base_does_not_block_but_another_one_does(self):
        base = {"src/a.ts": IGNORE}
        self.assertPasses(judge(self, base, {"src/a.ts": IGNORE + "export const a = 1;\n"}))
        self.assertBlocks(judge(self, base, {"src/a.ts": IGNORE + IGNORE}), "no-type-check-suppression")

    def test_an_unchecked_external_data_cast_is_reported_but_does_not_block(self):
        result = judge(self, {}, {"src/c.ts": "export const order = (await response.json()) as Order;\n"})
        self.assertPasses(result)
        self.assertIn("no-unchecked-external-data-cast", result.stdout)

    def test_the_supabase_pack_judges_jsx_files_too(self):
        admin = "export const admin = createServerClient(url, process.env.SUPABASE_SERVICE_ROLE_KEY, { cookies });\n"
        self.assertBlocks(judge(self, {}, {"src/admin.jsx": admin}, "supabase"), "supabase-service-role-not-ssr-client")


if __name__ == "__main__":
    unittest.main()
