"""The state store: what it keeps, who may read it, and every way a file is refused and deleted."""

import io
import json
import os
import stat
import subprocess
import tempfile
import unittest
import zlib
from contextlib import redirect_stderr
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from dashboard import state_store
from dashboard.config import BotDefinition, Config
from dashboard.gh_api import GitHubAPI
from dashboard.github import GitHubCollector
from dashboard.state_store import (MAX_AGE, StateStore, collector_state, old_reading, open_store,
                                   restore_collector, restore_usage, usage_state)
from dashboard.usage_artifacts import UsageArtifacts

NOW = datetime(2026, 10, 8, 15, 0, tzinfo=timezone.utc)
REPO = "octocat/example"


def config(owner="octocat", repositories=(REPO,), workflow="review.yml"):
    bots = {role: BotDefinition(workflow, (role,)) for role in ("reviewer", "explorer", "verifier")}
    return Config(owner, tuple(repositories), bots, None)


class Clock:
    def __init__(self, value=NOW):
        self.value = value

    def __call__(self):
        return self.value


class StoreCase(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.root = Path(self.folder.name) / "state"
        self.clock = Clock()
        self.store = StateStore(self.root, config(), clock=self.clock)
        self.file = self.root / "octocat" / "reading.json.z"

    def write(self, document, mode=0o600):
        """A file as another version, or anything else, might have left it."""
        self.file.write_bytes(zlib.compress(json.dumps(document).encode()))
        os.chmod(self.file, mode)

    def document(self, **changes):
        return {"version": 1, "identity": self.store.identity, "saved_at": NOW.isoformat(),
                "value": {"kept": True}, **changes}

    def refused(self):
        """load() gives nothing and the file is gone, so the dashboard starts empty."""
        with redirect_stderr(io.StringIO()):
            self.assertIsNone(self.store.load("reading"))
        self.assertFalse(os.path.lexists(self.file))


class KeepsAndReturns(StoreCase):
    def test_a_saved_document_comes_back_and_only_its_account_can_read_it(self):
        self.assertTrue(self.store.save("reading", {"pulls": [1, 2], "title": "naïve"}))
        self.assertEqual(self.store.load("reading"), {"pulls": [1, 2], "title": "naïve"})
        self.assertEqual(stat.S_IMODE(self.file.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.file.parent.stat().st_mode), 0o700)
        self.assertEqual(sorted(path.name for path in self.file.parent.iterdir()), ["reading.json.z"])

    def test_a_list_is_written_one_item_at_a_time_and_comes_back_whole(self):
        for value in ([], [{"etag": 'W/"ü"', "rows": [1, 2]}], [["endpoint", "etag", {"deep": [None, True, 1.5]}], "text", 7]):
            with self.subTest(value=value):
                self.assertTrue(self.store.save("responses", value))
                self.assertEqual(self.store.load("responses"), value)
        pieces = list(self.store._texts([{"a": 1}, {"b": 2}]))
        self.assertEqual(len(pieces), 5)
        self.assertEqual(json.loads("".join(pieces))["value"], [{"a": 1}, {"b": 2}])

    def test_a_save_replaces_the_file_in_one_rename_and_never_leaves_a_draft(self):
        self.store.save("reading", {"pass": 1})
        first = self.file.stat().st_ino
        replaced = []
        real = os.replace
        with patch("dashboard.state_store.os.replace", side_effect=lambda a, b: (replaced.append((Path(a).name, Path(b).name)), real(a, b))):
            self.store.save("reading", {"pass": 2})
        self.assertEqual(replaced, [("reading.draft", "reading.json.z")])
        self.assertNotEqual(self.file.stat().st_ino, first)
        self.assertEqual(self.store.load("reading"), {"pass": 2})
        self.assertFalse((self.file.parent / "reading.draft").exists())

    def test_dashboards_of_different_owners_may_share_one_directory(self):
        other = StateStore(self.root, config("hubot", ("hubot/site",)), clock=self.clock)
        self.store.save("reading", {"owner": "octocat"})
        other.save("reading", {"owner": "hubot"})
        self.assertEqual(self.store.load("reading"), {"owner": "octocat"})
        self.assertEqual(other.load("reading"), {"owner": "hubot"})
        other.clear()
        self.assertEqual(self.store.load("reading"), {"owner": "octocat"})

    def test_nothing_stored_is_nothing_loaded(self):
        self.assertIsNone(self.store.load("reading"))

    def test_clear_deletes_every_document_and_draft_or_only_the_named_ones(self):
        for name in ("reading", "jobs", "usage"):
            self.store.save(name, {"name": name})
        (self.file.parent / "jobs.draft").write_bytes(b"half written")
        self.store.clear("usage")
        self.assertEqual(sorted(path.name for path in self.file.parent.iterdir()),
                         ["jobs.draft", "jobs.json.z", "reading.json.z"])
        self.store.clear()
        self.assertEqual(list(self.file.parent.iterdir()), [])


class RefusesAndDeletes(StoreCase):
    def test_a_corrupt_file_starts_empty(self):
        for content in (b"", b"not zlib at all", zlib.compress(b"{not json"), zlib.compress(b'["a list"]'),
                        zlib.compress(json.dumps(self.document()).encode())[:-9]):
            with self.subTest(content=content[:12]):
                self.file.write_bytes(content)
                os.chmod(self.file, 0o600)
                self.refused()

    def test_a_file_that_breaks_the_reader_itself_starts_empty_too(self):
        # Neither is an OSError or a ValueError: nesting deeper than the decoder recurses, and a
        # date that overflows when it is moved to UTC. A start must not die on either, every time.
        nested = b'{"value": ' + b"[" * 200000 + b"]" * 200000 + b"}"
        for content in (nested, json.dumps(self.document(saved_at="0001-01-01T00:00:00+14:00")).encode()):
            with self.subTest(content=content[:24]):
                self.file.write_bytes(zlib.compress(content))
                os.chmod(self.file, 0o600)
                self.refused()

    def test_an_old_format_or_another_configuration_starts_empty(self):
        other_repositories = StateStore(self.root, config(repositories=(REPO, "octocat/second")), clock=self.clock)
        other_bots = StateStore(self.root, config(workflow="other.yml"), clock=self.clock)
        self.assertNotEqual(other_repositories.identity, self.store.identity)
        self.assertNotEqual(other_bots.identity, self.store.identity)
        for changes in ({"version": 0}, {"version": 2}, {"identity": other_repositories.identity},
                        {"identity": other_bots.identity}, {"saved_at": "yesterday"}, {"saved_at": None}):
            with self.subTest(changes=changes):
                self.write(self.document(**changes))
                self.refused()

    def test_a_document_expires_by_age_and_one_from_the_future_is_refused(self):
        self.store.save("reading", {"kept": True})
        self.clock.value = NOW + MAX_AGE
        self.assertEqual(self.store.load("reading"), {"kept": True})
        self.clock.value = NOW + MAX_AGE + timedelta(seconds=1)
        self.refused()
        self.write(self.document(saved_at=(NOW + timedelta(minutes=6)).isoformat()))
        self.clock.value = NOW
        self.refused()

    def test_a_file_anyone_else_can_read_or_write_is_refused(self):
        for mode in (0o640, 0o604, 0o660, 0o606):
            with self.subTest(mode=oct(mode)):
                self.write(self.document(), mode)
                self.refused()

    def test_a_file_of_another_account_is_refused(self):
        self.write(self.document())
        with patch("dashboard.state_store.os.getuid", return_value=os.getuid() + 1):
            self.refused()

    def test_a_symlink_is_never_followed(self):
        target = Path(self.folder.name) / "elsewhere.json.z"
        target.write_bytes(zlib.compress(json.dumps(self.document()).encode()))
        os.chmod(target, 0o600)
        os.symlink(target, self.file)
        self.refused()
        self.assertTrue(target.exists())
        os.symlink(target, self.file.parent / "reading.draft")
        with redirect_stderr(io.StringIO()):
            self.assertFalse(self.store.save("reading", {"written": "through the link"}))
        self.assertEqual(json.loads(zlib.decompress(target.read_bytes()))["value"], {"kept": True})
        self.assertEqual(list(self.file.parent.iterdir()), [])

    def test_a_document_over_either_size_limit_is_not_written_and_the_previous_one_stays(self):
        noise = os.urandom(3000).hex()
        for limit, value in (("MAX_RAW", "x" * 5000), ("MAX_RAW", ["x" * 900] * 5), ("MAX_BYTES", noise),
                             ("MAX_BYTES", [os.urandom(700).hex() for _ in range(4)])):
            with self.subTest(limit=limit):
                self.assertTrue(self.store.save("reading", {"small": True}))
                errors = io.StringIO()
                with patch.object(state_store, limit, 2000), redirect_stderr(errors):
                    self.assertFalse(self.store.save("reading", value))
                    self.assertFalse(self.store.save("reading", value))
                self.assertEqual(self.store.load("reading"), {"small": True})
                self.assertEqual([path.name for path in self.file.parent.iterdir()], ["reading.json.z"])
                self.assertEqual(errors.getvalue().count("not keeping reading across a restart: it is over the size limit"), 1)
                self.assertNotIn(str(value)[2:40], errors.getvalue())

    def test_a_file_over_either_size_limit_is_not_loaded(self):
        self.store.save("reading", {"text": os.urandom(3000).hex()})
        with patch.object(state_store, "MAX_BYTES", 2000):
            self.refused()
        self.store.save("reading", {"text": "x" * 5000})
        with patch.object(state_store, "MAX_RAW", 2000):
            self.refused()

    def test_a_disk_that_refuses_the_write_is_reported_once_and_keeps_the_previous_file(self):
        self.assertTrue(self.store.save("reading", {"pass": 1}))
        errors = io.StringIO()
        with patch("dashboard.state_store.os.fsync", side_effect=OSError(28, "No space left on device")), redirect_stderr(errors):
            self.assertFalse(self.store.save("reading", {"pass": 2}))
            self.assertFalse(self.store.save("reading", {"pass": 3}))
        self.assertEqual(errors.getvalue().count("not keeping reading across a restart: No space left on device"), 1)
        self.assertEqual([path.name for path in self.file.parent.iterdir()], ["reading.json.z"])
        self.assertEqual(self.store.load("reading"), {"pass": 1})
        self.assertTrue(self.store.save("reading", {"pass": 4}))
        with patch("dashboard.state_store.os.fsync", side_effect=OSError(28, "No space left on device")), redirect_stderr(errors):
            self.assertFalse(self.store.save("reading", {"pass": 5}))
        self.assertEqual(errors.getvalue().count("No space left on device"), 2)

    def test_clearing_a_directory_that_is_gone_is_not_an_error(self):
        self.store.save("reading", {"kept": True})
        self.file.unlink()
        self.file.parent.rmdir()
        self.store.clear()
        self.store.clear("reading")
        with redirect_stderr(io.StringIO()):
            self.assertFalse(self.store.save("reading", {"kept": True}))
        self.assertIsNone(self.store.load("reading"))

    def test_a_directory_that_cannot_be_used_keeps_nothing_and_says_so(self):
        blocker = Path(self.folder.name) / "a-file"
        blocker.write_text("in the way")
        errors = io.StringIO()
        with redirect_stderr(errors):
            self.assertIsNone(open_store(str(blocker), config()))
        self.assertIn("keeping nothing across a restart", errors.getvalue())
        self.assertIsNone(open_store(None, config()))
        self.assertIsNone(open_store("", config()))
        self.assertEqual(open_store(str(self.root), config()).directory, self.root / "octocat")


class OldReading(unittest.TestCase):
    def reading(self):
        return {"sampled_at": NOW.isoformat(), "coverage": {"selected": 2, "readable": 1},
                "repositories": [{"repository": REPO, "sampled_at": NOW.isoformat(), "stale": False,
                                  "pulls": [{"number": 1, "merge_ready": True}, {"number": 2, "merge_ready": False}]},
                                 {"repository": "octocat/gone", "pulls": [], "unavailable": True, "sampled_at": None}],
                "bots": {"roles": {"reviewer": {"state": "idle", "sampled_at": NOW.isoformat()}}}}

    def test_a_kept_reading_is_marked_old_row_by_row_and_bot_by_bot(self):
        marked = old_reading(self.reading())
        self.assertTrue(marked["restored"] and marked["stale"] and marked["partial"])
        self.assertEqual([row.get("stale") for row in marked["repositories"]], [True, None])
        self.assertEqual([row["restored"] for row in marked["repositories"]], [True, True])
        self.assertEqual(marked["repositories"][0]["pulls"], self.reading()["repositories"][0]["pulls"])
        self.assertTrue(marked["bots"]["roles"]["reviewer"]["stale"])
        self.assertEqual(marked["bots"]["roles"]["reviewer"]["state"], "idle")

    def test_a_reading_nested_too_deep_to_copy_is_refused(self):
        deep = self.reading()
        value = []
        deep["repositories"][0]["pulls"][0]["deep"] = value
        for _ in range(2000):
            value.append([])
            value = value[0]
        self.assertIsNone(old_reading(deep))
        self.assertNotIn("restored", deep)

    def test_anything_that_is_not_a_reading_is_refused(self):
        broken = [None, [], "text", {}, {**self.reading(), "coverage": None}, {**self.reading(), "repositories": {}},
                  {**self.reading(), "repositories": [{"repository": REPO}]},
                  {**self.reading(), "repositories": [{"repository": 7, "pulls": []}]},
                  {**self.reading(), "repositories": [{"repository": REPO, "pulls": ["text"]}]},
                  {**self.reading(), "bots": []}, {**self.reading(), "bots": {"roles": {"reviewer": "idle"}}}]
        for value in broken:
            with self.subTest(value=str(value)[:50]):
                self.assertIsNone(old_reading(value))


def response(body, etag='W/"one"', status="200 OK"):
    head = "HTTP/2.0 %s\nEtag: %s\nX-Ratelimit-Remaining: 4000\n\n" % (status, etag)
    return subprocess.CompletedProcess([], 0 if status.startswith("200") else 1, head + json.dumps(body), "")


class CachesAcrossARestart(StoreCase):
    def test_a_kept_answer_is_confirmed_with_304_and_costs_no_budget(self):
        commands = []

        def first(command, **_):
            commands.append(command)
            return response({"name": "example"})

        def unchanged(command, **_):
            commands.append(command)
            return response(None, status="304 Not Modified")

        before = GitHubAPI(runner=first)
        before.begin()
        self.assertEqual(before.one("repos/octocat/example"), {"name": "example"})
        self.assertEqual(before.calls, 1)
        self.store.save("responses", before.export_cache())

        after = GitHubAPI(runner=unchanged)
        self.assertEqual(after.import_cache(self.store.load("responses")), 1)
        for _ in range(2):
            after.begin()
            self.assertEqual(after.one("repos/octocat/example"), {"name": "example"})
            self.assertEqual(after.calls, 0)
        self.assertEqual(commands[1][-2:], ["--header", 'If-None-Match: W/"one"'])

    def test_an_answer_this_token_may_no_longer_read_is_not_served_from_the_kept_copy(self):
        refused = subprocess.CompletedProcess([], 1, "HTTP/2.0 404 Not Found\n\n{}", "gh: Not Found (HTTP 404)")
        api = GitHubAPI(runner=lambda command, **_: refused)
        api.import_cache([["repos/octocat/example", 'W/"one"', {"name": "example"}]])
        api.begin()
        with self.assertRaisesRegex(Exception, "not_found"):
            api.one("repos/octocat/example")

    def test_rows_that_are_not_kept_answers_are_ignored(self):
        api = GitHubAPI(runner=None)
        rows = [["repos/octocat/example", 'W/"one"', {"ok": True}], ["short"], "text", None, [1, 'W/"x"', {}],
                ["repos/octocat/other", "bad\r\nInjected: header", {}], ["repos/octocat/third", 5, {}]]
        self.assertEqual(api.import_cache(rows), 1)
        self.assertEqual(api.import_cache({"not": "a list"}), 0)
        self.assertEqual(api.export_cache(), [["repos/octocat/example", 'W/"one"', {"ok": True}]])

    def test_completed_job_lists_come_back_and_a_restored_run_is_not_read_again(self):
        run = {"id": 7, "run_attempt": 2, "status": "completed", "head_sha": "a" * 40,
               "updated_at": (NOW - timedelta(hours=1)).isoformat()}
        job = {"id": 70, "name": "reviewer", "status": "completed", "conclusion": "success", "run_id": 7,
               "runner_name": "example-ci-1-1700000000"}
        calls = []

        def jobs(command, **_):
            calls.append(command[3])
            return response({"jobs": [job]})

        before = GitHubCollector(config(), GitHubAPI(runner=jobs), clock=lambda: NOW)
        before.api.begin()
        read = before._run_jobs(REPO, run)
        for name, document in collector_state(before).items():
            self.store.save(name, document)

        after = GitHubCollector(config(), GitHubAPI(runner=jobs), clock=lambda: NOW)
        self.assertTrue(restore_collector(after, self.store))
        after.api.begin()
        self.assertEqual(after._run_jobs(REPO, run), read)
        self.assertEqual(len(calls), 1)
        self.assertEqual(after.api.calls, 0)

    def test_job_rows_of_another_shape_are_dropped_one_by_one(self):
        good = [REPO, 7, 1, (NOW + timedelta(days=1)).isoformat(), [{"id": 70}]]
        self.store.save("jobs", [good, [REPO, "7", 1, good[3], []], [REPO, 7, True, good[3], []], [REPO, 7, 1, "soon", []],
                                 [REPO, 8, 1, good[3], ["text"]], [REPO, 9], "text"])
        collector = GitHubCollector(config(), GitHubAPI(runner=None), clock=lambda: NOW)
        self.assertTrue(restore_collector(collector, self.store))
        self.assertEqual(list(collector._completed_jobs), [(REPO, 7, 1)])
        self.assertEqual(collector._completed_jobs[(REPO, 7, 1)][1], [{"id": 70}])

    def test_a_collector_without_caches_keeps_and_restores_nothing(self):
        self.assertEqual(collector_state(object()), {})
        self.assertFalse(restore_collector(object(), self.store))

    def test_usage_records_and_the_last_result_come_back(self):
        result = {"available": True, "sampled_at": NOW.isoformat(), "accounts": [{"id": "a"}], "samples": [{"bot": "explorer"}]}
        before = UsageArtifacts(config(), api=GitHubAPI(runner=None), downloader=None)
        before.cache = {(REPO, 9): {"sample": {"timestamp": NOW.isoformat()}, "_lookup": (REPO, 42, 1, ("explore",))},
                        (REPO, 10): {"gap_at": NOW.isoformat()}}
        before.api.import_cache([["repos/octocat/example/actions/artifacts?per_page=100&page=1", 'W/"page"', {"artifacts": []}]])
        for name, document in usage_state(before, result).items():
            self.store.save(name, document)

        after = UsageArtifacts(config(), api=GitHubAPI(runner=None), downloader=None)
        self.assertEqual(restore_usage(after, self.store), result)
        self.assertEqual(sorted(after.cache), [(REPO, 9), (REPO, 10)])
        self.assertEqual(after.cache[(REPO, 9)]["_lookup"], [REPO, 42, 1, ["explore"]])
        self.assertEqual(len(after.api.export_cache()), 1)

    def test_a_usage_document_of_another_shape_gives_no_result_and_keeps_only_whole_records(self):
        self.store.save("usage", {"result": {"accounts": "many"}, "records": [[REPO, 9, {"gap_at": "x"}], [REPO, "9", {}], [REPO, 9], 4]})
        reader = UsageArtifacts(config(), api=object(), downloader=None)
        self.assertIsNone(restore_usage(reader, self.store))
        self.assertEqual(reader.cache, {(REPO, 9): {"gap_at": "x"}})
        self.store.save("usage", ["not", "a", "document"])
        self.assertIsNone(restore_usage(UsageArtifacts(config(), api=object(), downloader=None), self.store))


if __name__ == "__main__":
    unittest.main()
