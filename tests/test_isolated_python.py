"""Every inline Python a catalog action, template or workflow runs (a script read from stdin or `-c`) runs isolated
(`-I`). Without it Python puts the working directory on sys.path, and these steps run in a pull request's checkout
while holding a secret (a plan store's token, a Codex login, a test seat's session): a `json.py` the pull request
commits would run with it. `-m` puts the working directory there too; it is allowed only in the catalog's own
workflows, which test the catalog's own code and hold no secret."""
import re
import unittest

from helpers import ROOT

PYTHON = re.compile(r"(?<![\w/.$-])python(?:3(?:\.\d+)?)?(?![\w.-])")
TAKES_VALUE = {"-X", "-W", "-Q"}
SHELL = re.compile(r"^(<|>|\||;|&|\)|\$\(|`)")
SOURCES = ("actions/**/*.yml", "actions/**/*.md", "harnesses/**/*.yml", "harnesses/**/*.md", ".github/workflows/*.yml")


def runs(text, markdown):
    """(line number, the rest of the line after `python`) for each Python invocation: YAML comment lines and, in
    Markdown, text outside code fences are prose, and a line ending in a backslash continues onto the next."""
    lines, fenced, out = text.split("\n"), False, []
    joined = []
    for number, line in enumerate(lines, 1):
        if markdown and line.lstrip().startswith(("```", "~~~")):
            fenced = not fenced
            continue
        if (markdown and not fenced) or (not markdown and line.lstrip().startswith("#")):
            continue
        if joined and joined[-1][1].endswith("\\"):
            joined[-1] = (joined[-1][0], joined[-1][1][:-1] + " " + line.strip())
        else:
            joined.append((number, line))
    for number, line in joined:
        out += [(number, line[match.end():]) for match in PYTHON.finditer(line)]
    return out


def mode(rest):
    """(isolated, how the script is given: "stdin", "c", "m" or "path") from the words after `python`."""
    words, isolated, skip = rest.split(), False, False
    for word in words:
        if skip:
            skip = False
            continue
        if word in TAKES_VALUE:
            skip = True
            continue
        if word == "-" or SHELL.match(word):
            return isolated, "stdin"
        if word.startswith("-") and not word.startswith("--"):
            letters = word[1:]
            for at, letter in enumerate(letters):
                if letter == "I":
                    isolated = True
                if letter in "cm":
                    return isolated, letter
                if letter in "XWQ":  # a value glued on, `-Xutf8`, or the next word
                    skip = at == len(letters) - 1
                    break
            continue
        if word.startswith("--"):
            continue
        return isolated, "path"
    return isolated, "stdin"


def unisolated(text, markdown=False, own_workflow=False):
    """The 1-based line numbers of each inline Python run without -I."""
    found = []
    for number, rest in runs(text, markdown):
        isolated, how = mode(rest)
        if not isolated and (how in ("stdin", "c") or (how == "m" and not own_workflow)):
            found.append(number)
    return found


class IsolatedPython(unittest.TestCase):

    def test_every_inline_python_runs_isolated(self):
        found = {}
        for pattern in SOURCES:
            for path in ROOT.glob(pattern):
                name = str(path.relative_to(ROOT))
                lines = unisolated(path.read_text(), markdown=name.endswith(".md"),
                                   own_workflow=name.startswith(".github/workflows/"))
                if lines:
                    found[name] = lines
        self.assertEqual(found, {})

    def test_the_check_tells_isolated_from_unisolated(self):
        flagged = ["python3 - \"$a\" <<'PY'", "python3 -c 'x'", "python -c 'y'", "python3 <<'PY'", "cat s | python3",
                   "python3.12 -c 'x'", "python3 -uc 'x'", "python3 -X utf8 -c 'x'", "python3 -W ignore -",
                   "python3 -Xutf8 -c 'x'", "env A=1 python3 - <<'PY'", "uv run python -c 'x'", "x=$(python3 -c 'y')",
                   "python3 -m pytest"]
        for line in flagged:
            with self.subTest(line):
                self.assertEqual(unisolated(line), [1])
        clean = ["python3 -I - <<'PY'", "python3 -I -c 'x'", "python3 -Ic 'y'", "python3 -X utf8 -I -c 'x'",
                 "python3 \"$GITHUB_ACTION_PATH/ticket.py\" --body x", "python3 -u bin/vibe-verifier run",
                 "# runs python3 - for the table", "x=$(python3 -I -c 'y')", "the PYTHON3 path", "/usr/bin/python3x"]
        for line in clean:
            with self.subTest(line):
                self.assertEqual(unisolated(line), [])
        self.assertEqual(unisolated("python3 \\\n  -c 'x'"), [1])
        self.assertEqual(unisolated("python3 -m unittest", own_workflow=True), [])
        self.assertEqual(unisolated("Run `python3 - <<PY` here.\n```\npython3 -c 'x'\n```\n", markdown=True), [3])
