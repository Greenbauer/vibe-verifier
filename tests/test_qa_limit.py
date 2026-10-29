"""`vibe-verifier qa-limit`, actions/qa-limit and the QAE verify job around them: when every explorer hit its model
usage limit the Acceptance criteria check passes with a warning naming each limit and its reset, and the QA review
records the head as limited (operator, 2026-10-29, the review harness's rule). Anything else stays red. Driven against
the fake `gh` the qa-review tests share."""
import json
import re
import subprocess
import unittest
from pathlib import Path

from helpers import ROOT, clean_env, runner
from qa_review_fixture import HEAD, RUN, GhFixture

COMMIT = "[this commit](https://github.com/acme/site/commit/%s)" % HEAD
LIMIT_ACTION = ROOT / "actions" / "qa-limit" / "action.yml"


class QaLimit(GhFixture):
    # `qa-limit` reads the explore jobs' failure annotations, and when
    # every explorer hit its limit it warns with the limit and its reset and says `limited=true`, so the verify job
    # skips its walk-judging steps and passes; the review then says the walk did not run and records the head as
    # limited. Messages as the lanes print them.

    CLAUDE_LIMIT = ("Action failed with error: SDK execution error: Error: Claude Code returned an error result: "
                    "You've hit your limit · resets 9am (UTC)")  # Greenbauer/zack.land run 38286901786
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

    def limit(self):
        """The qa-limit step on run 42: its stdout and the outputs it wrote to $GITHUB_OUTPUT."""
        output = self.file("github-output", "")
        result = runner("qa-limit", "--repo", "acme/site", "--run-id", "42", "--head", HEAD,
                        env=dict(self.env, GITHUB_OUTPUT=output))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        outputs = dict(line.split("=", 1) for line in Path(output).read_text().splitlines())
        self.assertEqual(sorted(outputs), ["limited", "limits"])
        return result.stdout, outputs

    def test_a_usage_limit_on_every_explorer_passes_with_a_warning(self):
        self.explorers(("Acceptance criteria: walk", "failure", [self.CLAUDE_LIMIT]))
        stdout, outputs = self.limit()
        self.assertEqual(outputs["limited"], "true")
        self.assertIn("::warning::The acceptance walk did not run on %s: every explorer hit its model usage limit "
                      "(Acceptance criteria: walk: You've hit your limit, resets 9am (UTC)). This head was NOT checked "
                      "in a browser; the QA review records it as limited, never as a walk." % HEAD[:10], stdout)
        self.assertIn("repos/acme/site/actions/runs/42/jobs?filter=latest&per_page=100", [c["endpoint"] for c in self.calls()])
        body = self.posted(explore="failure", gates="skipped", verdict=False, extra=("--limits", outputs["limits"]))
        self.assertEqual(body.splitlines()[1], "## QA review: not walked, model usage limit ⚠️")
        self.assertIn("The walk did not run on " + COMMIT + ": every explorer hit its model usage limit, so nothing was "
                      "checked in a browser. The Acceptance criteria check passes with a warning, and this head is "
                      "recorded as limited, never as a walk:", body)
        self.assertIn("\n- Acceptance criteria: walk: You've hit your limit, resets 9am (UTC)\n", body)
        self.assertIn("Rerun this workflow after the reset: the next walk checks every criterion", body)
        self.assertIn("[The run and its evidence](%s)" % RUN, body)
        self.assertTrue(body.endswith("\n<!-- qa-review: %s -- limited -- run 42 -->\n" % HEAD), body)
        self.assertNotIn("Its log has the cause", body)

    def test_the_codex_lanes_limit_and_a_split_run_read_alike(self):
        self.explorers(("qae-explore", "failure", [self.CODEX_LIMIT]), ("qae / qae-explore (2)", "failure", [self.CODEX_LIMIT]))
        stdout, outputs = self.limit()
        self.assertEqual(outputs["limited"], "true")
        self.assertIn("qae-explore: You've hit your usage limit, resets Oct 13th, 2026 9:00 AM; "
                      "qae / qae-explore (2): You've hit your usage limit, resets Oct 13th, 2026 9:00 AM", stdout)
        # The names docs/check-names.md gave the explore jobs read alike.
        self.explorers(("Acceptance criteria: walk", "failure", [self.CODEX_LIMIT]),
                       ("qa / Acceptance criteria: walk (2)", "failure", [self.CODEX_LIMIT]))
        self.assertEqual(self.limit()[1]["limited"], "true")
        self.explorers(("qae-explore", "failure", ["You've reached your Fable limit. Switch to another model."]))
        stdout, outputs = self.limit()
        self.assertIn("qae-explore: You've reached your Fable limit, its message names no reset time", stdout)
        body = self.posted(explore="failure", gates="skipped", verdict=False, extra=("--limits", outputs["limits"]))
        self.assertIn("- qae-explore: You've reached your Fable limit, its message names no reset time\n", body)

    def test_a_usage_limit_on_only_some_explorers_stays_red(self):
        # Any explorer that walked (its verdict is the gates' to judge) or failed another way keeps the job red.
        cases = {
            "one explorer of two for another reason": [("qae-explore", "failure", [self.CLAUDE_LIMIT]),
                                                       ("qae-explore (2)", "failure", ["site did not answer"])],
            "one explorer of two walked": [("Acceptance criteria: walk", "success", []),
                                           ("Acceptance criteria: walk (2)", "failure", [self.CODEX_LIMIT])],
        }
        for why, jobs in cases.items():
            self.explorers(*jobs)
            stdout, outputs = self.limit()
            self.assertEqual(outputs, {"limited": "false", "limits": "[]"}, why)
            self.assertNotIn("::warning::", stdout, why)

    def test_any_other_explore_failure_stays_red(self):
        cases = {
            "a server-side rate limit, which a rerun fixes": [("qae-explore", "failure", [
                "Codex ended with an error: API Error: Server is temporarily limiting requests (not your usage limit)"])],
            "no annotation but the exit code": [("qae-explore", "failure", [])],
            "the write scope": [("qae-explore", "failure", ["the explorer wrote outside qae-artifacts/:"])],
            "no failed explore job in the latest attempt": [("qae-explore", "success", [self.CLAUDE_LIMIT])],
            "a limit printed by another job": [("qae-verify", "failure", [self.CLAUDE_LIMIT])],
            "the verify job, named as now": [("Acceptance criteria", "failure", [self.CLAUDE_LIMIT])],
        }
        for why, jobs in cases.items():
            self.explorers(*jobs)
            self.assertEqual(self.limit()[1], {"limited": "false", "limits": "[]"}, why)
            body = self.posted(explore="failure", gates="", verdict=False, extra=("--limits", "[]"))
            self.assertEqual(body.splitlines()[1], "## QA review: could not run ⚠️", why)
            self.assertIn("The explore job ended `failure` on " + COMMIT + ", so nothing was checked. Its log has the cause", body, why)
            self.assertNotIn("limited", body, why)

    def test_jobs_that_cannot_be_read_stay_red(self):
        # A consumer workflow without `actions: read` and `checks: read`: the read fails, and nothing is excused.
        (Path(self.fixtures) / "acme" / "site" / ".jobs.json").write_text("not json")
        stdout, outputs = self.limit()
        self.assertIn("::warning::could not tell why the explore job failed", stdout)
        self.assertEqual(outputs, {"limited": "false", "limits": "[]"})

    def test_the_review_reads_no_annotations_itself(self):
        # The qa-limit step decides once; the review only reports what it was given.
        self.explorers(("qae-explore", "failure", [self.CLAUDE_LIMIT]))
        body = self.posted(explore="failure", gates="", verdict=False)
        self.assertEqual(body.splitlines()[1], "## QA review: could not run ⚠️")
        self.assertFalse([c for c in self.calls() if "/jobs" in c["endpoint"] or "annotations" in c["endpoint"]])

    def test_not_required_beats_a_usage_limit(self):
        limits = json.dumps([{"job": "qae-explore", "limit": "You've hit your limit", "reset": "9am (UTC)"}])
        self.assertIn("not required", self.posted(explore="failure", gates="skipped", not_required="a pin bump", verdict=False,
                                                  extra=("--limits", limits)))

    def test_the_limit_action_reads_the_run_it_belongs_to(self):
        text = LIMIT_ACTION.read_text()
        script = re.search(r"^      run: \|\n((?:        .*\n|\n)+)", text, re.MULTILINE).group(1)
        script = "".join(line[8:] if line.strip() else "\n" for line in script.splitlines(True))
        self.explorers(("Acceptance criteria: walk", "failure", [self.CLAUDE_LIMIT]))
        output = self.file("github-output", "")
        env = clean_env(dict(self.env, GITHUB_ACTION_PATH=str(ROOT / "actions" / "qa-limit"), GH_TOKEN="x",
                             VV_REPO="acme/site", VV_RUN_ID="42", VV_HEAD=HEAD, GITHUB_OUTPUT=output))
        result = subprocess.run(["bash", "-e", "-c", script], capture_output=True, text=True, env=env)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("limited=true\n", Path(output).read_text())
        for wired in ("default: ${{ github.token }}", "VV_RUN_ID: ${{ github.run_id }}",
                      "VV_HEAD: ${{ github.event.pull_request.head.sha }}", "value: ${{ steps.read.outputs.limited }}",
                      "value: ${{ steps.read.outputs.limits }}"):
            self.assertIn(wired, text)


class Templates(unittest.TestCase):
    def test_a_usage_limit_on_every_explorer_skips_what_judges_a_walk(self):
        # Operator, 2026-10-29: the verify job passes with the qa-limit step's warning when every explorer hit its
        # model usage limit, as the review harness does when every reviewer account is rate-limited. Both lanes.
        for name in ("explore.yml", "explore-codex.yml"):
            with self.subTest(template=name):
                text = (ROOT / "harnesses" / "qae" / name).read_text()
                verify = text[text.index("\n  verify:\n"):]
                steps = {step.split("\n", 1)[0]: step for step in verify.split("\n      - ")[1:]}
                limit = steps["name: Tell a model usage limit from a failure"]
                self.assertIn("        id: limit\n        if: needs.explore.result == 'failure'\n", limit)
                self.assertIn("uses: Greenbauer/vibe-verifier/actions/qa-limit@", limit)
                self.assertLess(verify.index("- name: Read the criteria"), verify.index("- name: Tell a model usage limit"))
                skipped = [key for key in steps if key.startswith("uses: actions/download-artifact@")] + [
                    "name: Merge the evidence and read the verdict", "name: Run the QA gates",
                    "name: Require every explorer of a split run"]
                for key in skipped:
                    self.assertLess(verify.index("- name: Tell a model usage limit"), verify.index("- " + key), key)
                    condition = re.search(r"^        if: (.*)$", steps[key], re.MULTILINE).group(1)
                    self.assertIn("steps.limit.outputs.limited != 'true'", condition, key)
                review = steps["name: Post the QA review"]
                self.assertIn("          limits: ${{ steps.limit.outputs.limits }}\n", review)
                self.assertNotIn("limited", "".join(step for key, step in steps.items() if key not in skipped
                                                    and key != "name: Tell a model usage limit from a failure"
                                                    and key != "name: Post the QA review"))



if __name__ == "__main__":
    unittest.main()
