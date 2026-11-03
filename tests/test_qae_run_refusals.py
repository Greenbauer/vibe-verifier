"""qae-artifacts reads `expected-refusal:` declarations as one set for the whole run: a refusal the pull
request's own criteria declare also excuses the ticket criterion that restates it (harnesses/qae/README.md,
"Beyond the criteria"). A plan is written before its route exists, so its criterion says only "is refused";
the builder declares the exact status and path in the pull request. Driven through the gate's command line."""
import os
import shutil
import tempfile
import unittest

from helpers import gate, make_repo, write
from test_qae_artifacts import CLEAN_REQUESTS, session_with

BODY = "## Acceptance criteria\n\n- The cart shows its total\n"
DECLARED = BODY.replace("its total\n", "its total; signed out, /api/orders answers 401 (expected-refusal: 401 /api/orders)\n")
TICKET = "## Acceptance criteria\n\n- Signed out, the orders API is refused with the reason\n"


class RunWideRefusals(unittest.TestCase):
    def setUp(self):
        self.repo = make_repo(self, {"README.md": "x\n"})
        self.work = tempfile.mkdtemp(prefix="vv-run-refusals-")
        self.addCleanup(shutil.rmtree, self.work, True)
        self.artifacts = os.path.join(self.work, "qae-artifacts")
        write(self.artifacts, {"qae/AC1.md": "- step 1: opened /cart -> a total\n", "qae/AC1-step-1.png": "png",
                               "qae/TC1.md": "- step 1: opened /api/orders signed out -> refused\n", "qae/TC1-step-1.png": "png",
                               "session-1/session.md": session_with(
                                   CLEAN_REQUESTS + ["[GET] http://localhost:3000/api/orders => [401] Unauthorized"])})

    def artifacts_gate(self, body):
        write(self.work, {"pr-body.md": body, "ticket.md": TICKET, "site-url": "http://localhost:3000\n"})
        return gate("qae-artifacts", self.repo, "--artifacts", self.artifacts, "--criteria", os.path.join(self.work, "pr-body.md"),
                    "--site-file", os.path.join(self.work, "site-url"), "--ticket", os.path.join(self.work, "ticket.md"))

    def test_a_refusal_the_pull_request_declares_excuses_the_ticket_criterion_that_restates_it(self):
        result = self.artifacts_gate(DECLARED)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_the_declaration_is_still_exact_and_still_needed(self):
        for body in (DECLARED.replace("401 /api", "403 /api"), DECLARED.replace("/api/orders)", "/api/order)"), BODY):
            result = self.artifacts_gate(body)
            self.assertEqual(result.returncode, 1, body)
            self.assertIn("/api/orders", result.stdout)


if __name__ == "__main__":
    unittest.main()
