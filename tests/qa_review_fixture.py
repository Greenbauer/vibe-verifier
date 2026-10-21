"""The fixture the `vibe-verifier qa-review` tests share: a fake `gh` (tests/fake_gh.py) that records every write,
a PR body and a verdict, and the command run against them."""
import json
import os
import shutil
import stat
import tempfile
import unittest
from pathlib import Path

from helpers import ROOT, runner

MARKER = "<!-- vibe-verifier:qa-review -->"
BOT = "github-actions[bot]"
HEAD = "0123456789abcdef0123456789abcdef01234567"
RUN = "https://github.com/acme/site/actions/runs/42"
BODY = "## Why\n\nx\n\n## Acceptance criteria\n\n- The home page loads\n- The menu opens\n"
VERDICT = ("acceptance-check: AC1 -- PASS -- loaded (qae/AC1.md::step 1: opened / -> the home page)\n"
           "acceptance-check: AC2 -- FAIL -- the menu stayed shut (qae/AC2.md::step 1: clicked Menu -> nothing)\n")


class GhFixture(unittest.TestCase):
    def setUp(self):
        self.fixtures = tempfile.mkdtemp(prefix="vv-gh-")
        self.addCleanup(shutil.rmtree, self.fixtures, True)
        bin_dir = Path(self.fixtures) / "bin"
        bin_dir.mkdir()
        gh = bin_dir / "gh"
        gh.write_text("#!/bin/sh\nexec %s %s \"$@\"\n" % (os.environ.get("PYTHON", "python3"), ROOT / "tests" / "fake_gh.py"))
        gh.chmod(gh.stat().st_mode | stat.S_IEXEC)
        self.env = {"PATH": "%s:%s" % (bin_dir, os.environ["PATH"]), "FAKE_GH_ROOT": self.fixtures}
        (Path(self.fixtures) / "acme" / "site").mkdir(parents=True)
        self.criteria = self.file("pr-body.md", BODY)
        self.verdict = self.file("verdict.md", VERDICT)

    def file(self, name, text):
        path = Path(self.fixtures) / name
        path.write_text(text)
        return str(path)

    def comments(self, comments):
        (Path(self.fixtures) / "acme" / "site" / ".comments.json").write_text(json.dumps(comments))

    def calls(self):
        log = Path(self.fixtures) / "calls.log"
        return [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []

    def writes(self):
        return [c for c in self.calls() if c["method"] != "GET"]

    def review(self, explore="success", gates="success", not_required="", verdict=True, ticket=None, carried="", run_id="42",
               extra=()):
        args = ["qa-review", "--repo", "acme/site", "--pr", "7", "--head", HEAD, "--run-url", RUN,
                "--criteria", self.criteria, "--explore-result", explore, "--gates-outcome", gates,
                "--not-required", not_required, "--carried-from", carried, "--run-id", run_id]
        args += (["--ticket", ticket] if ticket else []) + list(extra)
        return runner(*args, *(["--verdict", self.verdict] if verdict else []), env=self.env)

    def posted(self, **kwargs):
        (Path(self.fixtures) / "calls.log").unlink(missing_ok=True)
        result = self.review(**kwargs)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        writes = self.writes()
        self.assertEqual(len(writes), 1, writes)
        return writes[0]["body"]["body"]
