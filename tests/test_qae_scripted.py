"""A 4xx answered to a request the QAE explorer's own script sent (gates/_scripted.py): an import() of a module path the
explorer made up, which no page requests, is excused by the qae-artifacts gate; the same 404 the page itself caused, a
script that did not name the URL, a page that sent the same request too, and a 5xx still fail the run. Driven through
the gate's command line against evidence shaped like playwright-mcp's (0.0.81) and the harness's network record, as
Greenbauer/zack.land PR 197's run left it (QA run 45067004080, attempt 1, explorer 2)."""
import shutil
import tempfile
import unittest

from helpers import gate, make_repo, write
from test_qae_retry import call, shot

from _scripted import names  # noqa: E402  (gates/, which helpers puts on the path)

SITE = "http://localhost:3000"
PAGE = SITE + "/work-with-me"
GUESSED = "/_next/static/chunks/components/Background/skyState.js"
BODY = "## Acceptance criteria\n\n- The card title reads light on the dark sky\n"
STEPS = "- step 1: opened the page -> the card title read light on the dark sky\n"
IMPORT = ("async () => { const module = await import('%s').catch(() => null); "
          "try { await fetch('/robots.txt'); } catch {} return { module: !!module }; }" % GUESSED)


def refused(status=404, url=SITE + GUESSED, line=1):
    """Chromium's console line for an answer, as console-*.log holds it, and the call field that links to it."""
    entry = ("[ 49906ms] [ERROR] Failed to load resource: the server responded with a status of %s (Not Found) @ %s:0\n"
             % (status, url))
    return entry, "- New console entries: qae-artifacts/console-1.log#L%d" % line


def evaluate(script=IMPORT, events=refused()[1]):
    return call("browser_evaluate", {"function": script},
                {"result": '{"module": false}', "page": "- Page URL: %s" % PAGE, "events": events})


def navigate(events=""):
    return call("browser_navigate", {"url": PAGE}, {"page": "- Page URL: %s" % PAGE, "events": events})


def network(*answers):
    """The harness's record of every request of every page: one numbered line per request."""
    lines = ["1. [GET] %s => [200] OK" % PAGE] + ["%d. [GET] %s => [%s] Not Found" % (n, url, status)
                                                  for n, (status, url) in enumerate(answers, 2)]
    return "\n".join(lines + ["%d. [GET] %s/robots.txt => [200] OK" % (len(lines) + 1, SITE)]) + "\n"


class ScriptedFixture(unittest.TestCase):
    """Step 1 opened the page; then the explorer evaluated a script that imported a module path it made up, which
    the dev server answered 404, and Chromium logged it among that call's console entries."""

    def setUp(self):
        self.repo = make_repo(self, {"README.md": "x"})
        self.root = tempfile.mkdtemp(prefix="vv-scripted-") + "/qae-artifacts"
        self.addCleanup(shutil.rmtree, self.root.rpartition("/")[0], True)
        self.inputs = tempfile.mkdtemp(prefix="vv-scripted-in-")
        self.addCleanup(shutil.rmtree, self.inputs, True)
        write(self.inputs, {"pr-body.md": BODY})
        write(self.root, {"qae/AC1.md": STEPS, "qae/AC1-step-1.png": "png", "console-1.log": refused()[0],
                          "network-1-pages.log": network((404, SITE + GUESSED)),
                          "session-1/session.md": navigate() + shot("AC1", 1) + evaluate()})

    def artifacts(self):
        return gate("qae-artifacts", self.repo, "--artifacts", self.root, "--criteria", self.inputs + "/pr-body.md",
                    "--site", SITE)

    def fails(self, status=404, url=SITE + GUESSED):
        result = self.artifacts()
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("request answered %s outside the allowlist: [GET] %s" % (status, url), result.stdout)
        self.assertIn("console error outside the allowlist: Failed to load resource: the server responded with a "
                      "status of %s" % status, result.stdout)
        return result


class ExplorerCaused(ScriptedFixture):
    def test_a_404_to_a_module_the_explorers_script_imported_passes_and_says_why(self):
        result = self.artifacts()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("qae-artifacts: 404 %s was answered to a request the explorer's own script sent (it names that "
                      "URL), not one the page sent: excused" % GUESSED, result.stdout)

    def test_a_script_run_with_browser_run_code_counts_the_same(self):
        script = call("browser_run_code", {"code": "await page.evaluate(() => fetch('%s'))" % (SITE + GUESSED)},
                      {"result": "undefined", "events": refused()[1]})
        write(self.root, {"session-1/session.md": navigate() + shot("AC1", 1) + script})
        self.assertEqual(self.artifacts().returncode, 0)


class PageCaused(ScriptedFixture):
    def test_the_same_404_the_page_caused_still_fails(self):
        # The page asked for the module itself: the console line is the navigation's, and no script ran.
        write(self.root, {"session-1/session.md": navigate(refused()[1]) + shot("AC1", 1)})
        result = self.fails()
        self.assertNotIn("excused", result.stdout)

    def test_a_404_logged_while_a_script_ran_that_did_not_name_it_still_fails(self):
        # The page's own request failed while the explorer's script ran: the script asked for something else.
        write(self.root, {"session-1/session.md": navigate() + shot("AC1", 1) + evaluate("() => document.title")})
        self.fails()

    def test_a_page_that_sent_the_same_request_too_still_fails(self):
        entry = refused()[0]
        write(self.root, {"console-1.log": entry + entry,
                          "network-1-pages.log": network((404, SITE + GUESSED), (404, SITE + GUESSED)),
                          "session-1/session.md": navigate(refused(line=2)[1]) + shot("AC1", 1) + evaluate()})
        result = self.fails()
        self.assertIn("was requested by the page too: not excused", result.stdout)

    def test_a_page_request_for_the_same_url_while_the_script_ran_still_fails(self):
        # The script named the URL once; the page asked for it too while the script ran, so the call logged two lines.
        entry = refused()[0]
        write(self.root, {"console-1.log": entry + entry,
                          "network-1-pages.log": network((404, SITE + GUESSED), (404, SITE + GUESSED)),
                          "session-1/session.md": navigate() + shot("AC1", 1)
                          + evaluate(events="- New console entries: qae-artifacts/console-1.log#L1-L2")})
        result = self.fails()
        self.assertIn("was requested by the page too: not excused", result.stdout)

    def test_more_requests_than_the_script_logged_still_fail(self):
        write(self.root, {"network-1-pages.log": network((404, SITE + GUESSED), (404, SITE + GUESSED))})
        self.fails()

    def test_a_5xx_to_the_explorers_own_request_still_fails(self):
        entry, events = refused(500)
        write(self.root, {"console-1.log": entry, "network-1-pages.log": network((500, SITE + GUESSED)),
                          "session-1/session.md": navigate() + shot("AC1", 1) + evaluate(events=events)})
        self.fails(500)


class Names(unittest.TestCase):
    def test_a_script_names_a_url_whole_or_by_its_path_alone(self):
        url = SITE + "/api/quotes?id=1"
        self.assertEqual(names("fetch('/api/quotes?id=1'); fetch('%s')" % url, url), 2)
        self.assertTrue(names("fetch('/api/quotes?id=1')", url))
        self.assertTrue(names('fetch("%s")' % url, url))
        self.assertFalse(names("fetch('/api/quotes?id=12')", url))
        self.assertFalse(names("fetch('/v2/api/quotes?id=1')", url))
        self.assertFalse(names("fetch('/api/quotes')", url))
        self.assertTrue(names("fetch('/api/quotes')", SITE + "/api/quotes"))
        self.assertFalse(names("fetch('/api/quotes/1')", SITE + "/api/quotes"))
        self.assertFalse(names("fetch('/')", SITE + "/"))


if __name__ == "__main__":
    unittest.main()
