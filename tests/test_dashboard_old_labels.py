"""The page never changes how it looks to say a reading is old: no age, no gray, no "stale" or
"refreshing" flag. The server keeps every fetch current instead. A real read failure still says so."""

import json
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# The page's own load() while every request fails, on a virtual clock: ten minutes of the 30-second
# poll and whatever timers load() itself arms. Prints how many requests it made.
DOWNTIME = r"""
const app=require('fs').readFileSync('./dashboard/static/app.js','utf8');
const load=app.match(/\n  async function load\(\) \{[\s\S]*?\n  \}\n/)[0], visible=app.match(/\n  function refreshIfVisible\(\) \{.*\}\n/)[0];
let clock=0, requests=0, timers=[], ids=0; const node={replaceChildren(){}};
const window={setTimeout:(run,delay)=>{timers.push({id:++ids,at:clock+delay,run});return ids;},clearTimeout:id=>{timers=timers.filter(timer=>timer.id!==id);}};
const fetch=()=>{requests+=1;return Promise.reject(new Error('down'));};
const page=new Function('fetch','window','document','content','empty','el','render','restoreView',
 'let snapshot=null, loading=false, retry=0, refreshFailed=false;'+load+visible+'return {load, refreshIfVisible};')(
 fetch,window,{hidden:HIDDEN,activeElement:null,querySelector:()=>node,getElementById:()=>null},node,()=>node,()=>node,()=>{},()=>{});
const settle=()=>new Promise(done=>setImmediate(done));
(async()=>{
 page.load(); await settle();
 for(clock=1000;clock<=600000;clock+=1000){
  const due=timers.filter(timer=>timer.at<=clock); timers=timers.filter(timer=>timer.at>clock);
  due.forEach(timer=>timer.run());
  if(clock%30000===0) page.refreshIfVisible();
  await settle();
 }
 console.log(JSON.stringify(requests));
})();
"""


class NoOldDataSignals(unittest.TestCase):
    def node(self, source):
        result = subprocess.run(["node", "-e", source], cwd=ROOT, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return json.loads(result.stdout)

    def test_a_bot_state_is_shown_as_read_whatever_its_age(self):
        result = self.node(r"""
const h=require('./dashboard/static/helpers.js'), at='2026-10-08T15:00:00Z';
console.log(JSON.stringify([h.botState({state:'working',sampled_at:at}),h.botState({state:'idle',stale:true,sampled_at:at}),
 h.botState({state:'paused',stale:true}),h.botState({stale:true,sampled_at:at})]));
""")
        self.assertEqual(result, [{"status": "working", "label": "Working"}, {"status": "idle", "label": "Idle"},
                                  {"status": "paused", "label": "Paused"}, {"status": "unknown", "label": "Unknown"}])

    def test_the_header_and_the_banner_carry_no_age_and_a_read_failure_still_shows(self):
        result = self.node(r"""
const h=require('./dashboard/static/helpers.js'), at='2026-10-08T15:00:00Z';
const time=h.formatTime(at), coverage={label:'Selected repositories',selected:2,readable:2};
console.log(JSON.stringify({time,
 stamps:[h.sourceStamp({sampled_at:at}),h.sourceStamp({sampled_at:at,stale:true,refreshing:true,restored:true}),
  h.sourceStamp({refreshing:true}),h.sourceStamp({})],
 banners:[h.coverageBanner(coverage,{}),h.coverageBanner(coverage,{stale:true,restored:true}),h.coverageBanner(coverage,{errors:[{code:'unavailable'}]})]}));
""")
        time = result["time"]
        self.assertEqual(result["stamps"], ["GitHub sampled %s" % time, "GitHub sampled %s" % time,
                                            "Fetching GitHub data…", "GitHub unavailable"])
        counted = "Selected repositories: 2. 2 read successfully in this sample."
        self.assertEqual(result["banners"], [counted, counted, counted + " Some GitHub data is unavailable."])

    def test_the_bot_strip_draws_an_old_state_like_a_new_one_and_the_header_uses_the_stamp(self):
        result = self.node(r"""
const app=require('fs').readFileSync('./dashboard/static/app.js','utf8'), h=require('./dashboard/static/helpers.js');
const make=tag=>({tag,attrs:{},children:[],style:{setProperty(){}},append(...c){this.children.push(...c)},replaceChildren(...c){this.children=c}});
const el=(tag,attrs={},...c)=>Object.assign(make(tag),{attrs,children:c.flat().filter(v=>v!=null)});
const at=new Date(Date.now()-250000).toISOString(), strip=el('div');
const rows=[{id:'swe',name:'SWE',role:'swe',state:'idle',stale:true,sampled_at:at,recent_2h:[]},{id:'qae',name:'QAE',role:'qae',state:'working',recent_2h:[]}];
new Function('el','BOT_META','snapshot','state','badge','go','formatTime','document','recentRunLabel','botState',app.match(/  function renderBots\(\) \{([\s\S]*?)\n  \}\n\n  function coverage/)[1])(
 el,h.BOT_META,{agents:{rows}},{},(status,label)=>el('span',{class:'badge status-'+status},label),()=>{},v=>v,{querySelector:()=>strip},()=>'Last 2h',h.botState);
const pills=strip.children.filter(n=>n.tag==='button');
console.log(JSON.stringify({badges:pills.map(p=>p.children[1]).map(b=>[b.attrs.class,b.children[0]]),labels:pills.map(p=>p.attrs['aria-label']),
 stamp:app.includes('textContent = sourceStamp(snapshot.github);')}));
""")
        self.assertEqual(result["badges"], [["badge status-idle", "Idle"], ["badge status-working", "Working"]])
        self.assertEqual(result["labels"], ["SWE: Idle. Open usage and recent outcomes",
                                            "QAE: Working. Open usage and recent outcomes"])
        self.assertTrue(result["stamp"])

    def test_a_page_with_nothing_to_show_retries_every_five_seconds_in_one_chain(self):
        requests = self.node(DOWNTIME.replace("HIDDEN", "false"))
        # One retry every five seconds and the 30-second poll: 120 and 20 in ten minutes, never more.
        self.assertGreaterEqual(requests, 100)
        self.assertLessEqual(requests, 141)

    def test_a_hidden_page_does_not_retry(self):
        self.assertEqual(self.node(DOWNTIME.replace("HIDDEN", "true")), 1)


if __name__ == "__main__":
    unittest.main()
