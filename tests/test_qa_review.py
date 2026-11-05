"""`vibe-verifier qa-review` and actions/qa-review: the QAE's one review comment on a pull request,
driven through the command line (and the action's own script) against a fake `gh` that records writes."""
import json
import re
import subprocess
import unittest
from pathlib import Path

from helpers import ROOT, clean_env
from test_comments import problems
from qa_review_fixture import BOT, HEAD, MARKER, RUN, VERDICT, GhFixture

COMMIT = "[this commit](https://github.com/acme/site/commit/%s)" % HEAD

ACTION = ROOT / "actions" / "qa-review" / "action.yml"


class QaReview(GhFixture):
    # What the comment says, per state. The first line is the marker and the second the heading,
    # always: that is how a reader and the next run find it.

    def test_passed(self):
        body = self.posted()
        self.assertEqual(body.splitlines()[:2], [MARKER, "## QA review: passed ✅"])
        self.assertIn(COMMIT, body)
        self.assertIn("| ✅ | **AC1** · The home page loads | loaded |\n", body)
        self.assertIn("- **AC1**: `qae/AC1.md`, step 1: opened / -> the home page\n", body)
        self.assertIn("[The run and its evidence](%s)" % RUN, body)

    def test_the_tickets_criteria_are_listed_after_the_pull_requests(self):
        # The workflow supplies the ticket's criteria (harnesses/qae/README.md); a None in the body cannot hide them.
        ticket = self.file("ticket.md", "## Acceptance criteria\n\n- Prices show in euros\n")
        self.verdict = self.file("verdict.md", VERDICT + "acceptance-check: TC1 -- PASS -- euros (qae/TC1.md::step 1: x)\n")
        body = self.posted(ticket=ticket)
        self.assertIn("| ❌ | **AC2** · The menu opens | the menu stayed shut |\n| ✅ | **TC1** (from the ticket) · Prices show in euros | euros |\n", body)
        self.criteria = self.file("pr-body.md", "## Acceptance criteria\n\n- None: a refactor\n")
        body = self.posted(ticket=ticket)
        self.assertIn("| ✅ | **TC1** (from the ticket) · Prices show in euros |", body)
        self.assertNotIn("None: a refactor", body)

    def test_a_ticket_criterion_the_pull_request_proves_with_a_test_is_listed_as_not_walked(self):
        ticket = self.file("ticket.md", "## Acceptance criteria\n\n- Prices show in euros\n- The estimate lands in the ledger\n")
        self.criteria = self.file("pr-body.md", "## Acceptance criteria\n\n- None: a refactor\n\n"
                                                "ticket-proof: TC2 -- no browser sees the ledger (lib/ledger.test.ts::writes the estimate line)\n")
        self.verdict = self.file("verdict.md", "acceptance-check: TC1 -- PASS -- euros (qae/TC1.md::step 1: x)\n")
        body = self.posted(ticket=ticket)
        self.assertIn("| ✅ | **TC1** (from the ticket) · Prices show in euros | euros |\n| 🧪 | **TC2** (from the ticket) · "
                      "The estimate lands in the ledger | Not walked: the pull request proves it with a test, no browser sees "
                      "the ledger (lib/ledger.test.ts::writes the estimate line) |\n", body)

    def test_a_re_walked_feature_is_listed_as_the_explorer_judged_it(self):
        # docs/feature-map.md: the explorer adds one regression-check line per feature it re-walked.
        self.verdict = self.file("verdict.md", VERDICT + "regression-check: sign-in -- FAIL -- the dashboard never opened "
                                                         "(qae/features/sign-in.md::step 1: signed in -> a blank page)\n")
        body = self.posted(gates="failure")
        self.assertIn("**Features re-walked for regressions**\n\n| | Feature | What the browser showed | States not walked |\n"
                      "|:-:|---|---|---|\n| ❌ | `sign-in` | the dashboard never opened |  |\n", body)
        self.assertIn("- `sign-in`: `qae/features/sign-in.md`, step 1: signed in -> a blank page\n", body)
        self.verdict = self.file("verdict.md", VERDICT)
        self.assertNotIn("re-walked", self.posted(gates="failure"))

    def test_a_fail_the_base_branch_has_too_says_so(self):
        self.verdict = self.file("verdict.md", VERDICT + "regression-check: sign-in -- FAIL -- the dashboard never opened "
                                                         "(qae/features/sign-in.md::step 1: signed in -> a blank page)\n"
                                                         "regression-known: sign-in -- the same blank page "
                                                         "(qae/features/base/sign-in.md::step 1: signed in -> a blank page)\n")
        body = self.posted(gates="failure")
        self.assertIn("| ❌ | `sign-in` | the dashboard never opened. The base branch fails the same way: the same blank page |  |\n", body)
        self.assertIn("- `sign-in` on the base branch: `qae/features/base/sign-in.md`, step 1: signed in -> a blank page\n", body)

    def test_a_re_walk_names_the_states_the_explorer_left_unwalked(self):
        # A re-walk covers at most a few states of a feature, so "re-walked" alone would overstate it. A
        # skip line with no check line is a feature that was not re-walked: the gate's log names it.
        self.verdict = self.file("verdict.md", VERDICT + "regression-check: sign-in -- PASS -- still signs in "
                                                         "(qae/features/sign-in.md::step 1: signed in -> the dashboard)\n"
                                                         "regression-skip: sign-in -- 4, 5, 7\n"
                                                         "regression-skip: billing -- 1, 2\n")
        body = self.posted(gates="failure")
        self.assertIn("| ✅ | `sign-in` | still signs in | 4, 5, 7 |\n", body)
        self.assertNotIn("billing", body)

    def test_changes_needed_lists_each_criterion_as_the_explorer_judged_it(self):
        body = self.posted(gates="failure")
        self.assertEqual(body.splitlines()[1], "## QA review: changes needed ❌")
        self.assertIn("> [!IMPORTANT]\n> The QA gates refused " + COMMIT + ". Fix each row marked ❌", body)
        self.assertIn("| ✅ | **AC1** · The home page loads | loaded |\n", body)
        self.assertIn("| ❌ | **AC2** · The menu opens | the menu stayed shut |\n", body)

    def test_a_criterion_the_explorer_never_answered_says_so(self):
        self.verdict = self.file("verdict.md", VERDICT.splitlines()[0] + "\n")
        self.assertIn("| ⚠️ | **AC2** · The menu opens | No verdict: the explorer wrote no line for it. |\n", self.posted(gates="failure"))

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
        self.assertIn("No browser check on " + COMMIT + ": %s." % reason, body)
        self.assertNotIn("AC1", body)

    def test_a_carried_walk_says_which_head_passed(self):
        reason = "QA passed in [an earlier run](https://github.com/acme/site/actions/runs/5)"
        body = self.posted(explore="skipped", gates="skipped", not_required=reason, verdict=False, carried="a" * 40)
        self.assertEqual(body.splitlines()[1], "## QA review: passed on an earlier head ✅")
        self.assertIn("No new browser check on " + COMMIT + ": %s. The criteria were not edited since." % reason, body)

    def test_could_not_run_when_the_explore_job_did_not_finish(self):
        body = self.posted(explore="failure", gates="failure")
        self.assertEqual(body.splitlines()[1], "## QA review: could not run ⚠️")
        self.assertIn("The explore job ended `failure` on " + COMMIT, body)

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
            self.assertIn("The verify job stopped before its gates ran on " + COMMIT, body)
            self.assertNotIn("refused", body)

    def test_a_skipped_explore_whose_criteria_job_failed_could_not_run(self):
        # The verify job's first step refuses an unfinished criteria job, so its gates never ran. The
        # comment blames that, never the skipped explore job.
        body = self.posted(explore="skipped", gates="", verdict=False)
        self.assertEqual(body.splitlines()[1], "## QA review: could not run \u26a0\ufe0f")
        self.assertIn("The verify job stopped before its gates ran on " + COMMIT, body)
        self.assertNotIn("explore job ended", body)

    def test_a_missing_verdict_file_reads_as_no_verdict(self):
        self.verdict = str(Path(self.fixtures) / "never-written.md")
        self.assertIn("| ⚠️ | **AC1** · The home page loads | No verdict: the explorer wrote no line for it. |\n", self.posted(gates="failure"))

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

    # A consumer's pull request, 2026-11-04: copy-audit, in soak, found its dialog's copy unclear and only the job log said so.
    SOAK = [{"gate": "copy-audit", "path": None, "line": None,
             "message": 'everyday-words on https://site-abc.vercel.app/accounts/36: "One send a minute.". '
                        'Try: "You can send one invite per minute." (div > p)'},
            {"gate": "copy-audit", "path": None, "line": None,
             "message": 'no-system-terms on https://site-abc.vercel.app/accounts/36 (and on 2 more page(s)): '
                        '"as User". Try: "with read access" (div > p)'},
            {"gate": "ui-audit", "path": None, "line": None,
             "message": "touching on https://site-abc.vercel.app/accounts at 375px: Save / Cancel: 2px apart "
                        "(and 3 more like it) (div > button)"}]

    def soak(self, findings):
        return self.file("soak.jsonl", "".join(json.dumps(f) + "\n" for f in findings))

    def test_a_soak_gates_findings_are_listed_as_not_yet_blocking(self):
        body = self.posted(extra=("--soak-findings", self.soak(self.SOAK)))
        self.assertIn("**Not yet blocking.** These gates run in soak", body)
        self.assertIn('- `copy-audit`: everyday-words on /accounts/36: "One send a minute.". Try: "You can send one '
                      'invite per minute." (div > p)\n', body)
        self.assertIn('no-system-terms on /accounts/36: "as User". Try: "with read access" (div > p)\n', body)
        self.assertIn("- `ui-audit`: touching on /accounts at 375px: Save / Cancel: 2px apart (div > button)\n", body)
        self.assertIn("A gate that compares with the base lists only what this pull request added", body)
        self.assertLess(body.index("Not yet blocking"), body.index("[The run and its evidence]"))
        self.assertEqual(problems(body), [], body)
        # A failed walk lists them too: they are the same findings either way.
        self.assertIn("Not yet blocking", self.posted(gates="failure", extra=("--soak-findings", self.soak(self.SOAK))))

    def test_no_soak_findings_no_section(self):
        for extra in ((), ("--soak-findings", self.soak([])), ("--soak-findings", self.file("absent", "") + ".gone")):
            self.assertNotIn("Not yet blocking", self.posted(extra=extra), extra)

    def test_a_line_a_gate_left_half_written_is_counted_not_fatal(self):
        record = self.file("soak.jsonl", json.dumps(self.SOAK[0]) + "\n" + '{"gate": "ui-au')
        body = self.posted(extra=("--soak-findings", record))
        self.assertIn('everyday-words on /accounts/36: "One send a minute."', body)
        self.assertIn("- 1 record line(s) could not be read; the job log has every soak finding.", body)

    def test_a_finding_with_a_file_names_it(self):
        body = self.posted(extra=("--soak-findings", self.soak([{"gate": "new-source-has-test", "path": "lib/a.ts",
                                                                 "line": 3, "message": "no test imports this file"}])))
        self.assertIn("- `new-source-has-test`: lib/a.ts:3: no test imports this file\n", body)

    # The composite action is what a consumer pins: its script passes every input through.

    def test_the_action_posts_with_the_run_it_belongs_to(self):
        text = ACTION.read_text()
        script = re.search(r"^      run: \|\n((?:        .*\n|\n)+)", text, re.MULTILINE).group(1)
        script = "".join(line[8:] if line.strip() else "\n" for line in script.splitlines(True))
        action_path = ROOT / "actions" / "qa-review"
        env = clean_env(dict(self.env, GITHUB_ACTION_PATH=str(action_path), GH_TOKEN="x", VV_REPO="acme/site",
                             VV_PR="7", VV_HEAD=HEAD, VV_RUN=RUN, VV_CRITERIA=self.criteria, VV_VERDICT=self.verdict,
                             VV_TICKET="", VV_EXPLORE="success", VV_GATES="failure", VV_NOT_REQUIRED="", VV_CARRIED="",
                             VV_RUN_ID="42", VV_MANIFEST=".vibe-verifier-qae", VV_LIMITS="", RUNNER_TEMP=self.fixtures))
        (Path(self.fixtures) / "vibe-verifier-soak-findings.jsonl").write_text(json.dumps(self.SOAK[0]) + "\n")
        result = subprocess.run(["bash", "-e", "-c", script], capture_output=True, text=True, env=env)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        body = self.writes()[0]["body"]["body"]
        self.assertIn("changes needed", body)
        self.assertIn("| ❌ | **AC2** · The menu opens | the menu stayed shut |\n", body)
        self.assertIn('everyday-words on /accounts/36: "One send a minute."', body)
        for wired in ("${{ github.event.pull_request.head.sha }}", "${{ inputs.not-required }}", "${{ inputs.gates-outcome }}",
                      "${{ github.server_url }}/${{ github.repository }}/actions/runs/${{ github.run_id }}",
                      "VV_RUN_ID: ${{ github.run_id }}", '--run-id "$VV_RUN_ID"',
                      "VV_MANIFEST: ${{ inputs.manifest }}", '--manifest "$VV_MANIFEST"',
                      "VV_LIMITS: ${{ inputs.limits }}", '--limits "$VV_LIMITS"'):
            self.assertIn(wired, text)


if __name__ == "__main__":
    unittest.main()
