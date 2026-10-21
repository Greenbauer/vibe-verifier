"""`vibe-verifier qa-review` and actions/qa-review: the QAE's one review comment on a pull request,
driven through the command line (and the action's own script) against a fake `gh` that records writes."""
import json
import re
import subprocess
import unittest
from pathlib import Path

from helpers import ROOT, clean_env
from qa_review_fixture import BOT, HEAD, MARKER, RUN, VERDICT, GhFixture

ACTION = ROOT / "actions" / "qa-review" / "action.yml"


class QaReview(GhFixture):
    # What the comment says, per state. The first line is the marker and the second the heading,
    # always: that is how a reader and the next run find it.

    def test_passed(self):
        body = self.posted()
        self.assertEqual(body.splitlines()[:2], [MARKER, "## QA review: passed ✅"])
        self.assertIn("`01234567`", body)
        self.assertIn("- AC1, explorer says PASS: The home page loads\n", body)
        self.assertIn("[The run and its evidence](%s)" % RUN, body)

    def test_the_tickets_criteria_are_listed_after_the_pull_requests(self):
        # The workflow supplies the ticket's criteria (harnesses/qae/README.md); a None in the body cannot hide them.
        ticket = self.file("ticket.md", "## Acceptance criteria\n\n- Prices show in euros\n")
        self.verdict = self.file("verdict.md", VERDICT + "acceptance-check: TC1 -- PASS -- euros (qae/TC1.md::step 1: x)\n")
        body = self.posted(ticket=ticket)
        self.assertIn("- AC2, explorer says FAIL: The menu opens\n- TC1 (from the ticket), explorer says PASS: Prices show in euros\n", body)
        self.criteria = self.file("pr-body.md", "## Acceptance criteria\n\n- None: a refactor\n")
        body = self.posted(ticket=ticket)
        self.assertIn("- TC1 (from the ticket), explorer says PASS", body)
        self.assertNotIn("None: a refactor", body)

    def test_a_ticket_criterion_the_pull_request_proves_with_a_test_is_listed_as_not_walked(self):
        ticket = self.file("ticket.md", "## Acceptance criteria\n\n- Prices show in euros\n- The estimate lands in the ledger\n")
        self.criteria = self.file("pr-body.md", "## Acceptance criteria\n\n- None: a refactor\n\n"
                                                "ticket-proof: TC2 -- no browser sees the ledger (lib/ledger.test.ts::writes the estimate line)\n")
        self.verdict = self.file("verdict.md", "acceptance-check: TC1 -- PASS -- euros (qae/TC1.md::step 1: x)\n")
        body = self.posted(ticket=ticket)
        self.assertIn("- TC1 (from the ticket), explorer says PASS: Prices show in euros\n- TC2 (from the ticket), not walked, "
                      "the pull request proves it with a test: no browser sees the ledger (lib/ledger.test.ts::writes the "
                      "estimate line): The estimate lands in the ledger\n", body)

    def test_a_re_walked_feature_is_listed_as_the_explorer_judged_it(self):
        # docs/feature-map.md: the explorer adds one regression-check line per feature it re-walked.
        self.verdict = self.file("verdict.md", VERDICT + "regression-check: sign-in -- FAIL -- the dashboard never opened "
                                                         "(qae/features/sign-in.md::step 1: signed in -> a blank page)\n")
        body = self.posted(gates="failure")
        self.assertIn("- AC2, explorer says FAIL: The menu opens\n- Feature `sign-in` re-walked, explorer says FAIL\n", body)
        self.verdict = self.file("verdict.md", VERDICT)
        self.assertNotIn("re-walked", self.posted(gates="failure"))

    def test_a_fail_the_base_branch_has_too_says_so(self):
        self.verdict = self.file("verdict.md", VERDICT + "regression-check: sign-in -- FAIL -- the dashboard never opened "
                                                         "(qae/features/sign-in.md::step 1: signed in -> a blank page)\n"
                                                         "regression-known: sign-in -- the same blank page "
                                                         "(qae/features/base/sign-in.md::step 1: signed in -> a blank page)\n")
        self.assertIn("- Feature `sign-in` re-walked, explorer says FAIL, and says the base branch fails the same way: "
                      "the same blank page (qae/features/base/sign-in.md::step 1: signed in -> a blank page)\n",
                      self.posted(gates="failure"))

    def test_a_re_walk_names_the_states_the_explorer_left_unwalked(self):
        # A re-walk covers at most a few states of a feature, so "re-walked" alone would overstate it. A
        # skip line with no check line is a feature that was not re-walked: the gate's log names it.
        self.verdict = self.file("verdict.md", VERDICT + "regression-check: sign-in -- PASS -- still signs in "
                                                         "(qae/features/sign-in.md::step 1: signed in -> the dashboard)\n"
                                                         "regression-skip: sign-in -- 4, 5, 7\n"
                                                         "regression-skip: billing -- 1, 2\n")
        body = self.posted(gates="failure")
        self.assertIn("- Feature `sign-in` re-walked, explorer says PASS (not walked: 4, 5, 7)\n", body)
        self.assertNotIn("billing", body)

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

    def test_a_carried_walk_says_which_head_passed(self):
        reason = ("QA passed on `aaaaaaaa` (https://github.com/acme/site/actions/runs/5), and every file changed since (2) "
                  "is CI configuration or documentation no site renders")
        body = self.posted(explore="skipped", gates="skipped", not_required=reason, verdict=False, carried="a" * 40)
        self.assertEqual(body.splitlines()[1], "## QA review: passed on an earlier head ✅")
        self.assertIn("No new browser check on `01234567`: %s. The criteria were not edited since." % reason, body)

    def test_could_not_run_when_the_explore_job_did_not_finish(self):
        body = self.posted(explore="failure", gates="failure")
        self.assertEqual(body.splitlines()[1], "## QA review: could not run ⚠️")
        self.assertIn("The explore job ended `failure` on `01234567`", body)

    # A model usage limit: the explore job's failure annotations say so, and the comment tells the agent
    # driving the pull request to walk the criteria by hand until the reset. Messages as the lanes print them.

    CLAUDE_LIMIT = ("Action failed with error: SDK execution error: Error: Claude Code returned an error result: "
                    "You've hit your limit \u00b7 resets 9am (UTC)")  # Greenbauer/zack.land run 38286901786
    CODEX_LIMIT = ("Codex ended with an error: You've hit your usage limit. Visit https://chatgpt.com/settings/usage "
                   "to purchase more credits or try again at Oct 13th, 2026 9:00 AM.")

    def explorers(self, *jobs):
        """Each job a (name, conclusion, [failure annotation messages]); its id is its position plus 100."""
        site = Path(self.fixtures) / "acme" / "site"
        (site / ".annotations").mkdir(exist_ok=True)
        listed = []
        for number, (name, conclusion, messages) in enumerate(jobs, 100):
            listed.append({"id": number, "name": name, "conclusion": conclusion})
            notes = [{"annotation_level": "failure", "message": "Process completed with exit code 1."}]
            notes += [{"annotation_level": "failure", "message": message} for message in messages]
            notes.append({"annotation_level": "warning", "message": "Node.js 20 is deprecated."})
            (site / ".annotations" / ("%d.json" % number)).write_text(json.dumps(notes))
        (site / ".jobs.json").write_text(json.dumps([{"id": 1, "name": "qae-criteria", "conclusion": "success"}] + listed))

    def test_a_usage_limit_tells_the_driver_to_walk_by_hand(self):
        self.explorers(("qae-explore", "failure", [self.CLAUDE_LIMIT]))
        body = self.posted(explore="failure", gates="", verdict=False)
        self.assertEqual(body.splitlines()[1], "## QA review: could not run, model usage limit \u26a0\ufe0f")
        self.assertIn("The explorer hit its model usage limit on `01234567` (it resets 9am (UTC)), so nothing was "
                      "checked and these checks stay red.", body)
        for step in ("walk every bullet under `## Acceptance criteria`", "`--widths` of the `qae-artifacts` line",
                     "Post the evidence as a pull request comment: one line per criterion, PASS or FAIL",
                     "Rerun this workflow after the reset", "never a verdict"):
            self.assertIn(step, body)
        self.assertIn("[The run and its evidence](%s)" % RUN, body)
        self.assertNotIn("Its log has the cause", body)
        self.assertIn("repos/acme/site/actions/runs/42/jobs?filter=latest&per_page=100",
                      [c["endpoint"] for c in self.calls()])

    def test_the_codex_lanes_limit_and_a_split_run_read_alike(self):
        self.explorers(("qae-explore", "failure", [self.CODEX_LIMIT]), ("qae / qae-explore (2)", "failure", [self.CODEX_LIMIT]))
        body = self.posted(explore="failure", gates="failure", verdict=False)
        self.assertEqual(body.splitlines()[1], "## QA review: could not run, model usage limit \u26a0\ufe0f")
        # The names docs/check-names.md gave the explore jobs read alike.
        self.explorers(("Acceptance criteria: walk", "failure", [self.CODEX_LIMIT]),
                       ("qa / Acceptance criteria: walk (2)", "failure", [self.CODEX_LIMIT]))
        self.assertIn("model usage limit", self.posted(explore="failure", gates="", verdict=False).splitlines()[1])
        self.assertIn("(it resets Oct 13th, 2026 9:00 AM)", body)
        self.explorers(("qae-explore", "failure", ["You've reached your Fable limit. Switch to another model."]))
        self.assertIn("(its message names no reset time)", self.posted(explore="failure", gates="", verdict=False))

    def test_any_other_explore_failure_keeps_the_plain_body(self):
        cases = {
            "no annotation but the exit code": [("qae-explore", "failure", [])],
            "the write scope": [("qae-explore", "failure", ["the explorer wrote outside qae-artifacts/:"])],
            "a server-side rate limit, which a rerun fixes": [("qae-explore", "failure", [
                "Codex ended with an error: API Error: Server is temporarily limiting requests (not your usage limit)"])],
            "one explorer of two for another reason": [("qae-explore", "failure", [self.CLAUDE_LIMIT]),
                                                       ("qae-explore (2)", "failure", ["site did not answer"])],
            "no failed explore job in the latest attempt": [("qae-explore", "success", [self.CLAUDE_LIMIT])],
            "a limit printed by another job": [("qae-verify", "failure", [self.CLAUDE_LIMIT])],
            "the verify job, named as now": [("Acceptance criteria", "failure", [self.CLAUDE_LIMIT])],
        }
        for why, jobs in cases.items():
            self.explorers(*jobs)
            body = self.posted(explore="failure", gates="", verdict=False)
            self.assertEqual(body.splitlines()[1], "## QA review: could not run \u26a0\ufe0f", why)
            self.assertIn("The explore job ended `failure` on `01234567`, so nothing was checked. Its log has the cause", body, why)

    def test_a_cancelled_explore_or_a_missing_run_id_reads_nothing(self):
        self.explorers(("qae-explore", "failure", [self.CLAUDE_LIMIT]))
        for explore, run_id in (("cancelled", "42"), ("failure", "")):
            body = self.posted(explore=explore, gates="", verdict=False, run_id=run_id)
            self.assertEqual(body.splitlines()[1], "## QA review: could not run \u26a0\ufe0f")
            self.assertFalse([c for c in self.calls() if "/jobs" in c["endpoint"]], explore)

    def test_jobs_that_cannot_be_read_still_post_the_plain_body(self):
        # A consumer workflow without `actions: read` and `checks: read`: the read fails, the review is still posted.
        (Path(self.fixtures) / "acme" / "site" / ".jobs.json").write_text("not json")
        result = self.review(explore="failure", gates="", verdict=False)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("::warning::could not tell why the explore job failed", result.stdout)
        self.assertEqual(self.writes()[0]["body"]["body"].splitlines()[1], "## QA review: could not run \u26a0\ufe0f")

    def test_not_required_beats_a_usage_limit(self):
        self.explorers(("qae-explore", "failure", [self.CLAUDE_LIMIT]))
        self.assertIn("not required", self.posted(explore="failure", gates="skipped", not_required="a pin bump", verdict=False))

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
                             VV_TICKET="", VV_EXPLORE="success", VV_GATES="failure", VV_NOT_REQUIRED="", VV_CARRIED="",
                             VV_RUN_ID="42", VV_MANIFEST=".vibe-verifier-qae"))
        result = subprocess.run(["bash", "-e", "-c", script], capture_output=True, text=True, env=env)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        body = self.writes()[0]["body"]["body"]
        self.assertIn("changes needed", body)
        self.assertIn("- AC2, explorer says FAIL: The menu opens\n", body)
        for wired in ("${{ github.event.pull_request.head.sha }}", "${{ inputs.not-required }}", "${{ inputs.gates-outcome }}",
                      "${{ github.server_url }}/${{ github.repository }}/actions/runs/${{ github.run_id }}",
                      "VV_RUN_ID: ${{ github.run_id }}", '--run-id "$VV_RUN_ID"',
                      "VV_MANIFEST: ${{ inputs.manifest }}", '--manifest "$VV_MANIFEST"'):
            self.assertIn(wired, text)


if __name__ == "__main__":
    unittest.main()
