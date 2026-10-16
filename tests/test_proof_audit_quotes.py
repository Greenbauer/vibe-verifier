"""proof-audit: which assertion lines a grade may quote. A test declared `void it(...)` or `void test(...)` names
its assertions, a short value line counts only inside an assert or expect call still open above it, and a quote at
the wrong line number counts when it sits exactly once inside the test the grade names."""
import sys
import unittest

from helpers import ROOT

sys.path.insert(0, str(ROOT / "gates"))
import proof_audit  # noqa: E402


class Quotes(unittest.TestCase):
    def test_a_void_prefixed_declaration_is_a_test(self):
        # A type-aware lint forbids node:test's floating promise, so infra tests read `void it(...)`.
        lines = ['void describe("UserPool", () => {', '  void it("keeps the policy", () => {',
                 "    assert.equal(policy.MinimumLength, 8);", "  });", '  void test("another", () => {',
                 "    expect(other).toBe(true);", "  });", "});"]
        self.assertEqual(proof_audit.declared_title(proof_audit.enclosing_test(lines, 3)), "keeps the policy")
        self.assertEqual(proof_audit.declared_title(proof_audit.enclosing_test(lines, 6)), "another")
        self.assertIsNone(proof_audit.enclosing_test(lines, 1))

    def test_a_short_value_inside_a_wrapped_assertion_is_quotable(self):
        lines = ['test("counts", async () => {', "  assert.equal(", "    await count(),", "    0", "  );",
                 "  await expect(page.locator(\"[data-x]\")).toHaveCount(", "    1", "  );",
                 "  assert.deepEqual(result, {", "    nested: {", '      "named": [],', "      list: [", "        []",
                 "      ]", "    }", "  });", "  self.assertEqual(", "      x,", "      0,", "  )", "});"]
        for number in (4, 7, 13, 19):
            self.assertIsNone(proof_audit.quote_problem(lines, "a.test.ts", number, lines[number - 1].strip(),
                                                        whole=True), number)

    def test_a_short_line_outside_an_open_assertion_is_still_too_short(self):
        lines = ['test("counts", () => {', "  assert.equal(", "    count(),", "    0", "  );", "  const n =",
                 "    0;", "  helper(", "    0", "  );", "  assert.ok(done);", "  record(", "    []", "  );",
                 "});", "function helper(", "  1", ") {}"]
        for number, quote in ((5, ");"), (15, "});"), (7, "0;"), (9, "0"), (13, "[]"), (17, "1")):
            self.assertIn("too short to check",
                          proof_audit.quote_problem(lines, "a.test.ts", number, quote, whole=True) or "", number)
        self.assertIn("too short to check", proof_audit.quote_problem(["0"], "a.test.ts", 1, "0", whole=True))
        # A closed assert ends its statement even with no `;` (Python, unformatted JS): a helper call after it is
        # not the assertion's.
        for closed in (["def test_x(self):", "    self.assertEqual(a, b)", "    helper(", "        0", "    )"],
                       ['test("x", () => {', "  expect(a).toBe(b)", "  helper(", "    0", "  )", "})"]):
            self.assertIn("too short to check",
                          proof_audit.quote_problem(closed, "a.test.ts", 4, "0", whole=True) or "", closed[1])
        # A short fragment of a plan line is still refused.
        self.assertIn("too short to check", proof_audit.quote_problem(lines, "a.md", 4, "0", whole=False))


class Files:
    def __init__(self, lines):
        self.text = lines

    def lines(self, _ref, _path):
        return self.text


# Two tests, a hook and a helper, with the asserted line 5 lines below where a grader might cite it.
SPEC = ['import test from "node:test";',                          # 1
        "function helper() {",                                    # 2
        '  assert.equal(helperValue(), "only in the helper");',  # 3
        "}",                                                      # 4
        'test("ends inside the bounds", () => {',                 # 5
        "  const end = bound(10);",                               # 6
        "  const start = bound(0);",                              # 7
        "  log(end);",                                            # 8
        "  log(start);",                                          # 9
        "  assert.equal(end.day, 10);",                           # 10
        "  assert.ok(end.valid);",                                # 11
        "  assert.ok(end.valid);",                                # 12
        "});",                                                    # 13
        'beforeEach(() => {',                                     # 14
        '  assert.equal(reset(), "in the hook only");',          # 15
        "});",                                                    # 16
        'test("another test", () => {',                           # 17
        '  assert.equal(other(), "in another test");',           # 18
        "});"]                                                    # 19


def cite(line, assertion, title="ends inside the bounds"):
    return {"file": "tests/bounds.test.ts", "test": title, "line": line, "assertion": assertion}


class LineDrift(unittest.TestCase):
    def test_a_quote_off_by_five_inside_its_test_counts_at_its_real_line(self):
        proof = cite(5, "assert.equal(end.day, 10);")
        self.assertIsNone(proof_audit.proof_problem(Files(SPEC), proof))
        self.assertEqual((proof["line"], proof["cited_line"]), (10, 5))
        self.assertEqual(proof_audit.proof_cite(proof),
                         "`tests/bounds.test.ts:10` (ends inside the bounds; cited :5)")
        exact = cite(10, "assert.equal(end.day, 10);")
        self.assertIsNone(proof_audit.proof_problem(Files(SPEC), exact))
        self.assertNotIn("cited_line", exact)

    def test_a_quote_absent_twice_or_outside_its_test_is_still_fabricated(self):
        for proof in (cite(10, "assert.equal(end.day, 11);"),            # absent
                      cite(8, "assert.ok(end.valid);"),                  # twice in the test
                      cite(18, 'assert.equal(other(), "in another test");'),   # only in another test
                      cite(8, 'assert.equal(other(), "in another test");'),
                      cite(8, 'assert.equal(reset(), "in the hook only");'),   # only in a hook
                      cite(8, 'assert.equal(helperValue(), "only in the helper");')):  # only in a helper
            problem = proof_audit.proof_problem(Files(SPEC), proof) or ""
            self.assertRegex(problem, "fabricated quote|is in the test", proof["assertion"])
            self.assertNotIn("cited_line", proof)

    def test_a_misread_bracket_or_a_one_line_test_never_reaches_a_following_hook_or_helper(self):
        for spec in (['test("a", () => {', "  // note (", "  assert.equal(one(), 1);", "});", "beforeEach(() => {",
                      "  assert.equal(hookOnly(), 2);", "});"],
                     ['test("a", () => {', "  const q = `select (", "from t`;", "  assert.equal(one(), 1);", "});",
                      "function helper() {", "  assert.equal(hookOnly(), 2);", "}"],
                     ['test("a", () => { assert.ok(a); });', "function helper() {", "  assert.equal(hookOnly(), 2);",
                      "}"]):
            proof = {"file": "tests/a.test.ts", "test": "a", "line": 1, "assertion": "assert.equal(hookOnly(), 2);"}
            self.assertIn("fabricated quote", proof_audit.proof_problem(Files(spec), proof) or "", spec[1])
            self.assertNotIn("cited_line", proof)
        # Inside such a body, a drifted quote is still found.
        spec = ['test("a", () => {', "  // note (", "  assert.equal(one(), 1);", "});"]
        proof = {"file": "tests/a.test.ts", "test": "a", "line": 1, "assertion": "assert.equal(one(), 1);"}
        self.assertIsNone(proof_audit.proof_problem(Files(spec), proof))
        self.assertEqual(proof["line"], 3)

    def test_a_python_test_body_runs_to_its_dedent(self):
        lines = ["class Bounds(unittest.TestCase):", "    def test_end(self):", "        end = bound(10)",
                 "", "        self.assertEqual(end.day, 10)", "", "    def helper(self):",
                 "        self.assertEqual(other(), 1)"]
        proof = {"file": "tests/test_bounds.py", "test": "test_end", "line": 3,
                 "assertion": "self.assertEqual(end.day, 10)"}
        self.assertIsNone(proof_audit.proof_problem(Files(lines), proof))
        self.assertEqual(proof["line"], 5)
        stray = dict(proof, line=3, assertion="self.assertEqual(other(), 1)")
        self.assertIn("fabricated quote", proof_audit.proof_problem(Files(lines), stray) or "")


if __name__ == "__main__":
    unittest.main()
