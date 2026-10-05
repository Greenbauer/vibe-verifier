"""Loopback server boundaries and dependency-free browser helper behavior."""

import io
import json
import subprocess
import unittest
from pathlib import Path
from unittest import mock

from dashboard.config import BotDefinition, Config
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

    def test_filters_step_unknowns_and_pace_are_pure(self):
        source = r'''
const h=require('./dashboard/static/helpers.js');
const c=require('./dashboard/static/charts.js');
const pulls=[{repository:'octocat/example',number:1,title:'Safe',author:'octocat',subscription:'subscribed',attention:true},
             {repository:'octocat/other',number:2,title:'Quiet',author:'octocat',subscription:'unknown',attention:false}];
const filtered=h.filterPulls(pulls,{query:'safe',repository:'all',subscribed:true,attention:true});
const unknown=h.stepTotals({runs:[{step_summary:{known:false}}]});
console.log(JSON.stringify({filtered:filtered.map(x=>x.number),unknown,
 paceMissing:c.pacePerHour({allowance_tokens:null,pace_tokens_per_second:null}),
 pace:c.pacePerHour({allowance_tokens:1000,pace_tokens_per_second:2}),
 safe:h.safeUrl('https://github.com/octocat/example/pull/1','octocat'),
 unsafe:h.safeUrl('https://github.com/example/foreign/pull/1','octocat')}));
'''
        result = self.node(source)
        self.assertEqual(result["filtered"], [1])
        self.assertFalse(result["unknown"]["known"])
        self.assertIsNone(result["unknown"]["total"])
        self.assertIsNone(result["paceMissing"])
        self.assertEqual(result["pace"], 7200)
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

    def test_an_expected_required_check_keeps_the_pr_pending_and_its_steps_unknown(self):
        result = self.node(r'''
const h=require('./dashboard/static/helpers.js');
const pr={checks:[{category:'success'}],expected:[{category:'pending'}],
 runs:[{step_summary:{known:true,completed:83,total:83,remaining:0}}]};
console.log(JSON.stringify([h.combinedCategory(pr),h.stepTotals(pr).known]));
''')
        self.assertEqual(result, ["pending", False])

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


if __name__ == "__main__":
    unittest.main()
