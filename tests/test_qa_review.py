"""`vibe-verifier qa-review` and actions/qa-review: the QAE's one review comment on a pull request,
driven through the command line (and the action's own script) against a fake `gh` that records writes."""
import json
import os
import re
import shutil
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path

from helpers import ROOT, clean_env, runner

MARKER = "<!-- vibe-verifier:qa-review -->"
BOT = "github-actions[bot]"
HEAD = "0123456789abcdef0123456789abcdef01234567"
RUN = "https://github.com/acme/site/actions/runs/42"
BODY = "## Why\n\nx\n\n## Acceptance criteria\n\n- The home page loads\n- The menu opens\n"
VERDICT = ("acceptance-check: AC1 -- PASS -- loaded (qae/AC1.md::step 1: opened / -> the home page)\n"
           "acceptance-check: AC2 -- FAIL -- the menu stayed shut (qae/AC2.md::step 1: clicked Menu -> nothing)\n")
ACTION = ROOT / "actions" / "qa-review" / "action.yml"


class QaReview(unittest.TestCase):
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

    def review(self, explore="success", gates="success", not_required="", verdict=True):
        args = ["qa-review", "--repo", "acme/site", "--pr", "7", "--head", HEAD, "--run-url", RUN,
                "--criteria", self.criteria, "--explore-result", explore, "--gates-outcome", gates,
                "--not-required", not_required]
        return runner(*args, *(["--verdict", self.verdict] if verdict else []), env=self.env)

    def posted(self, **kwargs):
        (Path(self.fixtures) / "calls.log").unlink(missing_ok=True)
        result = self.review(**kwargs)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        writes = self.writes()
        self.assertEqual(len(writes), 1, writes)
        return writes[0]["body"]["body"]

    # What the comment says, per state. The first line is the marker and the second the heading,
    # always: that is how a reader and the next run find it.

    def test_passed(self):
        body = self.posted()
        self.assertEqual(body.splitlines()[:2], [MARKER, "## QA review: passed ✅"])
        self.assertIn("`01234567`", body)
        self.assertIn("- AC1, explorer says PASS: The home page loads\n", body)
        self.assertIn("[The run and its evidence](%s)" % RUN, body)

    def test_changes_needed_lists_each_criterion_as_the_explorer_judged_it(self):
        body = self.posted(gates="failure")
        self.assertEqual(body.splitlines()[1], "## QA review: changes needed ❌")
        self.assertIn("The QA gates refused `01234567`.", body)
        self.assertIn("- AC1, explorer says PASS: The home page loads\n", body)
        self.assertIn("- AC2, explorer says FAIL: The menu opens\n", body)

    def test_a_criterion_the_explorer_never_answered_says_so(self):
        self.verdict = self.file("verdict.md", VERDICT.splitlines()[0] + "\n")
        self.assertIn("- AC2, no verdict: The menu opens\n", self.posted(gates="failure"))

    def test_a_pull_request_with_no_criteria_is_told_how_to_add_them(self):
        # The criteria job found nothing to walk, so the explore job was skipped and the gates judged.
        self.criteria = self.file("pr-body.md", "## Why\n\nchanges the home page\n")
        body = self.posted(explore="skipped", gates="failure", verdict=False)
        self.assertEqual(body.splitlines()[1], "## QA review: changes needed ❌")
        self.assertIn("lists no acceptance criteria", body)
        self.assertIn("`- None: <why>`", body)

    def test_not_required_gives_the_reason(self):
        reason = "every changed file (3) is CI configuration or documentation no site renders"
        body = self.posted(gates="skipped", not_required=reason, verdict=False)
        self.assertEqual(body.splitlines()[1], "## QA review: not required ✅")
        self.assertIn("No browser check on `01234567`: %s." % reason, body)
        self.assertNotIn("AC1", body)

    def test_could_not_run_when_the_explore_job_did_not_finish(self):
        body = self.posted(explore="failure", gates="failure")
        self.assertEqual(body.splitlines()[1], "## QA review: could not run ⚠️")
        self.assertIn("The explore job ended `failure` on `01234567`", body)

    # Which state wins: the order is not-required, then a failed explore, then the gates.

    def test_not_required_beats_a_failed_explore(self):
        self.assertIn("not required", self.posted(explore="failure", gates="skipped", not_required="a pin bump", verdict=False))

    def test_a_failed_explore_beats_green_gates(self):
        self.assertIn("could not run", self.posted(explore="failure", gates="success"))

    def test_gates_that_never_ran_could_not_run(self):
        # An earlier verify step failed (an API error, a missing artifact): nothing was judged, and the
        # comment must not say the gates refused it.
        for outcome in ("skipped", ""):
            body = self.posted(gates=outcome)
            self.assertEqual(body.splitlines()[1], "## QA review: could not run \u26a0\ufe0f", outcome)
            self.assertIn("The verify job stopped before its gates ran on `01234567`", body)
            self.assertNotIn("refused", body)

    def test_a_skipped_explore_whose_criteria_job_failed_could_not_run(self):
        # The verify job's first step refuses an unfinished criteria job, so its gates never ran. The
        # comment blames that, never the skipped explore job.
        body = self.posted(explore="skipped", gates="", verdict=False)
        self.assertEqual(body.splitlines()[1], "## QA review: could not run \u26a0\ufe0f")
        self.assertIn("The verify job stopped before its gates ran on `01234567`", body)
        self.assertNotIn("explore job ended", body)

    def test_a_missing_verdict_file_reads_as_no_verdict(self):
        self.verdict = str(Path(self.fixtures) / "never-written.md")
        self.assertIn("- AC1, no verdict: The home page loads\n", self.posted(gates="failure"))

    def test_the_explorers_prose_never_decides(self):
        # Every line PASS, but the gates refused (an anchor that does not resolve): changes needed.
        self.verdict = self.file("verdict.md", VERDICT.replace("FAIL", "PASS"))
        self.assertIn("changes needed", self.posted(gates="failure"))

    # One comment per pull request.

    def test_the_first_run_posts_one_comment(self):
        self.comments([{"id": 1, "user": {"login": "someone"}, "body": "looks good"}])
        self.posted()
        self.assertEqual(self.writes()[0]["method"], "POST")
        self.assertEqual(self.writes()[0]["endpoint"], "repos/acme/site/issues/7/comments")

    def test_a_later_run_edits_its_comment_in_place(self):
        self.comments([{"id": 5, "user": {"login": BOT}, "body": VERDICT},
                       {"id": 9, "user": {"login": BOT}, "body": MARKER + "\n## QA review: changes needed ❌\n"}])
        self.posted()
        self.assertEqual([(c["method"], c["endpoint"]) for c in self.writes()],
                         [("PATCH", "repos/acme/site/issues/comments/9")])

    def test_it_finds_its_comment_on_a_later_page(self):
        filler = [{"id": n, "user": {"login": "someone"}, "body": "x"} for n in range(100)]
        self.comments(filler + [{"id": 777, "user": {"login": BOT}, "body": MARKER + "\nold\n"}])
        self.posted()
        self.assertEqual(self.writes()[0]["endpoint"], "repos/acme/site/issues/comments/777")

    def test_it_never_edits_a_comment_it_did_not_post(self):
        # Another account copying the marker, and the explorer's own verdict comment, are left alone.
        self.comments([{"id": 3, "user": {"login": "someone"}, "body": MARKER + "\n## QA review: passed ✅\n"},
                       {"id": 4, "user": {"login": BOT}, "body": "verdict\n" + MARKER}])
        self.posted()
        self.assertEqual(self.writes()[0]["method"], "POST")

    def test_gh_failing_is_exit_2_and_writes_nothing(self):
        (Path(self.fixtures) / "acme" / "site" / ".broken").write_text("")
        result = self.review()
        self.assertEqual(result.returncode, 2)
        self.assertIn("could not post the QA review", result.stderr)
        self.assertEqual(self.writes(), [])

    # The composite action is what a consumer pins: its script passes every input through.

    def test_the_action_posts_with_the_run_it_belongs_to(self):
        text = ACTION.read_text()
        script = re.search(r"^      run: \|\n((?:        .*\n|\n)+)", text, re.MULTILINE).group(1)
        script = "".join(line[8:] if line.strip() else "\n" for line in script.splitlines(True))
        action_path = ROOT / "actions" / "qa-review"
        env = clean_env(dict(self.env, GITHUB_ACTION_PATH=str(action_path), GH_TOKEN="x", VV_REPO="acme/site",
                             VV_PR="7", VV_HEAD=HEAD, VV_RUN=RUN, VV_CRITERIA=self.criteria, VV_VERDICT=self.verdict,
                             VV_EXPLORE="success", VV_GATES="failure", VV_NOT_REQUIRED=""))
        result = subprocess.run(["bash", "-e", "-c", script], capture_output=True, text=True, env=env)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        body = self.writes()[0]["body"]["body"]
        self.assertIn("changes needed", body)
        self.assertIn("- AC2, explorer says FAIL: The menu opens\n", body)
        for wired in ("${{ github.event.pull_request.head.sha }}", "${{ inputs.not-required }}", "${{ inputs.gates-outcome }}",
                      "${{ github.server_url }}/${{ github.repository }}/actions/runs/${{ github.run_id }}"):
            self.assertIn(wired, text)


if __name__ == "__main__":
    unittest.main()
