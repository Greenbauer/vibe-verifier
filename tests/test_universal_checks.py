"""The universal-checks gate, driven through its command line against a temp repository with a base
branch and a pull request branch: a rule passes, a rule fails, the merge base's rules govern a pull
request that edits them, and an empty population fails closed."""
import os
import unittest

from helpers import commit, gate, git, make_repo

RULES = ("# Standing rules\n\nUniversal checks:\n"
         "- `\\.route\\(` in `e2e/*.spec.ts` conforms to `apiPath\\(`\n")
SPEC = ("import { apiPath } from './api'\n"
        "test('quotes', async ({ page }) => {\n"
        "  await page.route(apiPath('/quotes'), (route) => route.fulfill({ body: '[]' }))\n"
        "  // page.route('/api/old', handler) is how it used to be done\n"
        "})\n")
BYPASS = "test('orders', async ({ page }) => {\n  await page.route('/api/orders', (route) => route.abort())\n})\n"


class UniversalChecks(unittest.TestCase):
    def branch(self, changes, base=None):
        repo = make_repo(self, base or {"ci/universal-checks.md": RULES, "e2e/quotes.spec.ts": SPEC, "e2e/api.ts": "x\n"})
        git(repo, "checkout", "-q", "-b", "feat")
        for path, text in changes.items():
            if text is None:
                os.remove(os.path.join(repo, path))
        commit(repo, {path: text for path, text in changes.items() if text is not None})
        return repo

    def run_gate(self, repo, *args):
        return gate("universal-checks", repo, "--base-ref", "main", *args)

    def test_every_member_conforming_passes_and_a_comment_is_not_a_member(self):
        result = self.run_gate(self.branch({"e2e/quotes.spec.ts": SPEC + "\n"}))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("1 member(s), 0 not conforming", result.stdout)

    def test_a_member_that_does_not_conform_fails_at_its_line(self):
        result = self.run_gate(self.branch({"e2e/nested/orders.spec.ts": BYPASS}))
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("e2e/nested/orders.spec.ts:2: does not conform to `apiPath\\(`", result.stdout)

    def test_the_first_call_argument_is_graded_not_the_handler(self):
        # The handler mentions apiPath, the matcher does not: still a bypass.
        spec = "test('x', async ({ page }) => {\n  await page.route('/api/x', () => apiPath('/x'))\n})\n"
        self.assertEqual(self.run_gate(self.branch({"e2e/x.spec.ts": spec})).returncode, 1)

    def test_a_pull_request_that_weakens_the_rules_is_still_judged_by_the_merge_base(self):
        weakened = RULES.replace("conforms to `apiPath\\(`", "conforms to `.`")
        result = self.run_gate(self.branch({"ci/universal-checks.md": weakened, "e2e/orders.spec.ts": BYPASS}))
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("e2e/orders.spec.ts:2", result.stdout)
        result = self.run_gate(self.branch({"ci/universal-checks.md": None, "e2e/orders.spec.ts": BYPASS}))
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)

    def test_a_rule_the_pull_request_adds_is_proven_before_it_merges(self):
        added = RULES + "- `^test\\(` in `e2e/*.spec.ts` conforms to `profile`\n"
        result = self.run_gate(self.branch({"ci/universal-checks.md": added}))
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("at the head", result.stdout)

    def test_an_empty_population_fails_closed_even_under_soak(self):
        result = self.run_gate(self.branch({"e2e/quotes.spec.ts": "test('none', () => {})\n"}), "--soak")
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn("matches nothing", result.stderr)

    def test_a_glob_matching_no_file_or_a_malformed_rule_cannot_run(self):
        moved = self.branch({"e2e/quotes.spec.ts": None, "tests/quotes.spec.ts": SPEC})
        self.assertIn("matches no tracked file", self.run_gate(moved).stderr)
        broken = RULES + "- `x` in e2e conforms to `y`\n"
        result = self.run_gate(self.branch({"ci/universal-checks.md": broken}))
        self.assertEqual(result.returncode, 2)
        self.assertIn("malformed rule", result.stderr)

    def test_a_listener_population_grades_the_listener_and_literals_do_not_split_arguments(self):
        rules = ("Universal checks:\n"
                 "- `\\.on\\(\\s*[\"']response[\"']\\s*,` in `e2e/*.spec.ts` conforms to `^\\s*matchesApi\\(`\n")
        good = "page.on('response', matchesApi('/x, (y)', handler))\n"
        bad = "page.on(\"response\", (r) => r.url().includes('/api/'))\n"
        repo = self.branch({"e2e/a.spec.ts": good + "\n"}, base={"ci/universal-checks.md": rules, "e2e/a.spec.ts": good})
        result = self.run_gate(repo)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        repo = self.branch({"e2e/b.spec.ts": bad}, base={"ci/universal-checks.md": rules, "e2e/a.spec.ts": good})
        result = self.run_gate(repo)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("e2e/b.spec.ts:1", result.stdout)

    def test_a_member_with_no_call_is_graded_on_the_rest_of_its_line(self):
        rules = "Universal checks:\n- `fetch\\b` in `src/*.ts` conforms to `apiFetch`\n"
        base = {"ci/universal-checks.md": rules, "src/a.ts": "const f = fetch // apiFetch wraps it\n"}
        result = self.run_gate(self.branch({"src/a.ts": "const f = fetch\n"}, base=base))
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)

    def test_a_file_that_is_not_strictly_rules_cannot_run(self):
        for text, why in (("# Rules\n\nUniversal checks:\n\n", "has no rule under it"),
                          (RULES + "A note ends the section.\n- `x` in `e2e/*.spec.ts` conforms to `y`\n", "outside a Universal checks section"),
                          ("# Rules\n\nNothing here yet.\n", "no rule in it"),
                          (RULES.replace("conforms to `apiPath\\(`", "conforms to `apiPath(`"), "does not compile")):
            result = self.run_gate(self.branch({"ci/universal-checks.md": text}))
            self.assertEqual(result.returncode, 2, why)
            self.assertIn(why, result.stderr)

    def test_no_rules_file_anywhere_cannot_run(self):
        repo = self.branch({"e2e/x.spec.ts": SPEC}, base={"e2e/quotes.spec.ts": SPEC})
        result = self.run_gate(repo)
        self.assertEqual(result.returncode, 2)
        self.assertIn("no ci/universal-checks.md", result.stderr)


if __name__ == "__main__":
    unittest.main()
