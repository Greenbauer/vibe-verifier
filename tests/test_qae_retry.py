"""A step the QAE explorer took once more after an environment error (gates/_retry.py): the qae-artifacts gate
counts the retry, and excuses its error, only on structural evidence; acceptance-verdict still judges the
explorer's verdict, so a retry that fails again stays a FAIL; and the QA review names every retry it saw.
Driven through the gates' and the runner's command lines against evidence shaped like playwright-mcp's."""
import hashlib
import json
import shutil
import tempfile
import unittest

from helpers import gate, make_repo, write
from qa_review_fixture import GhFixture

SITE = "http://localhost:3000"
BODY = "## Acceptance criteria\n\n- Organization search finds a company\n"
STEPS = ("- step 1: opened organization search -> the console reports a dashboard POST 500\n"
         "- step 2: retry of step 1: opened organization search -> the search is active\n")
FIVE_HUNDRED = "1. [POST] %s/api/dashboard => [500] Internal Server Error\n" % SITE
# Chromium's console line for that answer, as console-*.log holds it.
CONSOLE_500 = ("[  900ms] [ERROR] Failed to load resource: the server responded with a status of 500 () @ "
               "%s/api/dashboard:0\n" % SITE)
SOCKET = "wss://abc.supabase.co/realtime/v1/websocket?apikey=k&vsn=1.0.0"
SOCKET_LINE = ("WebSocket connection to '%s' failed: Error during WebSocket handshake: Unexpected response code: 500 "
               "@ %s/_next/static/chunks/app.js:1" % (SOCKET, SITE))


def call(name, args, result):
    return "### Tool call: %s\n- Args\n```json\n%s\n```\n- Result\n```json\n%s\n```\n\n" % (
        name, json.dumps(args), json.dumps(result))


def shot(item, k):
    path = "qae-artifacts/qae/%s-step-%s.png" % (item, k)
    return call("browser_take_screenshot", {"filename": path}, {"result": "- [Screenshot](%s)" % path})


def saved(tool, path):
    return call(tool, {"filename": "qae-artifacts/" + path}, {"result": "- [Record](qae-artifacts/%s)" % path})


OPEN = call("browser_navigate", {"url": SITE + "/search"}, {"page": "- Page URL: %s/search" % SITE})


def session(*records, requests=(FIVE_HUNDRED, "2. [POST] %s/api/dashboard => [200] OK\n" % SITE), retry=OPEN, step=OPEN):
    """The explorer's session: step 1's action (opening the search) and screenshot, then `records` saved, then the
    retry's calls and screenshot, then the criterion's network record."""
    return (step + shot("AC1", 1) + "".join(records) + retry + shot("AC1", 2)
            + call("browser_network_requests", {}, {"result": "".join(requests)}))


NETWORK = saved("browser_network_requests", "qae/AC1-step-1-network.txt")
CONSOLE = saved("browser_console_messages", "qae/AC1-step-1-console.txt")


class Retry(unittest.TestCase):
    def setUp(self):
        self.repo = make_repo(self, {"README.md": "x"})
        # Named as the verify job names it: a record saved to qae-artifacts/<path> is read at <path> under it.
        self.root = tempfile.mkdtemp(prefix="vv-retry-") + "/qae-artifacts"
        self.addCleanup(shutil.rmtree, self.root.rpartition("/")[0], True)
        self.inputs = tempfile.mkdtemp(prefix="vv-retry-in-")
        self.addCleanup(shutil.rmtree, self.inputs, True)
        write(self.inputs, {"pr-body.md": BODY, "changed-files": "src/search/OrgSearch.tsx\n"})
        write(self.root, {"qae/AC1.md": STEPS, "qae/AC1-step-1.png": "png", "qae/AC1-step-2.png": "png",
                          "qae/AC1-step-1-network.txt": FIVE_HUNDRED, "console-1.log": CONSOLE_500,
                          "session-1/session.md": session(NETWORK)})

    def artifacts(self, changed=True):
        extra = ["--changed-files", self.inputs + "/changed-files"] if changed else []
        return gate("qae-artifacts", self.repo, "--artifacts", self.root, "--criteria", self.inputs + "/pr-body.md",
                    "--site", SITE, *extra)

    def verdict(self, word):
        write(self.inputs, {"verdict.md": "acceptance-check: AC1 -- %s -- searched (qae/AC1.md::step 2: retry of step 1: "
                                          "opened organization search -> the search is active)\n" % word})
        return gate("acceptance-verdict", self.repo, "--criteria", self.inputs + "/pr-body.md",
                    "--verdict", self.inputs + "/verdict.md", "--artifacts", self.root)

    def refused(self, why, changed=True, step=2):
        result = self.artifacts(changed)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("qae/AC1.md: step 1 retried as step %d, which does not count: %s" % (step, why), result.stdout)
        return result

    # A retried environment FAIL: the 5xx is on a path no changed file names, and the endpoint answered in the run.

    def test_a_retried_5xx_on_an_unchanged_endpoint_passes_and_says_why(self):
        result = self.artifacts()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("qae-artifacts: AC1: step 1 retried once as step 2 after an environment error: POST /api/dashboard "
                      "answered 500", result.stdout)
        self.assertEqual(self.verdict("PASS").returncode, 0)

    def test_without_the_retry_the_same_500_fails_the_run(self):
        write(self.root, {"qae/AC1.md": STEPS.splitlines(True)[0]})
        result = self.artifacts()
        self.assertEqual(result.returncode, 1)
        self.assertIn("request answered 500 outside the allowlist: [POST] %s/api/dashboard" % SITE, result.stdout)
        self.assertIn("console error outside the allowlist: Failed to load resource", result.stdout)

    def test_vercel_protection_counts_whatever_the_pull_request_changed(self):
        # The network record leaves out a document answered below 400, Vercel's redirects included, so the protection
        # shows as the page URL playwright-mcp reports for the step's navigation.
        page = "- Page URL: https://vercel.com/login?next=%2Fsso-api%3Furl%3Dhttps%253A%252F%252Fsite\n- Page Title: Login"
        navigate = call("browser_navigate", {"url": SITE + "/search"}, {"page": page})
        hidden = "\nNote: 3 static requests not shown, run with \"static\" option to see them."
        write(self.root, {"qae/AC1-step-1-network.txt": hidden, "console-1.log": "",
                          "session-1/session.md": session(NETWORK, step=navigate, requests=())})
        result = self.artifacts(changed=False)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("after an environment error: the site answered with Vercel's deployment protection login", result.stdout)
        # A script's own result naming that page shows nothing: only the page field playwright-mcp writes does.
        faked = call("browser_evaluate", {"function": "() => 1"}, {"result": page, "page": "- Page URL: %s/search" % SITE})
        write(self.root, {"session-1/session.md": session(NETWORK, step=OPEN + faked, retry=OPEN + faked, requests=())})
        self.refused("step 1's records show no environment error", changed=False)

    def test_a_step_shot_twice_counts_only_when_nothing_changed_the_page_between(self):
        # A screenshot taken too early and taken again is one step; a walk in between is not.
        wait = call("browser_wait_for", {"time": 1}, {})
        write(self.root, {"session-1/session.md": session(NETWORK, step=OPEN + shot("AC1", 1) + wait)})
        result = self.artifacts()
        self.assertEqual(result.returncode, 0, result.stdout)
        other = call("browser_navigate", {"url": SITE + "/other"}, {}) + call("browser_click", {"ref": "e9"}, {})
        write(self.root, {"session-1/session.md": session(NETWORK, step=OPEN + shot("AC1", 1) + other, retry=other)})
        self.refused("the session log shows calls (browser_navigate /other, browser_click) between two screenshots of step 1")

    def test_a_failed_websocket_counts_and_its_console_line_is_excused(self):
        write(self.root, {"qae/AC1-step-1-network.txt": "", "qae/AC1-step-1-console.txt": "[ERROR] " + SOCKET_LINE + "\n",
                          "console-1.log": "[  900ms] [ERROR] %s\n" % SOCKET_LINE,
                          "session-1/session.md": session(CONSOLE, requests=())})
        result = self.artifacts()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("after an environment error: the websocket to /realtime/v1/websocket failed", result.stdout)

    # A FAIL with any other cause is not retried: the retry does not count, and the run fails on it.

    def test_a_400_is_not_an_environment_error(self):
        # A consumer's walk (2026-10-08): an invalid input answered 400 failed it; a later walk that skipped it passed.
        write(self.root, {"qae/AC1-step-1-network.txt": "1. [POST] %s/api/lines => [400] Bad Request\n" % SITE,
                          "console-1.log": ""})
        self.refused("step 1's records show no environment error")

    def test_a_5xx_on_an_endpoint_the_pull_request_changes_is_its_own(self):
        write(self.inputs, {"changed-files": "app/api/dashboard/route.ts\n"})
        self.refused("step 1's records show POST /api/dashboard answered 500, on a URL the pull request's changed files name")

    def test_without_changed_files_no_5xx_counts(self):
        self.refused("step 1's records show POST /api/dashboard answered 500, and without the pull request's changed "
                     "paths no URL can be told apart from its own: give the qae-artifacts line --changed-files "
                     "qae-inputs/changed-files", changed=False)

    def test_a_retry_another_way_does_not_count(self):
        write(self.root, {"qae/AC1.md": STEPS.replace("retry of step 1: opened organization search",
                                                       "retry of step 1: typed into the header search")})
        self.refused("its action is not step 1's word for word")

    def test_a_step_retried_twice_does_not_count(self):
        write(self.root, {"qae/AC1.md": STEPS + "- step 3: retry of step 1: opened organization search -> the search\n",
                          "qae/AC1-step-3.png": "png"})
        self.refused("step 1 is retried 2 times, and a step is retried once")

    def test_a_retry_after_walking_on_another_way_does_not_count(self):
        # Steps between the failure and the retry would be the criterion walked another way, with a retry line added.
        write(self.root, {"qae/AC1.md": STEPS.splitlines(True)[0] + "- step 2: typed into the header search -> results\n"
                          + STEPS.splitlines(True)[1].replace("step 2:", "step 3:"), "qae/AC1-step-3.png": "png",
                          "session-1/session.md": session(NETWORK).replace("AC1-step-2.png", "AC1-step-3.png")})
        self.refused("it is not the step right after step 1", step=3)

    def test_a_dropped_connection_only_the_console_shows_counts(self):
        chunk = "%s/_next/static/chunks/app.js" % SITE
        write(self.root, {"qae/AC1-step-1-network.txt": "", "console-1.log": "",
                          "qae/AC1-step-1-console.txt": "[ERROR] Failed to load resource: net::ERR_CONNECTION_RESET @ %s:0\n" % chunk,
                          "console-pages.jsonl": json.dumps({"answered": hashlib.sha256(("GET " + chunk).encode()).hexdigest()}) + "\n",
                          "session-1/session.md": session(CONSOLE, requests=())})
        result = self.artifacts()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("after an environment error: GET /_next/static/chunks/app.js failed (net::ERR_CONNECTION_RESET)", result.stdout)

    def test_a_retry_whose_calls_are_not_the_steps_does_not_count(self):
        # The log copies step 1's words, but the session log shows the retry went somewhere else.
        retry = call("browser_navigate", {"url": SITE + "/companies"}, {}) + call("browser_type", {"text": "Acme"}, {})
        write(self.root, {"session-1/session.md": session(NETWORK, retry=retry)})
        self.refused("the session log shows step 1's calls (browser_navigate /search) and the retry's (browser_navigate "
                     "/companies, browser_type)")

    def test_a_retry_of_the_step_after_the_sign_in_before_it_counts(self):
        # The sign-in that opened the criterion is not part of the step the retry takes again.
        sign_in = (call("browser_navigate", {"url": SITE + "/login"}, {}) + call("browser_type", {"ref": "e3", "text": "qae"}, {})
                   + call("browser_click", {"ref": "e5"}, {}))
        write(self.root, {"session-1/session.md": session(NETWORK, step=sign_in + OPEN)})
        result = self.artifacts()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_a_retry_that_fills_in_something_else_does_not_count(self):
        # Refs change when the page renders again; the values typed are the step itself.
        fill = lambda value, ref: call("browser_fill_form", {"fields": [{"ref": ref, "value": value}]}, {})  # noqa: E731
        write(self.root, {"session-1/session.md": session(NETWORK, step=OPEN + fill("bad", "e1"), retry=fill("good@x.com", "f2e1"))})
        self.refused("the session log shows step 1's calls (browser_navigate /search, browser_fill_form) and the retry's "
                     "(browser_fill_form): a retry makes the last of the step's calls again, with the same URLs and values")
        write(self.root, {"session-1/session.md": session(NETWORK, step=OPEN + fill("bad", "e1"), retry=fill("bad", "f2e1"))})
        self.assertEqual(self.artifacts().returncode, 0)

    def test_a_retry_that_runs_other_code_or_hides_its_values_does_not_count(self):
        # Every argument but an element ref is the step: a script's code, and typed text that reads like the log's
        # own "- Result" heading.
        run = lambda code: call("browser_evaluate", {"function": code}, {})  # noqa: E731
        write(self.root, {"session-1/session.md": session(NETWORK, step=OPEN + run("() => save()"), retry=run("() => go()"))})
        self.refused("the session log shows step 1's calls (browser_navigate /search, browser_evaluate) and the retry's")
        typed = lambda text: call("browser_type", {"ref": "e1", "text": text}, {})  # noqa: E731
        write(self.root, {"session-1/session.md": session(NETWORK, step=OPEN + typed("bad - Result"),
                                                          retry=typed("good - Result"))})
        self.refused("the session log shows step 1's calls (browser_navigate /search, browser_type) and the retry's")

    def test_a_log_that_numbers_a_step_twice_counts_no_retry(self):
        write(self.root, {"qae/AC1.md": STEPS + STEPS.splitlines(True)[1]})
        self.refused("the log numbers step 2 more than once, so its steps cannot be told apart")

    def test_taking_the_step_a_third_time_after_the_retry_does_not_count(self):
        write(self.root, {"qae/AC1.md": STEPS + "- step 3: opened organization search -> the search is active\n",
                          "qae/AC1-step-3.png": "png"})
        self.refused("step 3 takes step 1's action again after the retry, and a step is retried once")

    def test_records_the_tool_did_not_save_between_the_screenshots_do_not_count(self):
        write(self.root, {"session-1/session.md": NETWORK + session()})
        self.refused("no record of step 1 (qae/AC1-step-1-network.txt or -console.txt) was saved by its tool")

    # A retry that fails again stays a FAIL.

    def test_a_retry_that_fails_again_stays_a_fail(self):
        # The retry counts, so its error is excused here; the explorer's FAIL is what acceptance-verdict judges.
        self.assertEqual(self.artifacts().returncode, 0)
        result = self.verdict("FAIL")
        self.assertEqual(result.returncode, 1)
        self.assertIn("AC1 is not a PASS", result.stdout)

    def test_an_endpoint_never_answered_in_the_run_is_not_excused(self):
        write(self.root, {"session-1/session.md": session(NETWORK, requests=(FIVE_HUNDRED,))})
        result = self.artifacts()
        self.assertEqual(result.returncode, 1)
        self.assertIn("POST /api/dashboard is not excused: it was never answered below 400 in this run", result.stdout)
        self.assertIn("request answered 500 outside the allowlist", result.stdout)

    def test_an_answer_the_explorer_wrote_itself_excuses_nothing(self):
        # Only a network record or the page record shows an answer; a step log, a verdict, or text the explorer typed
        # into another tool call is its own.
        answer = "9. [POST] %s/api/dashboard => [200] OK\n" % SITE
        typed = call("browser_type", {"text": answer}, {"result": answer}) + shot("AC9", 1)  # a step before this criterion
        write(self.root, {"session-1/session.md": typed + session(NETWORK, requests=(FIVE_HUNDRED,)),
                          "qae/AC1.md": STEPS + answer, "verdicts/1.md": answer})
        self.assertIn("POST /api/dashboard is not excused: it was never answered below 400 in this run", self.artifacts().stdout)


class NamedUrl(unittest.TestCase):
    def test_a_changed_path_names_a_url_by_a_word_and_errs_toward_named(self):
        import _retry
        self.assertTrue(_retry.named(SITE + "/api/dashboard", ["app/(main)/dashboard/page.tsx"]))
        self.assertTrue(_retry.named("https://db.example/rest/v1/journeys?id=eq.1", ["supabase/migrations/2026_journeys.sql"]))
        self.assertFalse(_retry.named(SITE + "/api/dashboard", ["src/search/OrgSearch.tsx"]))
        self.assertFalse(_retry.named(SOCKET, ["app/chat/page.tsx"]))
        # No word of its own, or no changed paths to compare with: named, so no retry counts on it.
        self.assertTrue(_retry.named(SITE + "/api/v1/8697e680-1111-2222-3333-444455556666", ["README.md"]))
        self.assertTrue(_retry.named(SITE + "/api/dashboard", None))


class SessionLog(unittest.TestCase):
    def test_a_calls_arguments_are_everything_but_its_element_refs(self):
        import _session_log
        url, given = _session_log.inputs(json.dumps({"url": SITE, "fields": [{"ref": "e1", "element": "Email", "value": "a"}]}))
        self.assertEqual((url, json.loads(given)), (SITE, {"url": SITE, "fields": [{"value": "a"}]}))
        self.assertEqual(_session_log.inputs("not json"), ("", ""))


class RetryInTheReview(GhFixture):
    def test_the_review_says_a_criterion_passed_on_a_retry_and_why(self):
        root = tempfile.mkdtemp(prefix="vv-retry-")
        self.addCleanup(shutil.rmtree, root, True)
        write(root, {"qae/AC1.md": STEPS, "qae/AC1-step-1-network.txt": FIVE_HUNDRED, "session-1/session.md": session(NETWORK)})
        changed = self.file("changed-files", "src/search/OrgSearch.tsx\n")
        body = self.posted(extra=("--artifacts", root, "--changed-files", changed))
        self.assertIn("- AC1, explorer says PASS (step 1 retried once as step 2 after an environment error: POST "
                      "/api/dashboard answered 500): The home page loads\n", body)
        self.assertIn("- AC2, explorer says FAIL: The menu opens\n", body)


if __name__ == "__main__":
    unittest.main()
