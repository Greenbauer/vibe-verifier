"""Loopback server boundaries and dependency-free browser helper behavior."""

import io
import json
import subprocess
import unittest
from pathlib import Path
from unittest import mock

from dashboard.config import BotDefinition, Config
from dashboard.favicon import Favicon
from dashboard.server import DashboardHandler, make_server

ROOT = Path(__file__).resolve().parent.parent


def config():
    bots = {name: BotDefinition(name + ".yml", (name,)) for name in ("reviewer", "explorer", "verifier")}
    return Config("octocat", ("octocat/example",), bots, None)


class FakeService:
    def snapshot(self):
        return {"version": 1, "owner": "octocat", "github": {"sampled_at": None, "repositories": [],
                "bots": {"roles": {}}, "coverage": {"selected": 1, "readable": 0},
                "title": "<img src=x onerror=alert(1)>"}, "telemetry": {"available": False}}

class FakeSocket:
    def __init__(self, request):
        self.request = io.BytesIO(request)
        self.response = bytearray()
    def makefile(self, mode, buffering=None):
        return self.request
    def sendall(self, data):
        self.response.extend(data)
    def close(self):
        pass
class FakeServer:
    server_port = 8765
    service = FakeService()
    favicon = Favicon("octocat", fetch=lambda owner: None)
    def __init__(self, proxy_origin=None):
        self.proxy_origin = proxy_origin
class ServerSecurity(unittest.TestCase):
    def request(self, method, path, headers=None, proxy_origin=None):
        if isinstance(headers, dict):
            header_items = list(headers.items())
        else:
            header_items = list(headers or ())
        if not any(name.lower() == "host" for name, _ in header_items):
            header_items.insert(0, ("Host", "127.0.0.1:8765"))
        raw = "%s %s HTTP/1.1\r\n%s\r\n\r\n" % (
            method, path, "\r\n".join("%s: %s" % item for item in header_items))
        connection = FakeSocket(raw.encode("ascii"))
        DashboardHandler(connection, ("127.0.0.1", 12345), FakeServer(proxy_origin))
        head, body = bytes(connection.response).split(b"\r\n\r\n", 1)
        lines = head.decode("ascii").split("\r\n")
        values = {name.lower(): value.strip() for name, value in
                  (line.split(":", 1) for line in lines[1:] if ":" in line)}
        return int(lines[0].split()[1]), values, body
    def test_server_binds_only_loopback_and_serves_json_without_literal_html(self):
        with mock.patch("dashboard.server.DashboardServer") as server:
            make_server(config(), 8765, FakeService())
            self.assertEqual(server.call_args.args[0], ("127.0.0.1", 8765))
        status, headers, body = self.request("GET", "/api/dashboard")
        self.assertEqual(status, 200)
        self.assertEqual(headers["content-type"], "application/json; charset=utf-8")
        self.assertNotIn(b"<img", body)
        self.assertEqual(json.loads(body)["owner"], "octocat")
        self.assertNotIn("access-control-allow-origin", headers)
    def test_favicon_is_served_same_origin_and_used_as_tab_icon_and_header_logo(self):
        status, headers, body = self.request("GET", "/favicon.svg")
        self.assertEqual(status, 200)
        self.assertEqual(headers["content-type"], "image/svg+xml")
        self.assertIn("img-src 'self' data:", headers["content-security-policy"])
        self.assertIn(b">O</text>", body)
        _, _, page = self.request("GET", "/")
        self.assertIn(b'<link rel="icon" href="/favicon.svg" type="image/svg+xml">', page)
        self.assertIn(b'<img class="brand-mark" src="/favicon.svg" alt="">', page)
    def test_untrusted_host_and_origin_are_rejected(self):
        self.assertEqual(self.request("GET", "/", {"Host": "example.invalid"})[0], 421)
        self.assertEqual(self.request("GET", "/", {"Origin": "https://example.invalid"})[0], 403)
    def test_valid_proxy_request_uses_exact_forwarded_authority_and_https(self):
        origin = "https://dashboard.example.test:8443"
        for host in ("localhost", "localhost:8765", "127.0.0.1:8765"):
            with self.subTest(host=host):
                status, headers, _ = self.request("GET", "/", {
                    "Host": host,
                    "Origin": origin,
                    "X-Forwarded-Host": "dashboard.example.test:8443",
                    "X-Forwarded-Proto": "https",
                }, proxy_origin=origin)
                self.assertEqual(status, 200)
                self.assertIn("script-src 'self'", headers["content-security-policy"])
                self.assertNotIn("access-control-allow-origin", headers)
    def test_host_preserving_proxy_may_send_the_configured_authority_as_host(self):
        origin = "https://dashboard.example.test:8443"
        forwarded = {"X-Forwarded-Host": "dashboard.example.test:8443", "X-Forwarded-Proto": "https"}
        for path in ("/", "/api/dashboard"):
            with self.subTest(path=path):
                self.assertEqual(self.request("GET", path, {
                    "Host": "dashboard.example.test:8443", "Origin": origin, **forwarded,
                }, proxy_origin=origin)[0], 200)
        for host in ("other.example.test:8443", "dashboard.example.test", "dashboard.example.test:9999"):
            with self.subTest(host=host):
                self.assertEqual(self.request("GET", "/", {"Host": host, **forwarded},
                                              proxy_origin=origin)[0], 421)
        # The public authority is only trusted alongside complete, matching forwarded headers.
        self.assertEqual(self.request("GET", "/", {"Host": "dashboard.example.test:8443"},
                                      proxy_origin=origin)[0], 421)
        self.assertEqual(self.request("GET", "/", {
            "Host": "dashboard.example.test:8443", "X-Forwarded-Host": "other.example.test:8443",
            "X-Forwarded-Proto": "https"}, proxy_origin=origin)[0], 403)
        self.assertEqual(self.request("GET", "/", {
            "Host": "dashboard.example.test:8443", **forwarded}, proxy_origin=None)[0], 403)
    def test_default_mode_rejects_forwarded_headers(self):
        self.assertEqual(self.request("GET", "/", {
            "X-Forwarded-Host": "dashboard.example.test",
            "X-Forwarded-Proto": "https",
        })[0], 403)
    def test_proxy_mode_rejects_missing_mismatched_and_duplicate_forwarded_headers(self):
        origin = "https://dashboard.example.test"
        denied = (
            {"X-Forwarded-Host": "dashboard.example.test"},
            {"X-Forwarded-Proto": "https"},
            {"X-Forwarded-Host": "other.example.test", "X-Forwarded-Proto": "https"},
            {"X-Forwarded-Host": "dashboard.example.test", "X-Forwarded-Proto": "http"},
            [("X-Forwarded-Host", "dashboard.example.test"),
             ("X-Forwarded-Host", "dashboard.example.test"), ("X-Forwarded-Proto", "https")],
            [("X-Forwarded-Host", "dashboard.example.test"), ("X-Forwarded-Proto", "https"),
             ("X-Forwarded-Proto", "https")],
        )
        for headers in denied:
            with self.subTest(headers=headers):
                self.assertNotEqual(self.request(
                    "GET", "/", headers, proxy_origin=origin)[0], 200)
    def test_duplicate_host_origin_and_hostile_proxy_origin_are_rejected(self):
        origin = "https://dashboard.example.test"
        self.assertEqual(self.request("GET", "/", [
            ("Host", "127.0.0.1:8765"), ("Host", "127.0.0.1:8765")])[0], 400)
        self.assertEqual(self.request("GET", "/", [
            ("Origin", "http://127.0.0.1:8765"),
            ("Origin", "http://127.0.0.1:8765")])[0], 400)
        self.assertEqual(self.request("GET", "/", {
            "Origin": "https://hostile.example.test",
            "X-Forwarded-Host": "dashboard.example.test",
            "X-Forwarded-Proto": "https",
        }, proxy_origin=origin)[0], 403)
    def test_direct_loopback_requests_still_work_when_proxy_is_configured(self):
        origin = "https://dashboard.example.test"
        local_headers = {"Host": "localhost:8765", "Origin": "http://localhost:8765"}
        self.assertEqual(self.request("GET", "/", local_headers, proxy_origin=origin)[0], 200)
        self.assertEqual(self.request(
            "GET", "/api/dashboard", local_headers, proxy_origin=origin)[0], 200)
    def test_path_traversal_is_rejected_before_static_lookup(self):
        self.assertEqual(self.request("GET", "/%2e%2e/secret")[0], 400)
        self.assertEqual(self.request("GET", "/assets/unknown.js")[0], 404)
    def test_every_mutating_method_is_denied(self):
        for method in ("POST", "PUT", "PATCH", "DELETE", "OPTIONS"):
            with self.subTest(method=method):
                self.assertEqual(self.request(method, "/api/dashboard")[0], 405)
    def test_every_mutating_method_is_denied_through_proxy(self):
        origin = "https://dashboard.example.test"
        headers = {"Host": "localhost", "X-Forwarded-Host": "dashboard.example.test",
                   "X-Forwarded-Proto": "https"}
        for method in ("POST", "PUT", "PATCH", "DELETE", "OPTIONS"):
            with self.subTest(method=method):
                self.assertEqual(self.request(
                    method, "/api/dashboard", headers, proxy_origin=origin)[0], 405)
    def test_static_response_has_no_remote_script_permission(self):
        status, headers, body = self.request("GET", "/")
        self.assertEqual(status, 200)
        self.assertIn("script-src 'self'", headers["content-security-policy"])
        self.assertNotIn(b"https://", body)
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
 expected:[{name:'qae-verify',category:'pending',status:'expected'}]};
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
const pr={checks:[{category:'success'}],expected:[{name:'qae-verify',category:'pending',status:'expected'}]};
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
    def test_pull_request_row_opens_github_instead_of_a_dashboard_page(self):
        result = self.node(r'''
const fs=require('fs');
const {safeUrl}=require('./dashboard/static/helpers.js');
const app=fs.readFileSync('./dashboard/static/app.js','utf8');
const source=app.slice(app.indexOf('  function runningNames('),app.indexOf('  function renderPulls('));
function el(tag, attrs={}, ...children) {
  return {tag, attrs, children:children.flat().filter(value => value !== null && value !== undefined)};
}
const prRow=new Function('el','safeUrl','snapshot','combinedCategory','badge','progress','since','ageClass','runningWork',
  source+'return prRow;')(el, safeUrl, {owner:'octocat'}, ()=>'failed',
  status=>el('span',{},status), ()=>el('div',{class:'progress'}),
  value=>value==='2026-09-01T00:00:00Z'?'5w':'2d',
  value=>value==='2026-09-01T00:00:00Z'?'age-red':'age-yellow', require('./dashboard/static/helpers.js').runningWork);
const pull=(html_url, stale)=>({repository:'octocat/example',number:3,title:'Fix the gate',author:'octocat',
  head_sha:'abcdef1234567890',html_url,attention_reason:'A current-head check failed',
  created_at:'2026-10-01T00:00:00Z',stale});
const ready=pull('https://github.com/octocat/example/pull/4', false);
ready.merge_ready=true; ready.title='Ship the gate';
ready.push={pushed_at:'2026-09-01T00:00:00Z', kind:'bug fix'};
console.log(JSON.stringify({
  linked:prRow(pull('https://github.com/octocat/example/pull/3', false)),
  stale:prRow(pull('https://github.com/octocat/example/pull/3', true)),
  foreign:prRow(pull('https://github.com/evil/example/pull/3', false)),
  missing:prRow(pull(null, false)),
  ready:prRow(ready),
  detail:app.includes('function renderDetail(')||app.includes('go("prs",')
}));
''')
        def text(node):
            if isinstance(node, str):
                return node
            return " ".join(text(child) for child in node.get("children", []))
        linked = result["linked"]
        self.assertEqual(linked["tag"], "a")
        self.assertEqual(linked["attrs"]["href"], "https://github.com/octocat/example/pull/3")
        self.assertEqual(linked["attrs"]["target"], "_blank")
        self.assertEqual(linked["attrs"]["rel"], "noreferrer")
        self.assertNotIn("onclick", linked["attrs"])
        self.assertIn("on GitHub", linked["attrs"]["aria-label"])
        self.assertIn("Fix the gate", text(linked))
        age = next(node for node in linked["children"] if node["attrs"].get("class") == "pr-age")
        got = ([part["children"] for stat in age["children"] for part in stat["children"]], age["children"][0]["children"][0]["attrs"]["class"])
        self.assertEqual(got, ([["2d"], ["PR age"]], "age-yellow"))
        self.assertEqual([text(node) for node in linked["children"][1]["children"][1:]], ["A current-head check failed"])
        ready = result["ready"]
        title = next(node for node in ready["children"] if node["attrs"].get("class") == "pr-identity")["children"][0]
        age_only = next(node for node in ready["children"] if node["attrs"].get("class") == "pr-age")
        self.assertEqual((("Fully merge-ready" in ready["attrs"]["aria-label"]), title["attrs"]["class"],
                          len(age_only["children"]), text(age_only)),
                         (True, "merge-ready", 1, "2d PR age"))
        self.assertEqual((result["stale"]["attrs"]["class"], result["stale"]["attrs"]["href"]), ("pr-row stale-row", linked["attrs"]["href"]))
        for key in ("foreign", "missing"):
            row = result[key]
            self.assertEqual((row["tag"], row["attrs"]["href"], row["attrs"]["aria-label"]), ("div", None, None))
            self.assertIn("Fix the gate", text(row))
        self.assertFalse(result["detail"])
    def test_a_held_slot_reads_as_occupied_and_a_job_name_stays_text(self):
        result = self.node(r"""
const app=require('fs').readFileSync('./dashboard/static/app.js','utf8');
const match=app.match(/  function renderCapacity\(\) \{([\s\S]*?)\n  \}\n\n  function render\(\)/);
if (!match) throw new Error('renderCapacity not found');
const keep=items=>items.flat().filter(value=>value!=null);
function el(tag, attrs={}, ...children) {
  const node={tag, attrs, children:keep(children)};
  node.append=(...items)=>node.children.push(...keep(items));
  node.prepend=(...items)=>node.children.unshift(...keep(items));
  node.querySelector=selector=>{
    const cls=selector.slice(1);
    const walk=item=>!item||typeof item==='string'?null:(item.attrs.class||'').split(' ').includes(cls)?item:(item.children||[]).reduce((found,child)=>found||walk(child),null);
    return walk(node);
  };
  return node;
}
const badge=status=>el('span',{class:`badge status-${status}`},status);
const link=(label,url)=>url?el('a',{href:url},label):null;
const content={children:[],replaceChildren(...nodes){this.children=nodes.flat()},append(...nodes){this.children.push(...nodes.flat())}};
const job=(name,url)=>({repository:'example/widgets',name,url});
const snapshot={owner:'example',telemetry:{available:true,capacity:{available:true,stale:false,sampled_at:'2026-10-08T03:18:00Z',
  host:{cpu_percent:10,memory_used_bytes:1,memory_total_bytes:2,workspace_disk_free_bytes:1,workspace_disk_total_bytes:2},
  limits:{slots:8,wait_slots:8,qae_concurrency:2,cpu_quota_cores:4,memory_max_bytes:100},
  lanes:[{id:'ci-1',state:'busy',registered:null,labels:['ci'],job:job('<img src=x>','https://github.com/example/widgets/actions/runs/9/job/8')},
    {id:'wait-3',state:'busy',registered:null,labels:['wait'],job:job('Wait for preview',null)},
    {id:'ci-2',state:'allocated',registered:null,labels:['ci']},{id:'on-demand-1',state:'provisionable',registered:false,labels:[]}]}}};
const renderCapacity=new Function('content','snapshot','heading','empty','sourceBanner','el','formatTime','bytes','diskUsage','badge','link','metric',match[1]+'\nreturn content;');
renderCapacity(content,snapshot,title=>el('h1',{},title),()=>el('p'),()=>null,el,value=>value,value=>String(value),()=>({used:1,total:2,percent:50}),badge,link,(label,value)=>el('div',{},label,String(value)));
const text=node=>typeof node==='string'?node:(node.children||[]).map(text).join(' ');
const tags=node=>!node||typeof node==='string'?[]:[node.tag,...(node.children||[]).flatMap(tags)];
const lanes=content.children[content.children.length-1],cards=lanes.querySelector('.lane-grid').children;
console.log(JSON.stringify({limits:text(lanes.children[0]),cards:cards.map(card=>({text:text(card),tags:tags(card)}))}));
""")
        self.assertIn("8 shared CI/QAE slots · 8 wait slots", result["limits"])
        self.assertNotIn("Registration unknown", json.dumps(result))
        busy, wait, held, free = result["cards"]
        self.assertIn("<img src=x>", busy["text"])
        self.assertNotIn("img", busy["tags"])
        self.assertIn("Wait for preview", wait["text"])
        self.assertIn("example/widgets", wait["text"])
        self.assertIn("Occupied; no job recorded for this slot", held["text"])
        self.assertNotIn("No current same-owner job", held["text"])
        self.assertIn("No runner registered", free["text"])
        self.assertNotIn("Registration unknown", free["text"])
    def test_ui_uses_text_nodes_and_the_approved_local_palette(self):
        app = (ROOT / "dashboard/static/app.js").read_text()
        helpers = (ROOT / "dashboard/static/helpers.js").read_text()
        css = (ROOT / "dashboard/static/styles.css").read_text()
        html = (ROOT / "dashboard/static/index.html").read_text()
        self.assertNotIn("innerHTML", app + helpers)
        for color in ("#090909", "#111111", "#282828"):
            self.assertIn(color, css)
        for color in ("#809cff", "#50d6e8"):
            self.assertIn(color, helpers)
        self.assertEqual(html.count('data-view="'), 3)
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
const {BOT_META,agedState}=require('./dashboard/static/helpers.js'),VVCharts=require('./dashboard/static/charts.js');
const make=tag=>({tag,attrs:{},children:[],style:{setProperty(k,v){this[k]=v}},setAttribute(k,v){this.attrs[k]=v},append(...c){this.children.push(...c)},replaceChildren(...c){this.children=c}});
const el=(tag,attrs={},...c)=>Object.assign(make(tag),{attrs,children:c.flat().filter(v=>v!=null)});
const text=n=>typeof n==='string'?n:(n.children||[]).map(text).join(' '),walk=n=>!n||typeof n==='string'?[]:[n,...(n.children||[]).flatMap(walk)];
const rows=[1,2,3].map(n=>({id:'ci-qae-'+n,name:'QAE '+n,role:'qae',state:'idle',recent_2h:[],coverage:{history:'complete'}}));
const snapshot={agents:{rows}},strip=el('div'),at=h=>new Date(Date.now()-h*3600000).toISOString();
const recent=(value,runs)=>runs[0]?.category==='failed'?'Latest run failed':runs.length?'Last 2h':value.coverage?.history==='complete'?'No runs in 2h':'History incomplete';
global.document={createElementNS:(_,tag)=>make(tag),createElement:make};
new Function('el','BOT_META','snapshot','state','badge','go','formatTime','document','recentRunLabel','agedState',app.match(/  function renderBots\(\) \{([\s\S]*?)\n  \}\n\n  function coverage/)[1])(el,BOT_META,snapshot,{failureBot:null},s=>el('span',{},s),()=>{},v=>v,{querySelector:()=>strip},recent,agedState);
global.document={createElementNS:(_,tag)=>make(tag),createElement:make};
const samples=rows.flatMap(row=>[31,30,3,2].map(h=>({account:'a',bot:row.id,timestamp:at(h),input_tokens:40,output_tokens:0})));
const usage={sampled_at:at(0),accounts:[],samples,pace:{tokens_per_hour:100,plan:'ChatGPT subscription',window:'7d',used_percent:25,resets_at:at(-10),delta_points:-4,sized_from:'bot_tokens',window_tokens:400,allowance_tokens:1600}};
const src=app.match(/\n(  function usageCharts\([\s\S]*?)\n  function renderUsage\(/)[1];
const cards=new Function('el','BOT_META','VVCharts','snapshot','formatTime',src+'\nreturn usageCharts;')(el,BOT_META,VVCharts,snapshot,()=>'RESET')(usage).children[2].children;
const names=n=>walk(n).filter(i=>i.attrs&&i.attrs.class==='bot-name').map(text);
console.log(JSON.stringify({strip:names(strip),cards:cards.map(c=>text(c.children[0].children[0].children[0]).split(' · ')[0]),usual:cards.map(c=>walk(c).some(n=>n.attrs&&n.attrs.class==='burn-usual'))}));
""")
        self.assertEqual([result["strip"], result["cards"], result["usual"]], [["QAE 1", "QAE 2", "QAE 3"], ["QAE 1", "QAE 2", "QAE 3", "All bots"], [True, True, True, True]])

if __name__ == "__main__":
    unittest.main()
