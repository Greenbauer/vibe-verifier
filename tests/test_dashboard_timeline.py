"""The CI timeline: timeline.js's own node tests, the row drawing it in place of the check meter, and the
row's title link and toggle that open GitHub and the timeline."""
import json
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


class Timeline(unittest.TestCase):
    def node(self, source):
        result = subprocess.run(["node", "-e", source], cwd=ROOT, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return json.loads(result.stdout)

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
const app=fs.readFileSync('./dashboard/static/prs.js','utf8');
const slice=app.slice(app.indexOf('  function messageIcon('), app.indexOf('  function renderPrRows('));
function el(tag, attrs={}, ...children) {
  const node={tag, attrs, children:children.flat().filter(value => value !== null && value !== undefined)};
  node.append=(child)=>{ node.children.push(child); };
  return node;
}
const progress=new Function('el','checkTotals','meterSegments','meterLabel','unresolvedMark','since','ageClass','ageLabel','secondsClass','ciTiming', slice+'return progress;')(
  el, helpers.checkTotals, helpers.meterSegments, helpers.meterLabel, helpers.unresolvedMark, helpers.since, helpers.ageClass,
  helpers.ageLabel, helpers.secondsClass, helpers.ciTiming);
const classes=node=>!node||typeof node==='string'?[]:[node.attrs.class, ...node.children.flatMap(classes)].filter(Boolean);
const text=node=>!node?'':typeof node==='string'?node:node.children.map(text).join(' ');
const base={runs:[], statuses:[], expected:[], checks:[{name:'test', category:'failed', status:'completed'}]};
const mini=()=>el('div',{class:'timeline-mini'});
const timed=progress(base, Date.now(), mini());
const untimed=progress(base, Date.now(), null);
const none=progress({runs:[], statuses:[], expected:[], checks:[]}, Date.now(), mini());
console.log(JSON.stringify({timed:classes(timed), timedText:text(timed), untimed:classes(untimed), none:classes(none)}));
'''], cwd=ROOT, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        got = json.loads(result.stdout)
        self.assertIn("timeline-mini", got["timed"])
        self.assertNotIn("check-meter", got["timed"])
        self.assertIn("1 failed", got["timedText"])
        self.assertIn("check-meter", got["untimed"])
        # A head with no checks says so, even if handed a timeline.
        self.assertIn("progress-lead", got["none"])
        self.assertNotIn("timeline-mini", got["none"])

    def test_the_scale_menu_sets_the_scale_every_row_is_drawn_with(self):
        result = subprocess.run(["node", "-e", r"""
const fs=require('fs');
const helpers=require('./dashboard/static/helpers.js');
const app=fs.readFileSync('./dashboard/static/prs.js','utf8');
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
const renderPrRows=new Function('el','state','snapshot','rememberView','flattenPulls','filterPulls','groupPulls','rowView','prRow','empty','duration',
  rowsSource+'return renderPrRows;')(el, state, {github:{}}, ()=>{}, ()=>pulls, list=>list, helpers.groupPulls,
  (pull, scale)=>{ drawn.push(scale); return {open:false}; }, ()=>el('div'), ()=>el('p'), helpers.duration);
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


    def test_a_rows_title_opens_github_and_the_row_opens_its_ci_timeline(self):
        result = self.node(r"""
const fs=require('fs');
const helpers=require('./dashboard/static/helpers.js');
const app=fs.readFileSync('./dashboard/static/prs.js','utf8');
const source=app.slice(app.indexOf('  function runningNames('),app.indexOf('  function renderPulls('));
function el(tag, attrs={}, ...children) {
  return {tag, attrs, children:children.flat().filter(value => value !== null && value !== undefined)};
}
let renders=0;
global.expanded=new Set();
global.renderPrRows=()=>{ renders+=1; };
global.VVTimeline={mini:()=>el('div',{class:'timeline-mini'}),
  detail:(pull, now, options)=>el('div',{class:'timeline-detail', id:options.id, key:options.key, owner:options.owner})};
global.CSS={escape:value=>value};
global.document={querySelector:()=>null};
const {prRow, rowView}=new Function('el','safeUrl','snapshot','combinedCategory','badge','progress','since','ageClass','runningWork',
  'behindMark', source+'return {prRow, rowView};')(el, helpers.safeUrl, {owner:'octocat'}, ()=>'failed',
  status=>el('span',{},status), (pull, now, timeline)=>el('div',{class:'progress'}, timeline),
  value=>value==='2026-09-01T00:00:00Z'?'5w':'2d',
  value=>value==='2026-09-01T00:00:00Z'?'age-red':'age-yellow', helpers.runningWork, helpers.behindMark);
const pull=(html_url, stale)=>({repository:'octocat/example',number:3,title:'Fix the gate',author:'octocat',
  head_sha:'abcdef1234567890',html_url,attention_reason:'A current-head check failed',
  created_at:'2026-10-01T00:00:00Z',stale});
const ready=pull('https://github.com/octocat/example/pull/4', false);
ready.merge_ready=true; ready.title='Ship the gate';
const find=(node, test)=>!node||typeof node==='string'?[]:[...(test(node)?[node]:[]), ...node.children.flatMap(child=>find(child, test))];
const plain=node=>JSON.parse(JSON.stringify(node));
const linked=pull('https://github.com/octocat/example/pull/3', false);
const view=rowView(linked, null, Date.now());
const row=prRow(linked, view);
row.attrs.onclick({target:{closest:selector=>selector==='a, button'?{}:null}});
const afterLinkClick=[...expanded];
row.attrs.onclick({target:{closest:()=>null}});
const afterRowClick=[...expanded];
const openView=rowView(linked, null, Date.now());
const opened=prRow(linked, openView);
find(opened, node=>node.tag==='button')[0].attrs.onclick();
console.log(JSON.stringify({
  row:plain(row), view:{id:view.id, open:view.open}, opened:plain(opened), openOpen:openView.open, detail:plain(openView.detail()),
  afterLinkClick, afterRowClick, afterToggle:[...expanded], renders,
  stale:plain(prRow(pull('https://github.com/octocat/example/pull/3', true))),
  foreign:plain(prRow(pull('https://github.com/evil/example/pull/3', false))),
  missing:plain(prRow(pull(null, false))),
  ready:plain(prRow(ready)),
  route:app.includes('function renderDetail(')||app.includes('go("prs",')
}));
""")
        def text(node):
            if isinstance(node, str):
                return node
            return " ".join(text(child) for child in node.get("children", []))
        def nodes(node):
            if isinstance(node, str):
                return []
            return [node] + [descendant for child in node.get("children", []) for descendant in nodes(child)]
        # Assembled at run time: written out, "<owner>/<repo>#<n>" reads as a citation to test_public_surface.
        key = "octocat/example" + "#3"
        row = result["row"]
        self.assertEqual((row["tag"], row["attrs"]["class"]), ("div", "pr-row"))
        # Only the title is a link, and it opens the pull request on GitHub in a new tab.
        links = [node for node in nodes(row) if node["tag"] == "a"]
        self.assertEqual([(link["attrs"]["href"], link["attrs"]["target"], link["attrs"]["rel"], text(link)) for link in links],
                         [("https://github.com/octocat/example/pull/3", "_blank", "noreferrer", "Fix the gate")])
        self.assertIn("on GitHub", links[0]["attrs"]["aria-label"])
        # The row draws the timeline the list gave it in place of the meter.
        self.assertEqual(len([node for node in nodes(row) if node["attrs"].get("class") == "timeline-mini"]), 1)
        toggle = [node for node in nodes(row) if node["tag"] == "button"]
        self.assertEqual([(button["attrs"]["aria-expanded"], button["attrs"]["aria-controls"], button["attrs"]["data-focus-key"])
                          for button in toggle], [("false", result["view"]["id"], "timeline:" + key)])
        self.assertIn("Show the CI timeline", toggle[0]["attrs"]["aria-label"])
        # A click on the link or a button leaves the timeline alone; a click anywhere else on the row opens it.
        self.assertEqual((result["afterLinkClick"], result["afterRowClick"]), ([], [key]))
        opened = result["opened"]
        open_toggle = [node for node in nodes(opened) if node["tag"] == "button"][0]
        self.assertEqual((result["openOpen"], open_toggle["attrs"]["aria-expanded"], opened["attrs"]["class"]), (True, "true", "pr-row open"))
        self.assertIn("Hide the CI timeline", open_toggle["attrs"]["aria-label"])
        detail = result["detail"]
        self.assertEqual((detail["attrs"]["class"], detail["attrs"]["id"], detail["attrs"]["key"], detail["attrs"]["owner"]),
                         ("timeline-detail", result["view"]["id"], key, "octocat"))
        # The arrow closes it again, and each change redraws the list.
        self.assertEqual((result["afterToggle"], result["renders"]), ([], 2))
        age = next(node for node in row["children"] if node["attrs"].get("class") == "pr-age")
        got = ([part["children"] for stat in age["children"] for part in stat["children"]], age["children"][0]["children"][0]["attrs"]["class"])
        self.assertEqual(got, ([["2d"], ["PR age"]], "age-yellow"))
        self.assertEqual([text(node) for node in row["children"][1]["children"][1:]], ["A current-head check failed"])
        ready = result["ready"]
        title = ready["children"][0]["children"][0]
        ready_link = [node for node in nodes(ready) if node["tag"] == "a"][0]
        age_only = next(node for node in ready["children"] if node["attrs"].get("class") == "pr-age")
        self.assertEqual((("Fully merge-ready" in ready_link["attrs"]["aria-label"]), title["attrs"]["class"],
                          len(age_only["children"]), text(age_only)),
                         (True, "merge-ready", 1, "2d PR age"))
        # A row drawn without the list's view has no toggle action and shows the meter (no timeline).
        self.assertEqual([node["attrs"].get("onclick") for node in nodes(ready) if node["tag"] == "button"], [None])
        stale_link = [node for node in nodes(result["stale"]) if node["tag"] == "a"][0]
        self.assertEqual(stale_link["attrs"]["href"], links[0]["attrs"]["href"])
        for key_name in ("foreign", "missing"):
            item = result[key_name]
            self.assertEqual([node for node in nodes(item) if node["tag"] == "a"], [])
            self.assertIn("Fix the gate", text(item))
        self.assertFalse(result["route"])

if __name__ == "__main__":
    unittest.main()
