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
NAMES = re.compile(r"\$\{\{\s*([A-Za-z_]+)")


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
                if match.group(3).strip() in (">", ">-", "|", "|-", ">+", "|+"):
                    block = len(match.group(1))
        if "${{" in line:
            found.append((path[0], key if len(path) >= 3 else "", set(NAMES.findall(line))))
    return found


class ActionMetadata(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
