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

    def test_daylight_saving_changes_keep_samples_on_their_clock_hour_and_day(self):
        # Fall back: 2026-11-01 10:30 EST. 00:30 EDT is today's 12a, 01:30 EDT is 1a, and
        # yesterday's 12:30 EDT is yesterday's 12p (in the tail), even though that day had 25 hours.
        fall = burn([sample("2026-11-01T04:30:00Z", 1), sample("2026-11-01T05:30:00Z", 2),
                     sample("2026-10-31T16:30:00Z", 3)], "Date.UTC(2026,10,1,15,30,0)", tz="America/New_York")
        self.assertEqual([fall["last24h"][0], fall["last24h"][1], fall["last24h"][12]], [1, 2, 3])
        self.assertEqual(fall["last24h"][11], None)  # before the first sample
        # Spring forward: 2026-03-08 10:30 EDT. 01:30 EST is 1a; 2a did not exist and holds nothing.
        spring = burn([sample("2026-03-08T06:30:00Z", 5)], "Date.UTC(2026,2,8,14,30,0)", tz="America/New_York")
        self.assertEqual(spring["last24h"][1:4], [5, 0, 0])


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

    def test_a_first_sample_older_than_the_oldest_hour_still_starts_observation(self):
        # 167 hours 50 minutes old: still retained, older than the first prior-day hour shown.
        result = burn([sample("2026-09-23T15:30:00Z", 5), sample("2026-09-27T12:10:00Z", 50)], NOW)
        self.assertAlmostEqual(result["avg"][12], 50 / 6)
        self.assertEqual(result["baselineDays"], 6)

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

    def test_an_unobserved_hour_breaks_the_line(self):
        result = node("const c=require('./dashboard/static/charts.js');"
                      "console.log(JSON.stringify(c.runs([null,1,2,null,3,null,4,5],0,7)));")
        self.assertEqual([[point["value"] for point in run] for run in result], [[1, 2], [3], [4, 5]])

    def test_todays_line_reaches_the_now_marker_so_the_hour_after_midnight_is_drawn(self):
        result = node(r"""
const make=tag=>({tag,attrs:{},children:[],style:{},setAttribute(k,v){this.attrs[k]=v;},append(...c){this.children.push(...c);}});
global.document={createElementNS:(_,tag)=>make(tag),createElement:make};
const c=require('./dashboard/static/charts.js');
const burn={last24h:Array(24).fill(null),avg:Array(24).fill(null)};
burn.last24h[0]=500; burn.last24h[22]=9;
const svg=c.drawBurn(burn,{color:'#809cff',maximum:500,nowMs:Date.UTC(2026,8,30,0,20,0),label:'x'}).children[0];
const paths=svg.children.filter(n=>n.tag==='path').map(n=>({cls:n.attrs.class,d:n.attrs.d}));
const now=svg.children.find(n=>n.attrs.class==='burn-now').attrs.x1;
console.log(JSON.stringify({paths,now}));
""")
        # Hour 0 alone becomes a flat stub ending at the now marker; yesterday's lone 10p draws nothing.
        self.assertEqual([path["cls"] for path in result["paths"]], ["burn-line"])
        self.assertTrue(result["paths"][0]["d"].startswith("M 4 8 "))
        self.assertTrue(result["paths"][0]["d"].endswith(" %s 8" % round(float(result["now"]), 1)))


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
const usage={sampled_at:new Date().toISOString(),accounts:[],
  pace:{tokens_per_hour:3600,plan:'ChatGPT subscription',window:'7d',used_percent:40,resets_at:at(-10),delta_points:-11.6,
        sized_from:'bot_tokens',window_tokens:940,allowance_tokens:2350},
  samples:[{account:'a',bot:'ci-swe',timestamp:at(2),input_tokens:40,output_tokens:0},
           {account:'a',bot:'ci-qae',timestamp:at(3),input_tokens:900,output_tokens:0}]};
const formatTime=value=>'RESET';
const build=new Function('el','BOT_META','VVCharts','snapshot','formatTime',SOURCE+'\nreturn usageCharts;');
const charts=build(el,BOT_META,VVCharts,snapshot,formatTime);
const section=charts(usage);
const cards=section.children[2].children, note=section.children[3];
const head=section=>section.children[2].children[3].children[0].children[0];
const header=delta=>text(head(charts({...usage,pace:{...usage.pace,delta_points:delta}})));
const missing=charts({...usage,pace:{tokens_per_hour:null,reason:'the plan shows 0% used, so its size cannot be measured yet.'}});
console.log(JSON.stringify({drawn,names:cards.map(card=>card.children[0].children[0].children[0]),
  qae2:text(cards[2]),allClass:cards[3].attrs.class,note:text(note),noteTitle:note.attrs.title,
  missing:text(missing.children[3]),missingPace:drawn[drawn.length-1].pace,
  header:text(head(section)),headerTitle:head(section).attrs.title,missingHeader:text(head(missing)),
  ahead:header(21.2),even:header(0.6),swe:text(cards[0].children[0].children[0])}));
""".replace("SOURCE", json.dumps(source)))
        self.assertEqual(result["names"], ["SWE", "QAE", "QAE2", "All bots · 12% under pace"])
        self.assertIn("Not yet observed", result["qae2"])
        self.assertIn("all-bots", result["allClass"])
        bots, everyone = result["drawn"][:2], result["drawn"][2]
        self.assertEqual([row["maximum"] for row in bots], [900, 900])
        self.assertEqual([row["pace"] for row in bots], [None, None])
        self.assertEqual(everyone["pace"], 3600)
        self.assertEqual(everyone["maximum"], 3600)
        self.assertEqual(result["note"], "Flat line on All bots: 4k tokens an hour is the even pace of ChatGPT subscription "
                                         "(7d, 40% used), the same as the tick, through RESET.")
        self.assertIn("940 tokens since the window began made 40% of it, so it holds about 2k", result["noteTitle"])
        self.assertIn("keep their current share", result["noteTitle"])
        self.assertEqual(result["missing"], "No flat pace line: the plan shows 0% used, so its size cannot be measured yet.")
        self.assertIsNone(result["missingPace"])
        self.assertEqual(result["header"], "All bots · 12% under pace")
        self.assertEqual(result["headerTitle"], "12 points behind an even burn: headroom.")
        self.assertEqual(result["ahead"], "All bots · 21% ahead of pace")
        self.assertEqual(result["even"], "All bots · on pace")
        self.assertEqual(result["missingHeader"], "All bots")
        self.assertEqual(result["swe"], "SWE")

    def test_three_qae_cards_and_a_prior_day_usual_line(self):
        app = (ROOT / "dashboard/static/app.js").read_text()
        source = re.search(r"\n(  function usageCharts\([\s\S]*?)\n  function renderUsage\(", app).group(1)
        result = node(r"""
const make=tag=>({tag,attrs:{},children:[],style:{},setAttribute(k,v){this.attrs[k]=v;},append(...c){this.children.push(...c);}});
global.document={createElementNS:(_,tag)=>make(tag),createElement:make};
const VVCharts=require('./dashboard/static/charts.js');
const {BOT_META}=require('./dashboard/static/helpers.js');
function el(tag,attrs={},...children){
  const node={tag,attrs,children:children.flat().filter(value=>value!==null&&value!==undefined)};
  node.append=(...items)=>node.children.push(...items); return node;
}
const text=node=>typeof node==='string'?node:(node.children||[]).map(text).join(' ');
const walk=node=>!node||typeof node==='string'?[]:[node,...(node.children||[]).flatMap(walk)];
const snapshot={agents:{rows:[
  {id:'ci-qae-1',name:'QAE 1',role:'qae'},
  {id:'ci-qae-2',name:'QAE 2',role:'qae'},
  {id:'ci-qae-3',name:'QAE 3',role:'qae'}]}};
const at=hours=>new Date(Date.now()-hours*3600000).toISOString();
const samples=[];
for (const bot of ['ci-qae-1','ci-qae-2','ci-qae-3']) {
  for (const hours of [31,30,3,2]) samples.push({account:'a',bot,timestamp:at(hours),input_tokens:40,output_tokens:0});
}
const usage={sampled_at:new Date().toISOString(),accounts:[],
  pace:{tokens_per_hour:3600,plan:'ChatGPT subscription',window:'7d',used_percent:40,resets_at:at(-10),
    delta_points:-11.6,sized_from:'bot_tokens',window_tokens:900,allowance_tokens:2250},
  samples};
const build=new Function('el','BOT_META','VVCharts','snapshot','formatTime',SOURCE+'\nreturn usageCharts;');
const cards=build(el,BOT_META,VVCharts,snapshot,()=>'RESET')(usage).children[2].children;
const usual=card=>walk(card).some(node=>node.attrs&&node.attrs.class==='burn-usual');
console.log(JSON.stringify({
  names:cards.map(card=>text(card.children[0].children[0].children[0]).split(' · ')[0]),
  usual:cards.map(usual)
}));
""".replace("SOURCE", json.dumps(source)))
        self.assertEqual(result["names"], ["QAE 1", "QAE 2", "QAE 3", "All bots"])
        self.assertEqual(result["usual"], [True, True, True, True])

    def test_all_bots_counts_and_names_tokens_on_no_roster_row(self):
        app = (ROOT / "dashboard/static/app.js").read_text()
        source = re.search(r"\n(  function usageCharts\([\s\S]*?)\n  function renderUsage\(", app).group(1)
        result = node(r"""
const make=tag=>({tag,attrs:{},children:[],style:{},setAttribute(k,v){this.attrs[k]=v;},append(...c){this.children.push(...c);}});
global.document={createElementNS:(_,tag)=>make(tag),createElement:make};
const VVCharts=require('./dashboard/static/charts.js');
const {BOT_META}=require('./dashboard/static/helpers.js');
function el(tag,attrs={},...children){
  const node={tag,attrs,children:children.flat().filter(value=>value!==null&&value!==undefined)};
  node.append=(...items)=>node.children.push(...items); return node;
}
const text=node=>typeof node==='string'?node:(node.children||[]).map(text).join(' ');
const snapshot={agents:{rows:[
  {id:'ci-qae-1',name:'QAE 1',role:'qae'},
  {id:'ci-qae-2',name:'QAE 2',role:'qae'},
  {id:'ci-qae-3',name:'QAE 3',role:'qae'}]}};
const at=hours=>new Date(Date.now()-hours*3600000).toISOString();
const usage={available:true,sampled_at:at(0),samples:[
  {account:'a',bot:'ci-qae-1',timestamp:at(1),input_tokens:100,output_tokens:0},
  {account:'a',bot:'explorer',timestamp:at(1),input_tokens:40,output_tokens:7},
  {account:'a',bot:'explorer',timestamp:at(48),input_tokens:9000,output_tokens:0}],
  pace:{tokens_per_hour:null,reason:'unused',delta_points:null}};
const build=new Function('el','BOT_META','VVCharts','snapshot',SOURCE+'\nreturn usageCharts;');
const cards=build(el,BOT_META,VVCharts,snapshot)(usage).children[2].children;
const all=cards[3];
const totals=all.children.find(child=>child.attrs&&child.attrs.class==='burn-totals');
console.log(JSON.stringify({names:cards.map(card=>text(card.children[0].children[0].children[0]).split(' · ')[0]),
  all:text(all),title:totals.attrs.title}));
""".replace("SOURCE", json.dumps(source)))
        self.assertEqual(result["names"], ["QAE 1", "QAE 2", "QAE 3", "All bots"])
        self.assertIn("147", result["all"])
        self.assertIn("47 not on a numbered bot", result["all"])
        self.assertNotIn("9k", result["all"])
        self.assertIn("47 tokens are not on a numbered bot", result["title"])


if __name__ == "__main__":
    unittest.main()
