"""proof-audit: the body bound (gates/_test_body.py) on the declaration, comment and string shapes formatted test
files hold. Each shape has a line inside the test, which a grade may quote, or a hook or helper line after it, which
it may not; every case cites its line exactly, and the inside ones once more off by two."""
import sys
import unittest

from helpers import ROOT
from test_proof_audit_quotes import Files

sys.path.insert(0, str(ROOT / "gates"))
import proof_audit  # noqa: E402

HOOK = ["afterEach(() => {", "  expect(cleanupHookOnly()).toBe(true);", "});"]
HELPER = ["", "    def helper(self):", "        self.assertEqual(hook_only(), 1)"]
INSIDE = "  expect(one()).toBe(1);"
PY_INSIDE = "        self.assertEqual(one(), 1)"
PY_ASSERT = "        assert one() == 1"

# (what the shape is, the file's lines, the title): a line equal to INSIDE, PY_INSIDE or PY_ASSERT is inside the test,
# and a line calling a hook-only function is in the hook or helper after it.
SHAPES = [
    ("a body opening on the declaration's line after a regex holding a bracket",
     ['test("a", () => { const r = /\\(/;', INSIDE, "});"] + HOOK, "a"),
    ("a wrapped declaration with a body longer than the header search",
     ['test("a", async ({', "  page,", "}) => {"] + ["  await step%d();" % n for n in range(45)] + [INSIDE, "});"]
     + HOOK, "a"),
    ("a wrapped expression arrow whose title holds a bracket",
     ["test(", '  "renders a ( card",', "  () => expect(card()).toBe(1),", "  30_000,", ");"] + HOOK,
     "renders a ( card"),
    ("a block comment holding brackets in a wrapped declaration",
     ['test("a", /* ))) */ async ({', "  page,", "}) => {", INSIDE, "});"] + HOOK, "a"),
    ("a block comment holding `) {` on a wrapped declaration's line",
     ['test("a", /* f() { */ async ({', "  page,", "}) => {", INSIDE, "});"] + HOOK, "a"),
    ("a title holding `) {` on a wrapped declaration's line",
     ['test("calls f() {", async ({', "  page,", "}) => {", INSIDE, "});"] + HOOK, "calls f() {"),
    ("a callback passed by name", ["test(", '  "runs the scenario",', "  runTheSharedScenario,", ");"] + HOOK,
     "runs the scenario"),
    ("a named function as the callback",
     ['test("a", async function named({ page }) {', INSIDE, "});"] + HOOK, "a"),
    ("a test nested in describe blocks",
     ['describe("x", () => {', '  describe("y", () => {', '    test("a", () => {', "      const v = one();",
      "      expect(one()).toBe(1);", "    });", "    afterEach(() => {", "      expect(cleanupHookOnly()).toBe(true);",
      "    });", "  });", "});"], "a"),
    ("a column-0 block comment line", ['test("a", () => {', "/* eslint-disable */", INSIDE, "});"] + HOOK, "a"),
    ("a block comment whose close is at column 0", ['test("a", () => {', "  /*", "  old", "*/", INSIDE, "});"] + HOOK,
     "a"),
    ("a block comment holding a backtick", ['test("a", () => {', "  x /* the ` key */;", INSIDE, "});"] + HOOK, "a"),
    ("a regex after a comma holding a backtick", ['test("a", () => {', "  assert.match(out, /^`{3}/);", INSIDE, "});"]
     + HOOK, "a"),
    ("a division after a bracket before a template with column-0 lines",
     ['test("a", () => {', "  const half = (a + b) / 2, sql = `", "select 1", "`;", INSIDE, "});"] + HOOK, "a"),
    ("a template opened after a closed block comment",
     ['test("a", () => {', "  /* note */ const sql = `", "select 1", "`;", INSIDE, "});"] + HOOK, "a"),
    ("a line comment holding a backtick", ['test("a", () => {', "  // press the ` key", INSIDE, "});"] + HOOK, "a"),
    ("an escaped / in a regex before a backtick",
     ['test("a", () => {', "  expect(s).toMatch(/a\\/`/);", INSIDE, "});"] + HOOK, "a"),
    ("a Python comment holding a triple quote",
     ["class A(T):", "    def test_a(self):", '        # strip the """ marker', PY_INSIDE] + HELPER, "test_a"),
    ("a Python ''' string with column-0 lines",
     ["class A(T):", "    def test_a(self):", "        s = '''", "x", "'''", PY_INSIDE] + HELPER, "test_a"),
    ("a Python lambda's positional-only slash before a triple-quoted string",
     ["class A(T):", "    def test_a(self):", "        render = lambda value, /: '''", "<p>x</p>", "'''", PY_INSIDE]
     + HELPER, "test_a"),
    ("a Python def with a return annotation", ["class A(T):", "    def test_a(self) -> None:", PY_INSIDE] + HELPER,
     "test_a"),
    ("a wrapped Python def with a comment holding a bracket",
     ["class A(T):", "    def test_a(", "        self,  # the (", "    ):", PY_ASSERT] + HELPER, "test_a"),
]


def problem(lines, title, line):
    path = "tests/test_a.py" if lines[0].startswith("class") else "tests/a.test.ts"
    proof = {"file": path, "test": title, "line": line, "assertion": lines[line - 1].strip() if line <= len(lines)
             else ""}
    return proof_audit.proof_problem(Files(lines), proof)


class Shapes(unittest.TestCase):
    def test_each_shape_keeps_its_own_lines_and_turns_away_the_hook_after_it(self):
        for shape, lines, title in SHAPES:
            inside = [n for n, text in enumerate(lines, 1) if text.strip() in (INSIDE.strip(), PY_INSIDE.strip(), PY_ASSERT.strip())]
            after = [n for n, text in enumerate(lines, 1) if "hook_only()" in text or "cleanupHookOnly()" in text]
            for line in inside:
                self.assertIsNone(problem(lines, title, line), shape)
                drifted = dict(file="tests/test_a.py" if lines[0].startswith("class") else "tests/a.test.ts",
                               test=title, line=line - 2, assertion=lines[line - 1].strip())
                self.assertIsNone(proof_audit.proof_problem(Files(lines), drifted), shape)
                self.assertEqual(drifted["line"], line, shape)
            for line in after:
                self.assertIn("below the end of the test '%s'" % title, problem(lines, title, line) or "", shape)


if __name__ == "__main__":
    unittest.main()
