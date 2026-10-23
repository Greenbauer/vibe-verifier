"""proof-audit: a line cited exactly counts only inside the named test's body (gates/_test_body.py), never in a hook
or helper after it, wherever strings, comments, regexes, templates and wrapped declarations put that body's end."""
import sys
import unittest

from helpers import ROOT
from test_proof_audit_quotes import SPEC, Files, cite

sys.path.insert(0, str(ROOT / "gates"))
import _test_body  # noqa: E402
import proof_audit  # noqa: E402


class TestBody(unittest.TestCase):
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

    def test_an_escaped_backtick_an_unclosed_regex_and_an_overlong_header_keep_their_stated_bounds(self):
        def problem(spec, line, title="a"):
            return proof_audit.proof_problem(Files(spec), {"file": "tests/a.test.ts", "test": title, "line": line,
                                                           "assertion": spec[line - 1].strip()})
        hook = ["afterEach(() => {", "  assert.equal(hookOnly(), 2);", "});"]
        # An escaped backtick does not close the template, so its column-0 text does not end the test early.
        escaped = ['test("a", () => {', "  const fixture = `", "a \\` b", "c", "`;", "  assert.equal(one(), 1);",
                   "});"] + hook
        self.assertIsNone(problem(escaped, 6))
        self.assertIn("below the end of the test 'a'", problem(escaped, 9) or "")
        # A regex left open on its line ends there: its backtick opens no template that would run to the file's end.
        unclosed = ['test("a", () => {', "  const fence = /`", "  assert.equal(one(), 1);", "});"] + hook
        self.assertIsNone(problem(unclosed, 3))
        self.assertIn("below the end of the test 'a'", problem(unclosed, 6) or "")
        # A header wrapped past HEADER_LINES fails closed, as the README says: its own assertion is turned away too.
        wide = ['test("wide", async ({'] + ["  fixture%d," % number for number in range(_test_body.HEADER_LINES + 5)]
        wide += ["}) => {", "  assert.equal(one(), 1);", "});"] + hook
        self.assertEqual(_test_body.header_end(wide, 0), 0)
        for line in (len(wide) - 4, len(wide) - 1):
            self.assertIn("below the end of the test 'wide'", problem(wide, line, "wide") or "", line)

    def test_a_template_nested_in_a_template_expression_closes_where_it_does(self):
        def problem(spec, line):
            title = spec[0].split('"')[1]
            return proof_audit.proof_problem(Files(spec), {"file": "tests/a.test.ts", "test": title, "line": line,
                                                           "assertion": spec[line - 1].strip()})
        hook = ["afterEach(() => {", '  expect(document.body.innerHTML).toBe("");', "});"]
        # The `/` of `</li>` in the nested template is its text, not a regex that swallows the closing backtick.
        inline = ['test("renders the list", () => {',
                  '  const html = `<ul>${items.map((i) => `<li>${i}</li>`).join("")}</ul>`;',
                  '  expect(render(html)).toContain("li");', "});"] + hook
        self.assertIsNone(problem(inline, 3))
        self.assertIn("below the end of the test 'renders the list'", problem(inline, 6) or "")
        # The same at column 0 in a multi-line template stays inside the test, and a `{` in the expression nests.
        for spec in ((['test("renders the list", () => {', "  document.body.innerHTML = `", "<ul>",
                       '${items.map((i) => `<li>${i}</li>`).join("")}', "</ul>", "`;",
                       '  expect(render()).toContain("li");', "});"] + hook),
                     (['test("styles", () => {', "  const css = `", ".a { color: ${theme({ dark: true }).fg}; }", "`;",
                       '  expect(render()).toContain("li");', "});"] + hook)):
            inside = spec.index('  expect(render()).toContain("li");') + 1
            self.assertIsNone(problem(spec, inside), spec[0])
            self.assertIn("below the end of the test", problem(spec, len(spec) - 1) or "", spec[0])


if __name__ == "__main__":
    unittest.main()
