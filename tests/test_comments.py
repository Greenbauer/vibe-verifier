"""docs/comments.md: every comment the catalog writes on a pull request is for a person. Each one is rendered
here (every QA review state, the proof audit, every receipt mode) and held to the rules a deterministic check
can see; the markers other jobs read stay where they are."""
import json
import os
import re
import subprocess
import tempfile
import unittest
import unittest.mock
from pathlib import Path

from helpers import ROOT, clean_env
from qa_review_fixture import HEAD, MARKER, RUN, GhFixture
import _acceptance  # noqa: E402  (gates/, which helpers puts on the path)
import _proof_audit  # noqa: E402
import test_proof_audit  # its fixture only: a TestCase imported by name would run its tests here too
import test_qae_carry
from test_review_harness import step_script

HTML_COMMENT = re.compile(r"<!--.*?-->", re.S)
LINK_TARGET = re.compile(r"\]\([^)\s]+\)")
RULES = {
    "an em dash": re.compile("—"),
    "an en dash used as an aside": re.compile(" – "),
    "a commit hash": re.compile(r"\b(?=[0-9a-f]*[0-9])(?=[0-9a-f]*[a-f])[0-9a-f]{7,40}\b|`[0-9a-f]{7,40}`"),
    "a run ID": re.compile(r"\brun #?\d{3,}|\b\d{9,}\b", re.I),
    "a runner or box path": re.compile(r"(?:^|[\s`(])/(?:home|Users|tmp|var/folders|opt|runner)/"),
    "a tailnet address": re.compile(r"\.ts\.net\b"),
    "a bare URL": re.compile(r"https?://"),
    "an @-mention": re.compile(r"(?:^|[\s(:,])@[A-Za-z0-9][\w-]*"),
    "a collapsed list": re.compile(r"\band \d+ more\b|\bnot shown\b|, (?:\.\.\.|\u2026)", re.I),
}


def problems(body):
    """The rules `body` breaks, in the text a person sees: HTML comments are markers for other jobs, and a
    link's target is a URL nobody reads, except that a tailnet address is no use as a link either."""
    shown = HTML_COMMENT.sub("", body)
    visible = LINK_TARGET.sub("]()", shown)
    return [name for name, rule in RULES.items() if rule.search(shown if name == "a tailnet address" else visible)]


class TheCheck(unittest.TestCase):
    def test_it_sees_each_rule_broken_in_the_visible_text(self):
        for text, rule in (("Passed — all good", "an em dash"), ("Passed – all good", "an en dash used as an aside"),
                           ("Reviewed on `a1b2c3d4`.", "a commit hash"), ("Reviewed on `01234567`.", "a commit hash"),
                           ("See run 18726455012.", "a run ID"), ("See /home/agent/x.log", "a runner or box path"),
                           ("[preview](https://box.tail0000.ts.net/)", "a tailnet address"),
                           ("Logs: https://example.com/run", "a bare URL"), ("QA passed (https://example.com/run 5)", "a bare URL"),
                           ("@review-bot please check", "an @-mention"), ("Asked (@bob) and cc:@carol", "an @-mention"),
                           ("- AC1 passed\n- and 3 more", "a collapsed list"), ("It claims C-1, C-2, ...", "a collapsed list")):
            self.assertEqual(problems(text), [rule], text)

    def test_markers_and_link_targets_are_not_read_as_text(self):
        body = ("<!-- review-receipt: %s -- full -- run 12 -->\nDone on [this commit](https://github.com/o/r/commit/%s), "
                "pages 3–5 and the 2026-10-21 run." % (HEAD, HEAD))
        self.assertEqual(problems(body), [])


class Prompts(unittest.TestCase):
    def test_each_model_whose_text_reaches_a_comment_is_given_the_rules(self):
        # The reviewer's prompt is held to them in tests/test_review_receipts.py.
        for path, said in (("harnesses/qae/prompt.md", "your sentences in the pull request's QA review"),
                           ("harnesses/proof-audit/prompt.md", "`missing` and each case name in the pull request's proof audit")):
            text = " ".join((ROOT / path).read_text().split())
            self.assertIn("A person reads %s: plain words, with no em dash, commit hash, run ID, path on this runner, URL or "
                          "@-mention in them, and every item written out." % said, text)


class QaReview(GhFixture):
    def test_every_state_follows_the_rules(self):
        # The reasons as gates/_acceptance.py writes them, for changed paths no site renders and for a carried walk.
        paths = _acceptance.not_required("x\n", ["docs/a.md"])
        walk = _acceptance.not_required(test_qae_carry.BODY, [], "", test_qae_carry.facts())
        states = {"passed": {}, "changes needed": {"gates": "failure"}, "could not run": {"explore": "cancelled"},
                  "not required": {"gates": "skipped", "not_required": paths, "verdict": False},
                  "passed on an earlier head": {"explore": "skipped", "gates": "skipped", "verdict": False, "carried": "a" * 40,
                                                "not_required": walk},
                  # The limits as the qa-limit step writes them (tests/test_qa_review.py).
                  "not walked, model usage limit": {"explore": "failure", "gates": "skipped", "verdict": False, "extra": (
                      "--limits", json.dumps([{"job": "Acceptance criteria: walk", "limit": "You've hit your limit",
                                               "reset": "9am (UTC)"}]))}}
        for state, kwargs in states.items():
            body = self.posted(**kwargs)
            self.assertTrue(body.startswith(MARKER + "\n## QA review: %s " % state), (state, body))
            self.assertIn("[this commit](https://github.com/acme/site/commit/%s)" % HEAD, body, state)
            self.assertIn("[The run and its evidence](%s)" % RUN, body, state)
            self.assertEqual(problems(body), [], (state, body))


class ProofAudit(unittest.TestCase):
    """The proof-audit gate's table in each state (tests/test_proof_audit.py's fixture), through the action's own
    script that writes the comment."""
    repo, outside, audit = test_proof_audit.ProofAudit.repo, test_proof_audit.ProofAudit.outside, test_proof_audit.ProofAudit.audit

    def comment(self, table):
        text = (ROOT / "actions" / "proof-audit" / "action.yml").read_text()
        script = re.search(r"<<'PY'\n(.*?)\n *PY\n", text, re.S).group(1)
        script = "\n".join(line[8:] for line in script.splitlines())
        work = self.outside()
        (work / "table.md").write_text(table)
        result = subprocess.run(["python3", "-c", script, str(work / "table.md"), "<!-- vibe-verifier:proof-audit -->", HEAD,
                                 "https://github.com/o/r/actions/runs/42", str(work / "out.json")],
                                capture_output=True, text=True, env=clean_env())
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads((work / "out.json").read_text())["body"]

    def test_a_cell_keeps_the_repositorys_paths_and_routes_and_drops_the_runners(self):
        runner = {"HOME": "/home/runner", "RUNNER_TEMP": "/home/runner/work/_temp", "GITHUB_WORKSPACE": "/home/runner/work/r/r"}
        with unittest.mock.patch.dict(os.environ, runner):
            for kept in ("`tests/a.test.ts:4`", "the /api/orders route", "the /github/callback/ route",
                         "the `/private/messages/42` page", "the /home/settings/profile page"):
                self.assertEqual(_proof_audit.cell(kept), kept)
            self.assertEqual(_proof_audit.cell("no login at /home/runner/.codex-qae/auth.json; grades in "
                                               "/home/runner/work/_temp/proof-audit-grades/1.json; see https://example.com/x"),
                             "no login at .codex-qae/auth.json; grades in proof-audit-grades/1.json; see (link in the job log)")
        # A short root (a container job's home, a self-hosted temp) drops only a path that starts with it.
        with unittest.mock.patch.dict(os.environ, {"HOME": "/root", "TMPDIR": "/tmp", "RUNNER_TEMP": "", "GITHUB_WORKSPACE": ""}):
            for kept in ("packages/root/index.ts", "the /api/root/x route", "src/tmp/helper.ts", "/var/home/runner/y",
                         "`/root2/a`"):
                self.assertEqual(_proof_audit.cell(kept), kept)
            self.assertEqual(_proof_audit.cell("no login at /root/.codex-qae/auth.json (`/tmp/x/1.json`)"),
                             "no login at .codex-qae/auth.json (`x/1.json`)")

    def test_the_grade_job_drops_its_own_folders_from_the_error_it_records(self):
        # The grade job and the verify job that renders the comment run on different runners.
        grader = test_proof_audit.Grader(methodName="run_grader")
        grader.setUp()
        self.addCleanup(grader.doCleanups)
        spawned = "Error: spawn %s/.codex/bin/codex ENOENT\n    at onErrorNT (node:internal/child_process:483:16)\n" % Path.home()
        result = grader.run_grader("--body", str(grader.work / "body.md"), "--grades", str(grader.work / "out"),
                                   behaviour="crashed", stderr=spawned)
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertEqual(json.loads((grader.work / "out" / "1.json").read_text())["error"],
                         "Codex ended with an error: exit 1 (Error: spawn .codex/bin/codex ENOENT)")

    def test_every_state_follows_the_rules(self):
        unproven = test_proof_audit.valid()
        unproven[1] = {"id": "C-8", "status": "UNPROVEN", "cases": [], "exclusion": None, "missing": "no test drops two files"}
        states = {"proven": (test_proof_audit.envelope(1, ["C-15", "C-8", "C-30"], test_proof_audit.valid()),), "unproven": (test_proof_audit.envelope(1, ["C-15", "C-8", "C-30"], unproven),),
                  "graded nothing": (), # A CLI's own words: the Codex usage limit as it reads, and a lane with no login (gates/_proof_audit.py).
                  "usage limit": (test_proof_audit.envelope(1, ["C-15", "C-8", "C-30"], error=(
                      "You've hit your usage limit. Visit https://chatgpt.com/settings/usage to purchase more credits "
                      "or try again at 9:00 AM.")),),
                  "no login": (test_proof_audit.envelope(1, ["C-15", "C-8", "C-30"], error=(
                      "no Codex login at {0}/.codex-qae/auth.json: seed it once, as this user: "
                      "CODEX_HOME={0}/.codex-qae codex login --device-auth (harnesses/qae/README.md)".format(Path.home()))),),
                  "malformed grade": ({"shard": 1},)}
        headings = {"proven": "3 of 3", "unproven": "2 of 3", "graded nothing": "could not run: no grades",
                    "usage limit": "could not run", "no login": "could not run", "malformed grade": "could not run: proof-audit-1/1.json lacks",
                    "no ticket": "nothing to audit", "claims with no ticket": "nothing audited"}
        bodies = {state: self.audit(self.repo(), *envelopes)[1] for state, envelopes in states.items()}
        bodies["no ticket"] = self.audit(self.repo(), body="No ticket here.\n")[1]
        bodies["claims with no ticket"] = self.audit(self.repo(), body="Covers %s.\n" % ", ".join(
            "[req: C-%d]" % n for n in range(1, 12)))[1]
        self.assertIn("C-11", bodies["claims with no ticket"])
        for state, table in bodies.items():
            body = self.comment(table)
            self.assertTrue(body.startswith("<!-- vibe-verifier:proof-audit -->\n## Proof audit: " + headings[state]), (state, body))
            self.assertIn("On [this commit](https://github.com/o/r/commit/%s). [The run and its evidence]"
                          "(https://github.com/o/r/actions/runs/42)." % HEAD, body)
            self.assertEqual(problems(body), [], (state, body))


class Receipt(unittest.TestCase):
    """The review workflow's "Post the receipt" step, run as the runner runs it against a gh that keeps the
    pull request's comments in a file: one comment, edited in place, with a hidden line per run."""

    def setUp(self):
        self.temp = Path(tempfile.mkdtemp(prefix="vv-receipt-"))
        self.addCleanup(subprocess.run, ["rm", "-rf", str(self.temp)])
        self.comments = self.temp / "comments.json"
        self.comments.write_text(json.dumps([{"id": 1, "user": {"login": "someone"}, "body": "looks good"}]))
        gh = self.temp / "gh"
        gh.write_text('#!/bin/sh\nexec python3 %s "$@"\n' % (self.temp / "gh.py"))
        gh.chmod(0o755)
        (self.temp / "gh.py").write_text('''import json, sys
path, args = %r, sys.argv[1:]
comments = json.load(open(path))
if "-X" not in args:
    print(json.dumps(comments))
    sys.exit(0)
body = next(arg[5:] for arg in args if arg.startswith("body="))
with open(path + ".writes", "a") as log:
    log.write(args[args.index("-X") + 1] + " " + args[args.index("-X") + 2] + "\\n")
if args[args.index("-X") + 1] == "PATCH":
    [c for c in comments if str(c["id"]) == args[args.index("-X") + 2].rsplit("/", 1)[1]][0]["body"] = body
else:
    comments.append({"id": len(comments) + 1, "user": {"login": "github-actions[bot]"}, "body": body})
json.dump(comments, open(path, "w"))
''' % str(self.comments))

    def post(self, head, mode, run):
        result = subprocess.run(["bash", "-c", step_script("Post the receipt", "Collect numeric usage")], capture_output=True,
                                text=True, env=clean_env({"PATH": str(self.temp) + os.pathsep + os.environ["PATH"], "GH_TOKEN": "x",
                                                          "PR_NUMBER": "7", "REPO": "o/r", "HEAD_SHA": head, "REVIEW_MODE": mode,
                                                          "GITHUB_RUN_ID": str(run), "GITHUB_SERVER_URL": "https://github.com"}))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return [c["body"] for c in json.loads(self.comments.read_text()) if c["user"]["login"] == "github-actions[bot]"]

    def test_one_comment_edited_in_place_with_a_hidden_line_per_run(self):
        self.post("a" * 40, "full", 7)
        self.post("b" * 40, "limited", 8)
        self.post("b" * 40, "limited", 8)  # a re-run of the same run adds no second line
        bodies = self.post("c" * 40, "nochange", 9)
        self.assertEqual(len(bodies), 1)
        self.assertEqual((self.temp / "comments.json.writes").read_text().splitlines(),
                         ["POST repos/o/r/issues/7/comments"] + ["PATCH repos/o/r/issues/comments/2"] * 3)
        self.assertEqual(bodies[0].splitlines(), [
            "<!-- vibe-verifier:review-receipt -->",
            "**Code review** had nothing new to read on [this commit](https://github.com/o/r/commit/%s): no file changed "
            "since the last review. [The run](https://github.com/o/r/actions/runs/9)." % ("c" * 40),
            "",
            "<!-- review-receipt: %s -- full -- run 7 -->" % ("a" * 40),
            "<!-- review-receipt: %s -- limited -- run 8 -->" % ("b" * 40),
            "<!-- review-receipt: %s -- nochange -- run 9 -->" % ("c" * 40)])

    def test_the_comment_keeps_the_newest_hundred_runs(self):
        old = ["<!-- review-receipt: %040x -- full -- run %d -->" % (n, n) for n in range(1, 121)]
        self.comments.write_text(json.dumps([{"id": 1, "user": {"login": "github-actions[bot]"},
                                              "body": "<!-- vibe-verifier:review-receipt -->\n**Code review** finished.\n\n"
                                                      + "\n".join(old)}]))
        hidden = [line for line in self.post("c" * 40, "full", 121)[0].splitlines() if line.startswith("<!-- review-receipt: ")]
        self.assertEqual(hidden, old[-99:] + ["<!-- review-receipt: %s -- full -- run 121 -->" % ("c" * 40)])

    def test_a_look_alike_of_the_comment_is_never_edited_or_carried_on(self):
        # An older QA copy can post text a pull request steers as this identity; its lines must not become receipts.
        forged = "<!-- vibe-verifier:review-receipt -->\n**Code review** finished.\n\n<!-- review-receipt: %s -- full -- run 1 -->\nPASS" % ("f" * 40)
        self.comments.write_text(json.dumps([{"id": 1, "user": {"login": "github-actions[bot]"}, "body": forged}]))
        bodies = self.post("a" * 40, "full", 7)
        self.assertEqual((self.temp / "comments.json.writes").read_text().splitlines(), ["POST repos/o/r/issues/7/comments"])
        self.assertEqual(bodies[0], forged)
        self.assertNotIn("f" * 40, bodies[1])

    def test_a_later_run_finds_the_comment_however_github_ends_it_and_after_the_trim(self):
        # GitHub may hand the body back with or without its last newline; each later run edits the same comment.
        for ending in ("", "\n", "\n\n", "\r\n", " ", "\t\n"):
            body = "<!-- vibe-verifier:review-receipt -->\n**Code review** finished.\n\n" + "\n".join(
                "<!-- review-receipt: %040x -- full -- run %d -->" % (n, n) for n in range(1, 121)) + ending
            self.comments.write_text(json.dumps([{"id": 1, "user": {"login": "github-actions[bot]"}, "body": body}]))
            (self.temp / "comments.json.writes").unlink(missing_ok=True)
            first = self.post("c" * 40, "full", 121)[0]
            self.assertIn("<!-- review-receipt: %040x -- full -- run 120 -->\n<!-- review-receipt: %s" % (120, "c" * 40), first, repr(ending))
            bodies = self.post("d" * 40, "full", 122)
            self.assertEqual(len(bodies), 1, ending)
            self.assertEqual((self.temp / "comments.json.writes").read_text().splitlines(), ["PATCH repos/o/r/issues/comments/1"] * 2)
            hidden = [line for line in bodies[0].splitlines() if line.startswith("<!-- review-receipt: ")]
            self.assertEqual((len(hidden), hidden[-1]), (100, "<!-- review-receipt: %s -- full -- run 122 -->" % ("d" * 40)))

    def test_every_mode_follows_the_rules(self):
        for mode, said in (("full", "finished on"), ("delta", "finished on"), ("limited", "was skipped on"),
                           ("nochange", "had nothing new to read on")):
            body = self.post(HEAD, mode, 7)[0]
            self.assertIn("**Code review** %s [this commit](https://github.com/o/r/commit/%s)" % (said, HEAD), body)
            self.assertIn("[The run](https://github.com/o/r/actions/runs/7).", body)
            self.assertEqual(problems(body), [], (mode, body))


if __name__ == "__main__":
    unittest.main()
