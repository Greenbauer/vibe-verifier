"""The run's network record as the qae-artifacts gate reads it: the harness's own network-*.log (written by
actions/qae-browser/console-pages.js for every page, without a call) or, from a consumer copy whose browser lacks the
hook, the explorer's browser_network_requests results; neither is a finding. The fixture is tests/test_qae_artifacts.py's."""
import os
import re
import unittest
from pathlib import Path

from helpers import write
import test_qae_artifacts as artifacts
import test_qae_console_pages as pages


class NetworkRecord(unittest.TestCase):
    # The artifacts gate's fixture, borrowed rather than inherited, so its tests do not run twice.
    setUp, run_gate = artifacts.QaeArtifacts.setUp, artifacts.QaeArtifacts.run_gate

    def test_network_log_files_are_read_too(self):
        write(self.root, {"network-1.log": "1. [GET] http://localhost:3000/missing => [404] Not Found\n"})
        result = self.run_gate()
        self.assertEqual(result.returncode, 1)
        self.assertIn("http://localhost:3000/missing", result.stdout)

    def test_missing_network_record_fails(self):
        write(self.root, {"session-1/session.md": artifacts.SESSION.split("### Tool call: browser_network_requests")[0]})
        result = self.run_gate()
        self.assertEqual(result.returncode, 1)
        self.assertIn("no network record", result.stdout)

    def test_the_harness_network_record_alone_is_a_record_and_is_judged(self):
        # The explorer no longer asks for the record: the browser's page hook writes network-pages.log.
        write(self.root, {"session-1/session.md": artifacts.SESSION.split("### Tool call: browser_network_requests")[0],
                          "network-pages.log": "1. [GET] http://localhost:3000/ => [200] OK\n"})
        result = self.run_gate()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        write(self.root, {"network-pages.log": "1. [GET] http://localhost:3000/ => [200] OK\n"
                                               "2. [GET] http://localhost:3000/earlier-page.js => [404] Not Found\n"})
        result = self.run_gate()
        self.assertEqual(result.returncode, 1)
        self.assertIn("request answered 404 outside the allowlist: [GET] http://localhost:3000/earlier-page.js", result.stdout)

    def test_a_cancelled_request_is_excused_by_its_route_answered_for_another_record(self):
        # A consumer's run (2026-11-13): a reload cancelled one project's folders read, and that route
        # answered 200 for other projects all run. The route works; the page moved on.
        api, aborted = "http://localhost:3000/api/projects/%s/folders", " => [FAILED] net::ERR_ABORTED"
        cancelled_one = api % "2a59d61e-6b0c-4498-a49c-1806f10e45ea"
        answered = ["[GET] %s => [200] OK" % (api % "043ebd52-5c8d-4142-b4c4-a8bb980700d4"),
                    "[GET] http://localhost:3000/api/projects/17/folders => [200] OK"]
        for other in answered:
            write(self.root, {"session-1/session.md": artifacts.session_with(artifacts.CLEAN_REQUESTS + [other, "[GET] " + cancelled_one + aborted])})
            result = self.run_gate("--site", "http://localhost:3000")
            self.assertEqual(result.returncode, 0, other + result.stdout)
        # A route that never answered below 400 is still a finding: not when it only failed, not when only
        # another method, another route or another query answered.
        for other in ("[GET] %s => [503] Service Unavailable" % (api % "043ebd52-5c8d-4142-b4c4-a8bb980700d4"),
                      "[POST] %s => [200] OK" % (api % "043ebd52-5c8d-4142-b4c4-a8bb980700d4"),
                      "[GET] http://localhost:3000/api/projects/043ebd52-5c8d-4142-b4c4-a8bb980700d4/notes => [200] OK",
                      "[GET] http://localhost:3000/api/projects/folders => [200] OK",
                      "[GET] %s?view=all => [200] OK" % (api % "043ebd52-5c8d-4142-b4c4-a8bb980700d4")):
            write(self.root, {"session-1/session.md": artifacts.session_with(artifacts.CLEAN_REQUESTS + [other, "[GET] " + cancelled_one + aborted])})
            result = self.run_gate("--site", "http://localhost:3000")
            self.assertEqual(result.returncode, 1, other)
            self.assertIn("request cancelled and never answered in this run, outside the allowlist: [GET] " + cancelled_one, result.stdout, other)


@unittest.skipUnless(os.environ.get("QAE_MCP"), "needs the installed browser toolchain: QAE_MCP names playwright-mcp's entry point")
class LiveRecord(unittest.TestCase):
    """The real playwright-mcp and chromium, with the page hook and site of tests/test_qae_console_pages.py."""

    run_gate = pages.Live.run_gate

    def setUp(self):
        pages.Live.setUp(self)
        # A page that asks the API for something it does not have, then says it is done.
        self.site.pages["/fetches"] = ("<title>site</title><p>loading</p><script>fetch('/api/missing').then(() => "
                                       "document.body.append('done'))</script>")

    def test_the_network_record_holds_every_pages_requests_without_a_call(self):
        # The explorer asks for no record: the 404 on the page it left is still in the harness's, and fails the run.
        explorer = pages.Explorer(self, self.root)
        explorer.call("browser_navigate", url=self.site.url + "/dashboard")
        explorer.call("browser_wait_for", time=2)
        explorer.call("browser_navigate", url=self.site.url + "/fetches")
        explorer.call("browser_wait_for", text="done")
        explorer.call("browser_navigate", url=self.site.url + "/")
        explorer.call("browser_take_screenshot", filename="qae-artifacts/qae/AC1-step-1.png")
        explorer.call("browser_close")
        explorer.close()
        Path(self.root, "qae", "AC1.md").write_text("- step 1: opened / -> saw the page\n")
        lines = Path(self.root, "network-pages.log").read_text()
        self.assertRegex(lines, r"(?m)^[0-9]+\. \[GET\] %s/logo\.svg => \[200\] OK$" % re.escape(self.site.url))
        self.assertRegex(lines, r"(?m)^[0-9]+\. \[GET\] %s/api/missing => \[404\] Not Found$" % re.escape(self.site.url))
        result = self.run_gate()
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("request answered 404 outside the allowlist: [GET] %s/api/missing" % self.site.url, result.stdout)

    def test_a_static_answer_on_an_earlier_page_excuses_its_later_cancellation_without_the_page_record(self):
        # The logo answered on the dashboard, then the developers page cancelled it: the record holds both.
        explorer = pages.Explorer(self, self.root)
        for path in ("/dashboard", "/developers"):
            explorer.call("browser_navigate", url=self.site.url + path)
            explorer.call("browser_wait_for", time=2)
        explorer.call("browser_take_screenshot", filename="qae-artifacts/qae/AC1-step-1.png")
        explorer.call("browser_close")
        explorer.close()
        Path(self.root, "qae", "AC1.md").write_text("- step 1: opened /developers -> saw the page\n")
        logo = re.escape("[GET] %s/logo.svg" % self.site.url)
        lines = Path(self.root, "network-pages.log").read_text()
        self.assertRegex(lines, r"(?m)^[0-9]+\. %s => \[200\] OK$" % logo)
        self.assertRegex(lines, r"(?m)^[0-9]+\. %s => \[FAILED\] net::ERR_ABORTED$" % logo)
        os.remove(os.path.join(self.root, "console-pages.jsonl"))
        result = self.run_gate()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertNotIn("no network record", result.stdout)


if __name__ == "__main__":
    unittest.main()
