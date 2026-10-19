"""The CI timeline: timeline.js's own node tests, and the row drawing it in place of the check meter."""
import json
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


class Timeline(unittest.TestCase):
    def test_the_timelines_node_tests_pass(self):
        # timeline.test.js: lanes, critical path, row lines and scale, expanded rows, steps, axis ticks.
        result = subprocess.run(["node", "--test", "--test-reporter=tap", str(ROOT / "dashboard/static/timeline.test.js")],
                                cwd=ROOT, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("# fail 0", result.stdout)

    def test_a_row_draws_the_timeline_in_place_of_the_meter_and_keeps_its_caption(self):
        result = subprocess.run(["node", "-e", r'''
const fs=require('fs');
const helpers=require('./dashboard/static/helpers.js');
const app=fs.readFileSync('./dashboard/static/app.js','utf8');
const slice=app.slice(app.indexOf('  function messageIcon('), app.indexOf('  function renderPrRows('));
function el(tag, attrs={}, ...children) {
  const node={tag, attrs, children:children.flat().filter(value => value !== null && value !== undefined)};
  node.append=(child)=>{ node.children.push(child); };
  return node;
}
const scales=[];
global.VVTimeline={mini:(pull, now, scale)=>{ scales.push(scale); return pull.timed ? el('div',{class:'timeline-mini'}) : null; }};
const progress=new Function('el','checkTotals','meterSegments','meterLabel','unresolvedMark','since','ageClass','ageLabel','secondsClass','ciTiming', slice+'return progress;')(
  el, helpers.checkTotals, helpers.meterSegments, helpers.meterLabel, helpers.unresolvedMark, helpers.since, helpers.ageClass,
  helpers.ageLabel, helpers.secondsClass, helpers.ciTiming);
const classes=node=>!node||typeof node==='string'?[]:[node.attrs.class, ...node.children.flatMap(classes)].filter(Boolean);
const text=node=>!node?'':typeof node==='string'?node:node.children.map(text).join(' ');
const base={runs:[], statuses:[], expected:[], checks:[{name:'test', category:'failed', status:'completed'}]};
const timed=progress({...base, timed:true}, Date.now(), 600000);
const untimed=progress(base, Date.now(), null);
const none=progress({runs:[], statuses:[], expected:[], checks:[], timed:true}, Date.now(), 600000);
console.log(JSON.stringify({timed:classes(timed), timedText:text(timed), untimed:classes(untimed), none:classes(none), scales}));
'''], cwd=ROOT, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        got = json.loads(result.stdout)
        self.assertIn("timeline-mini", got["timed"])
        self.assertNotIn("check-meter", got["timed"])
        self.assertIn("1 failed", got["timedText"])
        self.assertIn("check-meter", got["untimed"])
        # A head with no checks says so and never asks for a timeline.
        self.assertIn("progress-lead", got["none"])
        self.assertEqual(got["scales"], [600000, None])

    def test_the_scale_menu_sets_the_scale_every_row_is_drawn_with(self):
        result = subprocess.run(["node", "-e", r"""
const fs=require('fs');
const helpers=require('./dashboard/static/helpers.js');
const app=fs.readFileSync('./dashboard/static/app.js','utf8');
const filters=app.slice(app.indexOf('  function prFilters('), app.indexOf('  function messageIcon('));
const rowsSource=app.slice(app.indexOf('  function renderPrRows('), app.indexOf('  function runningNames('));
function el(tag, attrs={}, ...children) {
  const node={tag, attrs, children:children.flat().filter(value => value !== null && value !== undefined)};
  node.append=(...items)=>{ node.children.push(...items); };
  node.replaceChildren=()=>{ node.children=[]; };
  return node;
}
const state={query:'', repository:'all', subscribed:false, attention:false, timeScale:'shared'};
const drawn=[];
const target={append(){}, replaceChildren(){}, textContent:''};
global.document={querySelector:()=>target};
global.VVTimeline={sharedScale:()=>3600000};
const pulls=[{repository:'octocat/a', number:1, title:'x', created_at:'2026-10-01T00:00:00Z'}];
const renderPrRows=new Function('el','state','snapshot','rememberView','flattenPulls','filterPulls','groupPulls','prRow','empty','duration',
  rowsSource+'return renderPrRows;')(el, state, {github:{}}, ()=>{}, ()=>pulls, list=>list, helpers.groupPulls,
  (pull, scale)=>{ drawn.push(scale); return el('div'); }, ()=>el('p'), helpers.duration);
const prFilters=new Function('el','state','repositoriesWithPulls','renderPrRows', filters+'return prFilters;')(
  el, state, helpers.repositoriesWithPulls, renderPrRows);
const toolbar=prFilters(pulls);
const menu=toolbar.children.find(node=>node.attrs.id==='time-scale');
renderPrRows();
menu.attrs.onchange({target:{value:'row'}});
console.log(JSON.stringify({options:menu.children.map(option=>[option.attrs.value, option.attrs.selected, option.children[0]]),
  label:menu.attrs['aria-label'], state:state.timeScale, drawn}));
"""], cwd=ROOT, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        got = json.loads(result.stdout)
        self.assertEqual(got["options"], [["shared", True, "Same time scale for every row"],
                                          ["row", False, "Fit each row's timeline"]])
        self.assertEqual(got["label"], "CI timeline scale")
        # The menu saves the choice and redraws: the shared scale first, then null, which fits each row.
        self.assertEqual((got["state"], got["drawn"]), ("row", [3600000, None]))

    def test_the_bars_use_the_dashboards_colors_and_stay_inside_their_box(self):
        css = (ROOT / "dashboard/static/styles.css").read_text()
        def rule(selector):
            start = css.index("\n" + selector + " {")
            return css[start:css.index("}", start)]
        # The same meaning as the check meter: green passed, red failed, yellow running, dim yellow waiting.
        self.assertIn("background: var(--green)", rule(".bar-success"))
        self.assertIn("background: var(--red)", rule(".bar-failed"))
        self.assertIn("background: var(--yellow)", rule(".bar-running"))
        self.assertIn("background: var(--yellow); opacity: .35", rule(".bar-waiting"))
        # Nothing drawn can leave the row's timeline box, so it never reaches the text under it.
        self.assertIn("overflow: hidden", rule(".timeline-mini"))
        self.assertIn("top: 0; bottom: 0", rule(".timeline-now"))
        # The toggle stays on screen at every width, so a keyboard can always open a timeline.
        self.assertNotIn(".timeline-toggle { display: none", css)


if __name__ == "__main__":
    unittest.main()
