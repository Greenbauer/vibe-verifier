"""The Work view: read-only plan and call queues behind MCP servers, and what waits on a person."""

import io
import json
import os
import subprocess
import tempfile
import unittest
import urllib.error
from datetime import datetime, timedelta, timezone
from pathlib import Path

from dashboard.config import ConfigError, WorkSource, load_config
from dashboard.work import McpClient, RestClient, WorkError, WorkFeed, _rpc_message, collect_calls, collect_plans, read_token
from test_dashboard_config_telemetry import config_value

ROOT = Path(__file__).resolve().parent.parent
NOW = datetime(2026, 10, 12, 15, 0, tzinfo=timezone.utc)


def plan_row(plan_id, status, **extra):
    row = {"planId": plan_id, "planLabel": "PLAN-" + plan_id[:8].upper(), "title": "Plan " + plan_id[:4],
           "status": status, "prNumber": None, "previewUrl": None, "parentPlanId": None, "claimedBy": None,
           "blockedReason": None, "createdAt": "2026-04-14T17:50:09.048558+00:00",
           "updatedAt": "2026-05-15T17:13:30+00:00", "removed": False}
    row.update(extra)
    return row


def call_row(call_id, status, **extra):
    row = {"id": call_id, "label": "CALL-" + call_id[:4].upper(), "title": "Call " + call_id[:4], "status": status,
           "statusDescription": None, "submitter": {"email": "boss@example.test", "name": "Boss"}, "claim": None,
           "prNumber": None, "previewUrl": None, "parentCallId": None,
           "createdAt": "2026-08-11T10:00:00Z", "updatedAt": "2026-09-03T10:00:00Z"}
    row.update(extra)
    return row


class FakeServer:
    """Answers MCP posts like a streamable HTTP server: text/event-stream, one message per reply."""

    def __init__(self, tools, status=200):
        self.tools, self.status, self.calls = tools, status, []

    def __call__(self, request, timeout):
        body = json.loads(request.data)
        self.calls.append((body["method"], body["params"].get("name"), body["params"].get("arguments"),
                           request.get_header("Authorization")))
        if self.status != 200:
            raise urllib.error.HTTPError(request.full_url, self.status, "denied", {}, io.BytesIO())
        if body["method"] == "initialize":
            result = {"protocolVersion": "2025-06-18", "capabilities": {}}
        else:
            name, arguments = body["params"]["name"], body["params"]["arguments"]
            answer = self.tools[name](arguments)
            result = ({"content": [{"type": "text", "text": answer.args[0]}], "isError": True}
                      if isinstance(answer, Exception) else {"content": [{"type": "text", "text": json.dumps(answer)}]})
        text = "event: message\ndata: %s\n\n" % json.dumps({"jsonrpc": "2.0", "id": body["id"], "result": result})
        return Reply(text)


class Reply(io.BytesIO):
    def __init__(self, text):
        super().__init__(text.encode())
        self.headers = {}

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False


class FakeApi:
    """Answers GETs like the plans read API: JSON, 404 for an unknown plan."""

    def __init__(self, routes, status=200):
        self.routes, self.status, self.calls = routes, status, []

    def __call__(self, request, timeout):
        self.calls.append((request.get_method(), request.full_url, request.get_header("Authorization")))
        path = request.full_url.split("/api/v1", 1)[1]
        if self.status != 200 or path not in self.routes:
            raise urllib.error.HTTPError(request.full_url, self.status if self.status != 200 else 404, "no", {}, io.BytesIO())
        return Reply(json.dumps(self.routes[path]))


def plans_server():
    plans = [plan_row("06f21214-0000", "submitted"),
             plan_row("dec46811-0000", "merged", prNumber=258),
             plan_row("5d2e1d27-0000", "blocked", prNumber=887),
             plan_row("5ad53758-0000", "in_progress", prNumber=738,
                      previewUrl="https://example-git-branch.example.test", claimedBy="dev@example.test")]
    details = {
        "06f21214-0000": {"planMarkdown": "# Plan\nBody", "amendments": []},
        "5d2e1d27-0000": {"status": "blocked", "planMarkdown": "body", "amendments": [], "blockedReason": None},
        # get_plan reports the status users see, derived from the PR: here the PR left draft.
        "5ad53758-0000": {"status": "pr_ready", "planMarkdown": "body", "updatedAt": "2026-10-08T19:05:04Z",
                          "amendments": [{"amendmentId": "a1", "body": "first", "createdAt": "2026-08-14T11:36:00Z"},
                                         {"amendmentId": "a2", "body": "second", "createdAt": "2026-09-01T09:00:00Z"}]},
    }
    routes = {"/plans?limit=100": {"plans": plans}}
    for row in plans:
        if row["planId"] in details:
            routes["/plans/" + row["planId"]] = {**row, **details[row["planId"]]}
    # Listed, then removed before its read: a 404 the list still shows without detail.
    plans.append(plan_row("9a682a92-0000", "submitted"))
    return FakeApi(routes)


def calls_server():
    calls = [call_row("aaaa-1", "blocked"), call_row("bbbb-2", "await_answer", claim="other"), call_row("cccc-3", "pr_ready", prNumber=12)]
    details = {
        "aaaa-1": {"brief": "do it", "followups": [{"id": "f1", "body": "also this", "createdAt": "2026-08-12T10:00:00Z",
                                                    "designRefs": [{"label": "Login screen", "kind": "screenshot", "url": "https://x.test/s?sig=1"}]}],
                   "designRefs": [], "asks": [{"body": "Which one?", "raisedAt": "2026-08-13T10:00:00Z",
                                               "ruling": {"text": "The left one", "answeredAt": "2026-08-14T10:00:00Z"}}]},
        "bbbb-2": {"brief": "b", "followups": [], "designRefs": [], "asks": [
            {"body": "Old question", "raisedAt": "2026-08-20T10:00:00Z", "ruling": {"text": "yes", "answeredAt": "2026-08-21T10:00:00Z"}},
            {"body": "Ship on Friday?", "raisedAt": "2026-09-01T10:00:00Z", "ruling": None}]},
        "cccc-3": {"brief": "c", "followups": [], "designRefs": [], "asks": []},
    }
    return FakeServer({
        "list_open_calls": lambda arguments: calls,
        "get_call": lambda arguments: {**next(row for row in calls if row["id"] == arguments["callId"]),
                                       **details[arguments["callId"]]},
    })


class WorkConfig(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory(prefix="vv-work-")
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)

    def load(self, sources):
        path = self.root / "dashboard.json"
        path.write_text(json.dumps({**config_value(), "work_sources": sources}), encoding="utf-8")
        os.chmod(path, 0o600)
        return load_config(path)

    def source(self, **extra):
        value = {"id": "plans", "label": "Plans", "kind": "plans", "endpoint": "https://queue.example.test/api/mcp",
                 "token_file": "/etc/vibe-dashboard/example/plans.token"}
        value.update(extra)
        return value

    def test_a_source_loads_with_its_links_and_no_source_is_the_default(self):
        config = self.load([self.source(repository="OCTOCAT/example", app_origin="https://queue.example.test")])
        self.assertEqual(config.work_sources, (WorkSource(
            "plans", "Plans", "plans", "https://queue.example.test/api/mcp",
            Path("/etc/vibe-dashboard/example/plans.token"), "octocat/example", "https://queue.example.test"),))
        self.assertEqual(self.load([]).work_sources, ())

    def test_unsafe_or_ambiguous_sources_are_refused(self):
        bad = [
            [self.source(endpoint="http://queue.example.test/api/mcp")],
            [self.source(endpoint="https://user:pw@queue.example.test/api/mcp")],
            [self.source(kind="tickets")],
            [self.source(id="Plans")],
            [self.source(), self.source(label="Again")],
            [self.source(token_file="relative.token")],
            [self.source(repository="someone-else/example")],
            [self.source(app_origin="https://queue.example.test/plans")],
            [self.source(token="inline-secret")],
            [self.source(id="s%d" % n) for n in range(9)],
        ]
        for sources in bad:
            with self.subTest(sources=sources), self.assertRaises(ConfigError):
                self.load(sources)


class Token(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory(prefix="vv-work-")
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "source.token"

    def test_only_a_private_regular_file_of_this_account_is_read(self):
        with self.assertRaises(WorkError) as missing:
            read_token(self.path, os.getuid())
        self.assertEqual(missing.exception.code, "credential_missing")
        self.path.write_text("secret-value\n")
        os.chmod(self.path, 0o640)
        with self.assertRaises(WorkError) as shared:
            read_token(self.path, os.getuid())
        self.assertEqual(shared.exception.code, "credential_unsafe")
        os.chmod(self.path, 0o600)
        self.assertEqual(read_token(self.path, os.getuid()), "secret-value")
        with self.assertRaises(WorkError):
            read_token(self.path, os.getuid() + 1)
        link = self.path.with_name("link.token")
        link.symlink_to(self.path)
        with self.assertRaises(WorkError):
            read_token(link, os.getuid())


class Protocol(unittest.TestCase):
    def test_replies_are_read_from_json_or_an_event_stream_by_request_id(self):
        self.assertEqual(_rpc_message('{"jsonrpc":"2.0","id":3,"result":{}}', 3)["id"], 3)
        stream = 'event: message\ndata: {"jsonrpc":"2.0","method":"note"}\n\nevent: message\ndata: {"jsonrpc":"2.0","id":2,"result":{"a":1}}\n\n'
        self.assertEqual(_rpc_message(stream, 2)["result"], {"a": 1})
        with self.assertRaises(WorkError):
            _rpc_message(stream, 9)

    def test_the_bearer_goes_in_the_header_and_failures_have_codes(self):
        server = calls_server()
        McpClient("https://queue.example.test/api/mcp", "secret", server).call("list_open_calls", {"limit": 1})
        self.assertEqual([call[0] for call in server.calls], ["initialize", "tools/call"])
        self.assertTrue(all(call[3] == "Bearer secret" for call in server.calls))
        api = plans_server()
        RestClient("https://q.example.test/api/v1/", "secret", api).get("/plans?limit=100")
        self.assertEqual(api.calls, [("GET", "https://q.example.test/api/v1/plans?limit=100", "Bearer secret")])
        with self.assertRaises(WorkError) as missing:
            RestClient("https://q.example.test/api/v1", "x", api).get("/plans/unknown")
        self.assertEqual(missing.exception.code, "not_found")
        with self.assertRaises(WorkError) as rejected:
            McpClient("https://q.example.test", "x", FakeServer({}, status=401)).call("list_plans", {})
        self.assertEqual(rejected.exception.code, "rejected")
        refusing = FakeServer({"list_plans": lambda arguments: Exception("read-only connection")})
        with self.assertRaises(WorkError) as tool:
            McpClient("https://q.example.test", "x", refusing).call("list_plans", {})
        self.assertEqual(tool.exception.code, "tool_error")


class Queues(unittest.TestCase):
    source = WorkSource("plans", "Plans", "plans", "https://q.example.test/api/mcp", Path("/x"),
                        "octocat/example", "https://q.example.test")

    def test_plans_read_every_amendment_oldest_first_with_gets_only(self):
        server = plans_server()
        items, truncated = collect_plans(RestClient("https://q.example.test/api/v1", "t", server), self.source)
        self.assertFalse(truncated)
        by_id = {item["id"]: item for item in items}
        self.assertEqual({call[0] for call in server.calls}, {"GET"})
        # A done plan is listed without reading its detail.
        self.assertNotIn("https://q.example.test/api/v1/plans/dec46811-0000", [call[1] for call in server.calls])
        # A plan removed between the list and its read stays listed, without detail.
        self.assertFalse(by_id["9a682a92-0000"]["detail"])
        self.assertEqual(by_id["dec46811-0000"]["phase"], "done")
        self.assertIsNone(by_id["dec46811-0000"]["waiting"])
        live = by_id["5ad53758-0000"]
        self.assertEqual(live["status"], "pr_ready")
        self.assertEqual([update["body"] for update in live["updates"]], ["first", "second"])
        self.assertEqual(live["waiting"], {"who": "operator", "reason": "PR #738 waits on review and merge"})
        self.assertEqual(live["pr_url"], "https://github.com/octocat/example/pull/738")
        self.assertEqual(live["item_url"], "https://q.example.test/plans/5ad53758-0000")
        self.assertEqual(live["preview_url"], "https://example-git-branch.example.test")
        self.assertEqual(live["claimed_by"], "dev@example.test")
        self.assertEqual(live["updated_at"], "2026-10-08T19:05:04Z")
        self.assertEqual(by_id["5d2e1d27-0000"]["waiting"]["reason"], "Blocked, with no reason recorded")
        queued = by_id["06f21214-0000"]
        self.assertEqual((queued["phase"], queued["waiting"], queued["body"]), ("queued", None, "# Plan\nBody"))

    def test_calls_show_follow_ups_and_an_ask_without_a_ruling_waits_on_the_requester(self):
        source = WorkSource("calls", "Calls", "calls", "https://q.example.test/api/mcp", Path("/x"))
        server = calls_server()
        items, _ = collect_calls(McpClient("https://q.example.test", "t", server), source)
        self.assertEqual({call[1] for call in server.calls if call[1]}, {"list_open_calls", "get_call"})
        by_id = {item["id"]: item for item in items}
        blocked = by_id["aaaa-1"]
        self.assertEqual(blocked["updates"], [{"body": "also this", "created_at": "2026-08-12T10:00:00Z", "refs": ["Login screen"]}])
        self.assertEqual(blocked["asks"][0]["ruling"], "The left one")
        self.assertFalse(blocked["asks"][0]["open"])
        self.assertEqual(blocked["waiting"]["reason"], "Blocked, with no reason recorded")
        self.assertEqual(blocked["requester"], "Boss")
        self.assertIsNone(blocked["item_url"])
        waiting = by_id["bbbb-2"]
        self.assertEqual(waiting["waiting"], {"who": "requester", "reason": "Ask awaiting a ruling: Ship on Friday?"})
        self.assertEqual(waiting["claimed_by"], "another session")
        self.assertEqual(by_id["cccc-3"]["waiting"]["who"], "operator")
        self.assertIsNone(by_id["cccc-3"]["pr_url"])


class Feed(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory(prefix="vv-work-")
        self.addCleanup(directory.cleanup)
        self.token = Path(directory.name) / "plans.token"
        self.token.write_text("secret")
        os.chmod(self.token, 0o600)
        self.clock = [0.0]
        self.wall = [NOW]

    def feed(self, opener):
        source = WorkSource("plans", "Plans", "plans", "https://q.example.test/api/v1", self.token)

        class Config:
            work_sources = (source,)
        return WorkFeed(Config(), opener=opener, monotonic=lambda: self.clock[0], wall_clock=lambda: self.wall[0])

    def test_no_sources_is_not_configured(self):
        class Config:
            work_sources = ()
        self.assertEqual(WorkFeed(Config()).snapshot(), {"configured": False, "sources": []})

    def test_a_failure_keeps_the_last_items_stale_and_lost_access_forgets_them(self):
        servers = {"current": plans_server()}
        feed = self.feed(lambda request, timeout: servers["current"](request, timeout))
        first = feed.snapshot(background=False)["sources"][0]
        self.assertEqual((first["error"], first["stale"], len(first["items"])), (None, False, 5))
        # Not due again until the refresh interval has passed.
        servers["current"] = FakeApi({}, status=503)
        self.assertEqual(len(feed.snapshot(background=False)["sources"][0]["items"]), 5)
        self.clock[0] += 301
        self.wall[0] += timedelta(minutes=5)
        failed = feed.snapshot(background=False)["sources"][0]
        self.assertEqual((failed["error"], failed["stale"], failed["available"], len(failed["items"])),
                         ("unavailable", True, True, 5))
        self.clock[0] += 301
        servers["current"] = FakeApi({}, status=401)
        lost = feed.snapshot(background=False)["sources"][0]
        self.assertEqual((lost["error"], lost["available"], lost["items"]), ("rejected", False, []))

    def test_a_missing_credential_shows_its_code_not_an_empty_queue(self):
        self.token.unlink()
        row = self.feed(plans_server()).snapshot(background=False)["sources"][0]
        self.assertEqual((row["error"], row["available"], row["loading"]), ("credential_missing", False, False))


class Page(unittest.TestCase):
    def node(self, source):
        result = subprocess.run(["node", "-e", source], cwd=ROOT, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def test_the_board_columns_are_oldest_first_and_a_waiting_item_is_in_one_column(self):
        result = self.node(r'''
const {workGroups, parseRoute, httpsUrl} = require('./dashboard/static/helpers.js');
const now = Date.parse('2026-10-12T15:00:00Z');
const item = (id, phase, created, updated, waiting) => ({id, phase, created_at: created, updated_at: updated, waiting});
const work = {sources: [{label: 'Plans', kind: 'plans', items: [
  item('q-new', 'queued', '2026-10-08T00:00:00Z', '2026-10-08T00:00:00Z', null),
  item('q-old', 'queued', '2026-04-14T00:00:00Z', '2026-05-15T00:00:00Z', null),
  item('blocked', 'active', '2026-09-25T00:00:00Z', '2026-10-08T00:00:00Z', {who: 'requester', reason: 'Blocked'}),
  item('asked', 'queued', '2026-09-20T00:00:00Z', '2026-10-08T00:00:00Z', {who: 'requester', reason: 'Ask'}),
  item('working', 'active', '2026-08-10T00:00:00Z', '2026-10-08T00:00:00Z', null),
  item('merged', 'done', '2026-05-01T00:00:00Z', '2026-08-12T00:00:00Z', null)]}]};
const g = workGroups(work, now);
const ids = key => g[key].map(row => row.id);
console.log(JSON.stringify({todo: ids('todo'), progress: ids('progress'), waiting: ids('waiting'), done: ids('done'),
  stale: g.stale, source: g.waiting[0].source, kind: g.waiting[0].source_kind, route: parseRoute('#/work').view,
  links: [httpsUrl('https://preview.example.test/x'), httpsUrl('http://preview.example.test'),
          httpsUrl('javascript:alert(1)'), httpsUrl('https://u:p@x.test')]}));
''')
        self.assertEqual(result["todo"], ["q-old", "q-new"])
        self.assertEqual(result["progress"], ["working"])
        self.assertEqual(result["waiting"], ["asked", "blocked"])
        self.assertEqual(result["done"], ["merged"])
        # Only open items count as not updated in two weeks; the merged one does not.
        self.assertEqual(result["stale"], 1)
        self.assertEqual((result["source"], result["kind"]), ("Plans", "plans"))
        self.assertEqual(result["route"], "work")
        self.assertEqual(result["links"], ["https://preview.example.test/x", None, None, None])

    def test_the_work_button_starts_hidden_and_the_view_has_a_title(self):
        index = (ROOT / "dashboard/static/index.html").read_text()
        self.assertIn('<button data-view="work" hidden>Work</button>', index)
        app = (ROOT / "dashboard/static/app.js").read_text()
        self.assertIn('work: "Work"', app)
        self.assertIn('.hidden = !snapshot.work?.configured', app)


if __name__ == "__main__":
    unittest.main()
