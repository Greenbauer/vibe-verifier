"""The catalog is public and its templates are copied verbatim into other repositories, so nothing
in it may cite another organization's repository. A pull request or issue citation of the form
`<owner>/<repo>#<n>` (or with a space before the `#`) may name only this catalog's owner or the
public vendors whose actions it pins. Found the hard way on 2026-09-24, when a fix comment cited a
private wrapper's pull request and three consumer repositories received the line."""
import pathlib
import re
import subprocess
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
ALLOWED_OWNERS = {"Greenbauer", "actions", "anthropics", "oven-sh", "supabase", "github", "openai"}
CITATION = re.compile(r"(?<![\w./-])([A-Za-z0-9][A-Za-z0-9-]*)/([A-Za-z0-9._-]+) ?#(\d+)")


def tracked_text_files():
    out = subprocess.run(["git", "-C", str(ROOT), "ls-files"], capture_output=True, text=True, check=True).stdout
    for name in out.split("\n"):
        path = ROOT / name
        if not name or not path.is_file() or path.suffix in (".png", ".jpg", ".lock", ".gz"):
            continue
        yield name, path


def foreign_citations(text):
    return [m.group(0) for m in CITATION.finditer(text) if m.group(1) not in ALLOWED_OWNERS]


class PublicSurface(unittest.TestCase):
    def test_no_file_cites_another_organizations_repository(self):
        hits = []
        for name, path in tracked_text_files():
            try:
                text = path.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                continue
            for cite in foreign_citations(text):
                hits.append(f"{name}: {cite}")
        self.assertEqual(hits, [], "a public template or doc cites another organization's repository:\n" + "\n".join(hits))

    def test_the_pattern_catches_the_citation_that_leaked(self):
        self.assertEqual(foreign_citations("(review finding on someorg/ci #13, 2026-09-24)"), ["someorg/ci #13"])
        self.assertEqual(foreign_citations("fixed in Greenbauer/vibe-verifier#16 and actions/checkout#1"), [])
