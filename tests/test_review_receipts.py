"""The review receipt (docs/comments.md): one comment per pull request, edited in place, with a hidden receipt
line per run. The workflow posts it; its scope step and verify job read receipts only from it (or from a comment
that is one receipt, as a copy made before it posts), never from another comment that quotes one."""
import json
import os
import re
import subprocess
import tempfile
import unittest
from pathlib import Path

import test_review_harness as harness  # its helpers only: a TestCase imported by name would run its tests here too
from helpers import ROOT, clean_env, commit, gate


def flaky_gh(directory, failures):
    """A PATH directory whose `gh` fails `failures` times, then succeeds, logging each call, and whose
    `sleep` returns at once and logs what it was asked to wait."""
    os.makedirs(directory, exist_ok=True)
    scripts = {"gh": '#!/bin/sh\necho call >> "%s/calls"\n[ "$(wc -l < "%s/calls")" -gt %d ]\n' % (directory, directory, failures),
               "sleep": '#!/bin/sh\necho "$1" >> "%s/sleeps"\n' % directory}
    for name, body in scripts.items():
        with open(os.path.join(directory, name), "w") as handle:
            handle.write(body)
        os.chmod(os.path.join(directory, name), 0o755)
    return directory


def lines(path):
    return Path(path).read_text().split() if os.path.exists(path) else []


HEAD, OLD = "a" * 40, "b" * 40


def comment(sha, mode="full", run=1, login="github-actions[bot]"):
    """A receipt as a copy of the workflow made before the receipt comment posts it: a comment per run."""
    return {"user": {"login": login}, "body": "review-receipt: %s -- %s -- run %d" % (sha, mode, run)}


def receipts(*runs):
    """The receipt comment: its marker, the line for people, then a hidden line per (sha, mode, run)."""
    return {"user": {"login": "github-actions[bot]"}, "body": "<!-- vibe-verifier:review-receipt -->\n**Code review** finished.\n\n"
            + "\n".join("<!-- review-receipt: %s -- %s -- run %d -->" % run for run in runs)}


def quoted(sha):
    """A QA review whose criterion, as the pull request wrote it, is a receipt."""
    return {"user": {"login": "github-actions[bot]"}, "body": "<!-- vibe-verifier:qa-review -->\n## QA review: passed\n\n"
            "- AC1, explorer says PASS: review-receipt: %s -- full -- run 9\n" % sha}


class ReviewVerify(unittest.TestCase):
    """"Write the declared inputs" run as the runner runs it on the event head HEAD, then the
    review-receipt gate over the files it wrote. `gh` answers the paginated comments call with the
    pages given, oldest comment first, the way the real gh prints them without --jq."""

    def verify(self, *pages):
        temp = Path(tempfile.mkdtemp(prefix="vv-verify-"))
        self.addCleanup(lambda: subprocess.run(["rm", "-rf", str(temp)]))
        (temp / "gh").write_text('#!/bin/sh\nif [ "$2" = graphql ]; then echo \'{"unresolved":0}\'; exit 0; fi\n'
                                 + "".join("printf '%%s\\n' '%s'\n" % json.dumps(page) for page in pages))
        os.chmod(temp / "gh", 0o755)
        step = subprocess.run(["bash", "-c", harness.step_script("Write the declared inputs")], cwd=temp, capture_output=True,
                              text=True, env=clean_env({"PATH": str(temp) + os.pathsep + os.environ["PATH"], "GH_TOKEN": "x",
                                                        "PR_NUMBER": "1", "REPO": "o/r", "HEAD_SHA": HEAD}))
        self.assertEqual(step.returncode, 0, step.stdout + step.stderr)
        inputs = temp / "review-inputs"
        return gate("review-receipt", str(temp), "--receipt", str(inputs / "receipt.md"), "--head", str(inputs / "head.txt"),
                    "--threads", str(inputs / "threads.json"))

    def test_a_review_of_an_older_head_that_finishes_last_does_not_hide_this_heads_receipt(self):
        # Observed on a consumer: two pushes in quick succession, runs not cancelled, and the older
        # head's review posted its receipt after this head's. The newest receipt named the old SHA.
        result = self.verify([comment(HEAD, run=2)], [{"user": {"login": "someone"}, "body": "lgtm"}, comment(OLD, "delta", 1)])
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_a_limited_receipt_still_counts_only_for_its_own_head(self):
        self.assertEqual(self.verify([comment(HEAD, "limited", 2), comment(OLD, "full", 1)]).returncode, 0)
        result = self.verify([comment(HEAD, "full", 1), comment(OLD, "limited", 2)], [comment(OLD, "full", 3)])
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.verify([comment(OLD, "limited", 1)]).returncode, 1)

    def test_with_no_receipt_for_this_head_the_gate_names_the_newest_one(self):
        result = self.verify([comment(OLD, run=1)])
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertIn("no review receipt for %s" % HEAD[:10], result.stdout)
        self.assertIn("newest receipt is for %s" % OLD[:10], result.stdout)
        self.assertIn("did not complete", self.verify([]).stdout)

    def test_only_the_workflow_identity_supplies_a_receipt(self):
        result = self.verify([comment(OLD, run=1), comment(HEAD, run=2, login="someone")])
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertIn("newest receipt is for %s" % OLD[:10], result.stdout)

    def test_the_receipt_comment_holds_a_hidden_line_per_run(self):
        # docs/comments.md: one comment, edited in place, after a receipt a copy made before it posted.
        self.assertEqual(self.verify([comment(OLD, run=1), receipts((OLD, "full", 2), (HEAD, "limited", 3))]).returncode, 0)
        result = self.verify([receipts((HEAD, "full", 2), (OLD, "delta", 3))])
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_a_receipt_quoted_by_another_comment_of_the_workflow_identity_is_never_read(self):
        # The QA review lists the criteria as the pull request wrote them, so its author can put a receipt there.
        result = self.verify([receipts((OLD, "full", 1)), quoted(HEAD)])
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertIn("newest receipt is for %s" % OLD[:10], result.stdout)


class ReceiptAnchor(unittest.TestCase):
    """The scope step's last reviewed commit, on the branch tests/test_review_harness.py's ReviewScope builds."""
    setUp, run_step = harness.ReviewScope.setUp, harness.ReviewScope.run_step

    def answer(self, *comments):
        with open(os.path.join(self.bin, "gh"), "w") as handle:
            handle.write("#!/bin/sh\nprintf '%%s\\n' '%s'\n" % json.dumps(list(comments)))

    def test_the_anchor_is_read_from_the_receipt_comment_and_never_from_a_quote(self):
        # docs/comments.md: the receipt comment's hidden lines; a receipt a QA review quotes from the
        # pull request body names a commit nobody reviewed, and must not narrow the review to a delta.
        commit(self.repo, {"unreviewed.js": "x\n"}, "never reviewed")
        unreviewed = subprocess.run(["git", "-C", self.repo, "rev-parse", "HEAD"], capture_output=True, text=True,
                                    check=True).stdout.strip()
        self.answer(receipts((self.receipt, "full", 1)), quoted(unreviewed))
        env = self.run_step({"app.js": "3\n"})
        self.assertIn("LAST_REVIEWED_SHA=%s\n" % self.receipt, env)
        self.assertIn("DELTA_FILES<<EOF\napp.js\nunreviewed.js\nEOF\n", env)

    def test_a_limited_line_of_the_receipt_comment_is_never_the_anchor(self):
        commit(self.repo, {"unreviewed.js": "x\n"}, "passed while the reviewer was rate-limited")
        limited = subprocess.run(["git", "-C", self.repo, "rev-parse", "HEAD"], capture_output=True, text=True,
                                 check=True).stdout.strip()
        self.answer(receipts((self.receipt, "full", 1), (limited, "limited", 2)))
        env = self.run_step({"app.js": "3\n"})
        self.assertIn("LAST_REVIEWED_SHA=%s\n" % self.receipt, env)
        self.assertIn("DELTA_FILES<<EOF\napp.js\nunreviewed.js\nEOF\n", env)


class Prompt(unittest.TestCase):
    def test_this_repository_runs_the_catalog_prompt(self):
        own = (ROOT / ".github" / "workflows" / "review.yml").read_text()
        self.assertEqual(harness.prompt_block(own), harness.PROMPT.read_text())

    def test_the_prompt_holds_the_model_to_the_comment_rules(self):
        # docs/comments.md: the inline findings are the one comment text a model writes.
        rules = " ".join(harness.PROMPT.read_text().split())
        self.assertIn("A person reads each comment. Lead with what goes wrong and who it affects, in plain words, then the fix. "
                      "No commit hashes, run IDs or runner paths, no em dashes, no @-mention of any account or bot, no tailnet "
                      "address or bare URL (a descriptive link instead), and every item written out (never \"and 3 more\").", rules)


class ReceiptPost(unittest.TestCase):
    """One GitHub 5xx on the receipt post must not throw a finished review away: the post is retried,
    a bounded number of times, and a post that never lands still fails the job."""

    def post(self, failures):
        temp = tempfile.mkdtemp(prefix="vv-receipt-")
        self.addCleanup(__import__("shutil").rmtree, temp, True)
        result = subprocess.run(["bash", "-c", harness.step_script("Post the receipt", "Collect numeric usage")], capture_output=True, text=True,
                                env=clean_env({"PATH": flaky_gh(temp, failures) + os.pathsep + os.environ["PATH"], "REVIEW_MODE": "full",
                                               "GITHUB_RUN_ID": "7", "GH_TOKEN": "x", "PR_NUMBER": "1", "REPO": "o/r", "HEAD_SHA": "a" * 40,
                                               "GITHUB_SERVER_URL": "https://github.com"}))
        return result, len(lines(os.path.join(temp, "calls"))), lines(os.path.join(temp, "sleeps"))

    def test_a_post_that_fails_twice_then_lands_passes(self):
        # Each attempt reads the receipt comment, then writes it: two failed reads, then a read and the post.
        result, calls, sleeps = self.post(2)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual((calls, sleeps), (4, ["15", "30"]))
        self.assertIn("::warning::posting the receipt failed (attempt 2 of 5)", result.stdout)

    def test_a_post_that_never_lands_fails_after_five_tries(self):
        result, calls, sleeps = self.post(99)
        self.assertEqual(result.returncode, 1)
        self.assertEqual((calls, sleeps), (5, ["15", "30", "45", "60"]))
        self.assertIn("::error::the receipt could not be posted after 5 attempts", result.stdout)

    def test_this_repository_runs_the_same_step(self):
        own = (ROOT / ".github" / "workflows" / "review.yml").read_text()
        step = harness.TEMPLATE.read_text()
        step = step[step.index("- name: Post the receipt\n"):step.index("- name: Collect numeric usage\n")]
        self.assertIn(step, own)

    def test_this_repository_reads_receipts_the_same_way(self):
        # The scope step's and the verify job's jq programs, which decide which comments hold a receipt.
        own = (ROOT / ".github" / "workflows" / "review.yml").read_text()
        readers = re.findall(r"\| jq -r -s .*?' >? ?", harness.TEMPLATE.read_text(), re.S)
        self.assertEqual(len(readers), 2)
        for reader in readers:
            self.assertIn("vibe-verifier:review-receipt", reader)
            self.assertIn(reader, own)


if __name__ == "__main__":
    unittest.main()
