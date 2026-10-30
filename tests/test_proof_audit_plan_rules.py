"""The plan-store rules a review of the first cut found failing open: an aside heading is its whole phrase, an aside
label leaves out only the block under it, only an `Out of scope:` label leaves an item out, a `Split off:` line is one
with `->` (a list item too) and never names this pull request, a pull request grades something, a title may start with
a dash, the store's token goes nowhere but the store, and never to a redirect. Fixtures:
tests/test_proof_audit_plan_store.py."""
import http.server
import subprocess
import threading
import unittest

from helpers import ROOT, clean_env, runner, write

import _plan_store  # noqa: E402  (tests/helpers.py puts gates/ on the path)
import test_proof_audit as base  # as modules, so their test classes are not collected twice
import test_proof_audit_plan_store as stored
import test_proof_audit_unlinked as unlinked
import test_qae_plan_store as store

PHRASES = ("# Plan\n\n## Context menu\n- Right-click opens the menu.\n## Background jobs\n- Exports run in the background.\n"
           "## Problems fixed\n- Totals no longer double count.\n"                                          # 1-8
           "## Why (business context, verified 2026-10-01)\n- left out\n## Repo baseline (verified on main)\n- left out\n"
           "## Why: the client asked\n- left out\n## Out of scope \N{EM DASH} later\n- left out\n## Why?\n- left out\n\n"  # 9-19
           "## Scope\n\n**Background**\nThe page is slow today.\n\n- The page loads in under 2s.\n\n"         # 20-25
           "**Context**\nSlow today.\n- Loads in under a second.\n\n"                                          # 27-29
           "- Out-of-scope requests return 403.\n- Out of scope: the PDF.\n- **Out of scope:** the app.\n"       # 31-33
           "- **Out of scope** the CLI.\n"                                                                   # 34
           "## Context-aware search\n- Results rank by the open project.\n**Problem-solving tools**\n- A search box.\n"  # 35-38
           "## Background - why it is slow\n- left out\n")                                                   # 39-40
# tests/test_proof_audit_plan_store.py's fake Codex, which also writes whether it was given the store's token.
FAKE_ENV = stored.FAKE.replace('behaviour = os.environ.get("FAKE_BEHAVIOUR", "")',
                               'open(os.environ["FAKE_LOG"] + ".env", "a").write("%s\\n" % ("VV_PLANS_TOKEN" in os.environ))\n'
                               'behaviour = os.environ.get("FAKE_BEHAVIOUR", "")')


class PlanRules(unittest.TestCase):
    setUp, audit, planned = stored.PlanStoreAudit.setUp, stored.PlanStoreAudit.audit, stored.PlanStoreAudit.planned
    run_ticket, outside = store.PlanStore.run_ticket, base.ProofAudit.outside

    def test_an_aside_heading_is_its_whole_phrase_and_a_label_leaves_out_one_block(self):
        items = _plan_store.plan_requirements([(stored.PLAN_ID, {"planMarkdown": PHRASES, "amendments": []})])
        self.assertEqual([(item["line"], item["text"]) for item in items], [
            (4, "Right-click opens the menu."), (6, "Exports run in the background."), (8, "Totals no longer double count."),
            (25, "The page loads in under 2s."), (29, "Loads in under a second."), (31, "Out-of-scope requests return 403."),
            (36, "Results rank by the open project."), (38, "A search box.")])

    def test_a_split_off_line_has_an_arrow_and_may_be_a_list_item(self):
        # Prose that starts "Split off:" splits nothing off, with a plan store or without one.
        write(self.work, {"prose.md": "Split off: the CSS cleanup went to #41\n"})
        result = runner("proof-audit", "--repo", self.repo, "--body", str(self.work / "prose.md"), "--plan",
                             str(self.work / "plan.json"))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        result, items = self.planned(body="Split off: the CSS cleanup went to #41\n")
        self.assertEqual((result.returncode, len(items)), (0, len(stored.CUT)), result.stderr)
        result, _ = self.planned(body="Split off: PL-99 -> #41\n")
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        # As a list item, it gives its IDs away, and its marker names the other owner's plan, which no step reads.
        body = "- Split off: PL-3 -> [plan:%s]\n1. Split off: PL-4 -> #12\n" % stored.OTHER
        result, items = self.planned(body=body)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertFalse({"PL-3", "PL-4"} & {item["id"] for item in items})
        self.plans[stored.PLAN_ID] = {"planMarkdown": store.STORED_PLAN, "amendments": []}  # one with Acceptance items
        result, _ = self.run_ticket(body, title="[plan:%s]" % stored.PLAN_ID)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual({path for path, _ in self.seen}, {"/api/v1/plans/%s" % stored.PLAN_ID})

    def test_no_owner_is_this_pull_request_and_something_is_graded(self):
        url = "https://github.com/Greenbauer/vibe-verifier/pull/272"
        for line in ("Split off: PL-1 -> #272", "Split off: PL-1 -> %s" % url):
            with self.subTest(line=line):
                result, _ = self.planned("--pr", "272", body=line + "\n", env={"GITHUB_REPOSITORY": "Greenbauer/vibe-verifier"})
                self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
                self.assertIn("which is this pull request or the plan it implements", result.stderr)
        # Another pull request, or the same number in another repository, takes it.
        body = "Split off: PL-1 -> #271\nSplit off: PL-2 -> https://github.com/Greenbauer/other/pull/272\n"
        result, items = self.planned("--pr", "272", body=body, env={"GITHUB_REPOSITORY": "Greenbauer/vibe-verifier"})
        self.assertEqual((result.returncode, len(items)), (0, len(stored.CUT) - 2), result.stderr)
        everything = "Split off: %s -> #9\n" % ", ".join(rid for rid, _, _ in stored.CUT)
        result, _ = self.planned(body=everything)
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn("every requirement (11) is split off, so nothing is graded", result.stderr)

    def test_a_title_that_starts_with_a_dash_is_still_the_title(self):
        title = "-WIP[plan:%s]" % stored.PLAN_ID  # no space, so argparse would read it as an option
        write(self.work, {"body.md": "Built it.\n", "output": ""})
        env = clean_env({"GITHUB_ACTION_PATH": str(ROOT / "actions" / "proof-audit"), "RUNNER_TEMP": str(self.work),
                         "GITHUB_OUTPUT": str(self.work / "output"), "VV_BODY": str(self.work / "body.md"),
                         "VV_MAX_IDS": "10", "VV_TITLE": title, "VV_PLANS_ENDPOINT": self.endpoint,
                         "VV_PLANS_TOKEN": "secret", "VV_PR": "272"})
        result = subprocess.run(["bash", "--noprofile", "--norc", "-eo", "pipefail", "-c",
                                 unlinked.action_step("- name: Plan the proof audit")], cwd=self.repo, env=env,
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("count=%d\n" % len(stored.CUT), (self.work / "output").read_text())
        grades = [stored.proven(rid) for rid, _, _ in stored.CUT]
        result, summary = unlinked.Unlinked.verify_step(self, self.repo, "Built it.\n", grades=[
            base.envelope(1, [grade["id"] for grade in grades], grades)], VV_TITLE=title, VV_PR="272",
            VV_PLANS_ENDPOINT=self.endpoint, VV_PLANS_TOKEN="secret")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("11 of 11 requirements proven or excluded", summary)
        # The local run hands the title on to the gate as one argument too.
        result = self.audit("--grades", str(self.work / "out"), title=title, body="Built it.\n", grades=grades)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_the_model_is_never_given_the_stores_token(self):
        write(self.work, {"codex": FAKE_ENV})
        grades = [stored.proven(rid) for rid, _, _ in stored.CUT]
        result = self.audit("--grades", str(self.work / "out"), body="Built it.\n", grades=grades)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)  # the plan step and the gate had it
        self.assertEqual((self.work / "calls.jsonl.env").read_text().split(), ["False", "False"])  # two shards

    def test_a_store_that_redirects_is_not_followed(self):
        target = "http://localhost:%d/api/v1/plans/%s" % (self.server.server_port, stored.PLAN_ID)

        class Moved(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(302)
                self.send_header("Location", target)
                self.end_headers()

            def log_message(self, *args):
                pass

        moved = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Moved)
        threading.Thread(target=moved.serve_forever, daemon=True).start()
        self.addCleanup(moved.server_close)
        self.addCleanup(moved.shutdown)
        self.endpoint = "http://127.0.0.1:%d/api/v1" % moved.server_port
        result, _ = self.planned(body="Built it.\n")
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn("the plan store answered 302 (a redirect, which is not followed", result.stderr)
        self.assertEqual(self.seen, [])  # the token never reached the address it pointed at
