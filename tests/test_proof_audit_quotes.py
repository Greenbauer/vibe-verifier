"""proof-audit: which assertion lines a grade may quote. A test declared `void it(...)` or `void test(...)` names
its assertions, and a short value line counts only inside an assert or expect call still open above it."""
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


if __name__ == "__main__":
    unittest.main()
