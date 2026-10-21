"""Every action's metadata loads on GitHub: an expression outside the places GitHub evaluates it with a context
it has stops the job at "Set up job" in every repository that uses the action."""
import re
import unittest

from helpers import ROOT

# Before `runs:`, GitHub evaluates `${{ }}` everywhere, descriptions included, and only `github`, `inputs`
# and the like exist there, never `runner`. A step output's `value:` and an input's `default:` are the
# places an expression belongs; anywhere else (a description quoting one, say) is written in words instead.
EXPRESSION_LINE = re.compile(r"^\s*(value|default):")


class ActionMetadata(unittest.TestCase):
    def test_no_action_puts_an_expression_in_its_descriptions(self):
        actions = sorted((ROOT / "actions").glob("*/action.yml"))
        self.assertTrue(actions)
        for path in actions:
            head = path.read_text().split("\nruns:", 1)[0]
            stray = [line.strip() for line in head.splitlines() if "${{" in line and not EXPRESSION_LINE.match(line)]
            with self.subTest(action=path.parent.name):
                self.assertEqual(stray, [])


if __name__ == "__main__":
    unittest.main()
