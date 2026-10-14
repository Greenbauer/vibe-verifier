"""qae-artifacts and a request the page cancelled whose answer only the page record holds: a static
request (an image, font, script or stylesheet), which the explorer's network record leaves out when it
answers and lists when the page cancels it. The record is read by gates/_page_record.py."""
import hashlib
import json
import os
import shutil
import sys
import tempfile
import unittest

from helpers import ROOT, gate, make_repo, write
from test_qae_artifacts import BLOCKED, page_record, session_saving_to

sys.path.insert(0, str(ROOT / "gates"))
import _page_record  # noqa: E402  (the gates directory is not a package)

# A consumer QAE run of 2026-10-14, attempt 1: qae/AC2-network.txt as the explorer saved it, with
# `static: false`, the tool's default. The navbar logo answered on every earlier page of the run, and
# the record hides a successful static request, so none of those answers is in any network record.
PREVIEW = "https://site-git-fix-api.vercel.app"
LOGO = PREVIEW + "/_next/image?url=%2Fbranding%2Flogo.svg&w=640&q=75"
AC2_NETWORK = "\n".join([
    "44. [GET] %s => [FAILED] net::ERR_ABORTED" % LOGO,
    "46. [POST] %s/developers => [200] " % PREVIEW,
    "47. [GET] %s/?_rsc=UmH_59hfhGLBpwNk => [200] " % PREVIEW,
    "48. [POST] %s/developers => [200] " % PREVIEW,
    "49. [POST] %s/developers => [200] " % PREVIEW,
    "",
    'Note: 44 static requests not shown, run with "static" option to see them.',
])


def answered(*requests):
    """The page record's line for each request a page got an answer below 400 for: `(method, url)`."""
    return "".join(json.dumps({"answered": hashlib.sha256(("%s %s" % request).encode()).hexdigest()}) + "\n" for request in requests)


class StaticAnswers(unittest.TestCase):
    def setUp(self):
        self.repo = make_repo(self, {"README.md": "x"})
        self.root = tempfile.mkdtemp(prefix="vv-qae-")
        self.addCleanup(shutil.rmtree, self.root, True)
        write(self.root, {
            "qae/AC2.md": "- step 1: opened /developers from the dashboard -> saw the API reference\n", "qae/AC2-step-1.png": "png",
            "session-1/session.md": session_saving_to("qae-artifacts/qae/AC2-network.txt"),
            "qae-artifacts/qae/AC2-network.txt": AC2_NETWORK,
        })

    def run_gate(self):
        return gate("qae-artifacts", self.repo, "--artifacts", self.root, "--site", PREVIEW)

    def test_a_cancelled_static_request_is_answered_by_the_page_record(self):
        # On the explorer's record alone the cancelled logo is listed and its answers are not.
        alone = self.run_gate()
        self.assertEqual(alone.returncode, 1)
        self.assertIn("request cancelled and never answered in this run, outside the allowlist: [GET] " + LOGO, alone.stdout)
        # The harness's page record holds every answer a page got, static ones included.
        write(self.root, {"console-pages.jsonl": answered(("GET", PREVIEW + "/developers"), ("GET", LOGO))})
        result = self.run_gate()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_only_the_same_request_answered_excuses_it(self):
        # Not the logo at another width, not another method, not the path without its query.
        for other in (("GET", LOGO.replace("w=640", "w=384")), ("POST", LOGO), ("GET", LOGO.partition("?")[0])):
            write(self.root, {"console-pages.jsonl": page_record(("https://pay.example/", BLOCKED)) + answered(other)})
            self.assertIn("request cancelled and never answered", self.run_gate().stdout, other)

    def test_an_answer_excuses_no_other_failure(self):
        write(self.root, {"console-pages.jsonl": answered(("GET", LOGO)),
                          "qae-artifacts/qae/AC2-network.txt": AC2_NETWORK.replace("ERR_ABORTED", "ERR_CONNECTION_RESET")})
        result = self.run_gate()
        self.assertEqual(result.returncode, 1)
        self.assertIn("request answered FAILED outside the allowlist: [GET] " + LOGO, result.stdout)


class PageRecord(unittest.TestCase):
    def test_pages_and_answers_are_read_apart_and_a_broken_line_names_neither(self):
        root = tempfile.mkdtemp(prefix="vv-record-")
        self.addCleanup(shutil.rmtree, root, True)
        self.assertEqual(_page_record.page_record(root), ({}, set()))
        write(root, {_page_record.NAME: page_record(("https://pay.example/", BLOCKED)) + answered(("GET", LOGO))
                     + '{"page": "https://pay.exa\n{"sha256": "x"}\n7\n"answered"\n'})
        pages, answers = _page_record.page_record(root)
        self.assertEqual(pages, {_page_record.digest(BLOCKED): ["https://pay.example/"]})
        self.assertEqual(answers, {_page_record.digest("GET " + LOGO)})


if __name__ == "__main__":
    unittest.main()
