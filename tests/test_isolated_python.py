"""Every inline Python a catalog action, template or workflow runs (`python3 - <<'PY'`, `python3 -c '...'`) runs
isolated (`-I`). Without it Python puts the working directory on sys.path, and these steps run in a pull request's
checkout while holding a secret (a plan store's token, a Codex login, a test seat's session): a `json.py` the pull
request commits would run with it."""
import re
import unittest

from helpers import ROOT

INLINE = re.compile(r"\bpython3?((?:[ \t]+-[A-Za-z]+)*?)[ \t]+(?:-c\b|-(?=[ \t]|$))", re.MULTILINE)
SOURCES = ("actions/**/*.yml", "harnesses/**/*.yml", "harnesses/**/*.md", ".github/workflows/*.yml")


def unisolated(text):
    """The 1-based line numbers of each inline Python run without -I among its flags."""
    return [text.count("\n", 0, match.start()) + 1 for match in INLINE.finditer(text)
            if not any("I" in flag.lstrip("-") for flag in match.group(1).split())]


class IsolatedPython(unittest.TestCase):

    def test_every_inline_python_runs_isolated(self):
        found = {str(path.relative_to(ROOT)): unisolated(path.read_text()) for pattern in SOURCES
                 for path in ROOT.glob(pattern)}
        self.assertEqual({path: lines for path, lines in found.items() if lines}, {})

    def test_the_check_tells_isolated_from_unisolated(self):
        self.assertEqual(unisolated("python3 - \"$a\" <<'PY'\npython3 -c 'x'\npython -c 'y'\n"), [1, 2, 3])
        self.assertEqual(unisolated("python3 -I - <<'PY'\npython3 -I -c 'x'\npython3 -Ic 'y'\n"), [])
        self.assertEqual(unisolated("python3 \"$GITHUB_ACTION_PATH/ticket.py\" --body x\npython3 -m pytest\n"), [])
