"""actions/ticket's plan store: a `[plan:<id>]` marker in a pull request's title or body makes a plan the consumer's
plan store answers (`GET <endpoint>/plans/<id>`), with its amendments, part of the ticket's criteria. Driven through
the action's script against a plan store served on localhost."""
import http.server
import json
import os
import subprocess
import sys
import threading
import unittest
from pathlib import Path

from helpers import ROOT, clean_env, runner, write
from test_qae_ticket import BODY, workdir


STORED_PLAN = ("# Show prices in euros\n\n## Goal\n\n- Not a criterion: the goal\n\n## Acceptance criteria\n\n"
               "1. Prices show in euros on the shop page.\n2. The checkout total matches the cart,\n   tax included.\n"
               "3. Unit tests cover the euro formatter.\n\n## Test steps\n\n1. Not a criterion either\n\n"
               "## Acceptance (to be carried into the PR)\n\n- A refund shows in euros\n")
AMENDED = {"body": "## Amendment 1: rounding\n\nRound to the cent.\n\n**Added acceptance criteria**\n"
                   "4. A price of 1.005 shows as 1.01.\n5. The PDF uses the same rounding.\n\n**Out of scope**\n- Not this\n"}
PLAIN_AMENDMENT = {"body": "Also show the currency code\nbeside every total."}
PLAN_ID = "5d2e1d27-1c19-4f5f-a5be-99f64acddd0a"


class PlanStore(unittest.TestCase):
    """actions/ticket: a `[plan:<id>]` marker in the title or body makes a plan in the consumer's plan store, with
    its amendments, part of the ticket; a plan the step cannot read fails, never "no ticket"."""

    def setUp(self):
        self.plans, self.seen = {}, []
        test = self

        class Store(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                test.seen.append((self.path, self.headers.get("Authorization")))
                plan = test.plans.get(self.path.rsplit("/", 1)[-1])
                status = 200 if plan is not None and self.headers.get("Authorization") == "Bearer secret" else 404
                body = json.dumps(plan if status == 200 else {"error": "not found"}).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Store)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.endpoint = "http://127.0.0.1:%d/api/v1" % self.server.server_port
        self.work = workdir(self)

    def run_ticket(self, body, title="", token="secret", endpoint=None, summary=False):
        write(self.work, {"body.md": body})
        out = os.path.join(self.work, "ticket.md")
        if os.path.exists(out):
            os.remove(out)
        env = clean_env({"VV_PLANS_TOKEN": token})
        if summary:
            env["GITHUB_STEP_SUMMARY"] = os.path.join(self.work, "summary.md")
        result = subprocess.run([sys.executable, str(ROOT / "actions" / "ticket" / "ticket.py"), "--body",
                                 os.path.join(self.work, "body.md"), "--repo", "o/r", "--base", "b" * 40, "--out", out,
                                 "--title", title, "--plans-endpoint", self.endpoint if endpoint is None else endpoint],
                                capture_output=True, text=True, env=env)
        return result, (Path(out).read_text() if os.path.exists(out) else None)

    def criteria(self, text):
        return text[text.index("## Acceptance criteria"):].splitlines()[2:]

    def test_the_plans_acceptance_sections_and_each_amendment_are_the_ticket(self):
        self.plans[PLAN_ID] = {"planMarkdown": STORED_PLAN, "amendments": [AMENDED, PLAIN_AMENDMENT]}
        result, text = self.run_ticket(BODY, title="Euro prices [plan:%s]" % PLAN_ID.upper(), summary=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.criteria(text), [
            "- Prices show in euros on the shop page.", "- The checkout total matches the cart, tax included.",
            "- Unit tests cover the euro formatter.", "- A refund shows in euros",
            "- A price of 1.005 shows as 1.01.", "- The PDF uses the same rounding.",
            "- Amendment 2: Also show the currency code beside every total."])
        self.assertEqual(self.seen, [("/api/v1/plans/%s" % PLAN_ID, "Bearer secret")])
        self.assertIn("ticket: 7 criteria", result.stdout)
        self.assertIn("- TC3: Unit tests cover the euro formatter.\n", Path(self.work, "summary.md").read_text())
        # The criteria job reads them as TC1-TC7 beside the body's AC1.
        write(self.work, {"pr-body.md": BODY})
        read = runner("criteria", os.path.join(self.work, "pr-body.md"), "--ticket", os.path.join(self.work, "ticket.md"))
        self.assertEqual(read.stdout.splitlines()[0], "criteria: 8", read.stdout + read.stderr)

    def test_a_body_marker_and_an_amendments_criteria_heading_are_read_too(self):
        heading = {"body": "## Amendment 1\n\nWhy it changed.\n\n### Acceptance criteria\n\n- Totals round to the cent\n\n"
                           "### Notes\n\n- not a criterion\n"}
        self.plans[PLAN_ID] = {"planMarkdown": STORED_PLAN, "amendments": [heading]}
        result, text = self.run_ticket(BODY + "\nImplements [plan:%s].\n" % PLAN_ID)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.criteria(text)[-2:], ["- A refund shows in euros", "- Totals round to the cent"])

    def test_a_marker_in_a_fence_or_a_comment_names_no_plan_and_no_store_reads_none(self):
        self.plans[PLAN_ID] = {"planMarkdown": STORED_PLAN, "amendments": []}
        for body in ("```\n[plan:%s]\n```\n" % PLAN_ID, "<!-- [plan:%s] -->\n" % PLAN_ID):
            self.assertEqual(self.run_ticket(body)[1], "", body)
        # A consumer that names no store reads no plan, whatever the body says.
        result, text = self.run_ticket("[plan:%s]\n" % PLAN_ID, endpoint="")
        self.assertEqual((result.returncode, text), (0, ""), result.stderr)
        self.assertEqual(self.seen, [])

    def test_a_plan_the_step_cannot_read_fails(self):
        self.plans[PLAN_ID] = {"planMarkdown": "# A plan\n\n## Goal\n\n- prose only\n", "amendments": []}
        other = "a8f1bf53-0a04-4d73-8ebd-57bffa15257d"
        for body, token, says in (("[plan:%s]" % PLAN_ID, "secret", "has no list under an Acceptance heading"),
                                  ("[plan:%s]" % other, "secret", "the plan store answered 404"),
                                  ("[plan:%s]" % PLAN_ID, "", "the step has no token for the plan store"),
                                  ("[plan:%s]" % PLAN_ID, "wrong", "the plan store answered 404")):
            result, text = self.run_ticket(body + "\n", token=token)
            self.assertEqual(result.returncode, 2, says)
            self.assertIn(says, result.stderr)
            self.assertIsNone(text, says)
        # Only an http(s) store is read: a file:// endpoint is refused before any read.
        result, text = self.run_ticket("[plan:%s]\n" % PLAN_ID, endpoint="file:///etc/passwd")
        self.assertEqual(result.returncode, 2)
        self.assertIn("not an http(s) URL", result.stderr)
