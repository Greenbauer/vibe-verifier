"""The CI timeline on a phone: a job's time wraps inside its narrow column instead of running into the Log
link beside it. Beside test_dashboard_timeline.py, which holds the timeline's other tests."""
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


class TimelinePhone(unittest.TestCase):
    def test_a_phone_wraps_a_long_time_in_its_column_instead_of_running_into_the_log_link(self):
        css = (ROOT / "dashboard/static/styles.css").read_text()
        def rule(selector, within=css):
            start = within.index("\n" + selector + " {") + 1
            return within[start:within.index("}", start)]
        # Wide: the 84px time column holds "23h 59m so far" on one line.
        self.assertIn("84px 28px", rule(".timeline-row"))
        self.assertIn("white-space: nowrap", rule(".timeline-time"))
        # Phone: the column is 52px, so "1h 2m so far" or "waiting 12m 3s" wraps to two balanced lines
        # inside it; on one line it ran past the column and over the row's Log link.
        phone = css[css.index("@media (max-width: 650px)"):]
        phone = phone[:phone.index("\n}\n")]
        self.assertIn("52px 24px", rule("  .timeline-row", phone))
        self.assertIn(".timeline-time { white-space: normal; text-wrap: balance; ", rule("  .timeline-time", phone))


if __name__ == "__main__":
    unittest.main()
