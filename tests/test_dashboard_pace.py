"""The paced plan window: which one it is, when it began, and its fill against an even burn."""
import unittest
from datetime import datetime, timedelta, timezone

from dashboard.pace import plan_pace, plan_window_start
from dashboard.usage_artifacts import apply_plan_window

NOW = datetime(2026, 10, 5, 21, 0, tzinfo=timezone.utc)
RESET = NOW + timedelta(hours=96)  # 72 of the window's 168 hours have elapsed
EVEN = 72 / 168 * 100


def iso(value):
    return value.isoformat().replace("+00:00", "Z")


def window(**changes):
    return {"name": "7d", "used_percent": 40.0, "resets_at": iso(RESET), "allowance_tokens": None,
            "window_minutes": 10080, **changes}


def usage(windows=None, samples=(), **extra):
    return {"available": True, "stale": False, "accounts": [
        {"id": "runtime-observed", "label": "Agent runtime tokens", "quota_windows": []},
        {"id": "subscription-0", "label": "ChatGPT subscription",
         "quota_windows": [window()] if windows is None else windows}],
        "samples": list(samples), **extra}


def tokens(hours_ago, count):
    return {"account": "runtime-observed", "bot": "agent-a", "timestamp": iso(NOW - timedelta(hours=hours_ago)),
            "input_tokens": count, "output_tokens": 0}


class Pace(unittest.TestCase):
    def test_delta_is_fill_minus_the_elapsed_share_of_the_window(self):
        # 72 of 168 hours elapsed is 42.9% of the window; 40% used is 2.9 points under an even burn.
        self.assertAlmostEqual(plan_pace(usage(), NOW)["delta_points"], 40 - EVEN)
        ahead = plan_pace(usage([window(used_percent=62.0, resets_at=iso(NOW + timedelta(hours=126)))]), NOW)
        self.assertAlmostEqual(ahead["delta_points"], 62 - 25)

    def test_the_pace_does_not_move_with_how_much_the_bots_used(self):
        # The plan is a percent with a reset. Bot tokens once sized it: 107m tokens gave one line and
        # 227m, four hours later on the same plan, a line nearly twice as high.
        quiet = plan_pace(usage(samples=[tokens(10, 107_000_000)]), NOW)
        busy = plan_pace(usage(samples=[tokens(10, 227_000_000)], stale=True, history_stale=True, partial=True), NOW)
        self.assertEqual(quiet, busy)
        self.assertEqual(set(quiet), {"delta_points"})  # a percent against the window, and no figure in tokens
        self.assertAlmostEqual(quiet["delta_points"], 40 - EVEN)

    def test_the_longest_window_is_paced(self):
        five_hours = window(name="5h", used_percent=90.0, resets_at=iso(NOW + timedelta(hours=1)), window_minutes=300)
        self.assertAlmostEqual(plan_pace(usage([five_hours, window()]), NOW)["delta_points"], 40 - EVEN)

    def test_no_window_to_pace_has_no_delta(self):
        two_plans = usage()
        two_plans["accounts"].append({"id": "subscription-1", "label": "Second plan", "quota_windows": [window()]})
        cases = {"two plans": two_plans,
                 "no window length": usage([window(window_minutes=None)]),
                 "already reset": usage([window(resets_at=iso(NOW - timedelta(minutes=1)))]),
                 "not begun": usage([window(resets_at=iso(NOW + timedelta(hours=169)))]),
                 "no plan": usage([]),
                 "unavailable": {"available": False}}
        for name, value in cases.items():
            with self.subTest(name):
                self.assertEqual(plan_pace(value, NOW), {"delta_points": None})


class WindowStart(unittest.TestCase):
    def test_the_paced_window_began_its_length_before_its_reset(self):
        self.assertEqual(plan_window_start(usage()), RESET - timedelta(days=7))
        five_hours = window(resets_at=iso(NOW + timedelta(hours=1)), window_minutes=300)
        self.assertEqual(plan_window_start(usage([five_hours, window()])), RESET - timedelta(days=7))

    def test_no_single_plan_has_no_start(self):
        two_plans = usage()
        two_plans["accounts"].append({"id": "subscription-1", "label": "Second plan", "quota_windows": [window()]})
        for value in (two_plans, usage([window(window_minutes=None)]), usage([]), {}, None):
            self.assertIsNone(plan_window_start(value))

    def test_history_that_covers_the_plan_window_is_complete(self):
        # The listing never reached seven days, but its oldest artifact is before the window began.
        history = {"hard_partial": False, "listing_complete": False, "covered_until": iso(NOW - timedelta(days=6)),
                   "partial": True, "completeness": "Partial capture. leftover"}
        apply_plan_window(history, plan_window_start(usage()))
        self.assertFalse(history["partial"])
        self.assertNotIn("Partial capture.", history["completeness"])

    def test_history_that_starts_inside_the_plan_window_stays_partial(self):
        history = {"hard_partial": False, "listing_complete": False, "covered_until": iso(NOW - timedelta(hours=1)),
                   "partial": False, "completeness": "leftover"}
        apply_plan_window(history, plan_window_start(usage()))
        self.assertTrue(history["partial"])
        self.assertIn("Partial capture.", history["completeness"])


if __name__ == "__main__":
    unittest.main()
