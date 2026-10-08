"""The All-bots pace line spends a plan's remainder exactly at its reset, in the lines' own unit."""
import unittest
from datetime import datetime, timedelta, timezone

from dashboard.pace import plan_pace
from dashboard.usage_artifacts import apply_plan_window

NOW = datetime(2026, 10, 5, 21, 0, tzinfo=timezone.utc)
RESET = NOW + timedelta(hours=96)  # 72 of the window's 168 hours have elapsed


def iso(value):
    return value.isoformat().replace("+00:00", "Z")


def sample(hours_ago, tokens, account="runtime-observed"):
    return {"account": account, "bot": "agent-a", "timestamp": iso(NOW - timedelta(hours=hours_ago)),
            "input_tokens": tokens - 10, "output_tokens": 10}


def usage(samples, windows=None, **extra):
    windows = windows if windows is not None else [
        {"name": "7d", "used_percent": 40.0, "resets_at": iso(RESET), "allowance_tokens": None, "window_minutes": 10080}]
    return {"available": True, "stale": False, "accounts": [
        {"id": "runtime-observed", "label": "Agent runtime tokens", "quota_windows": []},
        {"id": "subscription-0", "label": "ChatGPT subscription", "quota_windows": windows}],
        "samples": samples, **extra}


class DerivedPace(unittest.TestCase):
    def test_history_that_covers_the_plan_window_draws_the_line(self):
        # The listing never reached seven days, but its oldest artifact is before the window began.
        history = {"hard_partial": False, "listing_complete": False,
                   "covered_until": (NOW - timedelta(days=6)).isoformat().replace("+00:00", "Z"),
                   "partial": True, "completeness": "Partial capture. leftover"}
        apply_plan_window(history, NOW - timedelta(hours=72))
        self.assertFalse(history["partial"])
        self.assertNotIn("Partial capture.", history["completeness"])
        pace = plan_pace(usage([sample(10, 400)], partial=False), NOW)
        self.assertEqual(pace["sized_from"], "bot_tokens")
        self.assertAlmostEqual(pace["tokens_per_hour"], 600 / 96)

    def test_a_gap_inside_the_plan_window_still_refuses_the_line(self):
        history = {"hard_partial": False, "listing_complete": False,
                   "covered_until": (NOW - timedelta(hours=1)).isoformat().replace("+00:00", "Z"),
                   "partial": False, "completeness": "leftover"}
        apply_plan_window(history, NOW - timedelta(hours=72))
        self.assertTrue(history["partial"])
        refused = plan_pace(usage([sample(10, 400)], partial=True), NOW)
        self.assertIsNone(refused["tokens_per_hour"])
        self.assertIn("stale or incomplete", refused["reason"])

    def test_pace_is_the_percent_pace_in_tokens(self):
        # 400 tokens since the window began made 40% of it: the window holds 1,000 tokens.
        pace = plan_pace(usage([sample(10, 300), sample(70, 100)]), NOW)
        self.assertEqual(pace["sized_from"], "bot_tokens")
        self.assertEqual(pace["window_tokens"], 400)
        self.assertEqual(pace["allowance_tokens"], 1000)
        self.assertEqual(pace["plan"], "ChatGPT subscription")
        # 600 tokens left over 96 hours; as a share of the window it is the plan's own (100 - 40)% / 96h.
        self.assertAlmostEqual(pace["tokens_per_hour"], 600 / 96)
        self.assertAlmostEqual(pace["tokens_per_hour"] / pace["allowance_tokens"] * 100, (100 - 40) / 96)

    def test_header_delta_is_fill_minus_elapsed_share_of_the_window(self):
        # 72 of 168 hours elapsed is 42.9% of the window; 40% used is 2.9 points under an even burn.
        self.assertAlmostEqual(plan_pace(usage([sample(1, 400)]), NOW)["delta_points"], 40 - 72 / 168 * 100)

    def test_header_delta_needs_no_token_history(self):
        # The plan's own percentage is enough for the header even when its size cannot be measured.
        pace = plan_pace(usage([], stale=True), NOW)
        self.assertIsNone(pace["tokens_per_hour"])
        self.assertAlmostEqual(pace["delta_points"], 40 - 72 / 168 * 100)

    def test_tokens_before_the_window_began_do_not_size_it(self):
        pace = plan_pace(usage([sample(10, 400), sample(73, 5000), sample(150, 9000)]), NOW)
        self.assertEqual(pace["window_tokens"], 400)

    def test_the_longest_window_is_paced(self):
        windows = [{"name": "5h", "used_percent": 90.0, "resets_at": iso(NOW + timedelta(hours=1)),
                    "allowance_tokens": None, "window_minutes": 300},
                   {"name": "7d", "used_percent": 40.0, "resets_at": iso(RESET), "allowance_tokens": None,
                    "window_minutes": 10080}]
        self.assertEqual(plan_pace(usage([sample(1, 400)], windows), NOW)["window"], "7d")

    def test_a_full_plan_paces_at_zero(self):
        windows = [{"name": "7d", "used_percent": 100.0, "resets_at": iso(RESET), "allowance_tokens": None,
                    "window_minutes": 10080}]
        self.assertEqual(plan_pace(usage([sample(1, 400)], windows), NOW)["tokens_per_hour"], 0)


class NoPace(unittest.TestCase):
    def assertNoPace(self, value, reason):
        self.assertIsNone(value["tokens_per_hour"])
        self.assertIn(reason, value["reason"])

    def window(self, **changes):
        return [{"name": "7d", "used_percent": 40.0, "resets_at": iso(RESET), "allowance_tokens": None,
                 "window_minutes": 10080, **changes}]

    def test_unsized_cases_say_why(self):
        cases = [
            (usage([sample(1, 400)], self.window(used_percent=0.0)), "0% used"),
            (usage([sample(1, 400)], self.window(window_minutes=None)), "window length"),
            (usage([sample(80, 400)]), "no bot tokens"),
            (usage([sample(1, 400)], stale=True), "stale or incomplete"),
            (usage([sample(1, 400)], history_stale=True), "stale or incomplete"),
            (usage([sample(1, 400)], partial=True), "stale or incomplete"),
            # A monthly window began before the seven days of samples this dashboard keeps.
            (usage([sample(1, 400)], self.window(window_minutes=60 * 24 * 30)), "seven days"),
            (usage([sample(1, 400)], self.window(resets_at=iso(NOW - timedelta(minutes=1)))), "has reset"),
            ({"available": False}, "unavailable"),
        ]
        for value, reason in cases:
            with self.subTest(reason=reason):
                self.assertNoPace(plan_pace(value, NOW), reason)

    def test_two_plans_cannot_split_one_token_history(self):
        value = usage([sample(1, 400)])
        value["accounts"].append({"id": "subscription-1", "label": "Second plan", "quota_windows": self.window()})
        self.assertNoPace(plan_pace(value, NOW), "more than one plan")


class ReportedAllowance(unittest.TestCase):
    def windows(self):
        return [{"name": "7d", "used_percent": 25.0, "resets_at": iso(RESET), "allowance_tokens": 1_000_000,
                 "window_minutes": None}]

    def test_reported_size_paces_tokens_billed_to_that_account(self):
        pace = plan_pace(usage([sample(1, 400, "subscription-0")], self.windows()), NOW)
        self.assertEqual(pace["sized_from"], "reported")
        self.assertAlmostEqual(pace["tokens_per_hour"], 750_000 / 96)

    def test_reported_size_for_another_account_draws_nothing(self):
        pace = plan_pace(usage([sample(1, 400)], self.windows()), NOW)
        self.assertIsNone(pace["tokens_per_hour"])
        self.assertIsNone(pace["delta_points"])  # no window length, so no elapsed share


if __name__ == "__main__":
    unittest.main()
