"""The subscription burn chart: a plan window's readings through time, against its even pace."""

import json
import os
import re
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HOUR = 3600000
# A week-long window that began 2026-09-30 12:00 UTC. at(h) is h hours into it.
PRELUDE = r"""
const c=require('./dashboard/static/charts.js');
const START=Date.UTC(2026,8,30,12,0,0), at=hours=>new Date(START+hours*3600000).toISOString();
const week=(history,extra)=>({window_minutes:10080,resets_at:at(168),used_percent:0,history,...extra});
const reading=(hours,used)=>({at:at(hours),used_percent:used});
"""


def node(source, tz="UTC"):
    result = subprocess.run(["node", "-e", PRELUDE + source], cwd=ROOT, capture_output=True, text=True,
                            env={**os.environ, "TZ": tz})
    if result.returncode:
        raise AssertionError(result.stdout + result.stderr)
    return json.loads(result.stdout)


class Readings(unittest.TestCase):
    def test_a_reading_sits_at_its_share_of_the_window_and_the_even_pace_is_the_windows_own(self):
        result = node("console.log(JSON.stringify(c.planBurn("
                      "week([reading(0.5,0),reading(1.25,2),reading(2,9)]),START+72*3600000)));")
        self.assertEqual(len(result["runs"]), 1)
        self.assertEqual([point["y"] for point in result["runs"][0]], [0, 2, 9])
        for point, hours in zip(result["runs"][0], (0.5, 1.25, 2)):
            self.assertAlmostEqual(point["x"], hours / 168)
        self.assertAlmostEqual(result["now"], 72 / 168)
        self.assertAlmostEqual(result["pointsPerHour"], 100 / 168)
        self.assertEqual(result["count"], 3)
        self.assertEqual(result["since"], 1790769600000 + HOUR // 2)

    def test_an_hour_with_no_reading_breaks_the_line(self):
        result = node("console.log(JSON.stringify(c.planBurn("
                      "week([0,1,3,4,6.1,7.9].map(hours=>reading(hours,hours))),START+9*3600000).runs.map(run=>run.map(point=>point.y))));")
        # Hours 2 and 5 were never read. The last two readings are nearly two hours apart but in
        # neighbouring clock hours, which is as close as two hourly readings are promised to be.
        self.assertEqual(result, [[0, 1], [3, 4], [6.1, 7.9]])

    def test_no_kept_history_is_no_chart_and_an_empty_one_is_a_chart_with_nothing_read_yet(self):
        result = node("const {history,...bare}=week([]);console.log(JSON.stringify(["
                      "c.planBurn(bare,START),c.planBurn(week([],{window_minutes:null}),START),"
                      "c.planBurn(week([],{resets_at:'soon'}),START),c.planBurn(week([]),START)]));")
        self.assertEqual(result[:3], [None, None, None])
        self.assertEqual([result[3][key] for key in ("runs", "count", "since")], [[], 0, None])

    def test_the_window_holds_what_falls_just_outside_it_to_its_edges(self):
        # The collector stamps a reading before it asks the provider, so the first reading of a
        # window can be a second older than the start the provider then reports.
        result = node("const burn=c.planBurn(week([{at:new Date(START-1000).toISOString(),used_percent:0}]),START+200*3600000);"
                      "console.log(JSON.stringify([burn.runs[0][0].x,burn.now]));")
        self.assertEqual(result, [0, 1])

    def test_totals_state_the_even_pace_and_how_much_history_there_is(self):
        result = node("console.log(JSON.stringify(["
                      "c.planTotals(c.planBurn(week([reading(0,0),reading(1,1),reading(2,1)]),START),'WHEN'),"
                      "c.planTotals(c.planBurn(week([reading(0,0)],{window_minutes:300,resets_at:at(5)}),START),'WHEN'),"
                      "c.planTotals(c.planBurn(week([reading(0,0)],{window_minutes:1440,resets_at:at(24)}),START),'WHEN')]));")
        self.assertEqual(result, ["Even pace 0.6 points an hour · 3 hourly readings since WHEN",
                                  "Even pace 20 points an hour · 1 hourly reading since WHEN",
                                  "Even pace 4.2 points an hour · 1 hourly reading since WHEN"])


class Ticks(unittest.TestCase):
    def test_a_week_is_marked_at_each_local_midnight_by_weekday(self):
        source = ("const SPAN=168*3600000,ticks=c.windowTicks(START,START+SPAN);"
                  "console.log(JSON.stringify({x:ticks.map(tick=>tick.x*168),labels:ticks.map(tick=>tick.label),"
                  "hours:ticks.map(tick=>new Date(START+tick.x*SPAN).getHours()),"
                  "names:ticks.map(tick=>new Date(START+tick.x*SPAN).toLocaleDateString([],{weekday:'short'}))}));")
        utc = node(source)
        self.assertEqual([round(x, 6) for x in utc["x"]], [12, 36, 60, 84, 108, 132, 156])
        self.assertEqual(utc["labels"], utc["names"])
        self.assertEqual(len(set(utc["labels"])), 7)
        # Midnight in India is 18:30 UTC, six and a half hours into this window.
        india = node(source, tz="Asia/Kolkata")
        self.assertEqual(round(india["x"][0], 6), 6.5)
        self.assertEqual(set(india["hours"]), {0})
        # 2026-11-01 has 25 hours in New York. Every mark is still a local midnight.
        fall = node("const BEGIN=Date.UTC(2026,9,29,15,0,0),SPAN=168*3600000,ticks=c.windowTicks(BEGIN,BEGIN+SPAN);"
                    "console.log(JSON.stringify(ticks.map(tick=>{const d=new Date(BEGIN+tick.x*SPAN);return d.getHours()*60+d.getMinutes();})));",
                    tz="America/New_York")
        self.assertEqual(fall, [0] * 7)

    def test_a_short_window_is_marked_by_the_hour_and_a_long_one_by_date_never_more_than_eight(self):
        result = node("const span=hours=>c.windowTicks(START+600000,START+600000+hours*3600000);"
                      "console.log(JSON.stringify({five:span(5).map(tick=>tick.label),day:span(24).map(tick=>tick.label),"
                      "month:span(720).length,monthLabel:span(720)[0].label,"
                      "expected:new Date(START+12*3600000).toLocaleDateString([],{month:'short',day:'numeric'})}));")
        self.assertEqual(result["five"], ["1p", "2p", "3p", "4p", "5p"])
        self.assertEqual(result["day"], ["1p", "4p", "7p", "10p", "1a", "4a", "7a", "10a"])
        self.assertEqual(result["month"], 8)
        self.assertEqual(result["monthLabel"], result["expected"])


DOM = r"""
const make=tag=>({tag,attrs:{},children:[],style:{setProperty(k,v){this[k]=v;}},setAttribute(k,v){this.attrs[k]=v;},append(...c){this.children.push(...c);}});
global.document={createElementNS:(_,tag)=>make(tag),createElement:make};
const walk=node=>!node||typeof node==='string'?[]:[node,...(node.children||[]).flatMap(walk)];
const text=node=>typeof node==='string'?node:node.textContent!==undefined?node.textContent:(node.children||[]).map(text).join(' ');
"""


class Drawing(unittest.TestCase):
    def test_the_even_pace_is_the_diagonal_and_a_lone_reading_is_a_dot(self):
        result = node(DOM + r"""
const burn=c.planBurn(week([reading(0,0),reading(1,10),reading(84,50)]),START+84*3600000);
const frame=c.drawPlan(burn,{color:'#fff',label:'Plan'});
const svg=frame.children[0], of=cls=>svg.children.filter(n=>n.attrs.class===cls);
const pace=of('burn-pace')[0];
console.log(JSON.stringify({pace:[pace.attrs.x1,pace.attrs.y1,pace.attrs.x2,pace.attrs.y2],title:pace.children[0].textContent,
  lines:of('burn-line').map(n=>n.attrs.d),dots:of('burn-line burn-dot').map(n=>n.attrs.d),now:of('burn-now')[0].attrs.x1,
  grid:of('burn-hourline').length,label:svg.attrs['aria-label'],
  ticks:frame.children.filter(n=>n.className==='burn-hour').length,
  levels:frame.children.filter(n=>n.className==='burn-level').map(n=>[n.textContent,n.style.top])}));
""")
        # The plot is 272 wide from x=4 and 102 tall from y=8: 0% at the start to 100% at the reset.
        self.assertEqual(result["pace"], ["4", "110", "276", "8"])
        self.assertEqual(result["title"], "Even pace: 0.6 points an hour, 100% over the window, the same as the tick.")
        self.assertEqual(result["lines"], ["M 4 110 L 5.6 99.8"])
        self.assertEqual(result["dots"], ["M 140 59 h 0"])
        self.assertEqual(result["now"], "140")
        self.assertEqual(result["grid"], 7 + 1)  # seven midnights and the half-used line
        self.assertEqual(result["label"], "Plan")
        self.assertEqual(result["ticks"], 7)
        self.assertEqual([level[0] for level in result["levels"]], ["100%", "50%"])
        self.assertEqual([round(float(level[1].rstrip("%")), 2) for level in result["levels"]], [6.35, 46.83])


CARDS = DOM + r"""
const VV=require('./dashboard/static/helpers.js');
const el=(tag,attrs={},...children)=>({tag,attrs,children:children.flat(Infinity).filter(value=>value!==null&&value!==undefined)});
const snapshot={agents:{rows:[]}};
const build=new Function('el','BOT_META','VVCharts','snapshot','paceHeader','quotaPace','quotaWindowLabel','quotaDisplayPercent','since','formatTime',
  SOURCE+'\nreturn planCharts;');
const planCharts=build(el,VV.BOT_META,c,snapshot,VV.paceHeader,VV.quotaPace,VV.quotaWindowLabel,VV.quotaDisplayPercent,VV.since,()=>'WHEN');
const NOW=START+84*3600000;
const account=(windows,label)=>({id:'collector:codex',label:label||'Codex',provider:'OpenAI',quota_windows:windows});
const fresh={stale:false,sampled_at:at(84)};
"""


class Cards(unittest.TestCase):
    def cards(self, source):
        app = (ROOT / "dashboard/static/app.js").read_text()
        chart_source = re.search(r"\n(  function usageCharts\([\s\S]*?)\n  function renderUsage\(", app).group(1)
        return node(CARDS.replace("SOURCE", json.dumps(chart_source)) + source)

    def test_each_window_with_a_history_gets_a_card_named_for_the_plan_and_its_pace(self):
        result = self.cards(r"""
const five={window_minutes:300,resets_at:at(86),used_percent:10,history:[reading(83,6),reading(84,10)]};
const panel=planCharts([account([five,week([reading(82,60),reading(83,61),reading(84,62)],{used_percent:62})])],fresh,NOW);
const cards=panel.children[2].children;
console.log(JSON.stringify({title:text(panel.children[0]),caption:text(panel.children[1]),grid:panel.children[2].attrs.class,
  heads:cards.map(card=>text(card.children[0].children[0])),hovers:cards.map(card=>card.children[0].children[0].attrs.title),
  read:cards.map(card=>text(card.children[0].children[1])),totals:cards.map(card=>text(card.children[2])),
  paths:cards.map(card=>walk(card.children[1]).filter(n=>n.attrs&&n.attrs.class==='burn-line').length),
  labels:cards.map(card=>card.children[1].children[0].attrs['aria-label'])}));
""")
        self.assertEqual(result["title"], "Subscription burn")
        self.assertIn("Dashed: the even pace, the same as the tick.", result["caption"])
        self.assertEqual(result["grid"], "burn-cards plan-cards")
        # Three of the five hours gone and 10% used; half the week gone and 62% used.
        self.assertEqual(result["heads"], ["Codex · 5 hours · 50% under pace", "Codex · 7 days · 12% ahead of pace"])
        self.assertEqual(result["hovers"][1], "12 points ahead of an even burn: spending the plan faster than its window resets.")
        self.assertEqual(result["read"], ["10% used", "62% used"])
        self.assertEqual(result["totals"], ["Even pace 20 points an hour · 2 hourly readings since WHEN",
                                            "Even pace 0.6 points an hour · 3 hourly readings since WHEN"])
        self.assertEqual(result["paths"], [1, 1])
        self.assertEqual(result["labels"][1], "Codex · 7 days: percent used across the window, against the even pace")

    def test_no_reading_kept_yet_says_so_and_an_old_reading_says_how_old(self):
        result = self.cards(r"""
const stale={stale:true,sampled_at:at(84-3.5)};
const empty=planCharts([account([week([],{used_percent:62})])],fresh,NOW).children[2].children[0];
const old=planCharts([account([week([reading(80,60)],{used_percent:60})])],stale,NOW).children[2].children[0];
console.log(JSON.stringify({empty:empty.children.map(text),emptyClass:empty.children[1].attrs.class,
  charts:walk(empty).filter(n=>n.tag==='svg').length,old:text(old.children[0].children[1])}));
""")
        self.assertEqual(result["empty"], ["Codex · 7 days · 12% ahead of pace 62% used",
                                           "No history yet. One reading an hour is kept from now on."])
        self.assertEqual(result["emptyClass"], "burn-empty muted")
        self.assertEqual(result["charts"], 0)
        self.assertEqual(result["old"], "60% used · read 3h 30m ago")

    def test_windows_no_collector_reads_have_no_panel(self):
        result = self.cards(r"""
const {history,...plain}=week([]);
console.log(JSON.stringify([planCharts([account([plain])],fresh,NOW),planCharts([],fresh,NOW),
  planCharts([account([plain]),account([week([reading(84,1)])],'Other')],fresh,NOW).children[2].children.length]));
""")
        self.assertEqual(result, [None, None, 1])


if __name__ == "__main__":
    unittest.main()
