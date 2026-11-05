"""proof-audit: the gate's reading of a test file, held to Python's own parser over every Python test in this
repository. Its quote checks read test source by hand (gates/_test_body.py: where a test is declared, where its
body ends, which short lines are values), and each past fix to them (#222, #225, #265) was a shape the hand-written
cases had not met. Here `ast` says which test function each assertion line sits in, and the gate must agree on
every one: a line inside a test, cited with that test's title, is proof at its line and at a nearby wrong line;
a line in a helper or hook after a test is never that test's proof."""
import ast
import re
import subprocess
import sys
import unittest

from helpers import ROOT

sys.path.insert(0, str(ROOT / "gates"))
import proof_audit  # noqa: E402

DRIFT = (3, -2)  # how far off a grader cites an assertion it has right
# What the grader is told never to quote (harnesses/proof-audit/prompt.md): a line of closers, or the line that only
# opens the call. Every other line of an assertion holds what it checks.
CLOSERS = re.compile(r"^[)\]},;:\s]*$")
OPENS_ONLY = re.compile(r"^[\w.]+\($")


class Files:
    """The gate's Repository, read from the working tree."""

    def lines(self, ref, path):
        return (ROOT / path).read_text(encoding="utf-8").split("\n")


def python_tests():
    """Every tracked Python test file, as the gate knows one."""
    listed = subprocess.run(["git", "-C", str(ROOT), "ls-files", "*.py"], capture_output=True, text=True, check=True)
    files = Files()
    return [path for path in listed.stdout.split() if proof_audit.is_test_file(path, files.lines("HEAD", path))]


def assertions(function):
    """The assertion statements whose innermost function is `function`: `assert ...` and `self.assert*(...)`."""
    found, stack = [], list(function.body)
    while stack:
        node = stack.pop()
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)):
            continue
        if isinstance(node, ast.Assert) or (
                isinstance(node, ast.Expr) and isinstance(node.value, ast.Call)
                and isinstance(node.value.func, ast.Attribute) and node.value.func.attr.startswith("assert")):
            found.append(node)
            continue
        stack.extend(ast.iter_child_nodes(node))
    return found


def strings(tree):
    """The line numbers inside a string literal that spans lines (the gate's quote is a line of code)."""
    inside = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Constant, ast.JoinedStr)) and node.end_lineno > node.lineno:
            inside.update(range(node.lineno, node.end_lineno + 1))
    return inside


def quotable(text):
    """A line a grader is told to quote: one that holds a value, not a blank, comment, opener or closers line."""
    return text and not text.startswith("#") and not CLOSERS.match(text) and not OPENS_ONLY.match(text)


def cases(path):
    """(lines, tests, inside, outside) for one file: its lines, its test functions in order, inside, (line, title,
    text) for each quotable line of an assertion in a test function, and outside, (line, title) for each such line in
    a helper or hook (nested or not) that is in no test and has a test above it, with that test's title."""
    lines = Files().lines("HEAD", path)
    tree = ast.parse("\n".join(lines))
    defined = [node for node in ast.walk(tree) if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))]
    tests = sorted((node for node in defined if node.name.startswith("test")), key=lambda node: node.lineno)
    in_string = strings(tree)
    inside, outside = [], []
    for function in defined:
        for statement in assertions(function):
            for number in range(statement.lineno, statement.end_lineno + 1):
                text = lines[number - 1].strip()
                if number in in_string or not quotable(text):
                    continue
                if function.name.startswith("test"):
                    inside.append((number, function.name, text))
                    continue
                above = [test for test in tests if test.lineno < number and test.end_lineno < number]
                if above and not any(test.lineno < number <= test.end_lineno for test in tests):
                    outside.append((number, above[-1].name))
    return lines, tests, inside, outside


def problem(files, path, number, title, text):
    proof = {"file": path, "line": number, "test": title, "assertion": text}
    return proof_audit.proof_problem(files, proof), proof["line"]


class AstTruth(unittest.TestCase):
    """Each subTest names the file and line, so a failure points at the shape the gate misread."""

    @classmethod
    def setUpClass(cls):
        cls.files = Files()
        cls.cases = {path: cases(path) for path in python_tests()}

    def test_the_corpus_has_every_shape(self):
        inside = sum(len(case[2]) for case in self.cases.values())
        outside = sum(len(case[3]) for case in self.cases.values())
        short = sum(1 for case in self.cases.values() for _, _, text in case[2] if len(text) < proof_audit.MIN_QUOTE)
        self.assertGreater(len(self.cases), 100)
        self.assertGreater(inside, 5000)
        self.assertGreater(outside, 20)
        self.assertGreater(short, 4)

    def test_an_assertion_line_in_a_test_is_proof_for_that_test_at_its_line(self):
        for path, (_, _, inside, _) in self.cases.items():
            for number, title, text in inside:
                with self.subTest(at="%s:%d" % (path, number)):
                    self.assertIsNone(problem(self.files, path, number, title, text)[0])

    def test_an_assertion_cited_a_few_lines_off_is_proof_where_it_sits(self):
        for path, (lines, tests, inside, _) in self.cases.items():
            names = [test.name for test in tests]
            spans = {test.name: (test.lineno, test.end_lineno) for test in tests}
            for index, (number, title, text) in enumerate(inside):
                first, last = spans[title]
                body = [line.strip() for line in lines[first:last]]
                cited = number + DRIFT[index % len(DRIFT)]
                if names.count(title) > 1 or body.count(text) > 1 or not 1 <= cited <= len(lines):
                    continue  # the gate refuses a quote it cannot place once: a shared title, a repeated line
                if lines[cited - 1].strip() == text:
                    continue
                with self.subTest(at="%s:%d" % (path, number), cited=cited):
                    self.assertEqual(problem(self.files, path, cited, title, text), (None, number))

    def test_an_assertion_in_a_helper_or_hook_after_a_test_is_never_that_tests_proof(self):
        for path, (lines, _, _, outside) in self.cases.items():
            for number, title in outside:
                with self.subTest(at="%s:%d" % (path, number), test=title):
                    self.assertIsNotNone(problem(self.files, path, number, title, lines[number - 1].strip())[0])


if __name__ == "__main__":
    unittest.main()
