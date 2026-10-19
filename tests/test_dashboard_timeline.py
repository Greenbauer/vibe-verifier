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


if __name__ == "__main__":
    unittest.main()
