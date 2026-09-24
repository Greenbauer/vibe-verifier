"""The qae-artifacts adjudicator, driven through its command line against a synthetic artifact
directory shaped like what playwright-mcp and the explorer write (see harnesses/qae/)."""
import json
import os
import shutil
import tempfile
import unittest

from helpers import gate, make_repo, write

SESSION = """### Tool call: browser_navigate
- Args
```json
{"url": "http://localhost:3000"}
```
- Result
```json
{"page": "- Page URL: http://localhost:3000/"}
```

### Tool call: browser_network_requests
- Args
```json
{}
```
- Result
```json
%s
```
"""


def session_with(requests):
    return SESSION % json.dumps({"result": "\n".join("%d. %s" % (n + 1, line) for n, line in enumerate(requests))})


CLEAN_REQUESTS = ["[GET] http://localhost:3000/ => [200] OK", "[GET] http://localhost:3000/bid-study => [200] OK"]


class QaeArtifacts(unittest.TestCase):
    def setUp(self):
        self.repo = make_repo(self, {"README.md": "x"})
        self.root = tempfile.mkdtemp(prefix="vv-qae-")
        self.addCleanup(shutil.rmtree, self.root, True)
        write(self.root, {
            "qae/AC1.md": "- step 1: navigated to / -> saw the heading\n- step 2: clicked the link -> reached /bid-study\n",
            "qae/AC1-step-1.png": "png", "qae/AC1-step-2.png": "png",
            "console-1.log": "[   606ms] [LOG] hello @ http://localhost:3000/app.js:0\n",
            "session-1/session.md": session_with(CLEAN_REQUESTS),
        })

    def run_gate(self, *args):
        return gate("qae-artifacts", self.repo, "--artifacts", self.root, *args)

    def test_clean_run_passes(self):
        result = self.run_gate()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_a_step_without_a_screenshot_fails(self):
        os.remove(os.path.join(self.root, "qae", "AC1-step-2.png"))
        result = self.run_gate()
        self.assertEqual(result.returncode, 1)
        self.assertIn("step 2 has no screenshot (expected qae/AC1-step-2.png)", result.stdout)

    def test_no_step_logs_fails(self):
        shutil.rmtree(os.path.join(self.root, "qae"))
        result = self.run_gate()
        self.assertEqual(result.returncode, 1)
        self.assertIn("no step logs", result.stdout)

    def test_console_error_fails_unless_allowlisted(self):
        write(self.root, {"console-1.log": "[   606ms] [ERROR] Failed to load resource: 404 @ http://localhost:3000/_vercel/insights/script.js:0\n"})
        result = self.run_gate()
        self.assertEqual(result.returncode, 1)
        self.assertIn("console error outside the allowlist", result.stdout)
        self.assertEqual(self.run_gate("--allow-console", "/_vercel/insights/script\\.js").returncode, 0)

    def test_failed_request_fails_unless_allowlisted(self):
        write(self.root, {"session-1/session.md": session_with(CLEAN_REQUESTS + ["[GET] http://localhost:3000/api/quote => [500] Internal Server Error"])})
        result = self.run_gate()
        self.assertEqual(result.returncode, 1)
        self.assertIn("request answered 500 outside the allowlist: [GET] http://localhost:3000/api/quote", result.stdout)
        self.assertEqual(self.run_gate("--allow-request", "/api/quote").returncode, 0)

    def test_a_network_failure_counts_like_an_error_status(self):
        write(self.root, {"session-1/session.md": session_with(CLEAN_REQUESTS + ["[POST] http://localhost:3000/api/contact => [FAILED] net::ERR_CONNECTION_RESET"])})
        result = self.run_gate()
        self.assertEqual(result.returncode, 1)
        self.assertIn("answered FAILED", result.stdout)

    def test_site_filter_ignores_third_party_requests(self):
        write(self.root, {"session-1/session.md": session_with(CLEAN_REQUESTS + ["[GET] https://fonts.example.com/x.woff2 => [404] Not Found"])})
        self.assertEqual(self.run_gate().returncode, 1)
        self.assertEqual(self.run_gate("--site", "http://localhost:3000").returncode, 0)

    def test_site_file_judges_the_url_the_workflow_declared(self):
        # A preview's URL reaches the gate in the file the verify job writes: requests to it are
        # judged, and requests to anywhere else, the local default included, are not.
        preview = "https://site-git-feat-team.vercel.app"
        write(self.root, {"session-1/session.md": session_with(CLEAN_REQUESTS + [
            "[GET] %s/api/quote => [500] Internal Server Error" % preview,
            "[GET] http://localhost:3000/gone => [404] Not Found"])})
        inputs = tempfile.mkdtemp(prefix="vv-site-")
        self.addCleanup(shutil.rmtree, inputs, True)
        write(inputs, {"site-url": preview + "\n"})
        result = self.run_gate("--site-file", os.path.join(inputs, "site-url"))
        self.assertEqual(result.returncode, 1)
        self.assertIn("request answered 500 outside the allowlist: [GET] %s/api/quote" % preview, result.stdout)
        self.assertNotIn("localhost:3000/gone", result.stdout)

    def test_a_missing_empty_or_malformed_site_file_cannot_run_even_under_soak(self):
        # Judging every request instead would pass a run against the wrong site.
        inputs = tempfile.mkdtemp(prefix="vv-site-")
        self.addCleanup(shutil.rmtree, inputs, True)
        malformed = {"empty": "\n", "words": "the preview\n", "path": "/bid-study\n", "bare": "localhost:3000\n",
                     "scheme": "ftp://example.com\n", "two": "http://localhost:3000\nhttps://elsewhere.example\n"}
        write(inputs, malformed)
        for name in ["absent", *malformed]:
            result = self.run_gate("--site-file", os.path.join(inputs, name), "--soak")
            self.assertEqual(result.returncode, 2, name)
            self.assertIn("site file", result.stderr, name)

    def test_site_and_site_file_are_one_or_the_other(self):
        result = self.run_gate("--site", "http://localhost:3000", "--site-file", "qae-inputs/site-url")
        self.assertEqual(result.returncode, 2)
        self.assertIn("not allowed with", result.stderr)

    def test_network_log_files_are_read_too(self):
        write(self.root, {"network-1.log": "1. [GET] http://localhost:3000/missing => [404] Not Found\n"})
        result = self.run_gate()
        self.assertEqual(result.returncode, 1)
        self.assertIn("http://localhost:3000/missing", result.stdout)

    def test_missing_network_record_fails(self):
        write(self.root, {"session-1/session.md": SESSION.split("### Tool call: browser_network_requests")[0]})
        result = self.run_gate()
        self.assertEqual(result.returncode, 1)
        self.assertIn("no network record", result.stdout)

    def test_missing_session_fails_and_never_navigating_fails(self):
        shutil.rmtree(os.path.join(self.root, "session-1"))
        result = self.run_gate()
        self.assertEqual(result.returncode, 1)
        self.assertIn("no session log", result.stdout)
        write(self.root, {"session-1/session.md": session_with(CLEAN_REQUESTS).replace("browser_navigate", "browser_snapshot")})
        self.assertIn("never navigated", self.run_gate().stdout)

    def test_a_declared_none_makes_an_empty_run_correct(self):
        # Nothing was required, so nothing was explored, and the adjudicator must not read that
        # as missing evidence. A criteria file with real criteria still judges the artifacts.
        inputs = tempfile.mkdtemp(prefix="vv-crit-")
        self.addCleanup(shutil.rmtree, inputs, True)
        write(inputs, {"none.md": "## Acceptance criteria\n\n- None: one pinned SHA, no rendered surface\n",
                       "real.md": "## Acceptance criteria\n\n- The home page shows the heading\n"})
        shutil.rmtree(os.path.join(self.root, "qae"))
        self.assertEqual(self.run_gate("--criteria", os.path.join(inputs, "none.md")).returncode, 0)
        real = self.run_gate("--criteria", os.path.join(inputs, "real.md"))
        self.assertEqual(real.returncode, 1)
        self.assertIn("no step logs", real.stdout)

    def test_a_missing_criteria_file_cannot_run(self):
        self.assertEqual(self.run_gate("--criteria", "/nonexistent/pr-body.md", "--soak").returncode, 2)

    def test_missing_artifact_directory_cannot_run_even_under_soak(self):
        result = gate("qae-artifacts", self.repo, "--artifacts", "/nonexistent/qae-artifacts", "--soak")
        self.assertEqual(result.returncode, 2)


if __name__ == "__main__":
    unittest.main()
