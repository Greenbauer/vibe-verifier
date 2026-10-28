"""Every action's metadata loads on GitHub: an expression where the runner evaluates it with a context it does not
offer stops the job at "Set up job" in every repository that uses the action."""
import re
import unittest

from helpers import ROOT

# Where the runner's action schema (actions/runner src/Runner.Worker/action_yaml.json) lets an expression appear
# before `runs:`, and with which contexts: an input's default and a composite output's value. A description or a
# name takes none, so an expression quoted there is written in words instead.
ALLOWED = {("inputs", "default"): {"github", "strategy", "matrix", "job", "runner", "hashFiles"},
           ("outputs", "value"): {"github", "strategy", "matrix", "steps", "inputs", "job", "runner", "env"}}
KEY = re.compile(r"^( *)([A-Za-z0-9_-]+):(.*)$")
# A value that goes on past its line: a block scalar (`>`, `|-`, `>2-`, a comment after it) or an open quote.
CONTINUES = re.compile(r"""^\s*([>|][0-9+-]*\s*(#.*)?|"([^"\\]|\\.)*|'([^']|'')*)$""")
EXPRESSION = re.compile(r"\$\{\{(.*?)\}\}")
# A context name: an identifier not after a dot (a property) and not inside a string literal. A call is a
# built-in function, allowed anywhere, except hashFiles, which the schema allows only in some places.
NAME = re.compile(r"(?<![.\w-])([A-Za-z_][A-Za-z0-9_-]*)(\s*\()?")
# A key on its own line or as a sequence item (`- name: x`), with the value after the colon.
ITEM = re.compile(r"^( *)(?:- +)?([A-Za-z0-9_-]+):(.*)$")


def names(line):
    """Every context or function an expression on the line names, each expression and operand counted."""
    found = set()
    for body in EXPRESSION.findall(line):
        found |= {name for name, call in NAME.findall(re.sub(r"'([^']|'')*'", "''", body))
                  if not call or name == "hashFiles"} - {"true", "false", "null"}
    return found


def expressions(text):
    """(section, key, contexts) for each line before `runs:` holding an expression. The key is the one the line
    belongs to, a block scalar's lines (`description: >-` and what follows) included, so a wrapped description
    line that happens to start with `default:` still counts as description."""
    found, path, block = [], [], None
    for line in text.split("\nruns:", 1)[0].splitlines():
        indent = len(line) - len(line.lstrip(" "))
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if block is not None and indent > block:
            key = path[-1]
        else:
            block, match = None, KEY.match(line)
            if match is None:
                key = path[-1] if path else ""
            else:
                depth = len(match.group(1)) // 2
                path = path[:depth] + [match.group(2)]
                key = match.group(2)
                if match.group(3).strip() and CONTINUES.match(match.group(3)):
                    block = len(match.group(1))
        if "${{" in line:
            found.append((path[0], key if len(path) >= 3 else "", names(line)))
    return found


def plain_values_with_a_colon(text):
    """Line numbers of each unquoted one-line value holding `: `. YAML reads that colon as a second key, so the
    whole file fails to load ("mapping values are not allowed in this context") and every job using the action
    stops at "Set up job". A quoted value or a block scalar (`>-`, `|`) may hold one."""
    found, block = [], None
    for number, line in enumerate(text.splitlines(), 1):
        indent = len(line) - len(line.lstrip(" "))
        if not line.strip() or line.lstrip().startswith("#") or (block is not None and indent > block):
            continue
        block, match = None, ITEM.match(line)
        if match is None:
            continue
        value = match.group(3).strip()
        if value and CONTINUES.match(value):
            block = len(match.group(1))
        elif value and value[0] not in "\"'[{&*!" and ": " in value.split(" #", 1)[0]:
            found.append(number)
    return found


class ActionMetadata(unittest.TestCase):
    def test_every_action_loads_as_yaml(self):
        for path in sorted((ROOT / "actions").glob("*/action.yml")):
            with self.subTest(action=path.parent.name):
                self.assertEqual(plain_values_with_a_colon(path.read_text()), [])

    def test_the_check_sees_a_colon_in_a_plain_value_only(self):
        self.assertEqual(plain_values_with_a_colon("name: a\ndescription: posts nothing: the verify job\n"), [2])
        self.assertEqual(plain_values_with_a_colon("steps:\n  - name: a: b\n"), [2])
        self.assertEqual(plain_values_with_a_colon('description: "posts nothing: the verify job"\n'), [])
        self.assertEqual(plain_values_with_a_colon("description: >-\n  posts nothing: the verify job\n"), [])
        self.assertEqual(plain_values_with_a_colon("run: |\n  echo a: b\nname: x # a: b\n"), [])
        self.assertEqual(plain_values_with_a_colon("default: ${{ github.event_name }}\nurl: https://x.y/z\n"), [])

    def test_every_expression_before_runs_is_one_the_runner_evaluates(self):
        actions = sorted((ROOT / "actions").glob("*/action.yml"))
        self.assertTrue(actions)
        for path in actions:
            with self.subTest(action=path.parent.name):
                bad = [(section, key, sorted(names)) for section, key, names in expressions(path.read_text())
                       if not names <= ALLOWED.get((section, key), set())]
                self.assertEqual(bad, [])

    def test_the_check_sees_an_expression_in_each_place_it_does_not_belong(self):
        wrapped = "inputs:\n  a:\n    description: >-\n      say\n      default: ${{ runner.temp }}\n    default: x\n"
        self.assertEqual(expressions(wrapped), [("inputs", "description", {"runner"})])
        self.assertEqual(expressions('inputs:\n  a:\n    description: "x ${{ runner.temp }}"\n'),
                         [("inputs", "description", {"runner"})])
        self.assertEqual(expressions("inputs:\n  a:\n    default: ${{ steps.s.outputs.o }}\n"),
                         [("inputs", "default", {"steps"})])
        self.assertEqual(expressions("outputs:\n  o:\n    value: ${{ steps.s.outputs.o }}\n# ${{ runner.temp }}\n"),
                         [("outputs", "value", {"steps"})])
        for opening in ('>2-', '>- # note', '"starts'):
            text = "inputs:\n  a:\n    description: %s\n      default: ${{ runner.temp }}\n" % opening
            self.assertEqual(expressions(text), [("inputs", "description", {"runner"})], opening)
        self.assertEqual(expressions("inputs:\n  a:\n    default: ${{ github.x || steps.s.outputs.o == 'a.b' }}\n"),
                         [("inputs", "default", {"github", "steps"})])
        self.assertEqual(expressions("inputs:\n  a:\n    default: ${{ format('{0}', github.x) }} ${{ hashFiles('a') }}\n"),
                         [("inputs", "default", {"github", "hashFiles"})])


if __name__ == "__main__":
    unittest.main()
