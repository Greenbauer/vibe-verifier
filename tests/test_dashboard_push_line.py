"""The last push under the check meter, and the names in the Current work column."""
import json
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


class PushLine(unittest.TestCase):
    def node(self, source):
        result = subprocess.run(["node", "-e", source], cwd=ROOT, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return json.loads(result.stdout)

    def test_push_line_and_current_work_name_the_row(self):
        result = self.node(r'''
const fs=require('fs');
const helpers=require('./dashboard/static/helpers.js');
const app=fs.readFileSync('./dashboard/static/app.js','utf8');
const progressSource=app.slice(app.indexOf('  function messageIcon('), app.indexOf('  function renderPrRows('));
const rowSource=app.slice(app.indexOf('  function runningNames('), app.indexOf('  function renderPulls('));
function el(tag, attrs={}, ...children) {
  const node={tag, attrs, children:children.flat().filter(value => value !== null && value !== undefined)};
  node.append=(child)=>{ node.children.push(child); };
  return node;
}
function createElementNS(ns, tag) {
  const node={ns, tag, attrs:{}, children:[]};
  node.setAttribute=(key, value)=>{ node.attrs[key]=value; };
  node.append=(child)=>{ node.children.push(child); };
  return node;
}
global.document={createElementNS};
const progress=new Function('el','checkTotals','meterSegments','meterLabel','unresolvedMark','since','ageClass',
  progressSource+'return progress;')(el, helpers.checkTotals, helpers.meterSegments, helpers.meterLabel,
  helpers.unresolvedMark, helpers.since, helpers.ageClass);
const prRow=new Function('el','safeUrl','snapshot','combinedCategory','badge','progress','since','ageClass','runningWork',
  rowSource+'return prRow;')(el, helpers.safeUrl, {owner:'octocat'}, helpers.combinedCategory,
  status=>el('span',{class:'badge'}, status), progress, helpers.since, helpers.ageClass, helpers.runningWork);
const NOW=Date.parse('2026-10-06T00:00:00Z');
Date.now=()=>NOW;
const text=node=>{
  if (!node) return '';
  if (typeof node==='string') return node;
  return (node.children||[]).map(text).join('');
};
function byClass(node, cls) {
  const found=[];
  (function walk(item) {
    if (!item || typeof item==='string') return;
    if (String(item.attrs&&item.attrs.class||'').split(' ').includes(cls)) found.push(item);
    (item.children||[]).forEach(walk);
  })(node);
  return found;
}
function summarize(row) {
  const age=byClass(row,'pr-age')[0];
  const work=byClass(row,'pr-work')[0];
  const push=byClass(row,'push-line')[0];
  const layout=byClass(row,'progress-layout')[0];
  const caption=byClass(row,'progress-caption')[0];
  const time=(push.children||[]).find(child=>child&&child.tag==='span'&&child.attrs.class!=='push-kind');
  return {
    age:[text(age.children[0].children[0]), text(age.children[0].children[1]), age.children[0].children[0].attrs.class, age.children.length],
    counts:text(byClass(row,'progress-counts')[0]),
    lead:text(byClass(row,'progress-lead')[0]||''),
    pushText:text(push), pushTitle:push.attrs.title, pushWhen:time?text(time):null, pushClass:time?time.attrs.class:null,
    pushKind:text(byClass(push,'push-kind')[0]||''),
    markInCaption:byClass(caption,'comment-mark').length,
    markBesideMeter:String(layout.attrs.class).split(' ').includes('has-mark') && byClass(layout,'check-meter').length===1,
    meterInLayout:byClass(layout,'check-meter').length,
    workTitle:work.attrs.title,
    workLines:work.children.filter(child=>child.tag==='small').map(text),
    lineTitles:work.children.filter(child=>child.attrs&&String(child.attrs.class||'').split(' ').includes('work-line')).map(child=>child.attrs.title),
    reason:text(byClass(work,'sr-only')[0]||''),
    checks:text(byClass(row,'progress-copy')[0])
  };
}
const base={repository:'octocat/example', number:7, title:'Fix the gate', author:'octocat',
  head_sha:'abcdef1234567890', html_url:'https://github.com/octocat/example/pull/7',
  created_at:'2026-10-01T00:00:00Z', checks:[], statuses:[], expected:[], runs:[]};
const step=(id, name, stepName, at)=>({
  check:{id, name, status:'in_progress', category:'pending', started_at:at},
  job:{id, steps:[{name:stepName, status:'in_progress', started_at:at}]}
});
const older=step(1,'sync-preview-branch','Redeploy the preview with a very long step name', '2026-10-06T00:00:10Z');
const newest=step(2,'auth-probe','Resolve the preview under test with another long step name', '2026-10-06T00:02:10Z');
const middle=step(3,'package-audit','Read the lockfile and report outdated packages', '2026-10-06T00:01:10Z');
const rows={
  mixed:summarize(prRow({...base, attention_reason:'Current-head work is still running',
    unresolved_comments:1, review_threads:1, comments_complete:true, comment_count:1,
    push:{pushed_at:'2026-09-15T00:00:00Z', kind:'sync with a very long base branch name'},
    checks:[
      {name:'unit', category:'success', status:'completed'},
      {name:'lint', category:'pending', status:'queued'},
      {name:'review', category:'pending', status:'queued'},
      {name:'e2e', category:'skipped', status:'completed'},
      {name:'docs', category:'skipped', status:'completed'}
    ]})),
  passed:summarize(prRow({...base, attention_reason:'No current-head failure observed',
    push:{pushed_at:'2026-10-05T00:00:00Z', kind:'bug fix'},
    checks:[{name:'unit', category:'success', status:'completed'}]})),
  none:summarize(prRow({...base, attention_reason:'Current-head evidence is unavailable', push:null})),
  running:summarize(prRow({...base, attention_reason:'Current-head work is still running',
    push:{pushed_at:'2026-10-04T00:00:00Z', kind:'feature'},
    checks:[older.check, newest.check, middle.check], runs:[{jobs:[older.job, newest.job, middle.job]}]})),
  failed:summarize(prRow({...base, attention_reason:'A current-head check failed',
    push:{pushed_at:'2026-09-15T00:00:00Z', kind:'bug fix'},
    checks:[
      {name:'lint', category:'failed', status:'completed'},
      {name:'unit', category:'failed', status:'completed'},
      {name:'e2e', category:'failed', status:'completed'},
      {name:'docs', category:'success', status:'completed'}
    ]})),
  required:summarize(prRow({...base, attention_reason:'2 required checks not run on this head',
    push:{pushed_at:'2026-10-05T00:00:00Z', kind:'chore'},
    checks:[{name:'unit', category:'success', status:'completed'}],
    expected:[{name:'qae-verify', category:'pending', status:'expected'},
      {name:'preview', category:'pending', status:'expected'}]})),
  queued:summarize(prRow({...base, attention_reason:'Current-head work is still running',
    checks:[{name:'lint', category:'pending', status:'queued'}]})),
  fallback:summarize(prRow({...base, attention_reason:'A current-head check was cancelled',
    checks:[{name:'unit', category:'cancelled', status:'completed'}]}))
};
console.log(JSON.stringify(rows));
''')
        self.assertEqual(result["mixed"]["age"], ["5d", "PR age", "age-yellow", 1])
        self.assertEqual(result["mixed"]["counts"], "2 waiting · 2 skipped")
        self.assertEqual(result["mixed"]["pushWhen"], "3w")
        self.assertEqual(result["mixed"]["pushClass"], "age-red")
        self.assertEqual(result["mixed"]["pushKind"], "· sync with a very long base branch name")
        self.assertEqual(result["mixed"]["pushTitle"], "3w · sync with a very long base branch name")
        self.assertEqual(result["mixed"]["markInCaption"], 0)
        self.assertTrue(result["mixed"]["markBesideMeter"])
        self.assertEqual(result["mixed"]["workLines"], ["2 checks queued"])
        self.assertEqual(result["passed"]["counts"], "All passed")
        self.assertEqual(result["passed"]["pushWhen"], "1d")
        self.assertIsNone(result["passed"]["pushClass"])
        self.assertEqual(result["passed"]["pushKind"], "· bug fix")
        self.assertEqual(result["passed"]["workLines"], ["No current-head failure observed"])
        self.assertEqual(result["passed"]["reason"], "")
        self.assertEqual(result["none"]["lead"], "No checks reported")
        self.assertEqual(result["none"]["counts"], "Progress is not shown as complete")
        self.assertEqual(result["none"]["pushText"], "Last push unavailable")
        self.assertEqual(result["none"]["meterInLayout"], 0)
        self.assertEqual(result["none"]["workLines"], ["Current-head evidence is unavailable"])
        self.assertNotIn("auth-probe", result["running"]["checks"])
        self.assertNotIn("Redeploy", result["running"]["checks"])
        self.assertEqual(result["running"]["workLines"], [
            "auth-probe: Resolve the preview under test with another long step name",
            "package-audit: Read the lockfile and report outdated packages",
            "+1 more"])
        self.assertEqual(result["running"]["lineTitles"], result["running"]["workLines"][:2])
        self.assertEqual(result["running"]["reason"], "Current-head work is still running")
        self.assertEqual(result["running"]["workTitle"], "Current-head work is still running")
        self.assertEqual(result["failed"]["workLines"], ["lint", "unit", "+1 more"])
        self.assertEqual(result["failed"]["reason"], "A current-head check failed")
        self.assertNotIn("e2e", result["failed"]["workLines"])
        self.assertEqual(result["required"]["workLines"], ["qae-verify", "preview"])
        self.assertEqual(result["required"]["reason"], "2 required checks not run on this head")
        self.assertEqual(result["queued"]["workLines"], ["1 check queued"])
        self.assertEqual(result["fallback"]["workLines"], ["A current-head check was cancelled"])
        self.assertEqual(result["fallback"]["reason"], "")
        css = (ROOT / "dashboard/static/styles.css").read_text()
        app = (ROOT / "dashboard/static/app.js").read_text()
        self.assertIn(".progress-layout > .progress-caption { grid-column: 1;", css)
        self.assertIn(".progress-layout > .comment-mark { grid-column: 2;", css)
        self.assertIn(".push-kind { min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }", css)
        self.assertNotIn("running-steps", css + app)

if __name__ == "__main__":
    unittest.main()
