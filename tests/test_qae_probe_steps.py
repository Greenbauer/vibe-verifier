"""Which answer is the QAE invalid-input probe's own (gates/_probe.py): the one 400 or 422 its last action logged, to
a write, and never the same status and URL answered to another step. The evidence is tests/test_qae_probe.py's."""
import unittest

from helpers import write
from test_qae_probe import FILL, LINES, SITE, STEPS, HANDLED, ProbeFixture, call, page, refused, save, session, shot


class NotTheProbesOwn(ProbeFixture):
    def test_a_second_refused_request_in_the_step_excuses_neither(self):
        # The save's 400 and a totals request's 400 in the same step: which one the invalid input sent cannot be told.
        totals = SITE + "/api/quotes/1/totals"
        write(self.root, {"console-1.log": refused(400)[0] + refused(400, url=totals)[0],
                          "session-1/session.md": session(save(events="- New console entries: qae-artifacts/console-1.log#L1-L2"))})
        result = self.artifacts()
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertIn("request answered 400 outside the allowlist: [POST] %s" % LINES, result.stdout)
        self.assertIn("not excused: the step was answered 400 or 422 more than once, and a probe sends one invalid input",
                      result.stdout)

    def test_an_answer_to_an_earlier_action_of_the_step_is_not_its_submits(self):
        # The fill logged the 400 (an autosave of the field), then the step clicked Save, which logged nothing.
        filled = call("browser_fill_form", {"fields": [{"target": "e11", "name": "Quantity", "type": "textbox", "value": "-1"}]},
                      {"events": refused(400)[1]})
        write(self.root, {"session-1/session.md": session(save(events="")).replace(FILL, filled)})
        self.not_excused("the answer came before the step's last action, so it is not that action's")


    def test_a_refused_read_is_not_the_forms_write(self):
        # The click set off a GET the site answered 400 (a background request), not the form's write.
        other = SITE + "/api/quotes/1/totals"
        entry, events = refused(400, url=other)
        text = session(save(events=events)).replace("[POST] " + LINES, "[GET] " + other)
        write(self.root, {"console-1.log": entry, "session-1/session.md": text})
        result = self.artifacts()
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertIn("not excused: the network records do not show 400 /api/quotes/1/totals answered to a write", result.stdout)


class AnotherStep(ProbeFixture):
    def test_the_excuse_does_not_reach_the_same_answer_to_another_step(self):
        # Step 3 saved a valid line, and the site answered it 400 too: by status and URL the probe's excuse would
        # cover it, so the probe's is withdrawn and both stay findings.
        entry = refused(400)[0]
        third = (call("browser_click", {"element": "Save button", "target": "e13"},
                      {"page": page(), "snapshot": HANDLED, "events": refused(400, line=2)[1]}) + shot("AC1", 3))
        write(self.root, {"qae/AC1.md": STEPS + "- step 3: saved the row with quantity 2 -> the row said it failed\n",
                          "qae/AC1-step-3.png": "png", "console-1.log": entry + entry,
                          "session-1/session.md": session(after=third)})
        self.not_excused("the run shows 400 /api/quotes/1/lines answered to another step too, and an excuse by status "
                         "and URL would cover it")

    def test_more_requests_in_the_network_record_than_logged_answers_withdraw_it(self):
        # Two 400s to the same URL, one console line: the other request is not shown to be the probe's.
        text = session().replace("=> [400] Bad Request", "=> [400] Bad Request\\n9. [POST] %s => [400] Bad Request" % LINES)
        write(self.root, {"session-1/session.md": text})
        self.not_excused("the run shows 400 /api/quotes/1/lines answered to another step too")


if __name__ == "__main__":
    unittest.main()
