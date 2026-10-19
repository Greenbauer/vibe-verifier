"""The Work view's supabase-todos source: one PostgREST read of a person's to-do table, and a key that
never reaches the page."""

import io
import json
import os
import subprocess
import tempfile
import unittest
import urllib.error
from datetime import timedelta
from pathlib import Path

from dashboard.config import ConfigError, WorkSource, load_config
from dashboard.work import SupabaseClient, WorkError, WorkFeed, collect_todos
from test_dashboard_config_telemetry import config_value
from test_dashboard_work import NOW, ROOT, Reply

KEY = "placeholder-todos-key"
TODOS_URL = "https://project-ref.supabase.example.test"


def todo_row(todo_id, status, **extra):
    row = {"id": todo_id, "title": "To-do " + todo_id, "notes": None, "status": status, "area": "personal",
           "source": "chat", "source_url": None, "due": None, "created_at": "2026-10-01T09:00:00+00:00",
           "updated_at": "2026-10-02T09:00:00+00:00", "done_at": None}
    row.update(extra)
    return row


class FakeTable:
    """Answers GETs like PostgREST under a Supabase project: a JSON array, or an HTTP error."""

    def __init__(self, rows, status=200, error=None):
        self.rows, self.status, self.error, self.calls = rows, status, error, []

    def __call__(self, request, timeout):
        self.calls.append((request.get_method(), request.full_url, request.get_header("Authorization"),
                           request.get_header("Apikey")))
        if self.error:
            raise self.error
        if self.status != 200:
            raise urllib.error.HTTPError(request.full_url, self.status, "no", {}, io.BytesIO())
        return Reply(json.dumps(self.rows))


def todos_table():
    return FakeTable([
        todo_row("t-todo", "todo", area="errands", due="2026-10-20", notes="Call back about the <b>quote</b>",
                 source_url="https://chat.example.test/thread/1"),
        todo_row("t-doing", "in_progress", area="home", source_url="http://insecure.example.test/x"),
        todo_row("t-wait", "waiting", area=None, notes=""),
        todo_row("t-done-recent", "done", done_at="2026-09-29T15:00:00Z"),
        # Older than 14 days, or done with no done_at: the filter should have left these out; they never show.
        todo_row("t-done-old", "done", done_at="2026-09-27T15:00:00Z"),
        todo_row("t-done-undated", "done"),
        todo_row("t-odd", "someday"),
    ])


class Todos(unittest.TestCase):
    source = WorkSource("todos", "Zach's to-dos", "supabase-todos", TODOS_URL, Path("/x"), table="todos")

    def setUp(self):
        directory = tempfile.TemporaryDirectory(prefix="vv-work-")
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)

    def load(self, sources):
        path = self.root / "dashboard.json"
        path.write_text(json.dumps({**config_value(), "work_sources": sources}), encoding="utf-8")
        os.chmod(path, 0o600)
        return load_config(path)

    def config(self, **extra):
        value = {"id": "todos", "label": "Zach's to-dos", "kind": "supabase-todos", "url": TODOS_URL,
                 "table": "todos", "token_file": "/etc/vibe-dashboard/example/todos.token"}
        value.update(extra)
        return value

    def test_the_source_loads_next_to_a_plans_source_and_bad_shapes_are_refused(self):
        plans = {"id": "plans", "label": "Plans", "kind": "plans", "endpoint": "https://q.example.test/api/v1",
                 "token_file": "/etc/vibe-dashboard/example/plans.token"}
        config = self.load([plans, self.config()])
        self.assertEqual(config.work_sources[1], WorkSource(
            "todos", "Zach's to-dos", "supabase-todos", TODOS_URL, Path("/etc/vibe-dashboard/example/todos.token"),
            table="todos"))
        bad = [self.config(url=TODOS_URL + "/rest/v1"), self.config(url="http://project-ref.supabase.example.test"),
               self.config(table="todos?select=*"), self.config(table=None), self.config(endpoint=TODOS_URL),
               self.config(repository="octocat/example"), self.config(key="inline-secret"),
               self.config(token_file="todos.token")]
        for source in bad:
            with self.subTest(source=source), self.assertRaises(ConfigError):
                self.load([source])
        with self.assertRaises(ConfigError):
            self.load([self.config(id="t%d" % n) for n in range(9)])

    def test_one_get_sends_the_key_as_apikey_and_bearer_and_asks_for_open_and_recently_done_rows(self):
        table = todos_table()
        collect_todos(SupabaseClient(TODOS_URL + "/", KEY, table), self.source, NOW)
        self.assertEqual(len(table.calls), 1)
        method, url, authorization, apikey = table.calls[0]
        self.assertEqual((method, authorization, apikey), ("GET", "Bearer " + KEY, KEY))
        self.assertTrue(url.startswith(TODOS_URL + "/rest/v1/todos?select=id,title,notes,status,area,source_url,due,"
                                       "created_at,updated_at,done_at&"), url)
        # NOW less 14 days, as PostgREST's or filter: the rows not done, and done ones since then.
        self.assertIn("&or=(status.neq.done,done_at.gte.2026-09-28T15%3A00%3A00Z)&order=created_at.asc&limit=100", url)
        self.assertNotIn(KEY, url)

    def test_statuses_map_to_the_four_columns_with_area_due_and_source_link(self):
        items, truncated = collect_todos(SupabaseClient(TODOS_URL, KEY, todos_table()), self.source, NOW)
        self.assertFalse(truncated)
        by_id = {item["id"]: item for item in items}
        self.assertEqual({key: (item["phase"], item["waiting"]) for key, item in by_id.items()}, {
            "t-todo": ("queued", None), "t-doing": ("active", None),
            "t-wait": ("active", {"who": "operator", "reason": "Marked waiting"}), "t-done-recent": ("done", None)})
        first = by_id["t-todo"]
        self.assertEqual((first["label"], first["title"], first["due"], first["item_url"], first["body"], first["detail"]),
                         ("errands", "To-do t-todo", "2026-10-20", "https://chat.example.test/thread/1",
                          "Call back about the <b>quote</b>", True))
        self.assertEqual((first["created_at"], first["updated_at"]), ("2026-10-01T09:00:00Z", "2026-10-02T09:00:00Z"))
        # Only an https source link becomes a link; a row with no area is labelled "to-do".
        self.assertIsNone(by_id["t-doing"]["item_url"])
        self.assertEqual((by_id["t-wait"]["label"], by_id["t-wait"]["due"], by_id["t-wait"]["detail"]), ("to-do", None, False))

    def test_done_rows_show_only_inside_the_14_day_window(self):
        items, _ = collect_todos(SupabaseClient(TODOS_URL, KEY, todos_table()), self.source, NOW)
        done = {item["id"] for item in items if item["phase"] == "done"}
        self.assertEqual(done, {"t-done-recent"})
        later, _ = collect_todos(SupabaseClient(TODOS_URL, KEY, todos_table()), self.source, NOW + timedelta(days=2))
        self.assertNotIn("t-done-recent", {item["id"] for item in later})

    def test_a_full_page_reads_as_truncated_and_a_non_list_is_an_invalid_response(self):
        full = FakeTable([todo_row("t%03d" % n, "todo") for n in range(100)])
        self.assertTrue(collect_todos(SupabaseClient(TODOS_URL, KEY, full), self.source, NOW)[1])
        with self.assertRaises(WorkError) as shape:
            collect_todos(SupabaseClient(TODOS_URL, KEY, FakeTable({"message": "no"})), self.source, NOW)
        self.assertEqual(shape.exception.code, "invalid_response")


class TodosFeed(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory(prefix="vv-work-")
        self.addCleanup(directory.cleanup)
        self.token = Path(directory.name) / "todos.token"
        self.token.write_text(KEY + "\n")
        os.chmod(self.token, 0o600)

    def feed(self, opener):
        source = WorkSource("todos", "Zach's to-dos", "supabase-todos", TODOS_URL, self.token, table="todos")

        class Config:
            work_sources = (source,)
        return WorkFeed(Config(), opener=opener, monotonic=lambda: 0.0, wall_clock=lambda: NOW)

    def render(self, snapshot):
        script = r"""
const fs = require('fs');
class Node {
  constructor(tag) { Object.assign(this, {tag, children: [], attrs: {}, className: '', textContent: '', hidden: false}); }
  append(...items) { this.children.push(...items); }
  replaceChildren(...items) { this.children = items; }
  setAttribute(key, value) { this.attrs[key] = value; }
  addEventListener() {}
}
global.Node = Node;
global.document = {createElement: tag => new Node(tag), createTextNode: data => ({data})};
global.window = global;
global.VV = require('./dashboard/static/helpers.js');
require('./dashboard/static/work.js');
const content = new Node('main');
const banner = text => Object.assign(new Node('div'), {className: 'source-banner', textContent: text});
VVWork.render(content, {work: JSON.parse(fs.readFileSync(0, 'utf8'))},
  {heading: () => new Node('h1'), empty: (a, b) => banner(a + ' ' + b), sourceBanner: banner});
console.log(JSON.stringify(content));
"""
        result = subprocess.run(["node", "-e", script], cwd=ROOT, input=json.dumps(snapshot), capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout

    def test_the_board_shows_the_rows_and_never_the_key(self):
        snapshot = self.feed(todos_table()).snapshot(background=False)
        row = snapshot["sources"][0]
        self.assertEqual((row["kind"], row["label"], row["error"], len(row["items"])), ("supabase-todos", "Zach's to-dos", None, 4))
        page = self.render(snapshot)
        for text in ("Zach's to-dos", "To-do t-todo", "due 2026-10-20", "errands", "https://chat.example.test/thread/1",
                     "Marked waiting", "To do"):
            self.assertIn(text, page)
        # A to-do has no claim, so no card says "unclaimed".
        self.assertNotIn("unclaimed", page)
        self.assertNotIn(KEY, json.dumps(snapshot))
        self.assertNotIn(KEY, page)

    def test_a_missing_key_or_a_failed_read_is_a_source_error_on_the_board_without_the_key(self):
        cases = [(FakeTable([], status=503), "unavailable", "The source answered with an error."),
                 (FakeTable([], status=401), "rejected", "The source refused the credential."),
                 (FakeTable([], error=urllib.error.URLError("connection refused for " + KEY)), "unreachable",
                  "The source could not be reached.")]
        for opener, code, message in cases:
            with self.subTest(code=code):
                snapshot = self.feed(opener).snapshot(background=False)
                self.assertEqual((snapshot["sources"][0]["error"], snapshot["sources"][0]["items"]), (code, []))
                page = self.render(snapshot)
                self.assertIn("Zach's to-dos. " + message, page)
                self.assertNotIn(KEY, json.dumps(snapshot) + page)
        self.token.unlink()
        table = todos_table()
        snapshot = self.feed(table).snapshot(background=False)
        self.assertEqual(snapshot["sources"][0]["error"], "credential_missing")
        self.assertEqual(table.calls, [])
        self.assertIn("No credential file on this host yet.", self.render(snapshot))



if __name__ == "__main__":
    unittest.main()
