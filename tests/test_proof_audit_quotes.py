"""proof-audit: which assertion lines a grade may quote. A test declared `void it(...)` or `void test(...)` names
its assertions, a short value line counts only inside an assert or expect call still open above it, and a quote at
the wrong line number counts when it sits exactly once inside the test the grade names, and a line cited exactly
counts only inside that test's body (gates/_test_body.py), never in a hook or helper after it."""
import sys
import unittest

from helpers import ROOT

sys.path.insert(0, str(ROOT / "gates"))
import _proof_audit  # noqa: E402
import _test_body  # noqa: E402
import proof_audit  # noqa: E402



def declared_above(lines, number):
    """The title of the test declared nearest above line `number`."""
    start = proof_audit.declaration_above(lines, number)
    return proof_audit.declared_title("\n".join(lines[start:start + 3]))

class Quotes(unittest.TestCase):
    def test_a_void_prefixed_declaration_is_a_test(self):
        # A type-aware lint forbids node:test's floating promise, so infra tests read `void it(...)`.
        lines = ['void describe("UserPool", () => {', '  void it("keeps the policy", () => {',
                 "    assert.equal(policy.MinimumLength, 8);", "  });", '  void test("another", () => {',
                 "    expect(other).toBe(true);", "  });", "});"]
        self.assertEqual(declared_above(lines, 3), "keeps the policy")
        self.assertEqual(declared_above(lines, 6), "another")
        self.assertIsNone(proof_audit.declaration_above(lines, 1))

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
        self.assertEqual(_proof_audit.proof_cite(proof),
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
                      "}"],
                     # A one-line test whose comment opens a bracket, before a deeper-indented helper.
                     ['test("a", () => { x; }); // (', "  function helper() {", "    assert.equal(hookOnly(), 2);",
                      "  }"],
                     ['test("a", () => { x; }); /* ( */', "  function helper() {", "    assert.equal(hookOnly(), 2);",
                      "  }"],
                     ['test("a", () => { x(/a\\//); });', "  function helper() {", "    assert.equal(hookOnly(), 2);",
                      "  }"],
                     ['test("a", () => { x(/[(]/); });', "  function helper() {", "    assert.equal(hookOnly(), 2);",
                      "  }"]) + tuple([line, "  function helper() {", "    assert.equal(hookOnly(), 2);", "  }"] for line in (
                         'test("a", () => { x(/[(]/); });// c',
                         'test("a", () => expect(/[(]/.test(s)).toBe(true));',
                         'test("a", wrap(() => { x(/[(]/); }));',
                         'test("a", () => { x("a //b", /[(]/); });',
                         'test("a", () => { x(/a\\//, /[(]/); }); // c')):
            proof = {"file": "tests/a.test.ts", "test": "a", "line": 1, "assertion": "assert.equal(hookOnly(), 2);"}
            self.assertIn("fabricated quote", proof_audit.proof_problem(Files(spec), proof) or "", spec[1])
            self.assertNotIn("cited_line", proof)
        # A multi-line test whose opening line ends in a comment closing a bracket is still multi-line.
        for opening in ('test("a", () => { // wip (see x)', 'test("a", async () => { // gets "x" (y)'):
            spec = [opening, "  const one = 1;", "  assert.equal(one(), 1);", "});"]
            proof = {"file": "tests/a.test.ts", "test": "a", "line": 1, "assertion": "assert.equal(one(), 1);"}
            self.assertIsNone(proof_audit.proof_problem(Files(spec), proof), opening)
            self.assertEqual(proof["line"], 3)
        # Inside such a body, a drifted quote is still found.
        spec = ['test("a", () => {', "  // note (", "  assert.equal(one(), 1);", "});"]
        proof = {"file": "tests/a.test.ts", "test": "a", "line": 1, "assertion": "assert.equal(one(), 1);"}
        self.assertIsNone(proof_audit.proof_problem(Files(spec), proof))
        self.assertEqual(proof["line"], 3)

    def test_a_hook_or_helper_line_cited_exactly_is_not_the_test_above_it(self):
        self.assertEqual(_test_body.body_end(SPEC, 4, brackets=False), 13)
        problem = proof_audit.proof_problem(Files(SPEC), cite(15, 'assert.equal(reset(), "in the hook only");'))
        self.assertIn("below the end of the test 'ends inside the bounds' (in a hook or helper", problem or "")
        lines = ["class Bounds(unittest.TestCase):", "    def test_end(self):", "        self.assertEqual(end.day, 10)",
                 "", "    def helper(self):", "        self.assertEqual(other(), 1)"]
        helper = {"file": "tests/test_bounds.py", "test": "test_end", "line": 6,
                  "assertion": "self.assertEqual(other(), 1)"}
        self.assertIn("below the end of the test 'test_end'", proof_audit.proof_problem(Files(lines), helper) or "")
        inside = dict(helper, line=3, assertion="self.assertEqual(end.day, 10)")
        self.assertIsNone(proof_audit.proof_problem(Files(lines), inside))
        # A bracket the count misreads (in a regex) does not cut the body short for a line cited exactly.
        spec = ['test("a", () => {', "  const r = /\\)\\)/;", "  assert.equal(one(), 1);", "});"]
        proof = {"file": "tests/a.test.ts", "test": "a", "line": 3, "assertion": "assert.equal(one(), 1);"}
        self.assertIsNone(proof_audit.proof_problem(Files(spec), proof))
        # A declaration that wraps onto a line at its own indent, and a template literal whose text starts at
        # column 0, are still inside the test; a helper after it is not.
        spec = ['test("a", async ({', "  page", "}, testInfo) => {", "  const fixture = `", "select 1", "`;",
                "  assert.equal(one(), 1);", "});", "async function helper() {", "  assert.equal(hookOnly(), 2);", "}"]
        self.assertEqual(_test_body.body_end(spec, 0, brackets=False), 8)
        proof = {"file": "tests/a.test.ts", "test": "a", "line": 7, "assertion": "assert.equal(one(), 1);"}
        self.assertIsNone(proof_audit.proof_problem(Files(spec), proof))
        # A declaration wrapped over many fixtures, one per line, opens its body where it ends.
        fixtures = ["  fixture%d," % number for number in range(12)]
        wide = ['test("wide", async ({'] + fixtures + ["}) => {", "  assert.equal(one(), 1);", "});"]
        self.assertIsNone(proof_audit.proof_problem(Files(wide), dict(proof, test="wide", line=len(wide) - 1)))
        # A body that opens on the declaration's own line, with code after it there, still ends at its own close.
        for opened, title, path in (([' test("a", async () => { const x = 1;', "   expect(x).toBe(1);", " });",
                                    " function helper() {", "   expect(cleanup()).toBe(true);", " }"], "a", "tests/a.test.ts"),
                                  (["class A(unittest.TestCase):", "    def test_a(self): pass", "",
                                    "    def helper(self):", "        expect(cleanup()).toBe(true);"], "test_a",
                                   "tests/test_a.py")):
            cited = {"file": path, "test": title, "line": 5, "assertion": "expect(cleanup()).toBe(true);"}
            self.assertIn("below the end of the test", proof_audit.proof_problem(Files(opened), cited) or "", path)
        # An expression-arrow test with a timeout, as prettier wraps it, ends at its own `);`, not at the hook's `=> {`.
        arrow = ["test(", '  "renders the summary card",', "  () => expect(card()).toBe(1),", "  30_000,", ");",
                 "afterAll(() => {", "  expect(cleanupEverythingAgain()).toBe(true);", "});"]
        cited = {"file": "tests/a.test.ts", "test": "renders the summary card", "line": 7,
                 "assertion": "expect(cleanupEverythingAgain()).toBe(true);"}
        self.assertIn("below the end of the test", proof_audit.proof_problem(Files(arrow), cited) or "")
        # A regex literal holding a quote, a backtick or a bracket is stepped over whole, so it neither ends the
        # body early nor lets a following hook in; a division is not a regex.
        regex = ['test("a", () => {', "  expect(q.replace(/'/g, \"\")).toBe(`", "select 1", "`);",
                 "  expect(s).toMatch(/^[^`(]*$/);", "  const r = total / count; const t = `", "x`;",
                 "  expect(after()).toBe(1);", "});", "afterEach(() => {", "  expect(cleanupAfterStrip()).toBe(true);",
                 "});"]
        inside = {"file": "tests/a.test.ts", "test": "a", "line": 8, "assertion": "expect(after()).toBe(1);"}
        self.assertIsNone(proof_audit.proof_problem(Files(regex), inside))
        hook = dict(inside, line=11, assertion="expect(cleanupAfterStrip()).toBe(true);")
        self.assertIn("below the end of the test", proof_audit.proof_problem(Files(regex), hook) or "")
        stray = dict(proof, line=10, assertion="assert.equal(hookOnly(), 2);")
        self.assertIn("below the end of the test 'a'", proof_audit.proof_problem(Files(spec), stray) or "")

    def test_strings_comments_and_shared_titles_neither_end_a_test_early_nor_reopen_one(self):
        def problem(spec, line, assertion, title):
            return proof_audit.proof_problem(Files(spec), {"file": "tests/a.test.ts" if spec[0].startswith(("test", "describe"))
                                                           else "tests/test_a.py", "test": title, "line": line,
                                                           "assertion": assertion})
        # A Python fixture string and a comment at column 0 stay inside the test.
        python = ["class A(unittest.TestCase):", "    def test_a(self):", '        text = """', "line one", '"""',
                  "# a commented-out line", "        self.assertEqual(parse(text), 2)", "", "    def helper(self):",
                  "        self.assertEqual(other(), 1)"]
        self.assertIsNone(problem(python, 7, "self.assertEqual(parse(text), 2)", "test_a"))
        self.assertIn("below the end of the test", problem(python, 10, "self.assertEqual(other(), 1)", "test_a") or "")
        # An apostrophe inside a template literal does not hide its closing backtick.
        spec = ['test("a", () => {', "  expect(normalize(`it's`)).toBe(\"it's\");", "});", "", "afterEach(() => {",
                "  expect(cleanup()).toBe(true);", "});"]
        self.assertIn("below the end of the test", problem(spec, 6, "expect(cleanup()).toBe(true);", "a") or "")
        # A title two tests share, and a title that is not a literal, still end at their own test.
        for spec, title in ((['describe("x", () => {', '  test("renders", () => {', "    expect(a).toBe(1);", "  });",
                              "  afterEach(() => {", "    expect(cleanup()).toBe(true);", "  });", "});",
                              'describe("y", () => {', '  test("renders", () => {', "    expect(b).toBe(2);", "  });",
                              "});"], "renders"),
                            (['test(name + " renders the card", () => {', "  expect(a).toBe(1);", "});",
                              "afterEach(() => {", "  expect(cleanup()).toBe(true);", "});"], "renders the card")):
            line = spec.index("    expect(cleanup()).toBe(true);" if title == "renders" else "  expect(cleanup()).toBe(true);") + 1
            self.assertIn("below the end of the test", problem(spec, line, "expect(cleanup()).toBe(true);", title) or "")

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
