"""Latest push kind, merge-ready titles, and the unresolved-comment count."""
import copy
import json
import subprocess
import unittest
from datetime import datetime, timezone

from dashboard.config import BotDefinition, Config
from dashboard.gh_api import ApiError, GitHubAPI
from dashboard.github import GitHubCollector
from dashboard.pull_signals import (
    classify_push, comment_count, load_signals, merge_ready, pull_face, read_pull_page,
    signals_from_node, thread_summary,
)

ROOT_UI = __import__("pathlib").Path(__file__).resolve().parent.parent
NOW = datetime(2026, 9, 30, 15, 0, tzinfo=timezone.utc)
REPO = "octocat/example"
SHA = "a" * 40


def pushed(headline, at, parents=1, committed=None):
    return {"headline": headline, "pushed_at": at, "committed_at": committed or at, "parents": parents}


class PushKind(unittest.TestCase):
    def test_the_newest_push_is_labeled_and_older_pushes_are_ignored(self):
        earlier, later = "2026-09-29T12:00:00Z", "2026-09-30T14:00:00Z"
        rows = [pushed("feat: add the gate", earlier), pushed("fix(scope): close it", later),
                pushed("Merge branch 'main' into fix/gate", later, parents=2)]
        self.assertEqual(classify_push(rows, "main"), {"pushed_at": later, "kind": "bug fix"})
        self.assertEqual(classify_push([pushed("feat(quote)!: dates", later)], "main")["kind"], "feature")
        self.assertEqual(classify_push([pushed("feature: dates", later)], "main")["kind"], "feature")
        self.assertEqual(classify_push([pushed("docs: notes", later)], "main")["kind"], "docs")
        self.assertEqual(classify_push([pushed("hotfix: stop", later)], "main")["kind"], "bug fix")
        self.assertEqual(classify_push([pushed("wip: scratch", later)], "main")["kind"], "work in progress")
        self.assertEqual(classify_push([pushed("please review", later)], "main")["kind"], "update")
        self.assertEqual(classify_push([pushed("fix:", later)], "main")["kind"], "update")
        self.assertIsNone(classify_push([], "main"))

    def test_a_base_merge_is_a_sync_unless_the_push_also_has_other_work(self):
        at = "2026-09-30T14:10:00Z"
        sync = pushed("Merge remote-tracking branch 'origin/main' into feat/quote", at, parents=2)
        self.assertEqual(classify_push([pushed("feat: add", "2026-09-29T12:00:00Z"), sync], "main"),
                         {"pushed_at": at, "kind": "sync with main"})
        self.assertEqual(classify_push([sync, pushed("fix: close", at)], "main")["kind"], "bug fix")
        develop = pushed("Merge branch 'develop' into feat/quote", at, parents=2)
        self.assertEqual(classify_push([develop], "develop")["kind"], "sync with develop")
        self.assertEqual(classify_push([develop], "main")["kind"], "merge")
        self.assertEqual(classify_push([pushed("sync with main", at)], "main")["kind"], "sync with main")

    def test_without_a_push_time_the_newest_commit_time_stands_in(self):
        row = {"headline": "fix: close", "pushed_at": None, "committed_at": "2026-09-30T13:00:00Z", "parents": 1}
        older = {"headline": "feat: add", "pushed_at": None, "committed_at": "2026-09-29T13:00:00Z", "parents": 1}
        self.assertEqual(classify_push([older, row], "main"),
                         {"pushed_at": "2026-09-30T13:00:00Z", "kind": "bug fix"})
        self.assertIsNone(classify_push([{"headline": "fix: x", "pushed_at": None, "committed_at": None, "parents": 1}], "main"))


def thread(resolved, replies):
    return {"isResolved": resolved, "comments": {"totalCount": replies}}


class ReviewThreads(unittest.TestCase):
    def test_unresolved_threads_and_a_missing_reply_block_a_settled_pull(self):
        settled = thread_summary([thread(True, 2)], complete=True)
        self.assertEqual(settled["unresolved_comments"], 0)
        self.assertTrue(settled["threads_settled"])
        open_thread = thread_summary([thread(False, 2), thread(True, 2)], complete=True)
        self.assertEqual(open_thread["unresolved_comments"], 1)
        self.assertFalse(open_thread["threads_settled"])
        unreplied = thread_summary([thread(True, 1)], complete=True)
        self.assertEqual(unreplied["unresolved_comments"], 0)
        self.assertFalse(unreplied["threads_settled"])
        self.assertTrue(thread_summary([], complete=True)["threads_settled"])
        partial = thread_summary([thread(False, 2)], complete=False)
        self.assertFalse(partial["comments_complete"])
        self.assertFalse(partial["threads_settled"])
        broken = thread_summary([{"isResolved": "yes"}], complete=True)
        self.assertIsNone(broken["unresolved_comments"])
        self.assertFalse(broken["threads_settled"])

    def test_a_graphql_node_keeps_the_push_when_the_thread_list_is_unusable(self):
        node = {"number": 3, "baseRefName": "main", "reviewThreads": {"pageInfo": {"hasNextPage": False},
                "nodes": [thread(True, 2)]},
                "commits": {"nodes": [{"commit": {"messageHeadline": "fix: close",
                           "pushedDate": "2026-09-30T14:00:00Z", "committedDate": "2026-09-30T13:00:00Z",
                           "parents": {"totalCount": 1}}}]}}
        parsed = signals_from_node(node)
        self.assertEqual(parsed["push"], {"pushed_at": "2026-09-30T14:00:00Z", "kind": "bug fix"})
        self.assertEqual(parsed["review_threads"], 1)
        broken = signals_from_node({**node, "reviewThreads": None})
        self.assertIsNone(broken["unresolved_comments"])
        self.assertEqual(broken["push"]["kind"], "bug fix")


class MergeReady(unittest.TestCase):
    def test_green_means_clean_not_a_draft_every_check_green_and_every_thread_settled(self):
        ready = dict(evidence_available=True, draft=False, merge_state="clean",
                     threads_settled=True, categories=["success", "skipped"])
        self.assertTrue(merge_ready(**ready))
        for change in ({"draft": True}, {"merge_state": "behind"}, {"merge_state": "unstable"},
                       {"merge_state": None}, {"evidence_available": False}, {"threads_settled": False},
                       {"categories": ["success", "failed"]}, {"categories": ["pending"]},
                       {"categories": ["skipped"]}, {"categories": []}):
            self.assertFalse(merge_ready(**{**ready, **change}), change)

    def test_a_missing_read_is_not_a_green_title_or_a_zero_comment_count(self):
        face = pull_face(None, evidence_available=True, draft=False, merge_state="clean",
                         categories=["success"], comments=None)
        self.assertFalse(face["merge_ready"])
        self.assertIsNone(face["unresolved_comments"])
        self.assertIsNone(face["push"])
        self.assertIsNone(comment_count(None))
        self.assertIsNone(comment_count({"comments": 1}))
        self.assertEqual(comment_count({"comments": 1, "review_comments": 2}), 3)

    def test_a_bad_graphql_page_is_rejected_and_a_cursor_is_kept_only_when_it_is_safe(self):
        node = {"number": 3, "baseRefName": "main", "reviewThreads": {"pageInfo": {"hasNextPage": False}, "nodes": []},
                "commits": {"nodes": []}}
        page = {"data": {"repository": {"pullRequests": {"pageInfo": {"hasNextPage": True, "endCursor": "abc_=/+"},
                                                         "nodes": [node]}}}}
        found, cursor = read_pull_page(page)
        self.assertEqual(list(found), [3])
        self.assertEqual(cursor, "abc_=/+")
        page["data"]["repository"]["pullRequests"]["pageInfo"]["endCursor"] = "bad cursor"
        self.assertEqual(read_pull_page(page)[1], None)
        self.assertIsNone(read_pull_page({"errors": [{"message": "no"}]}))
        self.assertIsNone(read_pull_page({"data": {"repository": None}}))


def http(status, body="", extra=()):
    lines = ["Content-Type: application/json", *extra]
    return "HTTP/2.0 %d Status\n%s\r\n%s" % (status, "".join(line + "\r\n" for line in lines), body)


class GraphQLTransport(unittest.TestCase):
    def test_one_graphql_read_counts_as_one_budget_unit(self):
        commands = []

        def runner(command, **kwargs):
            commands.append(command)
            return subprocess.CompletedProcess(command, 0, http(200, '{"data":{"repository":null}}',
                                                                ("X-Ratelimit-Remaining: 40",)), "")
        api = GitHubAPI(runner=runner)
        self.assertEqual(api.graphql("query { viewer { login } }", {"owner": "octocat"}),
                         {"data": {"repository": None}})
        self.assertEqual(api.calls, 1)
        self.assertEqual(api.lowest_remaining, 40)
        self.assertEqual(commands[0][:4], ["gh", "api", "--include", "graphql"])
        self.assertIn("owner=octocat", commands[0])
        self.assertTrue(any(part.startswith("query=") for part in commands[0]))

    def test_a_graphql_rate_limit_pauses_only_graphql(self):
        def runner(command, **kwargs):
            if command[3] == "graphql":
                return subprocess.CompletedProcess(
                    command, 1, http(403, "{}", ("X-Ratelimit-Remaining: 0", "X-Ratelimit-Reset: 700")),
                    "gh: API rate limit exceeded")
            return subprocess.CompletedProcess(command, 0, http(200, "{}"), "")
        api = GitHubAPI(runner=runner, clock=lambda: 100)
        with self.assertRaisesRegex(ApiError, "rate_limited"):
            api.graphql("query { x }", {})
        self.assertEqual(api.one("repos/o/r/pulls/1"), {})
        with self.assertRaisesRegex(ApiError, "rate_limited"):
            api.graphql("query { x }", {})
        self.assertEqual(api.calls, 2)

    def test_graphql_rejects_a_non_object_body_and_obeys_the_budget(self):
        api = GitHubAPI(max_calls=0)
        with self.assertRaisesRegex(ApiError, "request_budget_exhausted"):
            api.graphql("query { x }", {})
        api = GitHubAPI(runner=lambda command, **kwargs: subprocess.CompletedProcess(command, 0, http(200, "[1]"), ""))
        with self.assertRaisesRegex(ApiError, "invalid_response"):
            api.graphql("query { x }", {})
        with self.assertRaisesRegex(ApiError, "invalid_response"):
            GitHubAPI().graphql("query { x }", {"nope!": "x"})


class RecordingAPI:
    def __init__(self, items, ones, graphql):
        self.item_values, self.one_values, self._graphql, self.calls = items, ones, graphql, []

    def items(self, endpoint, key=None):
        value = self.item_values[endpoint]
        if isinstance(value, Exception):
            raise value
        return copy.deepcopy(value)

    def one(self, endpoint):
        return copy.deepcopy(self.one_values[endpoint])

    def graphql(self, query, variables):
        self.calls.append(variables.get("cursor"))
        value = self._graphql(query, variables) if callable(self._graphql) else self._graphql
        if isinstance(value, Exception):
            raise value
        return copy.deepcopy(value)


def config():
    return Config("octocat", (REPO,), {name: BotDefinition(name + ".yml", (name,))
                                        for name in ("reviewer", "explorer", "verifier")}, None)


def quiet_api(pull, graphql):
    paths = {
        "list": f"repos/{REPO}/pulls?state=open&per_page=100",
        "suites": f"repos/{REPO}/commits/{SHA}/check-suites?per_page=100",
        "checks": f"repos/{REPO}/commits/{SHA}/check-runs?per_page=100&filter=all",
        "runs": f"repos/{REPO}/actions/runs?head_sha={SHA}&per_page=100",
        "status": f"repos/{REPO}/commits/{SHA}/status",
        "pull": f"repos/{REPO}/pulls/3",
    }
    return RecordingAPI(
        {paths["list"]: [{"number": 3, "title": "Improve", "user": {"login": "octocat"},
                          "created_at": "2026-09-29T15:00:00Z", "updated_at": "2026-09-30T14:00:00Z",
                          "head": {"sha": SHA}, "draft": False, "html_url": f"https://github.com/{REPO}/pull/3"}],
         paths["suites"]: [], paths["checks"]: [], paths["runs"]: []},
        {paths["status"]: {"statuses": [{"id": 5, "context": "ci", "state": "success",
                                         "created_at": "2026-09-30T14:40:00Z", "updated_at": "2026-09-30T14:41:00Z"}]},
         paths["pull"]: pull}, graphql)


def payload(threads, commits, more=False):
    return {"data": {"repository": {"pullRequests": {
        "pageInfo": {"hasNextPage": more, "endCursor": "abc" if more else None},
        "nodes": [{"number": 3, "baseRefName": "main",
                   "reviewThreads": {"pageInfo": {"hasNextPage": False}, "nodes": threads},
                   "commits": {"nodes": commits}}]}}}}


def commit(headline, at, parents=1):
    return {"commit": {"messageHeadline": headline, "pushedDate": at, "committedDate": at,
                       "parents": {"totalCount": parents}}}


class CollectedPull(unittest.TestCase):
    def test_a_clean_green_pull_with_replied_threads_is_merge_ready_and_names_the_sync(self):
        pull = {"head": {"sha": SHA}, "draft": False, "mergeable_state": "clean", "comments": 0, "review_comments": 2}
        graphql = payload([thread(True, 2)], [commit("Merge branch 'main' into fix/gate", "2026-09-30T14:10:00Z", 2)])
        result = GitHubCollector(config(), quiet_api(pull, graphql), clock=lambda: NOW)._repository(
            REPO, {"subscription": "subscribed"})["pulls"][0]
        self.assertTrue(result["merge_ready"])
        self.assertEqual(result["push"], {"pushed_at": "2026-09-30T14:10:00Z", "kind": "sync with main"})
        self.assertEqual(result["unresolved_comments"], 0)
        self.assertEqual(result["comment_count"], 2)

    def test_an_unresolved_thread_and_a_failed_read_do_not_invent_readiness(self):
        pull = {"head": {"sha": SHA}, "draft": False, "mergeable_state": "clean", "comments": 1, "review_comments": 1}
        graphql = payload([thread(False, 2)], [commit("fix: close", "2026-09-30T14:00:00Z")])
        open_pull = GitHubCollector(config(), quiet_api(pull, graphql), clock=lambda: NOW)._repository(
            REPO, {"subscription": "subscribed"})["pulls"][0]
        self.assertFalse(open_pull["merge_ready"])
        self.assertEqual(open_pull["push"]["kind"], "bug fix")
        self.assertEqual(open_pull["unresolved_comments"], 1)
        failed = GitHubCollector(config(), quiet_api(pull, ApiError("unavailable")), clock=lambda: NOW)._repository(
            REPO, {"subscription": "subscribed"})["pulls"][0]
        self.assertTrue(failed["evidence_available"])
        self.assertFalse(failed["merge_ready"])
        self.assertIsNone(failed["push"])
        self.assertIsNone(failed["unresolved_comments"])

    def test_a_failed_later_page_drops_the_partial_read_and_a_check_failure_keeps_the_push(self):
        calls = {"n": 0}

        def graphql(query, variables):
            calls["n"] += 1
            if calls["n"] == 1:
                return payload([], [commit("fix: close", "2026-09-30T14:00:00Z")], more=True)
            raise ApiError("unavailable")
        pull = {"head": {"sha": SHA}, "draft": False, "mergeable_state": "clean", "comments": 0, "review_comments": 0}
        api = quiet_api(pull, graphql)
        result = GitHubCollector(config(), api, clock=lambda: NOW)._repository(REPO, {"subscription": "subscribed"})
        self.assertIsNone(result["pulls"][0]["push"])
        self.assertEqual(api.calls, [None, "abc"])
        detail = quiet_api(pull, payload([], [commit("fix: close", "2026-09-30T14:00:00Z")]))
        detail.item_values[f"repos/{REPO}/commits/{SHA}/check-suites?per_page=100"] = ApiError("unavailable")
        failed = GitHubCollector(config(), detail, clock=lambda: NOW)._repository(REPO, {"subscription": "subscribed"})
        row = failed["pulls"][0]
        self.assertFalse(row["evidence_available"])
        self.assertEqual(row["push"]["kind"], "bug fix")
        self.assertFalse(row["merge_ready"])
        self.assertEqual(load_signals(RecordingAPI({}, {}, ApiError("unavailable")), REPO), {})


class CommentMark(unittest.TestCase):
    def test_the_message_icon_shows_unresolved_comments_beside_the_meter(self):
        source = r'''
const fs=require('fs');
const helpers=require('./dashboard/static/helpers.js');
const app=fs.readFileSync('./dashboard/static/app.js','utf8');
const slice=app.slice(app.indexOf('  function messageIcon('), app.indexOf('  function renderPrRows('));
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
const progress=new Function('el','stepTotals','meterSegments','meterLabel','unresolvedMark','runningStepNames', slice+'return progress;')(
  el, helpers.stepTotals, helpers.meterSegments, helpers.meterLabel, helpers.unresolvedMark, helpers.runningStepNames);
const runs=[{step_summary:{known:true, completed:4, total:4, remaining:0,
  counts:{success:4, failed:0, skipped:0, cancelled:0, pending:0, unknown:0}}}];
const base={runs, checks:[], statuses:[], expected:[]};
function marks(node) {
  const found=[];
  (function walk(item) {
    if (!item || typeof item==='string') return;
    if (String(item.attrs&&item.attrs.class||'').includes('comment-mark')) found.push(item);
    (item.children||[]).forEach(walk);
  })(node);
  return found;
}
const text=item=>item.children.filter(child=>typeof child==='string');
const open=progress({...base, unresolved_comments:2, review_threads:3, comments_complete:true, comment_count:4});
const none=progress(base);
const clear=progress({...base, unresolved_comments:0, review_threads:2, comments_complete:true, comment_count:2});
const partial=progress({...base, unresolved_comments:2, review_threads:100, comments_complete:false, comment_count:100});
const unknown=progress({unresolved_comments:1, review_threads:1, comments_complete:true, comment_count:1});
function headings(node) {
  const found=[];
  (function walk(item) {
    if (!item || typeof item==='string') return;
    if (item.tag==='b') found.push(text(item).join(''));
    (item.children||[]).forEach(walk);
  })(node);
  return found;
}
const running=progress({...base, runs:[{...runs[0], jobs:[
  {steps:[{name:'Build', status:'in_progress'},{name:'Lint', status:'completed'}]},
  {steps:[{name:'Walk', status:'in_progress'}]}
]}]});
console.log(JSON.stringify({
  open: marks(open).map(item=>({class:item.attrs.class, title:item.attrs.title, text:text(item)})),
  none: marks(none).length,
  clear: marks(clear).length,
  partial: marks(partial).map(text),
  unknown: marks(unknown).map(text),
  besideMeter: open.children.some(item=>item.attrs&&item.attrs.class==='progress-meter-row' && marks(item).length===1),
  hidden: helpers.unresolvedMark({unresolved_comments:null, review_threads:null, comment_count:null}),
  noComments: helpers.unresolvedMark({unresolved_comments:0, review_threads:0, comment_count:0}),
  resolved: helpers.unresolvedMark({unresolved_comments:0, review_threads:4, comments_complete:true, comment_count:6}),
  negative: helpers.unresolvedMark({unresolved_comments:-1, review_threads:1, comment_count:1}),
  idleHeadings: headings(none),
  runningHeadings: headings(running),
  names: helpers.runningStepNames({runs:[{jobs:[{steps:[{name:'Build', status:'in_progress'},{status:'in_progress'},{name:'Done', status:'completed'}]}]}]})
}));
'''
        result = subprocess.run(["node", "-e", source], cwd=ROOT_UI, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        parsed = json.loads(result.stdout)
        self.assertEqual(parsed["open"], [{"class": "comment-mark", "title": "2 unresolved comments", "text": ["2"]}])
        self.assertEqual(parsed["none"], 0)
        self.assertEqual(parsed["clear"], 0)
        self.assertEqual(parsed["partial"], [["2+"]])
        self.assertEqual(parsed["unknown"], [["1"]])
        self.assertTrue(parsed["besideMeter"])
        self.assertIsNone(parsed["hidden"])
        self.assertIsNone(parsed["noComments"])
        self.assertIsNone(parsed["resolved"])
        self.assertIsNone(parsed["negative"])
        css = (ROOT_UI / "dashboard/static/styles.css").read_text()
        self.assertIn(".pr-identity b.merge-ready { color: var(--green); }", css)
        self.assertIn(".progress-copy .comment-mark {", css)
        self.assertNotIn("comment-mark.is-clear", css)
        self.assertEqual(parsed["idleHeadings"], [])
        self.assertEqual(parsed["runningHeadings"], ["Build · Walk"])
        self.assertEqual(parsed["names"], ["Build"])
        self.assertIn(".step-meter { height: 8px;", css)


if __name__ == "__main__":
    unittest.main()
