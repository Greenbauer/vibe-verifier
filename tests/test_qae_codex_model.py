"""The Codex template's explorer names its model rather than taking the runner login's default
(harnesses/qae/README.md, "Caller-owned model settings")."""
import re
import unittest

from helpers import ROOT

CODEX = ROOT / "harnesses" / "qae" / "explore-codex.yml"


class Model(unittest.TestCase):
    def test_the_explorer_names_sol_at_low_effort(self):
        step = CODEX.read_text().split("      - name: Explore the acceptance criteria in a real browser\n", 1)[1]
        step = step.split("\n      - name: ", 1)[0]
        self.assertEqual(dict(re.findall(r"^ +(model|reasoning-effort): (\S+)$", step, re.M)),
                         {"model": "gpt-6.1-sol", "reasoning-effort": "low"})


if __name__ == "__main__":
    unittest.main()
