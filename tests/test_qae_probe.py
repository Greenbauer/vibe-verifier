"""The QAE explorer's invalid-input probe (gates/_probe.py): a 400 or 422 the site answered to the step the explorer
marked, while the page kept the controls the step used and showed new text, is a handled refusal the qae-artifacts
gate excuses; a 5xx, an error or blank page, an answer to another step and an unmarked step never are; and the QA
review names what was excused and why. Driven through the gates' and the runner's command lines against evidence
shaped like playwright-mcp's (0.0.81: a call's `page`, `snapshot` and `events` fields, its console-*.log lines)."""
import json
import shutil
import tempfile
import unittest

from helpers import gate, make_repo, write
from qa_review_fixture import GhFixture
from test_qae_retry import call, shot

from _probe import marked  # noqa: E402  (gates/, which helpers puts on the path)
from _snapshot import kept  # noqa: E402

SITE = "http://localhost:3000"
QUOTE = SITE + "/quotes/1"
LINES = SITE + "/api/quotes/1/lines"
BODY = "## Acceptance criteria\n\n- A line can be added to a quote\n"
STEPS = ("- step 1: opened the quote -> the lines table and its add-line row showed\n"
         "- step 2: invalid input: saved the add-line row with quantity -1 -> the row said quantity must be positive\n")
BANNER = '- generic [ref=e1]:\n  - banner [ref=e2]:\n    - heading "Quotes" [level=1] [ref=e3]\n'
FIELD = '              - spinbutton "Quantity" [ref=e11]: "1"\n'
FORM = (BANNER + '  - main [ref=e4]:\n    - region "Q-00001" [ref=e5]:\n      - table [ref=e6]:\n        - rowgroup [ref=e7]:\n'
        '          - row [ref=e8]:\n            - cell [ref=e10]:\n' + FIELD +
        '            - cell [ref=e12]:\n              - button "Save" [ref=e13] [cursor=pointer]\n')
# What the page shows once the site refused the line: the same row, its controls, and a message under a new ref.
HANDLED = FORM.replace(FIELD, FIELD + "              - paragraph [ref=e21]: quantity must be a positive number\n")


def page(url=QUOTE):
    return "- Page URL: %s\n- Page Title: Quotes" % url


def refused(status, line=1, url=LINES):
    """Chromium's console line for an answer, as console-*.log holds it, and the call field that links to it."""
    entry = ("[  900ms] [ERROR] Failed to load resource: the server responded with a status of %s () @ %s:0\n"
             % (status, url))
    return entry, "- New console entries: qae-artifacts/console-1.log#L%d" % line


def save(state=HANDLED, events=refused(400)[1], url=QUOTE, target="e13"):
    """The probe's click on Save, with what playwright-mcp wrote of the page after it."""
    return call("browser_click", {"element": "Save button", "target": target},
                {"page": page(url), "snapshot": state, "events": events})


OPEN = call("browser_navigate", {"url": QUOTE}, {"page": page(), "snapshot": FORM})
FILL = call("browser_fill_form", {"fields": [{"target": "e11", "name": "Quantity", "type": "textbox", "value": "-1"}]}, {})


def look(state=FORM, args=None):
    """The explorer's browser_snapshot of the whole page, right before the probe's last action, as the prompt asks."""
    return call("browser_snapshot", args or {}, {"page": page(), "inlineSnapshot": state})


BEFORE = look()


def session(probe=None, after=""):
    return (OPEN + shot("AC1", 1) + FILL + BEFORE + (save() if probe is None else probe) + shot("AC1", 2) + after
            + call("browser_network_requests", {}, {"result": "7. [POST] %s => [400] Bad Request\n" % LINES}))


class ProbeFixture(unittest.TestCase):
    """A walk whose step 2, the probe, saved a line with quantity -1; the site answered 400 and the row said so."""

    def setUp(self):
        self.repo = make_repo(self, {"README.md": "x"})
        self.root = tempfile.mkdtemp(prefix="vv-probe-") + "/qae-artifacts"
        self.addCleanup(shutil.rmtree, self.root.rpartition("/")[0], True)
        self.inputs = tempfile.mkdtemp(prefix="vv-probe-in-")
        self.addCleanup(shutil.rmtree, self.inputs, True)
        write(self.inputs, {"pr-body.md": BODY})
        write(self.root, {"qae/AC1.md": STEPS, "qae/AC1-step-1.png": "png", "qae/AC1-step-2.png": "png",
                          "console-1.log": refused(400)[0], "session-1/session.md": session()})

    def artifacts(self):
        return gate("qae-artifacts", self.repo, "--artifacts", self.root, "--criteria", self.inputs + "/pr-body.md",
                    "--site", SITE)

    def excused(self, said="400 /api/quotes/1/lines"):
        result = self.artifacts()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("qae-artifacts: AC1: step 2, the invalid-input probe, was answered %s while the page kept the "
                      "controls it used and showed new text: excused as a handled refusal" % said, result.stdout)

    def not_excused(self, why, status="400"):
        result = self.artifacts()
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("request answered %s outside the allowlist: [POST] %s" % (status, LINES), result.stdout)
        self.assertIn("console error outside the allowlist: Failed to load resource", result.stdout)
        if why:
            self.assertIn("step 2, the invalid-input probe, was answered %s /api/quotes/1/lines, not excused: %s"
                          % (status, why), result.stdout)
        return result


class HandledRefusal(ProbeFixture):
    def test_a_handled_400_to_the_marked_probe_is_excused_and_says_why(self):
        self.excused()

    def test_a_handled_422_is_excused_too(self):
        entry, events = refused(422)
        write(self.root, {"console-1.log": entry, "session-1/session.md": session(save(events=events)).replace(
            "=> [400] Bad Request", "=> [422] Unprocessable Entity")})
        self.excused("422 /api/quotes/1/lines")

    def test_an_answer_logged_after_the_click_counts_with_the_snapshot_taken_after_it(self):
        # The click returned before the answer; the explorer's browser_snapshot, before the step's screenshot, logged it.
        later = call("browser_snapshot", {}, {"page": page(), "inlineSnapshot": HANDLED, "events": refused(400)[1]})
        write(self.root, {"session-1/session.md": session(save(state=FORM, events="") + later)})
        self.excused()

    def test_the_same_step_unmarked_is_not_excused(self):
        write(self.root, {"qae/AC1.md": STEPS.replace("invalid input: ", "")})
        result = self.not_excused(None)
        self.assertNotIn("invalid-input probe", result.stdout)

    def test_the_marker_counts_once_per_role(self):
        write(self.root, {"qae/AC1.md": STEPS.replace("step 1: opened", "step 1: invalid input: opened")})
        self.not_excused("the log marks more than one invalid-input step, and a criterion tries one invalid input")
        self.assertEqual(marked("- step 1: as admin: invalid input: a -> b\n- step 2: as rep: invalid input: c -> d\n"),
                         ([1, 2], None))
        self.assertEqual(marked("- step 1: as admin: invalid input: a -> b\n- step 2: As  admin : invalid input: c -> d\n")[1],
                         "the log marks more than one invalid-input step as admin, and a criterion tries one invalid input")


class NotHandled(ProbeFixture):
    def test_an_error_page_in_place_of_the_form_is_not_excused(self):
        # A crash replaces the row: its controls are other elements under new refs, whatever text the page shows.
        crashed = '- generic [ref=e20]:\n  - heading "Application error: a client-side exception has occurred" [ref=e21]\n'
        write(self.root, {"session-1/session.md": session(save(state=crashed))})
        self.not_excused("the snapshot after the answer no longer holds the page's elements (e11, e13 of the step's controls lost")

    def test_a_blank_page_is_not_excused(self):
        write(self.root, {"session-1/session.md": session(save(state="- generic [ref=e20]\n"))})
        self.assertEqual(kept(FORM, "- generic [ref=e20]\n"), 0.0)
        self.not_excused("the snapshot after the answer no longer holds the page's elements (e11, e13 of the step's controls lost")

    def test_the_same_refs_on_other_elements_are_not_the_controls(self):
        # A form posted as a whole document came back as an error page at the same URL: refs restart with each
        # document, so e11 and e13 name other elements now, and only their role and name tell.
        error = ('- generic [ref=e1]:\n  - main [ref=e4]:\n    - generic [ref=e11]:\n      - generic [ref=e13]:\n'
                 '        - paragraph [ref=e21]: 400 Bad Request\n')
        write(self.root, {"session-1/session.md": session(save(state=error))})
        self.not_excused("the snapshot after the answer no longer holds the page's elements (e11, e13 of the step's controls lost")

    def test_a_value_typed_in_an_earlier_step_is_not_a_message(self):
        # The value entered in step 1, the snapshot and the save in step 2: the page echoing -1 is not its answer.
        echo = HANDLED.replace("quantity must be a positive number", "total -1 units")
        split = session(save(state=echo)).replace(FILL, "").replace(shot("AC1", 1), FILL + shot("AC1", 1))
        write(self.root, {"session-1/session.md": split})
        self.not_excused("the snapshot after the answer shows no new text beside the controls the step used")

    def test_the_typed_value_in_a_cells_name_is_not_a_message(self):
        # Playwright names a cell from its content, the field's value too, so typing -1 gives the cell a new ref.
        echoed = FORM.replace("- cell [ref=e10]:", '- cell "-1" [ref=e14]:').replace('[ref=e11]: "1"', '[ref=e11]: "-1"')
        write(self.root, {"session-1/session.md": session(save(state=echoed))})
        self.not_excused("the snapshot after the answer shows no new text beside the controls the step used")

    def test_an_error_in_place_of_the_main_content_is_not_beside_a_header_form(self):
        # A search form in the banner kept its controls while an error replaced the main content: the page lost part of
        # what it showed, and the root that holds both landmarks is not the form's part of the page.
        header = ('- generic [ref=e1]:\n  - banner [ref=e2]:\n    - search [ref=e3]:\n      - searchbox "Quantity" [ref=e11]\n'
                  '      - button "Save" [ref=e13]\n  - main [ref=e4]:\n    - heading "Quotes" [ref=e5]\n')
        crashed = header.replace('    - heading "Quotes" [ref=e5]\n', '    - paragraph [ref=e31]: Something went wrong\n')
        text = session(save(state=crashed)).replace(json.dumps(FORM)[1:-1], json.dumps(header)[1:-1])
        write(self.root, {"session-1/session.md": text})
        self.not_excused("the snapshot after the answer no longer holds the page's elements (none of the step's controls "
                         "lost, 85% of the page kept)")
        # The same page built of plain elements, no landmarks: what it lost still tells.
        plain = lambda state: state.replace("banner", "generic").replace("search", "generic").replace("main", "generic")  # noqa: E731
        write(self.root, {"session-1/session.md": text.replace(json.dumps(header)[1:-1], json.dumps(plain(header))[1:-1])
                          .replace(json.dumps(crashed)[1:-1], json.dumps(plain(crashed))[1:-1])})
        self.not_excused("the snapshot after the answer no longer holds the page's elements")

    def test_a_page_that_kept_its_controls_and_showed_nothing_new_is_not_excused(self):
        write(self.root, {"session-1/session.md": session(save(state=FORM))})
        self.not_excused("the snapshot after the answer shows no new text beside the controls the step used")

    def test_an_error_page_at_another_url_is_not_excused(self):
        write(self.root, {"session-1/session.md": session(save(url=SITE + "/500"))})
        self.not_excused("the page is not at the URL it was at before the step's last action (/quotes/1)")

    def test_the_words_of_the_log_or_a_script_show_nothing(self):
        # The step line says the page handled it, and a script returned a snapshot-like text: only the snapshot
        # playwright-mcp wrote after the answer counts, and it shows the row unchanged.
        script = call("browser_evaluate", {"function": "() => 1"}, {"result": HANDLED, "page": page(), "events": refused(400)[1]})
        write(self.root, {"session-1/session.md": session(save(state=FORM, events="") + script)})
        self.not_excused("the step ran a script, which can send a request and change the page itself")

    def test_a_control_named_by_a_selector_proves_nothing(self):
        selector = "the step acted on a control by a selector, or on none"
        write(self.root, {"session-1/session.md": session(save(target="#save")).replace('"target": "e11"', '"target": "#qty"')})
        self.not_excused(selector)
        # One control by a selector is enough: it is not checked, so neither is the step.
        write(self.root, {"session-1/session.md": session().replace('"target": "e11"', '"target": "#qty"')})
        self.not_excused(selector)

    def test_text_the_page_showed_before_the_answer_is_not_new(self):
        # The fill put up "unsaved changes" before the save; the save's answer re-rendered a total under a new ref.
        # Neither is the page's answer to the input: both were on the page before the last action.
        line = "              - paragraph [ref=e21]: Unsaved changes, line total 5\n"
        before = FORM.replace(FIELD, FIELD + line)
        for after in (before, before.replace("[ref=e21]", "[ref=e30]")):
            write(self.root, {"session-1/session.md": session(save(state=after)).replace(BEFORE, look(before))})
            self.not_excused("the snapshot after the answer shows no new text beside the controls the step used")

    def test_without_a_whole_page_snapshot_right_before_the_action_nothing_is_excused(self):
        why = "no snapshot of the whole page was written in the step right before its last action"
        write(self.root, {"session-1/session.md": session().replace(BEFORE, "")})
        self.not_excused(why)
        # The value entered and the page shot in step 1, the save alone in step 2: step 1's snapshot is not the step's.
        split = session().replace(FILL + BEFORE, "").replace(shot("AC1", 1), FILL + BEFORE + shot("AC1", 1))
        write(self.root, {"session-1/session.md": split})
        self.not_excused(why)
        write(self.root, {"session-1/session.md": session().replace(BEFORE, look(args={"depth": 4}))})
        self.not_excused(why)

    def test_new_text_away_from_the_controls_shows_nothing(self):
        # The row stayed as it was, and a status in the banner changed: not a message about the input.
        synced = FORM.replace(BANNER, BANNER + "    - status [ref=e22]: Last synced just now\n")
        write(self.root, {"session-1/session.md": session(save(state=synced))})
        self.not_excused("the snapshot after the answer shows no new text beside the controls the step used")

    def test_an_uncaught_exception_in_the_step_is_not_excused(self):
        entry, _ = refused(400)
        write(self.root, {"console-1.log": entry + "[  950ms] TypeError: lines is undefined\n",
                          "session-1/session.md": session(save(events="- New console entries: qae-artifacts/console-1.log#L1-L2"))})
        result = self.not_excused("a console entry of the step is an uncaught exception")
        self.assertIn("uncaught exception outside the allowlist: TypeError: lines is undefined", result.stdout)


class NeverExcused(ProbeFixture):
    def test_a_5xx_to_the_probe_is_never_excused(self):
        entry, events = refused(500)
        write(self.root, {"console-1.log": entry, "session-1/session.md": session(save(events=events)).replace(
            "=> [400] Bad Request", "=> [500] Internal Server Error")})
        result = self.not_excused(None, status="500")
        self.assertNotIn("invalid-input probe", result.stdout)

    def test_another_refusal_status_is_never_excused(self):
        entry, events = refused(409)
        write(self.root, {"console-1.log": entry, "session-1/session.md": session(save(events=events)).replace(
            "=> [400] Bad Request", "=> [409] Conflict")})
        self.not_excused(None, status="409")

    def test_an_answer_no_call_of_the_step_logged_is_not_the_probes(self):
        # The same 400 logged by step 1's navigation: an answer to another step, whatever step 2 shows.
        opened = OPEN.replace('"snapshot"', '"events": "%s", "snapshot"' % refused(400)[1])
        write(self.root, {"session-1/session.md": session(save(events="")).replace(OPEN, opened)})
        result = self.not_excused(None)
        self.assertNotIn("invalid-input probe", result.stdout)


class ProbeInTheReview(GhFixture):
    def test_the_review_says_what_the_probe_excused_and_why_not(self):
        # Named as the verify job names it: a console log a call links to as qae-artifacts/<path> is read at <path>.
        root = tempfile.mkdtemp(prefix="vv-probe-") + "/qae-artifacts"
        self.addCleanup(shutil.rmtree, root.rpartition("/")[0], True)
        write(root, {"qae/AC1.md": STEPS, "console-1.log": refused(400)[0], "session-1/session.md": session()})
        manifest = self.file("manifest", "qae-artifacts --artifacts qae-artifacts\n")
        body = self.posted(extra=("--artifacts", root, "--manifest", manifest))
        self.assertIn("- AC1, explorer says PASS (step 2, the invalid-input probe, was answered 400 /api/quotes/1/lines "
                      "while the page kept the controls it used and showed new text: excused as a handled refusal): "
                      "The home page loads\n", body)
        write(root, {"session-1/session.md": session(save(state=FORM))})
        body = self.posted(extra=("--artifacts", root, "--manifest", manifest))
        self.assertIn("(step 2, the invalid-input probe, was answered 400 /api/quotes/1/lines, not excused: the snapshot "
                      "after the answer shows no new text beside the controls the step used)", body)


if __name__ == "__main__":
    unittest.main()
