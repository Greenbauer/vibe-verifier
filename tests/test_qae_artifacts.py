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

    def test_an_empty_screenshot_counts_as_missing(self):
        write(self.root, {"qae/AC1-step-2.png": ""})
        result = self.run_gate()
        self.assertEqual(result.returncode, 1)
        self.assertIn("step 2 has no screenshot (expected qae/AC1-step-2.png)", result.stdout)

    def test_a_feature_re_walk_step_needs_its_screenshot_too(self):
        # A re-walk (docs/feature-map.md) logs each feature under qae/features/, beside its screenshots.
        write(self.root, {"qae/features/sign-in.md": "- step 1: signed in -> the dashboard\n- step 2: signed out -> the login page\n",
                          "qae/features/sign-in-step-1.png": "png"})
        result = self.run_gate()
        self.assertEqual(result.returncode, 1)
        self.assertIn("qae/features/sign-in.md: step 2 has no screenshot (expected qae/features/sign-in-step-2.png)", result.stdout)
        write(self.root, {"qae/features/sign-in-step-2.png": "png"})
        self.assertEqual(self.run_gate().returncode, 0)

    def test_feature_logs_do_not_stand_in_for_the_criteria_logs(self):
        shutil.rmtree(os.path.join(self.root, "qae"))
        write(self.root, {"qae/features/sign-in.md": "- step 1: signed in -> the dashboard\n", "qae/features/sign-in-step-1.png": "png"})
        self.assertIn("no step logs under qae/", self.run_gate().stdout)

    def test_no_step_logs_fails(self):
        shutil.rmtree(os.path.join(self.root, "qae"))
        result = self.run_gate()
        self.assertEqual(result.returncode, 1)
        self.assertIn("no step logs", result.stdout)

    def test_a_resource_error_on_another_host_is_not_judged_once_the_site_is_declared(self):
        # Cognito answers a refused refresh with 400, and Chromium logs that on the console.
        # The network record already ignores that host when a site is declared. The console
        # line for the same response does too. A resource error on the site itself still fails.
        write(self.root, {"console-1.log": "[   12ms] [ERROR] Failed to load resource: the server responded with a status of 400 (Bad Request) @ https://cognito-idp.us-east-1.amazonaws.com/:0\n"})
        self.assertEqual(self.run_gate().returncode, 1)
        scoped = self.run_gate("--site", "http://localhost:3000")
        self.assertEqual(scoped.returncode, 0, scoped.stdout + scoped.stderr)
        write(self.root, {"console-1.log": "[   12ms] [ERROR] Failed to load resource: the server responded with a status of 400 (Bad Request) @ http://localhost:3000/api/journeys:0\n"})
        on_site = self.run_gate("--site", "http://localhost:3000")
        self.assertEqual(on_site.returncode, 1)
        self.assertIn("status of 400 (Bad Request) @ http://localhost:3000/api/journeys:0", on_site.stdout)

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

    def test_a_400_is_the_first_error_status(self):
        write(self.root, {"session-1/session.md": session_with(CLEAN_REQUESTS + [
            "[GET] http://localhost:3000/api/quote => [400] Bad Request",
            "[GET] http://localhost:3000/moved => [399] Unassigned"])})
        result = self.run_gate()
        self.assertEqual(result.returncode, 1)
        self.assertIn("request answered 400 outside the allowlist: [GET] http://localhost:3000/api/quote", result.stdout)
        self.assertNotIn("answered 399", result.stdout)

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

    def test_a_declared_none_needs_no_site_file(self):
        # A pull request that declares None runs no explorer, so its site step may never have
        # written a URL. The site is resolved only when something is judged: the None run passes
        # with an absent site file, and real criteria with the same absent file still cannot run.
        inputs = tempfile.mkdtemp(prefix="vv-crit-")
        self.addCleanup(shutil.rmtree, inputs, True)
        write(inputs, {"none.md": "## Acceptance criteria\n\n- None: docs only\n",
                       "real.md": "## Acceptance criteria\n\n- The home page shows the heading\n"})
        absent = os.path.join(inputs, "site-url")
        self.assertEqual(self.run_gate("--criteria", os.path.join(inputs, "none.md"), "--site-file", absent).returncode, 0)
        real = self.run_gate("--criteria", os.path.join(inputs, "real.md"), "--site-file", absent)
        self.assertEqual(real.returncode, 2)
        self.assertIn("site file not found", real.stderr)

    def refusal_run(self, criterion, *requests):
        # The shape of a consumer's real failing run: a signed-out navigation to an API route on a
        # preview, which Chromium logs as a console error and the network record shows as a 401.
        preview = "https://site-git-fix-auth-gate.vercel.app"
        write(self.root, {
            "console-1.log": "".join("[  9%02dms] [ERROR] Failed to load resource: the server responded with a status of %s () @ %s%s:0\n"
                                     % (n, status, preview, path) for n, (status, path) in enumerate(requests)),
            "session-1/session.md": session_with(CLEAN_REQUESTS + ["[GET] %s%s => [%s] Error" % (preview, path, status)
                                                                   for status, path in requests])})
        inputs = tempfile.mkdtemp(prefix="vv-refusal-")
        self.addCleanup(shutil.rmtree, inputs, True)
        write(inputs, {"site-url": preview + "\n", "body.md": "## Acceptance criteria\n\n- %s\n" % criterion})
        return self.run_gate("--criteria", os.path.join(inputs, "body.md"), "--site-file", os.path.join(inputs, "site-url"))

    def test_an_expected_401_at_the_declared_url_passes(self):
        result = self.refusal_run("Signed out, the quotes API refuses (expected-refusal: 401 /api/quotes)", ("401", "/api/quotes"))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        full = self.refusal_run("Signed out, the API refuses (`expected-refusal: 403 https://site-git-fix-auth-gate.vercel.app/api/quotes`)",
                                ("403", "/api/quotes"))
        self.assertEqual(full.returncode, 0, full.stdout + full.stderr)

    def test_the_same_401_at_an_undeclared_url_fails(self):
        result = self.refusal_run("Signed out, the quotes API refuses (expected-refusal: 401 /api/quotes)",
                                  ("401", "/api/quotes"), ("401", "/api/customers"), ("401", "/api/quotes?id=1"))
        self.assertEqual(result.returncode, 1)
        self.assertIn("status of 401 () @ https://site-git-fix-auth-gate.vercel.app/api/customers:0", result.stdout)
        self.assertIn("request answered 401 outside the allowlist: [GET] https://site-git-fix-auth-gate.vercel.app/api/customers", result.stdout)
        self.assertIn("api/quotes?id=1", result.stdout)
        self.assertNotIn("vercel.app/api/quotes:0", result.stdout)

    def test_a_500_or_another_status_at_the_declared_url_fails(self):
        for status in ("500", "403"):
            result = self.refusal_run("Signed out, the quotes API refuses (expected-refusal: 401 /api/quotes)", (status, "/api/quotes"))
            self.assertEqual(result.returncode, 1, status)
            self.assertIn("request answered %s outside the allowlist" % status, result.stdout)
            self.assertIn("status of %s () @" % status, result.stdout)

    def test_only_401_or_403_can_be_declared_and_a_path_needs_a_site(self):
        result = self.refusal_run("The import endpoint fails (expected-refusal: 500 /api/import)", ("500", "/api/import"))
        self.assertEqual(result.returncode, 1)
        self.assertIn("AC1 declares expected-refusal 500: only 401 or 403 can be expected", result.stdout)
        inputs = tempfile.mkdtemp(prefix="vv-refusal-")
        self.addCleanup(shutil.rmtree, inputs, True)
        write(inputs, {"body.md": "## Acceptance criteria\n\n- Signed out refused (expected-refusal: 401 /api/quotes)\n"})
        siteless = self.run_gate("--criteria", os.path.join(inputs, "body.md"))
        self.assertEqual(siteless.returncode, 1)
        self.assertIn("AC1 declares expected-refusal at /api/quotes", siteless.stdout)

    def test_a_declaration_outside_a_criterion_line_does_not_count(self):
        hidden = "Signed out, the quotes page redirects <!-- expected-refusal: 401 /api/quotes -->"
        self.assertEqual(self.refusal_run(hidden, ("401", "/api/quotes")).returncode, 1)
        elsewhere = "Signed out, the quotes page redirects\n\n## Notes\n\n- expected-refusal: 401 /api/quotes"
        self.assertEqual(self.refusal_run(elsewhere, ("401", "/api/quotes")).returncode, 1)

    def test_a_missing_criteria_file_cannot_run(self):
        self.assertEqual(self.run_gate("--criteria", "/nonexistent/pr-body.md", "--soak").returncode, 2)

    def test_missing_artifact_directory_cannot_run_even_under_soak(self):
        result = gate("qae-artifacts", self.repo, "--artifacts", "/nonexistent/qae-artifacts", "--soak")
        self.assertEqual(result.returncode, 2)


if __name__ == "__main__":
    unittest.main()
