"""Pull-request age on a row: coarser units as it gets older, and yellow then red."""
import json
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def node(source):
    result = subprocess.run(["node", "-e", source], cwd=ROOT, capture_output=True, text=True)
    if result.returncode:
        raise AssertionError(result.stdout + result.stderr)
    return json.loads(result.stdout)


class PullRequestAge(unittest.TestCase):
    def test_pull_request_age_coarsens_and_colors_by_how_old_it_is(self):
        result = node(r'''
const h=require('./dashboard/static/helpers.js');
const now=Date.parse('2026-10-06T00:00:00Z'), day=86400;
const at=seconds=>new Date(now-seconds*1000).toISOString();
const row=seconds=>({label:h.since(at(seconds),now),tone:h.ageClass(at(seconds),now)});
console.log(JSON.stringify({
 seconds:row(25), minutes:row(4*60+25), hours:row(14*3600+15*60),
 justUnderADay:row(day-1), oneDay:row(day), dayAndHours:row(day+21*3600),
 threeDays:row(3*day), justOverThreeDays:row(3*day+1), sevenDays:row(7*day),
 justOverSevenDays:row(7*day+1), twoWeeks:row(14*day), justOverTwoWeeks:row(14*day+1),
 thirtyDays:row(30*day), justOverThirtyDays:row(30*day+1), seventyOneDays:row(71*day+3*3600),
 justUnderAYear:row(365*day-1), oneYear:row(365*day), twoYears:row(800*day),
 unavailable:{label:h.since('nope',now),tone:h.ageClass(null,now)},
 jobStillShowsHours:{day:h.duration(36*3600),long:h.duration(49*3600)}
}));
''')
        cases = {
            "seconds": ("25s", None), "minutes": ("4m 25s", None), "hours": ("14h 15m", None),
            "justUnderADay": ("23h 59m", None), "oneDay": ("1d", None), "dayAndHours": ("1d", None),
            "threeDays": ("3d", None), "justOverThreeDays": ("3d", "age-yellow"),
            "sevenDays": ("7d", "age-yellow"), "justOverSevenDays": ("1w", "age-yellow"),
            "twoWeeks": ("2w", "age-yellow"), "justOverTwoWeeks": ("2w", "age-red"),
            "thirtyDays": ("4w", "age-red"), "justOverThirtyDays": ("1mo", "age-red"),
            "seventyOneDays": ("2mo", "age-red"), "justUnderAYear": ("12mo", "age-red"),
            "oneYear": ("1y", "age-red"), "twoYears": ("2y", "age-red"),
            "unavailable": ("Unavailable", None),
        }
        for name, (label, tone) in cases.items():
            self.assertEqual(result[name], {"label": label, "tone": tone})
        self.assertEqual(result["jobStillShowsHours"], {"day": "36h 0m", "long": "2d 1h"})
        css = (ROOT / "dashboard/static/styles.css").read_text()
        self.assertIn(".pr-age .age-yellow { color: var(--yellow); }", css)
        self.assertIn(".pr-age .age-red { color: var(--red); }", css)


if __name__ == "__main__":
    unittest.main()
