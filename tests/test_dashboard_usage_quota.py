"""Subscription bars: length labels, pace tick, color, countdown, and one block per provider."""
import json
import os
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def node(source):
    result = subprocess.run(["node", "-e", source], cwd=ROOT, capture_output=True, text=True,
                            env={**os.environ, "TZ": "UTC"})
    if result.returncode:
        raise AssertionError(result.stdout + result.stderr)
    return json.loads(result.stdout)


class QuotaMath(unittest.TestCase):
    def test_windows_are_named_from_their_length(self):
        result = node(r"""
const q=require('./dashboard/static/helpers.js');
const label=minutes=>q.quotaWindowLabel({window_minutes:minutes,name:'codex · raw'});
console.log(JSON.stringify({
  five:label(300), week:label(10080), day:label(1440), hour:label(60),
  odd:label(90), named:q.quotaWindowLabel({name:'Weekly'})
}));
""")
        self.assertEqual(result, {"five": "5 hours", "week": "7 days", "day": "1 day", "hour": "1 hour",
                                  "odd": "90 min", "named": "Weekly"})

    def test_pace_tick_countdown_and_color_follow_an_even_burn(self):
        result = node(r"""
const q=require('./dashboard/static/helpers.js');
const now=Date.UTC(2026,9,6,12,0,0);
const weekly={used_percent:70, window_minutes:10080, resets_at:new Date(now+278208*1000).toISOString()};
const pace=q.quotaPace(weekly, now);
const fresh={used_percent:0, window_minutes:300, resets_at:new Date(now+300*60*1000).toISOString()};
const ahead={used_percent:30, window_minutes:300, resets_at:new Date(now+4*60*60*1000).toISOString()};
const full={used_percent:95, window_minutes:300, resets_at:new Date(now+4*60*60*1000).toISOString()};
console.log(JSON.stringify({
  expected:pace.expected, delta:pace.delta, phrase:q.quotaPacePhrase(pace),
  over:q.quotaDeltaLabel(pace.delta), under:q.quotaDeltaLabel(-12.4), even:q.quotaDeltaLabel(0.4),
  countdown:q.quotaCountdown(weekly.resets_at, now), fresh:q.quotaCountdown(fresh.resets_at, now),
  past:q.quotaCountdown(new Date(now-60000).toISOString(), now),
  freshPace:q.quotaPace(fresh, now),
  weekly:q.quotaTone(70, pace.delta), early:q.quotaTone(30, q.quotaPace(ahead, now).delta),
  hot:q.quotaTone(95, q.quotaPace(full, now).delta), room:q.quotaTone(20, -10)
}));
""")
        self.assertAlmostEqual(result["expected"], 54, places=5)
        self.assertAlmostEqual(result["delta"], 16, places=5)
        self.assertEqual(result["phrase"], "16 points ahead of an even 54% burn")
        self.assertEqual(result["over"], "+16%")
        self.assertEqual(result["under"], "-12%")
        self.assertIsNone(result["even"])
        self.assertEqual(result["countdown"], "resets 3d 5h")
        self.assertEqual(result["fresh"], "resets 5h")
        self.assertEqual(result["past"], "reset")
        self.assertIsNone(result["freshPace"])
        self.assertEqual(result["weekly"], "warn")
        self.assertEqual(result["early"], "warn")  # 30% used and 10 points ahead of a 5-hour window
        self.assertEqual(result["hot"], "hot")
        self.assertEqual(result["room"], "ok")

    def test_providers_group_their_models_and_a_missing_length_does_not_invent_pace(self):
        result = node(r"""
const q=require('./dashboard/static/helpers.js');
const groups=q.quotaGroups([
  {id:'a', label:'Codex', provider:'OpenAI'},
  {id:'b', label:'gpt-5.4', provider:'openai'},
  {id:'c', label:'Claude', provider:'anthropic'},
  {id:'d', label:'Local plan'}
]);
console.log(JSON.stringify({
  titles:groups.map(group=>group.title),
  sizes:groups.map(group=>group.accounts.length),
  none:q.quotaPace({used_percent:40, resets_at:'2026-10-10T00:00:00Z'}, Date.now())
}));
""")
        self.assertEqual(result["titles"], ["OpenAI usage", "Anthropic usage", "Local plan"])
        self.assertEqual(result["sizes"], [2, 1, 1])
        self.assertIsNone(result["none"])


class QuotaBars(unittest.TestCase):
    def test_bars_show_the_tick_the_overage_and_every_model_without_a_fallback_row(self):
        app = (ROOT / "dashboard/static/app.js").read_text()
        source = app[app.index("  function subscriptionPanel("):app.index("  function failurePanel(")]
        result = node(r"""
const q=require('./dashboard/static/helpers.js');
function el(tag, attrs={}, ...children) {
  const node={tag, attrs, children:children.flat().filter(value=>value!==null&&value!==undefined)};
  node.append=(...items)=>node.children.push(...items);
  node.appendChild=child=>{ node.children.push(child); return child; };
  return node;
}
const text=node=>typeof node==='string'?node:(node.children||[]).map(text).join('');
const build=new Function('el','formatTime','quotaWindowLabel','quotaDisplayPercent','quotaPace','quotaPacePhrase',
  'quotaDeltaLabel','quotaTone','quotaCountdown','quotaGroups', SOURCE+'\nreturn subscriptionPanel;');
const panel=build(el, ()=>'RESET', q.quotaWindowLabel, q.quotaDisplayPercent, q.quotaPace, q.quotaPacePhrase,
  q.quotaDeltaLabel, q.quotaTone, q.quotaCountdown, q.quotaGroups);
const now=Date.UTC(2026,9,6,12,0,0);
const weekly={name:'codex · 7d', used_percent:70, window_minutes:10080, resets_at:new Date(now+278208*1000).toISOString(), allowance_tokens:1000000};
const session={name:'codex · 5h', used_percent:0, window_minutes:300, resets_at:new Date(now+300*60*1000).toISOString(), allowance_tokens:null};
const behind={name:'week', used_percent:20, window_minutes:10080, resets_at:weekly.resets_at, allowance_tokens:null};
const section=panel([
  {id:'codex', label:'Codex', provider:'OpenAI', quota_windows:[weekly, session]},
  {id:'spark', label:'gpt-5.4', provider:'OpenAI', quota_windows:[behind]},
  {id:'claude', label:'Claude', provider:'anthropic', quota_windows:[{name:'Weekly', used_percent:95, resets_at:weekly.resets_at}]}
], now);
const blocks=section.children;
const row=line=>({label:text(line.children[0]), track:line.children[1], meta:text(line.children[2]), title:line.attrs.title});
const describe=line=>{
  const view=row(line);
  const fill=view.track.children.find(node=>node.attrs.class&&node.attrs.class.includes('quota-fill'));
  const tick=view.track.children.find(node=>node.attrs.class==='quota-tick');
  return {label:view.label, meta:view.meta, title:view.title, fill:fill?fill.attrs.class:null,
          width:fill?fill.attrs.style:null, tick:tick?tick.attrs.style:null};
};
const models=block=>{
  const rows=[]; let current=null;
  block.children.forEach(node=>{
    if(node.tag==='h3'){ current={name:text(node), lines:[]}; rows.push(current); }
    else if(node.attrs.class==='quota-lines'){
      if(!current){ current={name:null, lines:[]}; rows.push(current); }
      current.lines.push(...node.children.map(describe));
    }
  });
  return rows;
};
console.log(JSON.stringify({
  titles:blocks.map(block=>text(block.children[0])),
  openai:models(blocks[0]),
  claude:models(blocks[1]),
  text:text(section)
}));
""".replace("SOURCE", json.dumps(source)))
        self.assertEqual(result["titles"], ["OpenAI usage", "Anthropic usage"])
        codex, spark = result["openai"]
        self.assertEqual([codex["name"], spark["name"]], ["Codex", "gpt-5.4"])
        five, week = codex["lines"]
        self.assertEqual(five["label"], "5 hours")
        self.assertIsNone(five["fill"])
        self.assertIsNone(five["tick"])
        self.assertEqual(five["meta"], "0% · resets 5h")
        self.assertEqual(week["label"], "7 days")
        self.assertEqual(week["fill"], "quota-fill tone-warn")
        self.assertEqual(week["width"], "width:70%")
        self.assertTrue(week["tick"].startswith("left:54"))
        self.assertEqual(week["meta"], "70% · resets 3d 5h · +16%")
        self.assertIn("1,000,000 token allowance reported", week["title"])
        self.assertIn("16 points ahead", week["title"])
        behind = spark["lines"][0]
        self.assertEqual(behind["fill"], "quota-fill tone-ok")
        self.assertEqual(behind["width"], "width:20%")
        self.assertIn("-34%", behind["meta"])
        self.assertTrue(behind["tick"].startswith("left:54"))
        claude = result["claude"][0]
        self.assertIsNone(claude["name"])
        self.assertEqual(claude["lines"][0]["label"], "Weekly")
        self.assertEqual(claude["lines"][0]["fill"], "quota-fill tone-hot")
        self.assertIsNone(claude["lines"][0]["tick"])
        self.assertNotIn("Fallback", result["text"])
        self.assertNotIn("Primary", result["text"])


if __name__ == "__main__":
    unittest.main()
