"""The same-step rule of a QAE retry (gates/_retry.py, gates/_session_log.py): the retry's page-changing calls in the
session log must be the end of the failed step's, with the same arguments, read-only tools, snapshot refs and
descriptions left out. The evidence is tests/test_qae_retry.py's."""
import json
import unittest

from test_qae_retry import NETWORK, OPEN, SITE, RetryFixture, call, session, shot
from helpers import write


class SameStep(RetryFixture):
    def test_reads_new_refs_and_reworded_descriptions_do_not_make_another_step(self):
        # Between the screenshots the explorer may read (find, inspect one request); on the retry the page has new refs
        # and the explorer may describe a field in other words. None of that changes the step.
        reads = call("browser_find", {"text": "Save"}, {}) + call("browser_network_request", {"index": 1}, {})
        drag = lambda a, b, said: call("browser_drag", {"startTarget": a, "endTarget": b, "startElement": said}, {})  # noqa: E731
        fill = lambda ref, said: call("browser_fill_form", {"fields": [{"ref": ref, "name": said, "value": "x"}]}, {})  # noqa: E731
        write(self.root, {"session-1/session.md": session(NETWORK, reads, step=OPEN + drag("e1", "e2", "card") + fill("e3", "Email"),
                                                          retry=drag("f1e1", "f1e2", "the card") + fill("f1e3", "Email address"))})
        result = self.artifacts()
        self.assertEqual(result.returncode, 0, result.stdout)
        # A target given as a selector is kept: another selector is another element.
        click = lambda target: call("browser_click", {"target": target}, {})  # noqa: E731
        write(self.root, {"session-1/session.md": session(NETWORK, step=OPEN + click("#save"), retry=click("#cancel"))})
        self.refused("the session log shows step 1's calls (browser_navigate /search, browser_click) and the retry's")


    def test_a_name_that_is_an_argument_is_compared(self):
        # Only a form field's name describes it; a cookie's name is what the call sets.
        cookie = lambda name: call("browser_cookie_set", {"name": name, "value": "1"}, {})  # noqa: E731
        write(self.root, {"session-1/session.md": session(NETWORK, step=OPEN + cookie("old_ui"), retry=cookie("new_ui"))})
        self.refused("the session log shows step 1's calls (browser_navigate /search, browser_cookie_set) and the retry's")

    def test_a_record_saved_after_the_retry_acted_is_not_the_steps(self):
        # Saved once the retry has acted, the record shows the retry, not the step.
        write(self.root, {"session-1/session.md": session(retry=OPEN + NETWORK)})
        self.refused("no record of step 1 (qae/AC1-step-1-network.txt or -console.txt) was saved by its tool")

    def test_a_walk_between_two_screenshots_of_the_retry_does_not_count(self):
        other = call("browser_navigate", {"url": SITE + "/companies"}, {}) + call("browser_type", {"text": "Acme"}, {})
        write(self.root, {"session-1/session.md": session(NETWORK, retry=OPEN + shot("AC1", 2) + other)})
        self.refused("the session log shows calls (browser_navigate /companies, browser_type) between two screenshots of "
                     "step 1 or of the retry")


    def test_a_step_shot_twice_counts_only_when_nothing_changed_the_page_between(self):
        # A screenshot taken too early and taken again is one step; a walk in between is not.
        wait = call("browser_wait_for", {"time": 1}, {})
        write(self.root, {"session-1/session.md": session(NETWORK, step=OPEN + shot("AC1", 1) + wait)})
        result = self.artifacts()
        self.assertEqual(result.returncode, 0, result.stdout)
        other = call("browser_navigate", {"url": SITE + "/other"}, {}) + call("browser_click", {"ref": "e9"}, {})
        write(self.root, {"session-1/session.md": session(NETWORK, step=OPEN + shot("AC1", 1) + other, retry=other)})
        self.refused("the session log shows calls (browser_navigate /other, browser_click) between two screenshots of step 1")


    def test_a_retry_whose_calls_are_not_the_steps_does_not_count(self):
        # The log copies step 1's words, but the session log shows the retry went somewhere else.
        retry = call("browser_navigate", {"url": SITE + "/companies"}, {}) + call("browser_type", {"text": "Acme"}, {})
        write(self.root, {"session-1/session.md": session(NETWORK, retry=retry)})
        self.refused("the session log shows step 1's calls (browser_navigate /search) and the retry's (browser_navigate "
                     "/companies, browser_type)")


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





class SessionLog(unittest.TestCase):
    def test_a_calls_arguments_are_everything_but_its_element_refs(self):
        import _session_log
        url, given = _session_log.inputs(json.dumps({"url": SITE, "fields": [{"ref": "e1", "element": "Email", "value": "a"}]}))
        self.assertEqual((url, json.loads(given)), (SITE, {"url": SITE, "fields": [{"value": "a"}]}))
        self.assertEqual(_session_log.inputs("not json"), ("", ""))



if __name__ == "__main__":
    unittest.main()
