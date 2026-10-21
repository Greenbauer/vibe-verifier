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
global.VV=helpers; const {ciTiming}=require('./dashboard/static/timeline.js');
const app=fs.readFileSync('./dashboard/static/prs.js','utf8');
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
  'ageLabel','secondsClass','ciTiming', progressSource+'return progress;')(el, helpers.checkTotals, helpers.meterSegments,
  helpers.meterLabel, helpers.unresolvedMark, helpers.since, helpers.ageClass, helpers.ageLabel, helpers.secondsClass, ciTiming);
const prRow=new Function('el','safeUrl','snapshot','combinedCategory','badge','progress','since','ageClass','runningWork',
  'behindMark', rowSource+'return prRow;')(el, helpers.safeUrl, {owner:'octocat'}, helpers.combinedCategory,
  status=>el('span',{class:'badge'}, status), progress, helpers.since, helpers.ageClass, helpers.runningWork, helpers.behindMark);
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
    checks:text(byClass(row,'progress-copy')[0]),
    behind:(byClass(row,'behind-mark')[0]||null)&&[text(byClass(row,'behind-mark')[0]), byClass(row,'behind-mark')[0].attrs.title],
    idle:(byClass(row,'idle-line')[0]||null)&&(line=>[text(line), line.children[1].attrs.class, line.children[0].attrs.class,
      line.attrs.title, byClass(caption,'idle-line').length])(byClass(row,'idle-line')[0])
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
// CI ran on the head seven hours ago, then again on the same commit ten minutes ago: a re-run.
const firstRound={check:{id:11, name:'unit', category:'success', status:'completed', started_at:'2026-10-05T17:00:00Z',
  completed_at:'2026-10-05T17:20:00Z'}, job:{id:11, created_at:'2026-10-05T16:59:30Z', started_at:'2026-10-05T17:00:00Z',
  completed_at:'2026-10-05T17:20:00Z', steps:[]}};
const rerun=(check, job)=>({...base, push:{pushed_at:'2026-10-05T16:59:00Z', kind:'feature'}, checks:[firstRound.check, {id:12, name:'unit', ...check}],
  runs:[{id:1, name:'CI', jobs:[firstRound.job, {id:12, steps:[], ...job}]}]});
const rows={
  rerunRunning:summarize(prRow(rerun({category:'pending', status:'in_progress', started_at:'2026-10-05T23:50:00Z'},
    {created_at:'2026-10-05T23:49:30Z', started_at:'2026-10-05T23:50:00Z'}))),
  rerunDone:summarize(prRow(rerun({category:'success', status:'completed', started_at:'2026-10-05T23:50:00Z', completed_at:'2026-10-05T23:58:00Z'},
    {created_at:'2026-10-05T23:49:30Z', started_at:'2026-10-05T23:50:00Z', completed_at:'2026-10-05T23:58:00Z'}))),
  rerunQueued:summarize(prRow(rerun({category:'pending', status:'queued'}, {created_at:'2026-10-05T23:55:00Z'}))),
  oneRound:summarize(prRow({...base, push:{pushed_at:'2026-10-05T23:48:00Z', kind:'feature'}, checks:[{id:13, name:'preview',
    category:'skipped', status:'completed', started_at:'2026-10-05T23:49:00Z', completed_at:'2026-10-05T23:49:00Z'}, {id:12, name:'unit',
    category:'success', status:'completed', started_at:'2026-10-05T23:50:00Z', completed_at:'2026-10-05T23:58:00Z'}],
    runs:[{id:1, name:'CI', jobs:[{id:12, steps:[], created_at:'2026-10-05T23:49:30Z', started_at:'2026-10-05T23:50:00Z',
      completed_at:'2026-10-05T23:58:00Z'}]}]})),
  mixed:summarize(prRow({...base, attention_reason:'Current-head work is still running',
    unresolved_comments:1, review_threads:1, comments_complete:true, comment_count:1,
    push:{pushed_at:'2026-09-15T00:00:00Z', kind:'sync with a very long base branch name'},
    checks:[
      {name:'unit', category:'success', status:'completed', started_at:'2026-10-05T22:00:00Z', completed_at:'2026-10-05T22:10:00Z'},
      {name:'lint', category:'pending', status:'queued'},
      {name:'review', category:'pending', status:'queued'},
      {name:'e2e', category:'skipped', status:'completed'},
      {name:'docs', category:'skipped', status:'completed'}
    ]})),
  passed:summarize(prRow({...base, attention_reason:'No current-head failure observed',
    push:{pushed_at:'2026-10-05T00:00:00Z', kind:'bug fix'},
    checks:[{name:'unit', category:'success', status:'completed', started_at:'2026-10-05T00:00:30Z', completed_at:'2026-10-05T00:42:30Z'},
      {name:'lint', category:'success', status:'completed', started_at:'2026-10-05T00:01:00Z', completed_at:'2026-10-05T00:05:00Z'}]})),
  waiting:summarize(prRow({...base, push:{pushed_at:'2026-10-05T23:57:00Z', kind:'picked up #12'},
    checks:[{name:'lint', category:'pending', status:'queued'}]})),
  slow:summarize(prRow({...base, push:{pushed_at:'2026-10-05T19:30:00Z', kind:'sync with main'},
    checks:[{name:'e2e', category:'pending', status:'in_progress', started_at:'2026-10-05T19:40:00Z'}]})),
  moved:summarize(prRow({...base, head_changed:true, push:{pushed_at:'2026-10-05T00:00:00Z', kind:'feature'},
    checks:[{name:'e2e', category:'pending', status:'in_progress', started_at:'2026-10-05T19:40:00Z'}]})),
  unchecked:summarize(prRow({...base, push:{pushed_at:'2026-10-05T22:00:00Z', kind:'docs'}})),
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
  behindOne:summarize(prRow({...base, behind_by:1, default_branch:'main'})),
  behindMany:summarize(prRow({...base, behind_by:12, default_branch:'trunk'})),
  current:summarize(prRow({...base, behind_by:0, default_branch:'main'})),
  fallback:summarize(prRow({...base, attention_reason:'A current-head check was cancelled',
    checks:[{name:'unit', category:'cancelled', status:'completed'}]})),
  // A finished round: how long the head has sat since its last check finished, and how it ended.
  idleFailed:summarize(prRow({...base, checks:[
    {name:'unit', category:'success', status:'completed', started_at:'2026-10-05T22:00:00Z', completed_at:'2026-10-05T22:20:00Z'},
    {name:'e2e', category:'failed', status:'completed', started_at:'2026-10-05T22:00:00Z', completed_at:'2026-10-05T22:30:00Z'},
    {name:'cleanup', category:'cancelled', status:'completed', started_at:'2026-10-05T22:00:00Z', completed_at:'2026-10-05T22:05:00Z'}]})),
  idleCancelled:summarize(prRow({...base, checks:[
    {name:'unit', category:'success', status:'completed', started_at:'2026-10-05T19:00:00Z', completed_at:'2026-10-05T19:10:00Z'},
    {name:'e2e', category:'cancelled', status:'completed', started_at:'2026-10-05T19:00:00Z', completed_at:'2026-10-05T19:30:00Z'}]})),
  // A failed first round, then a passing re-run on the same commit: the line tells the re-run's result.
  idleRerun:summarize(prRow({...base, checks:[
    {id:21, name:'unit', category:'failed', status:'completed', started_at:'2026-10-05T17:00:00Z', completed_at:'2026-10-05T17:20:00Z'},
    {id:22, name:'unit', category:'success', status:'completed', started_at:'2026-10-05T23:50:00Z', completed_at:'2026-10-05T23:58:00Z'}],
    runs:[{id:1, name:'CI', jobs:[{id:21, steps:[], created_at:'2026-10-05T16:59:30Z', started_at:'2026-10-05T17:00:00Z', completed_at:'2026-10-05T17:20:00Z'},
      {id:22, steps:[], created_at:'2026-10-05T23:49:30Z', started_at:'2026-10-05T23:50:00Z', completed_at:'2026-10-05T23:58:00Z'}]}]})),
  idleSkipped:summarize(prRow({...base, checks:[
    {name:'preview', category:'skipped', status:'completed', started_at:'2026-10-05T23:59:00Z', completed_at:'2026-10-05T23:59:30Z'}]})),
  idleMoved:summarize(prRow({...base, head_changed:true, push:{pushed_at:'2026-10-05T23:59:00Z', kind:'feature'}, checks:[
    {name:'unit', category:'success', status:'completed', started_at:'2026-10-05T22:00:00Z', completed_at:'2026-10-05T22:20:00Z'}]}))
};
console.log(JSON.stringify(rows));
''')
        self.assertEqual(result["mixed"]["age"], ["5d", "old", "age-yellow", 1])
        # The idle line: in the caption after the CI time, counted from the round's last finish.
        self.assertEqual(result["passed"]["idle"], ["23h 17midle since CI passed", "age-red", "idle-dot tl-success",
                                                    "Nothing has run on this head since CI passed 23h 17m ago", 1])
        self.assertEqual(result["rerunDone"]["idle"][:3], ["2m 0sidle since CI passed", None, "idle-dot tl-success"])
        # A failure outranks a cancel and a pass; a cancel outranks a pass; skipped alone is "finished".
        self.assertEqual(result["idleFailed"]["idle"][:3], ["1h 30midle since CI failed", "age-yellow", "idle-dot tl-failed"])
        self.assertEqual(result["idleCancelled"]["idle"][:3], ["4h 30midle since CI was cancelled", "age-red", "idle-dot tl-cancelled"])
        self.assertEqual(result["idleCancelled"]["idle"][3], "Nothing has run on this head since CI was cancelled 4h 30m ago")
        self.assertEqual(result["idleRerun"]["idle"][:3], ["2m 0sidle since CI passed", None, "idle-dot tl-success"])
        self.assertEqual(result["idleSkipped"]["idle"][:3], ["30sidle since CI finished", None, "idle-dot tl-cancelled"])
        # Nothing while any check is open or waiting, before CI has times, or while the checks are the previous push's.
        for name in ("mixed", "rerunRunning", "rerunQueued", "waiting", "slow", "running", "required", "failed", "unchecked", "none", "idleMoved"):
            self.assertIsNone(result[name]["idle"], name)
        self.assertEqual(result["mixed"]["counts"], "2 waiting · 2 skipped")
        # A queued check keeps CI running from the first start, not from the push three weeks ago.
        self.assertEqual(result["mixed"]["pushWhen"], "CI running 2h 0m")
        self.assertEqual(result["mixed"]["pushClass"], "age-yellow")
        self.assertEqual(result["mixed"]["pushKind"], "· sync with a very long base branch name")
        self.assertEqual(result["mixed"]["pushTitle"], "CI running 2h 0m · pushed 3w ago · sync with a very long base branch name")
        # A second round on the same commit hours later is timed alone, as the CI timeline draws it.
        self.assertEqual(result["rerunRunning"]["pushWhen"], "CI running 10m 0s")
        self.assertEqual(result["rerunDone"]["pushWhen"], "CI took 8m 0s")
        self.assertEqual(result["rerunQueued"]["pushWhen"], "CI waiting 5m 0s")
        # With one round, a check that started before the first job (here a skipped one) still counts.
        self.assertEqual(result["oneRound"]["pushWhen"], "CI took 9m 0s")
        # Once every check is done the time stops: first start to last finish.
        self.assertEqual(result["passed"]["pushWhen"], "CI took 42m 0s")
        self.assertIsNone(result["passed"]["pushClass"])
        self.assertEqual(result["passed"]["pushTitle"], "CI took 42m 0s · pushed 1d ago · bug fix")
        self.assertEqual(result["waiting"]["pushWhen"], "CI waiting 3m 0s")
        self.assertEqual(result["waiting"]["pushKind"], "· picked up #12")
        self.assertEqual(result["slow"]["pushWhen"], "CI running 4h 20m")
        self.assertEqual(result["slow"]["pushClass"], "age-red")
        # Checks from the previous push say nothing about this push's CI.
        self.assertEqual(result["moved"]["pushWhen"], "pushed 1d ago")
        self.assertEqual(result["moved"]["pushClass"], "age-yellow")
        self.assertEqual(result["moved"]["pushTitle"], "pushed 1d ago · feature")
        self.assertEqual(result["unchecked"]["pushWhen"], "pushed 2h 0m ago")
        self.assertEqual(result["unchecked"]["pushKind"], "· docs")
        self.assertEqual(result["mixed"]["markInCaption"], 0)
        self.assertTrue(result["mixed"]["markBesideMeter"])
        self.assertEqual(result["mixed"]["workLines"], ["2 checks queued"])
        self.assertEqual(result["passed"]["counts"], "All passed")
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
        # The meta line names how far the default branch has moved past the head, only when it has.
        self.assertEqual(result["behindOne"]["behind"], ["1 behind main", "main has 1 commit this branch does not"])
        self.assertEqual(result["behindMany"]["behind"], ["12 behind trunk", "trunk has 12 commits this branch does not"])
        self.assertIsNone(result["current"]["behind"])
        self.assertIsNone(result["mixed"]["behind"])
        css = (ROOT / "dashboard/static/styles.css").read_text()
        app = (ROOT / "dashboard/static/prs.js").read_text()
        self.assertIn(".progress-layout > .progress-caption { grid-column: 1;", css)
        self.assertIn(".progress-layout > .comment-mark { grid-column: 2;", css)
        self.assertIn(".push-kind { min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }", css)
        self.assertNotIn("running-steps", css + app)

if __name__ == "__main__":
    unittest.main()
