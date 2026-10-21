"""Dependency-free browser helper behavior."""

import json
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


class BrowserHelpers(unittest.TestCase):
    def node(self, source):
        result = subprocess.run(["node", "-e", source], cwd=ROOT, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return json.loads(result.stdout)
    def test_filters_check_unknowns_and_urls_are_pure(self):
        source = r'''
const h=require('./dashboard/static/helpers.js');
const pulls=[{repository:'octocat/example',number:1,title:'Safe',author:'octocat',subscription:'subscribed',attention:true},
             {repository:'octocat/other',number:2,title:'Quiet',author:'octocat',subscription:'unknown',attention:false}];
const filtered=h.filterPulls(pulls,{query:'safe',repository:'all',subscribed:true,attention:true});
const unknown=h.checkTotals({runs:[{jobs:[{status:'queued',steps:[]}]}]});
console.log(JSON.stringify({filtered:filtered.map(x=>x.number),unknown,
 safe:h.safeUrl('https://github.com/octocat/example/pull/1','octocat'),
 unsafe:h.safeUrl('https://github.com/example/foreign/pull/1','octocat')}));
'''
        result = self.node(source)
        self.assertEqual(result["filtered"], [1])
        self.assertFalse(result["unknown"]["known"])
        self.assertEqual(result["unknown"]["total"], 0)
        self.assertTrue(result["safe"].startswith("https://github.com/octocat/"))
        self.assertIsNone(result["unsafe"])
    def test_pulls_group_by_repository_newest_activity_first_oldest_pull_last(self):
        result = self.node(r'''
const h=require('./dashboard/static/helpers.js');
const p=(repository,number,created_at,updated_at)=>({repository,number,created_at,updated_at});
const groups=h.groupPulls([
 p('o/quiet',1,'2026-10-01T00:00:00Z','2026-10-01T00:00:00Z'),
 p('o/busy',2,'2026-09-01T00:00:00Z','2026-10-05T00:00:00Z'),
 p('o/busy',3,'2026-10-02T00:00:00Z','2026-10-02T00:00:00Z'),
 p('o/busy',4,'2026-09-15T00:00:00Z','2026-09-15T00:00:00Z'),
 p('o/blank',5,null,null)]);
console.log(JSON.stringify(groups.map(g=>[g.repository,g.pulls.map(x=>x.number)])));
''')
        self.assertEqual(result, [["o/busy", [3, 4, 2]], ["o/quiet", [1]], ["o/blank", [5]]])
        self.assertEqual(self.node("console.log(JSON.stringify(require('./dashboard/static/helpers.js').groupPulls([])))"), [])
    def test_pr_badge_is_passed_when_the_only_other_checks_were_skipped(self):
        result = self.node(r'''
const h=require('./dashboard/static/helpers.js');
const pr=(...c)=>({checks:c.map(category=>({category}))});
console.log(JSON.stringify([h.combinedCategory(pr('success','skipped')),h.combinedCategory(pr('skipped','skipped')),
 h.combinedCategory(pr('success','skipped','failed')),h.combinedCategory(pr('skipped','pending'))]));
''')
        self.assertEqual(result, ["success", "skipped", "failed", "pending"])
    def test_check_meter_segments_keep_each_outcome_share(self):
        result = self.node(r'''
const h=require('./dashboard/static/helpers.js');
const counts=(extra)=>({success:0,running:0,waiting:0,failed:0,skipped:0,cancelled:0,unknown:0,...extra});
const totals=(extra, fields)=>({known:true,completed:8,total:10,remaining:2,counts:counts(extra),...fields});
const mixed=h.meterSegments(totals({success:6,running:1,waiting:1,failed:1,skipped:1}));
console.log(JSON.stringify({
 mixed:mixed.map(segment=>[segment.state,segment.count]),
 failedStaysASlice:h.meterSegments(totals({success:60,failed:4})).map(segment=>segment.state),
 gray:h.meterSegments(totals({skipped:2,cancelled:1,unknown:1})).map(segment=>segment.state),
 empty:h.meterSegments(totals({})),
 unknown:h.meterSegments(h.checkTotals({})),
 label:h.meterLabel(totals({success:6,running:1,waiting:1,failed:1,skipped:1})),
 none:h.meterLabel(h.checkTotals({}))
}));
''')
        self.assertEqual(result["mixed"], [["success", 6], ["running", 1], ["waiting", 1], ["failed", 1], ["skipped", 1]])
        self.assertEqual(result["failedStaysASlice"], ["success", "failed"])
        self.assertEqual(result["gray"], ["skipped", "cancelled", "unknown"])
        self.assertEqual(result["empty"], [])
        self.assertEqual(result["unknown"], [])
        self.assertEqual(result["label"], "6 succeeded, 1 running, 1 waiting, 1 failed, 1 skipped of 10 checks")
        self.assertEqual(result["none"], "No checks reported")
    def test_check_totals_take_one_share_per_check_status_and_expected_row(self):
        result = self.node(r'''
const h=require('./dashboard/static/helpers.js');
const check=(category,status)=>({name:category,category,status});
const pull={checks:[check('success','completed'),check('success','completed'),check('pending','in_progress'),
  check('pending','queued'),check('failed','completed'),check('skipped','completed'),check('cancelled','completed'),
  check('odd','completed')],
 statuses:[{name:'deploy',category:'pending',status:'pending'},{name:'legacy',category:'success',status:'success'}],
 expected:[{name:'Acceptance criteria',category:'pending',status:'expected'}]};
console.log(JSON.stringify(h.checkTotals(pull)));
''')
        self.assertEqual(result, {"known": True, "completed": 6, "total": 11, "remaining": 5,
                                  "counts": {"success": 3, "running": 1, "waiting": 3, "failed": 1,
                                             "skipped": 1, "cancelled": 1, "unknown": 1}})
    def test_a_queued_job_with_no_steps_still_shows_the_check_line(self):
        result = self.node(r'''
const h=require('./dashboard/static/helpers.js');
const pull={runs:[{jobs:[{id:1,name:'build',status:'completed',steps:[{status:'completed'}]},
                         {id:2,name:'review',status:'queued',steps:[]}]},
                  {status:'completed',jobs:[]}],
 checks:[{id:1,name:'build',category:'success',status:'completed'},{id:2,name:'review',category:'pending',status:'queued'}]};
const totals=h.checkTotals(pull);
console.log(JSON.stringify([totals.known,totals.completed,totals.total,totals.counts.waiting,
 h.meterSegments(totals).map(segment=>segment.state)]));
''')
        self.assertEqual(result, [True, 1, 2, 1, ["success", "waiting"]])
    def test_an_expected_required_check_keeps_the_pr_pending_and_takes_one_share(self):
        result = self.node(r'''
const h=require('./dashboard/static/helpers.js');
const pr={checks:[{category:'success'}],expected:[{name:'Acceptance criteria',category:'pending',status:'expected'}]};
const none=h.checkTotals({runs:[],expected:[],checks:[]});
const totals=h.checkTotals(pr);
console.log(JSON.stringify([h.combinedCategory(pr),totals.known,totals.completed,totals.total,totals.remaining,totals.counts.waiting,none.known]));
''')
        self.assertEqual(result, ["pending", True, 1, 2, 1, 1, False])
    def test_running_work_names_each_check_in_progress_with_its_current_step(self):
        result = self.node(r'''
const h=require('./dashboard/static/helpers.js');
const pull={runs:[{jobs:[
  {id:1,name:'test',status:'in_progress',steps:[{name:'Set up',status:'completed'},
    {name:'Run unit tests',status:'in_progress',elapsed_seconds:95},{name:'Post',status:'pending'}]},
  {id:2,name:'lint',status:'queued',steps:[]},
  {id:3,name:'build',status:'in_progress',steps:[]}]}],
 checks:[{id:1,name:'test',status:'in_progress',elapsed_seconds:300},{id:2,name:'lint',status:'queued'},
  {id:3,name:'build',status:'in_progress',elapsed_seconds:12},{id:9,name:'Vercel',status:'in_progress',elapsed_seconds:40},
  {id:4,name:'done',status:'completed'}]};
console.log(JSON.stringify([h.runningWork(pull),h.runningWork({})]));
''')
        self.assertEqual(result, [[{"name": "test: Run unit tests", "elapsed": 95}, {"name": "build", "elapsed": 12},
                                   {"name": "Vercel", "elapsed": 40}], []])
    def test_disk_meter_reports_used_space_not_free_space(self):
        result = self.node(r'''
const h=require('./dashboard/static/helpers.js');
console.log(JSON.stringify([h.diskUsage({workspace_disk_free_bytes:25,workspace_disk_total_bytes:100}),
 h.diskUsage({workspace_disk_free_bytes:null,workspace_disk_total_bytes:100}),
 h.diskUsage({workspace_disk_free_bytes:5,workspace_disk_total_bytes:0})]));
''')
        self.assertEqual(result[0], {"used": 75, "total": 100, "percent": 75})
        self.assertEqual(result[1:], [None, None])
    def test_failure_panel_distinguishes_unavailable_empty_and_failed_history(self):
        result = self.node(r"""
const fs=require('fs');
const {BOT_META}=require('./dashboard/static/helpers.js');
const app=fs.readFileSync('./dashboard/static/app.js','utf8');
const match=app.match(/  function failurePanel\(\) \{([\s\S]*?)\n  \}\n\n  function usageCharts/);
if (!match) throw new Error('failurePanel not found');

function el(tag, attrs={}, ...children) {
  const node={tag, attrs, children:children.flat().filter(value => value !== null && value !== undefined)};
  node.append=(...items) => node.children.push(...items.flat().filter(value => value !== null && value !== undefined));
  return node;
}
const badge=status => el('span', {class:`badge status-${status}`}, status);
const link=(label, url) => url ? el('a', {href:url}, label) : null;
const failurePanel=new Function('el','BOT_META','snapshot','state','render','badge','link','formatTime','duration',match[1]);
const run={category:'failed',name:'Verify dashboard',repository:'octocat/example',
  completed_at:'2026-09-30T15:00:00Z',elapsed_seconds:42,
  html_url:'https://github.com/octocat/example/actions/runs/1/job/2'};

function render(roles) {
  const snapshot={owner:'octocat',agents:{rows:Object.entries(roles).map(([id,value])=>({id,name:'QAE1',role:'qae',coverage:{history:'complete'},...value}))}};
  const state={failureBot:'qae-1'};
  return failurePanel(el,BOT_META,snapshot,state,()=>{},badge,link,value=>value,value=>`${value}s`);
}
console.log(JSON.stringify({unavailable:render({}),empty:render({'qae-1':{recent_7d:[]}}),
  failed:render({'qae-1':{recent_7d:[run]}}),
  pressed:render({'swe-1':{name:'SWE1',role:'swe',recent_7d:[]},'qae-1':{recent_7d:[]}})}));
""")
        def text(node):
            if isinstance(node, str):
                return node
            return " ".join(text(child) for child in node.get("children", []))
        def nodes(node):
            if isinstance(node, str):
                return []
            return [node] + [descendant for child in node.get("children", [])
                             for descendant in nodes(child)]
        self.assertIn("Bot history is unavailable.", text(result["unavailable"]))
        self.assertNotIn("No failed completed run", text(result["unavailable"]))
        self.assertIn("No failed completed run in the available seven-day history.",
                      text(result["empty"]))
        self.assertNotIn("Bot history is unavailable.", text(result["empty"]))
        self.assertIn("Verify dashboard", text(result["failed"]))
        self.assertIn("octocat/example", text(result["failed"]))
        links = [node for node in nodes(result["failed"]) if node["tag"] == "a"]
        self.assertEqual([(text(node), node["attrs"]["href"]) for node in links], [
            ("Open original job", "https://github.com/octocat/example/actions/runs/1/job/2")])
        # aria-pressed is the string "true" or "false" on every bot button: the selected-button
        # CSS matches [aria-pressed="true"], and screen readers need both states to read a toggle.
        buttons = [node for node in nodes(result["pressed"]) if node["tag"] == "button"]
        self.assertEqual([(text(node).strip(), node["attrs"]["aria-pressed"]) for node in buttons],
                         [("SWE1", "false"), ("QAE1", "true")])
    def test_the_capacity_views_node_tests_pass(self):
        # capacity.test.js renders capacity.js: runner rows, folded free runners, bars, range buttons, text-only values.
        result = subprocess.run(["node", "--test", "--test-reporter=tap", str(ROOT / "dashboard/static/capacity.test.js")],
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertRegex(result.stdout, r"(?m)^# pass [1-9]")
        self.assertRegex(result.stdout, r"(?m)^# fail 0")
        index = (ROOT / "dashboard/static/index.html").read_text()
        self.assertIn('<script src="/assets/capacity.js"></script>', index)
        self.assertIn('<script src="/assets/timeline.js"></script>', index)
        self.assertIn('<script src="/assets/prs.js"></script>', index)
    def test_ui_uses_text_nodes_and_the_approved_local_palette(self):
        app = (ROOT / "dashboard/static/app.js").read_text()
        helpers = (ROOT / "dashboard/static/helpers.js").read_text()
        css = (ROOT / "dashboard/static/styles.css").read_text()
        html = (ROOT / "dashboard/static/index.html").read_text()
        self.assertNotIn("innerHTML", app + helpers + (ROOT / "dashboard/static/capacity.js").read_text()
                         + (ROOT / "dashboard/static/timeline.js").read_text() + (ROOT / "dashboard/static/prs.js").read_text())
        for color in ("#090909", "#111111", "#282828"):
            self.assertIn(color, css)
        for color in ("#809cff", "#50d6e8"):
            self.assertIn(color, helpers)
        self.assertEqual(html.count('data-view="'), 4)
        self.assertNotIn("footer", html.lower())
    def test_workspace_nav_stays_put_while_the_view_scrolls(self):
        css = (ROOT / "dashboard/static/styles.css").read_text()
        app = (ROOT / "dashboard/static/app.js").read_text()
        held = ("overflow: hidden; background: var(--bg)", "flex-direction: column; height: 100%",
                "flex: 1; min-height: 0; overflow: hidden", "min-height: 0; overflow: auto",
                "grid-template-rows: auto minmax(0, 1fr)")
        self.assertTrue(all(part in css for part in held))
        self.assertIn("content.scrollTo?.(0, 0)", app)
        self.assertFalse("min-height: auto" in css or "top: 102px" in css)
    def test_usage_page_lists_each_qae_and_draws_a_prior_day_line(self):
        result = self.node(r"""
const app=require('fs').readFileSync('./dashboard/static/app.js','utf8');
const {BOT_META,botState,paceHeader}=require('./dashboard/static/helpers.js'),VVCharts=require('./dashboard/static/charts.js');
const make=tag=>({tag,attrs:{},children:[],style:{setProperty(k,v){this[k]=v}},setAttribute(k,v){this.attrs[k]=v},append(...c){this.children.push(...c)},replaceChildren(...c){this.children=c}});
const el=(tag,attrs={},...c)=>Object.assign(make(tag),{attrs,children:c.flat().filter(v=>v!=null)});
const text=n=>typeof n==='string'?n:(n.children||[]).map(text).join(' '),walk=n=>!n||typeof n==='string'?[]:[n,...(n.children||[]).flatMap(walk)];
const rows=[1,2,3].map(n=>({id:'ci-qae-'+n,name:'QAE '+n,role:'qae',state:'idle',recent_2h:[],coverage:{history:'complete'}}));
const snapshot={agents:{rows}},strip=el('div'),at=h=>new Date(Date.now()-h*3600000).toISOString();
const recent=(value,runs)=>runs[0]?.category==='failed'?'Latest run failed':runs.length?'Last 2h':value.coverage?.history==='complete'?'No runs in 2h':'History incomplete';
global.document={createElementNS:(_,tag)=>make(tag),createElement:make};
new Function('el','BOT_META','snapshot','state','badge','go','formatTime','document','recentRunLabel','botState',app.match(/  function renderBots\(\) \{([\s\S]*?)\n  \}\n\n  function subscriptionPanel/)[1])(el,BOT_META,snapshot,{failureBot:null},s=>el('span',{},s),()=>{},v=>v,{querySelector:()=>strip},recent,botState);
global.document={createElementNS:(_,tag)=>make(tag),createElement:make};
const samples=rows.flatMap(row=>[31,30,3,2].map(h=>({account:'a',bot:row.id,timestamp:at(h),input_tokens:40,output_tokens:0})));
const usage={sampled_at:at(0),accounts:[],samples,pace_points:-4};
const src=app.match(/\n(  function usageCharts\([\s\S]*?)\n  function renderUsage\(/)[1];
const cards=new Function('el','BOT_META','VVCharts','snapshot','paceHeader',src+'\nreturn usageCharts;')(el,BOT_META,VVCharts,snapshot,paceHeader)(usage).children[2].children;
const names=n=>walk(n).filter(i=>i.attrs&&i.attrs.class==='bot-name').map(text);
console.log(JSON.stringify({strip:names(strip),cards:cards.map(c=>text(c.children[0].children[0].children[0]).split(' · ')[0]),usual:cards.map(c=>walk(c).some(n=>n.attrs&&n.attrs.class==='burn-usual'))}));
""")
        self.assertEqual([result["strip"], result["cards"], result["usual"]], [["QAE 1", "QAE 2", "QAE 3"], ["QAE 1", "QAE 2", "QAE 3", "All bots"], [True, True, True, True]])

if __name__ == "__main__":
    unittest.main()
