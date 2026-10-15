"""Explorers of one run keep off each other's records: the qae-artifacts gate reads what each explorer's browser
recorded (actions/qae-browser/console-pages.js, isolation lines of console-pages.jsonl) and fails the run on a
request the harness refused and on a record one explorer opened that another created (gates/_page_record.py).
The browser's side is tested in actions/qae-browser/console-pages.test.js and, in the real browser,
tests/test_qae_console_pages.py; the isolation file it reads, in tests/test_qae_chains.py."""
import hashlib
import json
import shutil
import tempfile
import unittest

from helpers import gate, make_repo, write
from test_qae_artifacts import CLEAN_REQUESTS, session_with

def sha256(text):
    return hashlib.sha256(text.encode()).hexdigest()


class Isolation(unittest.TestCase):
    def setUp(self):
        self.repo = make_repo(self, {"README.md": "x"})
        self.root = tempfile.mkdtemp(prefix="vv-qae-isolation-")
        self.addCleanup(shutil.rmtree, self.root, True)
        write(self.root, {"qae/AC1.md": "- step 1: navigated to / -> saw the heading\n", "qae/AC1-step-1.png": "png",
                          "session-1/session.md": session_with(CLEAN_REQUESTS)})

    def record(self, *lines, **files):
        write(self.root, dict(files, **{"console-pages.jsonl": "".join(json.dumps(line) + "\n" for line in lines)}))
        return gate("qae-artifacts", self.repo, "--artifacts", self.root)

    def test_a_record_one_explorer_opened_that_another_created_fails_the_run(self):
        # A consumer's split run (2026-10-15): explorer 4 created a survey, answered it and deleted it, while
        # explorer 2 opened it from the list both saw and failed a correct criterion on the 404 that followed.
        survey = "1d20852d-9a77-4a2f-8d20-5981833053d8"
        url = "https://api.example/preview/api/journeys/20000000-0000-4000-8000-000000000001/surveys/" + survey
        theirs = [{"isolation": "qae-9.1-e4"}, {"created": survey, "explorer": "qae-9.1-e4"},
                  {"opened": survey, "explorer": "qae-9.1-e4", "request": "DELETE " + url}, {"isolation": "qae-9.1-e2"}]
        result = self.record(*theirs, {"opened": survey, "explorer": "qae-9.1-e2", "request": "GET " + url},
                             {"opened": survey, "explorer": "qae-9.1-e2", "request": "GET " + url + "/results"})
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("explorer qae-9.1-e2's GET %s worked on record %s, which explorer qae-9.1-e4 created in this run" % (url, survey),
                      result.stdout)
        self.assertEqual(result.stdout.count("worked on record"), 1, result.stdout)
        # Its own record, and one two explorers' answers both named (a sign-in's user), are nobody else's.
        result = self.record(*theirs, {"created": survey, "explorer": "qae-9.1-e2"},
                             {"opened": survey, "explorer": "qae-9.1-e2", "request": "GET " + url})
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.record({"opened": survey, "explorer": "qae-9.1-e4", "request": "GET " + url}).returncode, 0)

    def test_a_request_the_harness_refused_is_named_once_and_its_409_is_not_judged_again(self):
        url = "http://localhost:3000/api/surveys/949a15ad-56e4-424f-8a7d-5885ebd385cc"
        why = "the record is a fixture a criterion outside this explorer's share uses"
        refused = {"refused": sha256("DELETE " + url), "location": sha256(url), "explorer": "qae-9.1-e2", "request": "DELETE " + url, "why": why}
        console = "[   15ms] [ERROR] Failed to load resource: the server responded with a status of 409 (Conflict) @ %s:0\n"
        result = self.record(refused, **{"session-1/session.md": session_with(CLEAN_REQUESTS + ["[DELETE] %s => [409] Conflict" % url]),
                                         "console-2.log": console % url})
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("explorer qae-9.1-e2's browser refused DELETE %s: %s" % (url, why), result.stdout)
        self.assertNotIn("answered 409", result.stdout)
        self.assertNotIn("console error", result.stdout)
        # A 409 at a URL the harness did not refuse is the site's, and still judged.
        other = url.replace("949a", "0000")
        result = self.record(refused, **{"session-1/session.md": session_with(CLEAN_REQUESTS + ["[DELETE] %s => [409] Conflict" % other]),
                                         "console-2.log": console % other})
        self.assertIn("request answered 409 outside the allowlist: [DELETE] " + other, result.stdout)
        self.assertIn("console error outside the allowlist: Failed to load resource", result.stdout)

    def test_a_run_of_one_explorer_records_no_isolation_and_is_judged_as_before(self):
        result = self.record({"page": "http://localhost:3000/", "sha256": sha256("x")})
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
