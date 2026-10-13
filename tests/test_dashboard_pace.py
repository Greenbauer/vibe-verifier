"""The paced plan window: which one it is, when it began, and its fill against an even burn."""
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from dashboard.agents import agent_view
from dashboard.pace import plan_burn, plan_pace, plan_window_start, weighted_tokens
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
    def test_pace_is_fill_minus_the_elapsed_share_of_the_window_in_points(self):
        # 72 of 168 hours elapsed is 42.9% of the window; 40% used is 2.9 points under an even burn.
        self.assertAlmostEqual(plan_pace(usage(), NOW), 40 - EVEN)
        ahead = usage([window(used_percent=62.0, resets_at=iso(NOW + timedelta(hours=126)))])
        self.assertAlmostEqual(plan_pace(ahead, NOW), 62 - 25)

    def test_the_pace_does_not_move_with_how_much_the_bots_used(self):
        # The plan is a percent with a reset. Bot tokens once sized it: 107m tokens gave one line and
        # 227m, four hours later on the same plan, a line nearly twice as high.
        quiet = plan_pace(usage(samples=[tokens(10, 107_000_000)]), NOW)
        busy = plan_pace(usage(samples=[tokens(10, 227_000_000)], stale=True, history_stale=True, partial=True), NOW)
        self.assertEqual(quiet, busy)
        self.assertAlmostEqual(quiet, 40 - EVEN)

    def test_the_page_is_given_points_and_nothing_in_tokens(self):
        view = agent_view(SimpleNamespace(agents=()), {}, {"available": True, "usage": usage(
            samples=[tokens(10, 107_000_000)])}, NOW)["usage"]
        self.assertAlmostEqual(view["pace_points"], 40 - EVEN)
        # `pace` was the token line's object. A page loaded before it went finds none and says so.
        self.assertNotIn("pace", view)
        self.assertIsNone(agent_view(SimpleNamespace(agents=()), {}, {"available": False}, NOW)["usage"]["pace_points"])

    def test_the_longest_window_is_paced(self):
        five_hours = window(name="5h", used_percent=90.0, resets_at=iso(NOW + timedelta(hours=1)), window_minutes=300)
        self.assertAlmostEqual(plan_pace(usage([five_hours, window()]), NOW), 40 - EVEN)

    def test_no_window_to_pace_has_no_pace(self):
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
                self.assertIsNone(plan_pace(value, NOW))


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


def plan_usage(samples, windows=None, **extra):
    return {"available": True, "stale": False, **extra, "accounts": [
        {"id": "collector:codex", "label": "Codex subscription", "provider": "OpenAI",
         "quota_windows": [window()] if windows is None else windows},
        {"id": "openai:runs", "label": "runs", "provider": "openai", "quota_windows": []},
        {"id": "anthropic:runs", "label": "runs", "provider": "anthropic", "quota_windows": []}],
        "samples": samples}


def run(hours_ago, fresh=0, cached=0, output=0, account="openai:runs"):
    return {"account": account, "bot": "agent-a", "timestamp": iso(NOW - timedelta(hours=hours_ago)),
            "input_tokens": fresh + cached, "cached_input_tokens": cached, "output_tokens": output}


class PlanLine(unittest.TestCase):
    def test_the_line_is_the_rest_of_the_plan_over_the_hours_left(self):
        line, _ = plan_burn(plan_usage([run(1, fresh=100)]), NOW)
        self.assertAlmostEqual(line["percent_per_hour"], (100 - 40) / 96)
        self.assertEqual((line["plan"], line["window"], line["used_percent"]), ("Codex subscription", "7d", 40.0))
        # The line needs no tokens: with none the bots stay unscaled, and the line is the same.
        empty, samples = plan_burn(plan_usage([]), NOW)
        self.assertEqual(empty["percent_per_hour"], line["percent_per_hour"])
        self.assertFalse(empty["sized"])
        self.assertEqual(samples, [])

    def test_each_billed_sample_is_its_weighted_share_of_the_used_percent(self):
        # Weighted: 1,000 fresh + 0.1 x 10,000 cached = 2,000; 200 output x 5 = 1,000. Window total 3,000 = 40%.
        line, samples = plan_burn(plan_usage([run(1, fresh=1000, cached=10_000), run(2, output=200)]), NOW)
        self.assertTrue(line["sized"])
        self.assertAlmostEqual(samples[0]["plan_percent"], 40 * 2000 / 3000)
        self.assertAlmostEqual(samples[1]["plan_percent"], 40 * 1000 / 3000)
        self.assertEqual(weighted_tokens({"input_tokens": 5, "output_tokens": 0}), 5)  # no cached count: all fresh

    def test_a_cache_read_counts_a_tenth_of_a_fresh_token(self):
        # The defect that removed the token line: raw counts, mostly cache reads, sized the plan.
        base = [run(30, fresh=1000), run(1, fresh=1000)]
        self.assertAlmostEqual(plan_burn(plan_usage(base), NOW)[1][1]["plan_percent"], 20)
        reread = plan_burn(plan_usage(base + [run(20, cached=10_000)]), NOW)[1][1]["plan_percent"]
        self.assertAlmostEqual(reread, 40 * 1000 / (2000 + 1000))  # 10,000 cache reads weigh 1,000

    def test_only_the_plans_provider_and_window_bill_to_it(self):
        samples = [run(1, fresh=1000), run(1, fresh=9000, account="anthropic:runs"), run(80, fresh=9000)]
        _, tagged = plan_burn(plan_usage(samples), NOW)
        self.assertAlmostEqual(tagged[0]["plan_percent"], 40)  # the window's only billed spend
        self.assertNotIn("plan_percent", tagged[1])           # another provider's plan
        self.assertAlmostEqual(tagged[2]["plan_percent"], 40 * 9)  # before the window: scaled, not counted

    def test_unsized_and_missing_cases(self):
        month = window(window_minutes=60 * 24 * 30)
        self.assertFalse(plan_burn(plan_usage([run(1, fresh=10)], [month]), NOW)[0]["sized"])
        self.assertFalse(plan_burn(plan_usage([run(1, fresh=10)], [window(used_percent=0.0)]), NOW)[0]["sized"])
        self.assertIsNone(plan_burn(plan_usage([run(1, fresh=10)], [window(resets_at=iso(NOW))]), NOW)[0])
        self.assertIsNone(plan_burn(plan_usage([run(1, fresh=10)], []), NOW)[0])
        self.assertIsNone(plan_burn({"available": False}, NOW)[0])
        self.assertTrue(plan_burn(plan_usage([run(1, fresh=10)], partial=True), NOW)[0]["partial"])

    def test_the_page_gets_the_line_and_tagged_samples(self):
        view = agent_view(SimpleNamespace(agents=()), {}, {"available": True, "usage": plan_usage([])}, NOW)["usage"]
        self.assertAlmostEqual(view["plan_line"]["percent_per_hour"], 60 / 96)
