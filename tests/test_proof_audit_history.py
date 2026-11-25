"""The proof audit comment's history of earlier commits (gates/_proof_comment.py): carried from the previous comment's
own data block, one entry per commit, capped, a fresh start from a comment with no block or an unreadable one, and
through the action's two verify steps against a gh that keeps the pull request's comments in a file. Fixtures:
tests/test_proof_audit.py."""
import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path

from helpers import write

import _proof_comment
import test_proof_audit as base  # as modules, so their test classes are not collected twice
import test_proof_audit_unlinked as unlinked

RUN = "https://github.com/o/r/actions/runs/%d"
TABLE = "## Proof audit: 2 of 3 requirements proven or excluded\n\n| ID | Verdict | Proof, or what is missing |\n"

# A gh that keeps the pull request's comments in $GH_COMMENTS, as GitHub's issue comments API answers them.
FAKE_GH = """#!/usr/bin/env python3
import json, os, sys
path = os.environ["GH_COMMENTS"]
comments = json.load(open(path))
args = sys.argv[1:]
method = args[args.index("-X") + 1] if "-X" in args else "GET"
url = next(arg for arg in args[1:] if arg.startswith("repos/"))
if method == "GET" and "--paginate" in args:
    marker = "<!-- vibe-verifier:proof-audit -->"
    for item in comments:
        if item["user"]["login"] == "github-actions[bot]" and item["body"].startswith(marker):
            print(item["id"])
elif method == "GET":
    print(json.dumps(next(item for item in comments if url.endswith("/%d" % item["id"]))))
else:
    body = json.load(open(args[args.index("--input") + 1]))["body"]
    if method == "PATCH":
        next(item for item in comments if url.endswith("/%d" % item["id"]))["body"] = body
    else:
        comments.append({"id": 100 + len(comments), "user": {"login": "github-actions[bot]"}, "body": body})
    json.dump(comments, open(path, "w"))
"""


def entry(sha, verdicts, at="2026-11-24T10:00Z", run=1):
    return {"sha": sha, "at": at, "run": RUN % run, "verdicts": verdicts}


def block(commits):
    return '<!-- vibe-verifier:proof-audit-history %s -->' % json.dumps({"v": 1, "commits": commits})


class History(unittest.TestCase):
    def setUp(self):
        self.work = Path(tempfile.mkdtemp(prefix="vv-history-"))
        self.addCleanup(shutil.rmtree, self.work, True)

    def comment(self, head, verdicts, previous=None, at="2026-11-24T10:00Z", table=TABLE, run=1):
        """The comment body _proof_comment.py writes for a run on `head`, after a comment whose body is `previous`."""
        write(self.work, {"table.md": table})
        for name, value in (("verdicts.json", verdicts), ("previous.json", previous and {"id": 9, "body": previous})):
            (self.work / name).unlink(missing_ok=True)
            if value is not None:
                (self.work / name).write_text(json.dumps(value))
        code = _proof_comment.main(["--table", str(self.work / "table.md"), "--verdicts", str(self.work / "verdicts.json"),
                                    "--previous", str(self.work / "previous.json"), "--head", head, "--run", RUN % run,
                                    "--out", str(self.work / "out.json"), "--at", at])
        self.assertEqual(code, 0)
        return json.loads((self.work / "out.json").read_text())["body"]

    def test_the_first_run_records_its_commit_and_lists_no_earlier_one(self):
        body = self.comment("a" * 40, {"C-8": "UNPROVEN", "C-15": "PROVEN"})
        self.assertTrue(body.startswith(_proof_comment.MARKER + "\n" + TABLE.rstrip("\n")), body)
        self.assertNotIn("Earlier commits", body)
        self.assertEqual(_proof_comment.history(body), [entry("a" * 40, {"C-8": "UNPROVEN", "C-15": "PROVEN"})])

    def test_a_second_commit_lists_the_first_with_what_was_unproven_there(self):
        first = self.comment("a" * 40, {"C-8": "UNPROVEN", "C-15": "PROVEN", "C-30": "SPLIT"}, at="2026-11-23T09:10Z")
        second = self.comment("b" * 40, {"C-8": "PROVEN", "C-15": "PROVEN", "C-30": "SPLIT"}, previous=first, run=2)
        self.assertIn("### Earlier commits\n\nWhat the audit found on each earlier commit of this pull request, newest "
                      "first: a requirement unproven there and proven above was fixed since.", second)
        self.assertIn("| [2026-11-23 09:10](https://github.com/o/r/commit/%s) | 1 of 2 | C-8 |" % ("a" * 40), second)
        self.assertNotIn("commit/%s)" % ("b" * 40), second.split("### Earlier commits")[1])  # the head is the table above
        self.assertEqual([item["sha"] for item in _proof_comment.history(second)], ["b" * 40, "a" * 40])

    def test_a_re_run_on_the_same_commit_replaces_its_entry(self):
        first = self.comment("a" * 40, {"C-8": "UNPROVEN"})
        second = self.comment("b" * 40, {"C-8": "UNPROVEN"}, previous=first, run=2)
        again = self.comment("b" * 40, {"C-8": "PROVEN"}, previous=second, at="2026-11-24T11:00Z", run=3)
        self.assertEqual(_proof_comment.history(again), [entry("b" * 40, {"C-8": "PROVEN"}, "2026-11-24T11:00Z", 3),
                                                         entry("a" * 40, {"C-8": "UNPROVEN"})])
        self.assertEqual(again.count("commit/%s)" % ("a" * 40)), 1)

    def test_the_list_keeps_the_newest_thirty_and_fits_a_github_comment(self):
        body = None
        for number in range(35):
            body = self.comment("%040x" % number, {"C-8": "UNPROVEN"}, previous=body, run=number + 1)
        kept = [item["sha"] for item in _proof_comment.history(body)]
        self.assertEqual(kept, ["%040x" % number for number in range(34, 4, -1)])
        # A table long enough to fill the comment leaves room by dropping the oldest entries, never the head's.
        long = TABLE + "| C-1 | UNPROVEN | %s |\n" % ("x" * 60000)
        many = {"C-%d" % n: "UNPROVEN" for n in range(1, 60)}
        body = self.comment("f" * 40, many, previous=body, table=long, run=99)
        self.assertLessEqual(len(body), _proof_comment.LIMIT)
        kept = _proof_comment.history(body)
        self.assertEqual(kept[0]["sha"], "f" * 40)
        self.assertLess(len(kept), 30)

    def test_a_comment_written_before_the_history_or_unreadable_starts_a_fresh_one(self):
        good = entry("a" * 40, {"C-8": "UNPROVEN"})
        olds = {"no block": "<!-- vibe-verifier:proof-audit -->\n## Proof audit: 3 of 3\n",
                "not JSON": "x\n<!-- vibe-verifier:proof-audit-history {not json} -->",
                "another version": "x\n" + block([good]).replace('"v": 1', '"v": 2'),
                "a short sha": "x\n" + block([good, entry("abc1234", {})]),
                "an unknown status": "x\n" + block([entry("a" * 40, {"C-8": "MAYBE"})]),
                "an ID that is markup": "x\n" + block([entry("a" * 40, {"<b>": "UNPROVEN"})]),
                "a run elsewhere": "x\n" + block([dict(good, run="javascript:alert(1)")]),
                # A block a cell of the table quotes (a grader's words) is not the comment's own last line.
                "a block inside the table": "x\n| C-8 | UNPROVEN | %s |\nOn this commit." % block([good])}
        for name, previous in olds.items():
            with self.subTest(name):
                body = self.comment("b" * 40, {"C-8": "PROVEN"}, previous=previous)
                self.assertNotIn("Earlier commits", body)
                self.assertEqual(_proof_comment.history(body), [entry("b" * 40, {"C-8": "PROVEN"})])
        body = self.comment("b" * 40, {"C-8": "PROVEN"}, previous="x\n" + block([good]))  # the same shape is carried
        self.assertEqual(len(_proof_comment.history(body)), 2)

    def test_a_run_that_graded_nothing_is_listed_as_not_graded(self):
        first = self.comment("a" * 40, None)
        second = self.comment("b" * 40, {"C-8": "PROVEN"}, previous=first, run=2)
        self.assertIn("| [2026-11-24 10:00](https://github.com/o/r/commit/%s) | not graded | |" % ("a" * 40), second)

    def test_unproven_on_one_commit_and_proven_on_the_next_is_read_from_the_data_alone(self):
        body = None
        for number, verdicts in enumerate(({"C-8": "UNPROVEN", "C-15": "UNPROVEN"}, None,
                                           {"C-8": "PROVEN", "C-15": "UNPROVEN"}, {"C-8": "PROVEN", "C-15": "EXCLUDED"})):
            body = self.comment(str(number) * 40, verdicts, previous=body, run=number + 1)
        # Only the last line of the comment is read: the block, never the table or the list above it.
        data = _proof_comment.history(body.rstrip().split("\n")[-1])
        self.assertEqual(_proof_comment.caught(data), [("C-8", "0" * 40, "2" * 40), ("C-15", "2" * 40, "3" * 40)])


class ThroughTheAction(unittest.TestCase):
    repo, outside = unlinked.Unlinked.repo, unlinked.Unlinked.outside

    def run_verify(self, repo, head, grades, run, token="t"):
        bin_dir = self.outside()
        write(bin_dir, {"gh": FAKE_GH})
        os.chmod(bin_dir / "gh", 0o755)
        return unlinked.Unlinked.verify_step(
            self, repo, base.BODY, grades=grades, GH_TOKEN=token, VV_PR="7", VV_REPO="o/r", VV_HEAD=head,
            VV_RUN=RUN % run, GH_COMMENTS=str(self.comments), PATH="%s:%s" % (bin_dir, os.environ["PATH"]))

    def test_each_run_edits_one_comment_and_its_history_counts_the_fix(self):
        self.comments = self.outside() / "comments.json"
        self.comments.write_text(json.dumps([{"id": 1, "user": {"login": "someone"}, "body": "looks good"}]))
        repo, ids = self.repo(), ["C-15", "C-8", "C-30"]
        unproven = base.valid()
        unproven[1] = base.grade("C-8", "UNPROVEN", missing="no test drops two files")
        result, _ = self.run_verify(repo, "a" * 40, (base.envelope(1, ids, unproven),), 41)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        result, summary = self.run_verify(repo, "b" * 40, (base.envelope(1, ids, base.valid()),), 42)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("## Proof audit: 3 of 3", summary)
        comments = json.loads(self.comments.read_text())
        self.assertEqual([item["id"] for item in comments], [1, 101])  # one comment, edited in place
        body = comments[1]["body"]
        self.assertIn("| 2 of 3 | C-8 |", body)
        self.assertEqual(_proof_comment.caught(_proof_comment.history(body)), [("C-8", "a" * 40, "b" * 40)])

    def test_with_no_token_the_summary_has_the_table_and_nothing_is_posted(self):
        self.comments = self.outside() / "comments.json"
        self.comments.write_text("[]")
        result, summary = self.run_verify(self.repo(), "a" * 40, (base.envelope(1, ["C-15", "C-8", "C-30"], base.valid()),),
                                          41, token="")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("## Proof audit: 3 of 3", summary)
        self.assertEqual(json.loads(self.comments.read_text()), [])
