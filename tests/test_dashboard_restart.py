"""A restart, a failed read and lost access: the last reading stays with its age, or is deleted."""

import copy
import io
import json
import subprocess
import tempfile
import threading
import unittest
from contextlib import redirect_stderr
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from dashboard.config import AgentDefinition, BotDefinition, Config
from dashboard.gh_api import ApiError
from dashboard.live_service import LiveService
from dashboard.server import make_server
from dashboard.service import DashboardService
from dashboard.state_store import StateStore

NOW = datetime(2026, 10, 8, 15, 0, tzinfo=timezone.utc)
ROOT = Path(__file__).resolve().parent.parent
REPO, OTHER = "octocat/example", "octocat/second"
BOTS = {role: BotDefinition(role + ".yml", (role,)) for role in ("reviewer", "explorer", "verifier")}


def config(**changes):
    return Config("octocat", (REPO, OTHER), BOTS, None, **changes)


def reading(at=NOW, repositories=(REPO, OTHER)):
    run = {"bot": "reviewer", "repository": REPO, "run_id": 1, "job_id": 1, "status": "completed",
           "conclusion": "success", "category": "success", "completed_at": (at - timedelta(minutes=5)).isoformat()}
    role = {"state": "idle", "state_source": "github_actions", "sampled_at": at.isoformat(),
            "history_sampled_at": at.isoformat(), "active": [], "recent_2h": [run], "recent_7d": [run],
            "latest_failure": None, "coverage": {"active": "complete", "history": "complete"}}
    rows = [{"repository": name, "subscription": "subscribed", "sampled_at": at.isoformat(), "errors": [],
             "pulls": [{"number": index + 1, "title": "Change %d" % (index + 1), "merge_ready": True,
                        "checks_sampled_at": at.isoformat()}]}
            for index, name in enumerate(repositories)]
    return {"owner": "octocat", "sampled_at": at.isoformat(), "repositories": rows,
            "coverage": {"selected": 2, "readable": len(rows), "label": "Selected repositories", "inventory": {}},
            "bots": {"partial": False, "roles": {"reviewer": role}, "errors": []},
            "errors": [], "partial": False, "api": {"calls": 55, "max_calls": 200}}


class Collector:
    class API:
        max_calls = 200

    api = API()

    def __init__(self, *values):
        self.values = list(values)
        self.cleared = 0
        self.hold = None

    def collect(self, inventory=None):
        if self.hold:
            self.hold.wait(2)
        value = self.values.pop(0)
        if isinstance(value, Exception):
            raise value
        return copy.deepcopy(value)

    def clear_private_cache(self):
        self.cleared += 1


class RestartCase(unittest.TestCase):
    service_class = DashboardService

    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.wall = NOW

    def store(self):
        return StateStore(self.folder.name, config(), clock=lambda: self.wall)

    def files(self):
        return sorted(path.name for path in (Path(self.folder.name) / "octocat").iterdir())

    def start(self, *values, settings=None):
        collector = Collector(*values)
        service = self.service_class(settings or config(), collector=collector, monotonic=lambda: 1000.0,
                                     wall_clock=lambda: self.wall, store=self.store())
        return service, collector

    def restarted(self, *values):
        """A service that read once and was stopped, then the one started in its place."""
        first, _ = self.start(reading())
        first.snapshot(force=True)
        self.wall = NOW + timedelta(seconds=40)
        return self.start(*values)


class ServesTheLastReading(RestartCase):
    def test_a_restart_serves_the_previous_reading_at_once_marked_old(self):
        service, collector = self.restarted(reading(NOW + timedelta(seconds=40)))
        collector.hold = threading.Event()
        github = service.snapshot(nonblocking=True)["github"]
        self.assertTrue(github["restored"] and github["stale"] and github["refreshing"])
        self.assertEqual(github["sampled_at"], NOW.isoformat())
        self.assertEqual([row["pulls"][0]["title"] for row in github["repositories"]], ["Change 1", "Change 2"])
        self.assertEqual([row["stale"] for row in github["repositories"]], [True, True])
        self.assertEqual([row["pulls"][0]["merge_ready"] for row in github["repositories"]], [False, False])
        role = github["bots"]["roles"]["reviewer"]
        self.assertTrue(role["stale"])
        self.assertEqual((role["state"], role["sampled_at"], len(role["recent_2h"])), ("idle", NOW.isoformat(), 1))
        collector.hold.set()
        fresh = service.snapshot()["github"]
        self.assertNotIn("restored", fresh)
        self.assertFalse(fresh["stale"])
        self.assertEqual([row["stale"] for row in fresh["repositories"]], [False, False])
        self.assertTrue(fresh["repositories"][0]["pulls"][0]["merge_ready"])

    def test_a_first_pass_that_fails_after_a_restart_keeps_the_previous_reading(self):
        for code in ("rate_limited", "unavailable", "request_budget_exhausted", "invalid_response"):
            with self.subTest(code=code):
                service, _ = self.restarted(ApiError(code))
                github = service.snapshot(force=True)["github"]
                self.assertTrue(github["restored"] and github["stale"])
                self.assertEqual(github["errors"], [{"code": code}])
                self.assertEqual([len(row["pulls"]) for row in github["repositories"]], [1, 1])
                self.assertEqual(self.files(), ["reading.json.z"])

    def test_a_repository_the_first_pass_misses_keeps_its_previous_row(self):
        partial = reading(NOW + timedelta(seconds=40), repositories=(REPO,))
        partial["errors"] = [{"repository": OTHER, "code": "rate_limited"}]
        service, _ = self.restarted(partial)
        rows = service.snapshot(force=True)["github"]["repositories"]
        self.assertEqual([(row["repository"], row["stale"], len(row["pulls"])) for row in rows],
                         [(REPO, False, 1), (OTHER, True, 1)])
        self.assertEqual(rows[1]["source_error"], "rate_limited")
        self.assertFalse(rows[1]["pulls"][0]["merge_ready"])

    def test_a_first_start_shows_loading_and_then_keeps_what_it_read(self):
        service, collector = self.start(reading())
        collector.hold = threading.Event()
        github = service.snapshot(nonblocking=True)["github"]
        self.assertEqual((github["sampled_at"], github["errors"]), (None, [{"code": "loading"}]))
        self.assertNotIn("restored", github)
        collector.hold.set()
        service.snapshot()
        self.assertEqual(self.files(), ["reading.json.z"])

    def test_a_failed_pass_does_not_replace_the_kept_reading(self):
        service, _ = self.start(reading(), ApiError("unavailable"))
        service.snapshot(force=True)
        self.assertTrue(service.snapshot(force=True)["github"]["stale"])
        kept = self.store().load("reading")
        self.assertEqual((kept["sampled_at"], kept.get("stale")), (NOW.isoformat(), None))
        self.assertFalse(kept["repositories"][0]["stale"])

    def test_a_reading_older_than_a_day_is_not_served(self):
        first, _ = self.start(reading())
        first.snapshot(force=True)
        self.wall = NOW + timedelta(hours=24, seconds=1)
        service, collector = self.start(reading(self.wall))
        collector.hold = threading.Event()
        self.assertEqual(service.snapshot(nonblocking=True)["github"]["repositories"][0]["pulls"], [])
        self.assertEqual(self.files(), [])
        collector.hold.set()
        self.assertEqual(len(service.snapshot()["github"]["repositories"][0]["pulls"]), 1)

    def test_another_selection_of_repositories_never_sees_the_kept_reading(self):
        first, _ = self.start(reading())
        first.snapshot(force=True)
        narrowed = Config("octocat", (REPO,), BOTS, None)
        service = DashboardService(narrowed, Collector(reading()), monotonic=lambda: 1000.0, wall_clock=lambda: NOW,
                                   store=StateStore(self.folder.name, narrowed, clock=lambda: NOW))
        self.assertIsNone(service._github)
        self.assertEqual(self.files(), [])


class OnlyLostAccessDeletes(RestartCase):
    def test_lost_access_deletes_the_kept_reading_and_the_next_start_is_empty(self):
        for code in ("authentication_failed", "forbidden", "not_found"):
            with self.subTest(code=code):
                service, collector = self.restarted(ApiError(code))
                self.assertEqual(self.files(), ["reading.json.z"])
                github = service.snapshot(force=True)["github"]
                self.assertEqual([row["pulls"] for row in github["repositories"]], [[], []])
                self.assertEqual((github["errors"], collector.cleared, self.files()), ([{"code": code}], 1, []))
                again, _ = self.start(ApiError("unavailable"))
                self.assertIsNone(again._github)

    def test_a_pass_that_meets_lost_access_on_one_source_keeps_nothing(self):
        revoked = reading(NOW + timedelta(seconds=40))
        revoked["repositories"][1]["errors"] = [{"pull": 2, "code": "forbidden"}]
        service, collector = self.restarted(revoked)
        service.snapshot(force=True)
        self.assertEqual((collector.cleared, self.files()), (1, []))

    def test_a_failure_this_code_does_not_name_keeps_the_last_reading(self):
        service, _ = self.start(reading(), ApiError("teapot"))
        service.snapshot(force=True)
        github = service.snapshot(force=True)["github"]
        self.assertEqual([len(row["pulls"]) for row in github["repositories"]], [1, 1])
        self.assertEqual(([row["stale"] for row in github["repositories"]], github["errors"]), ([True, True], [{"code": "teapot"}]))

    def test_a_repository_that_fails_in_a_way_this_code_does_not_name_keeps_its_row(self):
        partial = reading(repositories=(REPO,))
        partial["errors"] = [{"repository": OTHER, "code": "teapot"}]
        service, _ = self.start(reading(), partial)
        service.snapshot(force=True)
        kept = service.snapshot(force=True)["github"]["repositories"][1]
        self.assertEqual((kept["repository"], kept["stale"], kept["source_error"], len(kept["pulls"])), (OTHER, True, "teapot", 1))

    def test_a_repository_that_lost_access_does_not_keep_its_row(self):
        partial = reading(repositories=(REPO,))
        partial["errors"] = [{"repository": OTHER, "code": "not_found"}]
        service, _ = self.start(reading(), partial)
        service.snapshot(force=True)
        gone = service.snapshot(force=True)["github"]["repositories"][1]
        self.assertEqual((gone["pulls"], gone["unavailable"], gone["sampled_at"]), ([], True, None))


class ForgetsWhatItCannotUse(RestartCase):
    def keep(self, value):
        self.store().save("reading", value)

    def test_a_kept_reading_this_code_cannot_serve_is_dropped_instead_of_failing_every_request(self):
        unreadable = reading()
        unreadable["bots"]["roles"]["reviewer"]["recent_2h"] = ["a row from another version"]
        self.keep(unreadable)
        service, collector = self.start(reading())
        collector.hold = threading.Event()
        self.assertTrue(service._github["restored"])
        github = service.snapshot(nonblocking=True)["github"]
        self.assertEqual((github["errors"], github["repositories"][0]["pulls"]), ([{"code": "loading"}], []))
        self.assertEqual((collector.cleared, self.files(), service._kept), (1, [], False))
        collector.hold.set()
        self.assertEqual(len(service.snapshot()["github"]["repositories"][0]["pulls"]), 1)

    def test_a_failure_with_nothing_kept_still_reaches_the_caller(self):
        service, _ = self.start(reading())
        with patch.object(DashboardService, "_expire", side_effect=KeyError("defect")), self.assertRaises(KeyError):
            service.snapshot(force=True)

    def test_a_pass_that_fails_unexpectedly_blames_what_was_kept_once(self):
        service, collector = self.restarted(KeyError("a cached row of another shape"), KeyError("again"), reading())
        github = service.snapshot(force=True)["github"]
        self.assertEqual((github["errors"], github["repositories"][0]["pulls"]), ([{"code": "unavailable"}], []))
        self.assertEqual((collector.cleared, self.files()), (1, []))
        service.snapshot(force=True)
        self.assertEqual(collector.cleared, 1)
        self.assertEqual(len(service.snapshot(force=True)["github"]["repositories"][0]["pulls"]), 1)

    def test_an_unexpected_failure_with_nothing_kept_keeps_the_last_reading(self):
        service, collector = self.start(reading(), KeyError("defect"))
        service.snapshot(force=True)
        github = service.snapshot(force=True)["github"]
        self.assertEqual(([len(row["pulls"]) for row in github["repositories"]], collector.cleared), ([1, 1], 0))
        self.assertEqual(self.files(), ["reading.json.z"])


USAGE = {"available": True, "sampled_at": NOW.isoformat(), "stale": False, "accounts": [{"id": "openai:plan", "label": "plan", "provider": "openai", "quota_windows": []}],
         "samples": [{"account": "openai:plan", "bot": "reviewer", "timestamp": NOW.isoformat(), "input_tokens": 900, "output_tokens": 100, "partial": False}],
         "gaps": [], "hard_partial": False, "listing_complete": True, "covered_until": None, "partial": False, "completeness": "Observed."}


class UsageAcrossARestart(RestartCase):
    service_class = LiveService

    def live(self, *values, settings=None):
        """A live service whose token history is read only when a test says so, and whose pass,
        started in the background by its own snapshot, is held until the test ends."""
        service, collector = self.start(*values, settings=settings)
        service._usage_next = float("inf")
        collector.hold = threading.Event()
        self.addCleanup(self.read_github, service, collector)
        return service, collector

    @staticmethod
    def read_github(service, collector):
        """One whole pass, finished before this returns. A live service's own snapshot never waits."""
        collector.hold.set()
        return DashboardService.snapshot(service, force=bool(collector.values))

    def read_usage(self, service, outcome):
        service._usage_running = True
        with patch.object(service.usage_reader, "collect", side_effect=[outcome]):
            service._refresh_usage()

    def test_token_history_and_its_records_are_served_right_after_a_restart(self):
        first, collector = self.live(reading())
        self.read_github(first, collector)
        first.usage_reader.cache = {(REPO, 9): {"gap_at": NOW.isoformat()}}
        self.read_usage(first, copy.deepcopy(USAGE))
        self.assertEqual(self.files(), ["reading.json.z", "usage-responses.json.z", "usage.json.z"])
        self.wall = NOW + timedelta(minutes=3)
        service, _ = self.live(reading(self.wall))
        self.assertTrue(service._kept)
        self.assertEqual(service.usage_reader.cache, {(REPO, 9): {"gap_at": NOW.isoformat()}})
        usage = service.snapshot()["telemetry"]["usage"]
        self.assertEqual((usage["samples"][0]["input_tokens"], usage["sampled_at"], usage["stale"]), (900, NOW.isoformat(), False))
        self.wall = NOW + timedelta(minutes=11)
        self.assertTrue(service.snapshot()["telemetry"]["usage"]["stale"])

    def test_a_read_that_fails_without_losing_access_keeps_the_last_token_history(self):
        service, _ = self.live(reading())
        self.read_usage(service, copy.deepcopy(USAGE))
        reader = service.usage_reader
        for failure in (ApiError("rate_limited"), ApiError("unavailable"), ApiError("teapot"), OSError("timed out"), ValueError("bad")):
            with self.subTest(failure=repr(failure)):
                self.read_usage(service, failure)
                self.assertEqual(service._usage_result["samples"][0]["input_tokens"], 900)
                self.assertIs(service.usage_reader, reader)
                self.assertFalse(service._usage_running)
                self.assertIn("usage.json.z", self.files())

    def test_lost_access_on_the_usage_source_deletes_the_token_history_and_nothing_else(self):
        service, collector = self.live(reading())
        self.read_github(service, collector)
        reader = service.usage_reader
        self.read_usage(service, copy.deepcopy(USAGE))
        self.assertEqual(self.files(), ["reading.json.z", "usage-responses.json.z", "usage.json.z"])
        self.read_usage(service, ApiError("forbidden"))
        self.assertIsNone(service._usage_result)
        self.assertIsNot(service.usage_reader, reader)
        self.assertEqual(self.files(), ["reading.json.z"])

    def test_a_scan_that_meets_lost_access_on_a_listing_keeps_nothing_on_disk(self):
        service, _ = self.live(reading())
        self.read_usage(service, copy.deepcopy(USAGE))
        self.assertEqual(self.files(), ["usage-responses.json.z", "usage.json.z"])
        emptied = {**copy.deepcopy(USAGE), "accounts": [], "samples": [], "partial": True}

        def scan():
            service.usage_reader.lost_access = True
            return emptied

        service._usage_running = True
        with patch.object(service.usage_reader, "collect", side_effect=scan):
            service._refresh_usage()
        self.assertEqual((self.files(), service._usage_result["samples"], service._usage_result["partial"]), ([], [], True))

    def test_lost_access_seen_by_a_pass_deletes_the_token_history_and_stops_a_read_in_flight(self):
        service, collector = self.live(reading(), ApiError("forbidden"))
        self.read_github(service, collector)
        self.read_usage(service, copy.deepcopy(USAGE))
        reader, generation = service.usage_reader, service._usage_generation
        self.read_github(service, collector)
        self.assertEqual((self.files(), service._usage_result, collector.cleared), ([], None, 1))
        self.assertIsNot(service.usage_reader, reader)
        service._usage_running = True
        service._finish_usage(reader, generation, copy.deepcopy(USAGE), False)
        self.assertEqual((self.files(), service._usage_result, service._usage_running), ([], None, False))

    def test_a_kept_record_this_code_cannot_read_is_forgotten_once(self):
        first, collector = self.live(reading())
        self.read_github(first, collector)
        self.read_usage(first, copy.deepcopy(USAGE))
        service, collector = self.live(reading())
        self.read_usage(service, KeyError("a record of another shape"))
        self.assertEqual((self.files(), service._usage_result, service._kept, collector.cleared), ([], None, False, 1))
        self.assertFalse(service._usage_running)
        with self.assertRaises(KeyError):
            self.read_usage(service, KeyError("a defect, with nothing kept to blame"))
        self.assertFalse(service._usage_running)

    def test_token_history_is_not_read_before_the_owner_wide_repository_list_exists(self):
        everything = Config("octocat", (), BOTS, None, all_repositories=True)
        service = LiveService(everything, collector=Collector(), monotonic=lambda: 1000.0, wall_clock=lambda: NOW)
        github = {"errors": [], "repositories": [], "coverage": {"selected": None}}
        with patch("dashboard.live_service.DashboardService.snapshot", return_value={"github": github, "telemetry": {"available": False}}), \
                patch("dashboard.live_service.threading.Thread") as thread:
            service.snapshot()
            self.assertEqual((thread.call_count, service._usage_next, service._usage_running), (0, 0, False))
            service._github = {"coverage": {"selected": 1}, "repositories": [{"repository": REPO}]}
            service.snapshot()
            self.assertEqual((thread.call_count, service._usage_running), (1, True))

    def test_a_bot_read_before_the_restart_carries_its_age_onto_the_strip(self):
        roster = config(agents=(AgentDefinition("swe", "SWE", "swe", "reviewer"),))
        first, collector = self.live(reading(), settings=roster)
        self.read_github(first, collector)
        self.wall = NOW + timedelta(seconds=40)
        service, _ = self.live(reading(self.wall), settings=roster)
        row = service.snapshot()["agents"]["rows"][0]
        self.assertEqual((row["state"], row["stale"], row["sampled_at"], len(row["recent_2h"])), ("idle", True, NOW.isoformat(), 1))
        fresh = self.read_github(service, service.collector)
        self.assertFalse(fresh["github"]["bots"]["roles"]["reviewer"].get("stale"))


class ThePageSaysHowOld(unittest.TestCase):
    def node(self, source):
        result = subprocess.run(["node", "-e", source], cwd=ROOT, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return json.loads(result.stdout)

    def test_an_old_state_keeps_its_name_with_its_age_and_is_never_the_present_tense(self):
        result = self.node(r"""
const h=require('./dashboard/static/helpers.js'), now=Date.parse('2026-10-08T15:04:10Z'), at='2026-10-08T15:00:00Z';
console.log(JSON.stringify({
 fresh:[h.agedState({state:'working',sampled_at:at},now),h.agedState({state:'idle',stale:false,sampled_at:at},now)],
 old:[h.agedState({state:'idle',stale:true,sampled_at:at},now),h.agedState({state:'working',stale:true,sampled_at:at},now)],
 undated:h.agedState({state:'paused',stale:true},now), unknown:[h.agedState({stale:true,sampled_at:at},now),h.agedState({state:'unknown',stale:true},now)]}));
""")
        self.assertEqual(result["fresh"], [{"status": "working", "label": "Working"}, {"status": "idle", "label": "Idle"}])
        self.assertEqual(result["old"], [{"status": "unknown", "label": "Idle · 4m 10s ago"},
                                         {"status": "unknown", "label": "Working · 4m 10s ago"}])
        self.assertEqual(result["undated"], {"status": "unknown", "label": "Paused · stale"})
        self.assertEqual(result["unknown"], [{"status": "unknown", "label": "Unknown"}] * 2)

    def test_the_header_and_the_banner_say_a_reading_is_old_and_where_it_came_from(self):
        result = self.node(r"""
const h=require('./dashboard/static/helpers.js'), now=Date.parse('2026-10-08T15:04:10Z'), at='2026-10-08T15:00:00Z';
const time=h.formatTime(at), coverage={label:'Selected repositories',selected:2,readable:2};
console.log(JSON.stringify({time,
 stamps:[h.sourceStamp({sampled_at:at},now),h.sourceStamp({sampled_at:at,refreshing:true},now),h.sourceStamp({sampled_at:at,stale:true},now),
  h.sourceStamp({sampled_at:at,stale:true,refreshing:true,restored:true},now),h.sourceStamp({refreshing:true},now),h.sourceStamp({},now)],
 banners:[h.coverageBanner(coverage,{}),h.coverageBanner(coverage,{stale:true}),h.coverageBanner(coverage,{stale:true,restored:true})]}));
""")
        time = result["time"]
        self.assertEqual(result["stamps"], [
            "GitHub sampled %s" % time, "GitHub sampled %s · refreshing" % time, "GitHub sampled %s · 4m 10s ago · stale" % time,
            "GitHub sampled %s · 4m 10s ago · stale · refreshing" % time, "Fetching GitHub data…", "GitHub unavailable"])
        counted = "Selected repositories: 2. 2 read successfully in this sample."
        self.assertEqual(result["banners"], [counted, counted + " Some GitHub data is unavailable or stale.",
                                             counted + " This is the reading from before a restart; GitHub is being read again."])

    def test_the_bot_strip_draws_an_old_state_in_gray_with_its_age_and_the_header_uses_the_stamp(self):
        result = self.node(r"""
const app=require('fs').readFileSync('./dashboard/static/app.js','utf8'), h=require('./dashboard/static/helpers.js');
const make=tag=>({tag,attrs:{},children:[],style:{setProperty(){}},append(...c){this.children.push(...c)},replaceChildren(...c){this.children=c}});
const el=(tag,attrs={},...c)=>Object.assign(make(tag),{attrs,children:c.flat().filter(v=>v!=null)});
const at=new Date(Date.now()-250000).toISOString(), strip=el('div');
const rows=[{id:'swe',name:'SWE',role:'swe',state:'idle',stale:true,sampled_at:at,recent_2h:[]},{id:'qae',name:'QAE',role:'qae',state:'working',recent_2h:[]}];
new Function('el','BOT_META','snapshot','state','badge','go','formatTime','document','recentRunLabel','agedState',app.match(/  function renderBots\(\) \{([\s\S]*?)\n  \}\n\n  function coverage/)[1])(
 el,h.BOT_META,{agents:{rows}},{},(status,label)=>el('span',{class:'badge status-'+status},label),()=>{},v=>v,{querySelector:()=>strip},()=>'Last 2h',h.agedState);
const pills=strip.children.filter(n=>n.tag==='button');
console.log(JSON.stringify({badges:pills.map(p=>p.children[1]).map(b=>[b.attrs.class,b.children[0]]),labels:pills.map(p=>p.attrs['aria-label']),
 stamp:app.includes('textContent = sourceStamp(snapshot.github, Date.now());'),retry:app.includes('window.setTimeout(load, 5000);')}));
""")
        self.assertEqual(result["badges"], [["badge status-unknown", "Idle · 4m 10s ago"], ["badge status-working", "Working"]])
        self.assertEqual(result["labels"], ["SWE: Idle · 4m 10s ago. Open usage and recent outcomes",
                                            "QAE: Working. Open usage and recent outcomes"])
        self.assertTrue(result["stamp"] and result["retry"])


class Wiring(unittest.TestCase):
    def test_the_launcher_hands_the_state_directory_to_the_service(self):
        with tempfile.TemporaryDirectory() as folder:
            server = make_server(config(), 0, state_dir=folder)
            try:
                self.assertEqual(server.service.store.directory, Path(folder) / "octocat")
            finally:
                server.server_close()
            plain = make_server(config(), 0)
            try:
                self.assertIsNone(plain.service.store)
            finally:
                plain.server_close()
        launcher = (ROOT / "bin" / "vibe-dashboard").read_text()
        self.assertIn('parser.add_argument("--state-dir", default=os.environ.get("VIBE_DASHBOARD_STATE_DIR"),', launcher)
        self.assertIn("make_server(config, args.port, state_dir=args.state_dir)", launcher)

    def test_a_state_directory_that_cannot_be_used_still_starts_the_dashboard(self):
        with tempfile.NamedTemporaryFile() as blocker, redirect_stderr(io.StringIO()) as errors:
            server = make_server(config(), 0, state_dir=blocker.name)
            try:
                self.assertIsNone(server.service.store)
            finally:
                server.server_close()
        self.assertIn("keeping nothing across a restart", errors.getvalue())


if __name__ == "__main__":
    unittest.main()
