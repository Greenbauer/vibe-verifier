"""Hour-of-day token burn charts: clock mapping, observed versus unobserved hours, baselines, scales."""

import json
import os
import re
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HOUR = 3600000


def node(source, tz="UTC"):
    result = subprocess.run(["node", "-e", source], cwd=ROOT, capture_output=True, text=True,
                            env={**os.environ, "TZ": tz})
    if result.returncode:
        raise AssertionError(result.stdout + result.stderr)
    return json.loads(result.stdout)


def burn(samples, now, through="undefined", tz="UTC"):
    return node("const c=require('./dashboard/static/charts.js');"
                "console.log(JSON.stringify(c.hourlyBurn(%s,%s,%s)));" % (json.dumps(samples), now, through), tz)


def sample(at, tokens):
    return {"timestamp": at, "bot": "ci-swe", "input_tokens": tokens, "output_tokens": 0}


NOW = "Date.UTC(2026,8,30,15,20,0)"


class HourOfDay(unittest.TestCase):
    def test_labels_name_the_four_gridline_hours(self):
        result = node("const c=require('./dashboard/static/charts.js');"
                      "console.log(JSON.stringify([0,6,12,18,23].map(c.hourLabel)));")
        self.assertEqual(result, ["12a", "6a", "12p", "6p", "11p"])

    def test_samples_land_on_the_viewers_local_clock_hour(self):
        samples = [sample("2026-09-30T14:30:00Z", 7)]
        utc = burn(samples, NOW)
        # 14:30Z is 20:00 in India (UTC+5:30), the same local hour as now (20:50), not the one before.
        india = burn(samples, NOW, tz="Asia/Kolkata")
        self.assertEqual(utc["last24h"][14], 7)
        self.assertEqual(utc["last24h"][15], 0)
        self.assertEqual(india["last24h"][20], 7)
        self.assertEqual(india["last24h"][19], None)


class ObservedHours(unittest.TestCase):
    def test_hours_before_the_first_sample_are_unobserved_and_later_empty_hours_are_measured_zero(self):
        result = burn([sample("2026-09-30T12:10:00Z", 0)], NOW)
        self.assertEqual(result["last24h"][12], 0)
        self.assertEqual(result["last24h"][13:16], [0, 0, 0])
        self.assertTrue(all(value is None for value in result["last24h"][:12] + result["last24h"][16:]))
        self.assertTrue(all(value is None for value in result["avg"]))
        self.assertIsNone(result["baselineDays"])

    def test_a_bot_with_no_sample_is_not_observed_rather_than_zero(self):
        self.assertIsNone(burn([], NOW))
        self.assertIsNone(burn([sample("not a time", 5), sample("2026-09-30T16:00:00Z", 5)], NOW))

    def test_hours_after_a_stale_source_last_observed_are_unobserved(self):
        through = "Date.UTC(2026,8,30,11,40,0)"
        result = burn([sample("2026-09-30T05:10:00Z", 9)], NOW, through)
        self.assertEqual(result["last24h"][5:12], [9, 0, 0, 0, 0, 0, 0])
        self.assertEqual(result["last24h"][12:16], [None] * 4)
        # A sample newer than the source timestamp still proves its own hour was observed.
        later = burn([sample("2026-09-30T05:10:00Z", 9), sample("2026-09-30T13:05:00Z", 4)], NOW, through)
        self.assertEqual(later["last24h"][11:16], [0, 0, 4, None, None])


class UsualDay(unittest.TestCase):
    def test_baseline_averages_only_the_prior_days_that_were_observed(self):
        result = burn([sample("2026-09-28T12:10:00Z", 100), sample("2026-09-29T12:40:00Z", 300),
                       sample("2026-09-30T12:05:00Z", 50)], NOW)
        self.assertEqual(result["last24h"][12], 50)
        self.assertEqual(result["avg"][12], 200)  # (300 + 100) / 2, the two observed prior days
        self.assertEqual(result["avg"][13], 0)
        # 11:00 on the 28th was before the first sample, so 11:00 averages one day, not two.
        self.assertEqual(result["avg"][11], 0)
        self.assertEqual(result["baselineDays"], 1)
        totals = node("const c=require('./dashboard/static/charts.js');console.log(JSON.stringify([c.totals(%s),"
                      "c.totals({last24h:Array(24).fill(1000),avg:[null,...Array(23).fill(5)]})]));" % json.dumps(result))
        self.assertEqual(totals, ["Last 24 hours 50 tokens · usual day 200", "Last 24 hours 24k tokens"])

    def test_all_bots_adds_only_observed_hours(self):
        result = node(r"""
const c=require('./dashboard/static/charts.js');
const blank=()=>Array(24).fill(null);
const a={last24h:blank(),avg:blank(),baselineDays:3}, b={last24h:blank(),avg:blank(),baselineDays:null};
a.last24h[3]=5; b.last24h[3]=2; b.last24h[4]=0; a.avg[3]=4;
console.log(JSON.stringify(c.sumBurns([a,b])));
""")
        self.assertEqual(result["last24h"][3:6], [7, 0, None])
        self.assertEqual(result["avg"][3:5], [4, None])
        self.assertEqual(result["baselineDays"], 3)


class Drawing(unittest.TestCase):
    def test_bot_cards_share_a_ceiling_and_all_bots_scales_to_itself_with_its_pace(self):
        result = node(r"""
const c=require('./dashboard/static/charts.js');
const blank=()=>Array(24).fill(null);
const quiet={last24h:blank(),avg:blank()}, busy={last24h:blank(),avg:blank()};
quiet.last24h[1]=10; busy.last24h[2]=400; busy.avg[5]=900;
console.log(JSON.stringify({shared:c.ceiling([quiet,busy]),own:c.ceiling([quiet],50),zero:c.ceiling([{last24h:[0],avg:[null]}])}));
""")
        self.assertEqual(result, {"shared": 900, "own": 50, "zero": 1})

    def test_smoothing_never_dips_below_the_zero_baseline(self):
        result = node(r"""
const c=require('./dashboard/static/charts.js');
const points=[{x:0,y:110},{x:10,y:110},{x:20,y:8},{x:30,y:110},{x:40,y:110}];
const ys=d=>d.replace(/[MC]/g,'').trim().split(/\s+/).map(Number).filter((_,i)=>i%2===1);
console.log(JSON.stringify({clamped:ys(c.smoothPath(points,{min:8,max:110})),free:ys(c.smoothPath(points,{min:-1e9,max:1e9}))}));
""")
        self.assertTrue(all(8 <= y <= 110 for y in result["clamped"]))
        self.assertGreater(max(result["free"]), 110)  # Unclamped Catmull-Rom does overshoot here.

    def test_an_unobserved_hour_breaks_the_line_and_a_lone_point_draws_nothing(self):
        result = node("const c=require('./dashboard/static/charts.js');"
                      "console.log(JSON.stringify(c.runs([null,1,2,null,3,null,4,5],0,7)));")
        self.assertEqual([[point["value"] for point in run] for run in result], [[1, 2], [4, 5]])


class Cards(unittest.TestCase):
    def test_unobserved_bots_say_so_and_only_all_bots_gets_the_pace_line(self):
        app = (ROOT / "dashboard/static/app.js").read_text()
        source = re.search(r"\n(  function usageCharts\([\s\S]*?)\n  function renderUsage\(", app).group(1)
        result = node(r"""
const VVCharts={...require('./dashboard/static/charts.js')};
const {BOT_META}=require('./dashboard/static/helpers.js');
const drawn=[];
VVCharts.drawBurn=(burn,options)=>(drawn.push({maximum:options.maximum,pace:options.pace}),{tag:'chart'});
function el(tag,attrs={},...children){
  const node={tag,attrs,children:children.flat().filter(value=>value!==null&&value!==undefined)};
  node.append=(...items)=>node.children.push(...items); return node;
}
const text=node=>typeof node==='string'?node:(node.children||[]).map(text).join(' ');
const snapshot={agents:{rows:[{id:'ci-swe',name:'SWE',role:'swe'},{id:'ci-qae',name:'QAE',role:'qae'},{id:'qae-2',name:'QAE2',role:'qae'}]}};
const at=hours=>new Date(Date.now()-hours*3600000).toISOString();
const usage={sampled_at:new Date().toISOString(),accounts:[{id:'a',quota_windows:[{allowance_tokens:1000,pace_tokens_per_second:1}]}],
  samples:[{account:'a',bot:'ci-swe',timestamp:at(2),input_tokens:40,output_tokens:0},
           {account:'a',bot:'ci-qae',timestamp:at(3),input_tokens:900,output_tokens:0}]};
const build=new Function('el','BOT_META','VVCharts','snapshot',SOURCE+'\nreturn usageCharts;');
const section=build(el,BOT_META,VVCharts,snapshot)(usage);
const cards=section.children[2].children;
console.log(JSON.stringify({drawn,names:cards.map(card=>card.children[0].children[0].children[0]),
  qae2:text(cards[2]),allClass:cards[3].attrs.class}));
""".replace("SOURCE", json.dumps(source)))
        self.assertEqual(result["names"], ["SWE", "QAE", "QAE2", "All bots"])
        self.assertIn("Not yet observed", result["qae2"])
        self.assertIn("all-bots", result["allClass"])
        bots, everyone = result["drawn"][:2], result["drawn"][2]
        self.assertEqual([row["maximum"] for row in bots], [900, 900])
        self.assertEqual([row["pace"] for row in bots], [None, None])
        self.assertEqual(everyone["pace"], 3600)
        self.assertEqual(everyone["maximum"], 3600)


if __name__ == "__main__":
    unittest.main()
