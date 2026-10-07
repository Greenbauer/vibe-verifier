"""The follower moves its checkout to merged code and restarts dashboards only when they need it."""

import importlib.machinery
import importlib.util
import io
import json
import subprocess
import tempfile
import threading
import unittest
import urllib.error
from email.message import Message
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
loader = importlib.machinery.SourceFileLoader("vibe_dashboard_follow", str(ROOT / "bin/vibe-dashboard-follow"))
spec = importlib.util.spec_from_loader(loader.name, loader)
follow = importlib.util.module_from_spec(spec)
loader.exec_module(follow)


def git(cwd, *args):
    return subprocess.run(["git", "-c", "user.name=Test", "-c", "user.email=test@example.test",
                           "-c", "init.defaultBranch=main", *args],
                          cwd=cwd, capture_output=True, text=True, check=True).stdout.strip()


class Repositories(unittest.TestCase):
    """An origin, an author who pushes to it, and the follower's own checkout."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.origin, self.author, self.checkout = base / "origin.git", base / "author", base / "checkout"
        git(base, "init", "--bare", "--quiet", str(self.origin))
        git(base, "clone", "--quiet", str(self.origin), str(self.author))
        self.commit("README.md", "first")
        git(base, "clone", "--quiet", str(self.origin), str(self.checkout))

    def tearDown(self):
        self.tmp.cleanup()

    def commit(self, path, text):
        target = self.author / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text)
        git(self.author, "add", path)
        git(self.author, "commit", "--quiet", "-m", "change " + path)
        git(self.author, "push", "--quiet", "origin", "HEAD:main")


class Follow(Repositories):
    def test_a_merge_fast_forwards_the_checkout_and_reports_what_changed(self):
        self.assertEqual(follow.update(str(self.checkout), "main"), [])
        self.commit("dashboard/static/app.js", "new")
        self.commit("dashboard/server.py", "new")
        self.assertEqual(sorted(follow.update(str(self.checkout), "main")),
                         ["dashboard/server.py", "dashboard/static/app.js"])
        self.assertEqual(git(self.checkout, "rev-parse", "HEAD"), git(self.author, "rev-parse", "HEAD"))
        self.assertEqual(follow.update(str(self.checkout), "main"), [])

    def test_a_checkout_that_cannot_fast_forward_is_left_alone(self):
        (self.checkout / "README.md").write_text("local edit")
        self.commit("README.md", "merged")
        with self.assertRaises(subprocess.CalledProcessError):
            follow.update(str(self.checkout), "main")
        self.assertEqual((self.checkout / "README.md").read_text(), "local edit")

    def test_only_server_code_needs_a_restart(self):
        self.assertFalse(follow.needs_restart(["dashboard/static/app.js", "docs/dashboard.md", "README.md"]))
        self.assertFalse(follow.needs_restart([]))
        self.assertTrue(follow.needs_restart(["dashboard/server.py"]))
        self.assertTrue(follow.needs_restart(["docs/dashboard.md", "bin/vibe-dashboard"]))

    def test_a_dashboard_that_exits_is_started_again_and_a_running_one_is_kept(self):
        running, exited = mock.Mock(), mock.Mock(returncode=1)
        running.poll.return_value, exited.poll.return_value = None, 1
        with mock.patch.object(follow.subprocess, "Popen", side_effect=[running, exited, mock.Mock()]) as popen:
            dashboards = follow.Dashboards([("a.json", "8765"), ("b.json", "8766")])
            dashboards.start()
            dashboards.start()
        self.assertEqual(popen.call_count, 3)
        self.assertEqual(popen.call_args.args[0][-4:], ["--config", "b.json", "--port", "8766"])
        self.assertIs(dashboards.processes[0], running)


def listing(*runs, total=None):
    return {"total_count": len(runs) if total is None else total,
            "check_runs": [{"status": status, "conclusion": conclusion} for status, conclusion in runs]}


GREEN = listing(("completed", "success"), ("completed", "skipped"), ("completed", "neutral"))


class GreenGate(unittest.TestCase):
    def test_only_a_complete_listing_of_finished_passing_runs_with_a_success_is_green(self):
        self.assertTrue(follow.checks_passed(GREEN))
        blocked = {
            "pending": listing(("completed", "success"), ("in_progress", None)),
            "queued": listing(("queued", None)),
            "failed": listing(("completed", "success"), ("completed", "failure")),
            "cancelled": listing(("completed", "success"), ("completed", "cancelled")),
            "no success": listing(("completed", "skipped"), ("completed", "neutral")),
            "no checks yet": listing(),
            "a second page unread": listing(("completed", "success"), total=101),
            "unavailable": None,
            "malformed": {"check_runs": "none"},
        }
        for name, value in blocked.items():
            with self.subTest(name):
                self.assertFalse(follow.checks_passed(value))

    def test_check_runs_are_read_without_a_credential_and_a_spent_limit_waits_for_its_reset(self):
        calls, clock = [], [1000]

        def opener(request, timeout):
            calls.append(request)
            if len(calls) == 1:
                response = io.BytesIO(json.dumps(GREEN).encode())
                response.headers = {"X-RateLimit-Remaining": "59", "X-RateLimit-Reset": "4600"}
                return response
            headers = Message()
            headers["X-RateLimit-Remaining"], headers["X-RateLimit-Reset"] = "0", "4600"
            raise urllib.error.HTTPError(request.full_url, 403, "rate limited", headers, io.BytesIO(b""))

        read = follow.CheckRuns("octocat/example", opener=opener, clock=lambda: clock[0])
        self.assertEqual(read("abc"), GREEN)
        self.assertEqual(calls[0].full_url, "https://api.github.com/repos/octocat/example/commits/abc/check-runs"
                                            "?filter=latest&per_page=100")
        self.assertIsNone(calls[0].get_header("Authorization"))
        self.assertIsNone(read("abc"))
        self.assertIsNone(read("abc"))
        self.assertEqual(len(calls), 2)
        clock[0] = 4600
        read("abc")
        self.assertEqual(len(calls), 3)


class Health(unittest.TestCase):
    def serve(self, status, body):
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                payload = json.dumps(body).encode()
                self.send_response(status if self.path == "/api/dashboard" else 404)
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, *args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return server.server_port

    def test_a_dashboard_is_healthy_only_when_its_api_answers_version_one(self):
        self.assertTrue(follow.healthy(self.serve(200, {"version": 1, "owner": "octocat"})))
        self.assertFalse(follow.healthy(self.serve(503, {"error": "dashboard source unavailable"}), seconds=0))
        self.assertFalse(follow.healthy(self.serve(200, {"version": 2}), seconds=0))


class SystemdMode(Repositories):
    def setUp(self):
        super().setUp()
        self.listings, self.restarts, self.health = {}, [], []
        self.unhealthy = set()

        def check_runs(sha):
            self.read.append(sha)
            return self.listings.get(sha)

        def restart(units):
            self.restarts.append(units)

        def health(port):
            self.health.append(port)
            return git(self.checkout, "rev-parse", "HEAD") not in self.unhealthy

        self.read = []
        self.units = follow.Units(str(self.checkout), "main", [("vibe-dashboard@a.service", 8765),
                                                                ("vibe-dashboard@b.service", 8766)],
                                  check_runs, restart, health)

    def head(self, repo):
        return git(repo, "rev-parse", "HEAD")

    def test_a_commit_whose_checks_have_not_all_passed_is_not_deployed(self):
        before = self.head(self.checkout)
        self.commit("dashboard/server.py", "new")
        target = self.head(self.author)
        for name, value in (("pending", listing(("in_progress", None))),
                            ("failed", listing(("completed", "failure"))),
                            ("no success", listing(("completed", "skipped")))):
            with self.subTest(name):
                self.listings[target] = value
                self.assertEqual(self.units.step(), [])
                self.assertEqual(self.head(self.checkout), before)
        self.assertEqual(self.read, [target] * 3)
        self.assertEqual(self.restarts, [])

    def test_a_green_commit_is_deployed_restarted_and_health_checked(self):
        self.commit("dashboard/server.py", "new")
        self.listings[self.head(self.author)] = GREEN
        self.assertEqual(self.units.step(), ["dashboard/server.py"])
        self.assertEqual(self.head(self.checkout), self.head(self.author))
        self.assertEqual(self.restarts, [["vibe-dashboard@a.service", "vibe-dashboard@b.service"]])
        self.assertEqual(self.health, [8765, 8766])
        self.assertEqual(self.units.step(), [])
        self.assertEqual(len(self.read), 1)

    def test_a_green_static_change_goes_live_without_a_restart(self):
        self.commit("dashboard/static/app.js", "new")
        self.listings[self.head(self.author)] = GREEN
        self.assertEqual(self.units.step(), ["dashboard/static/app.js"])
        self.assertEqual(self.head(self.checkout), self.head(self.author))
        self.assertEqual((self.restarts, self.health), ([], []))

    def test_an_unhealthy_commit_is_rolled_back_and_not_retried_until_main_moves(self):
        before = self.head(self.checkout)
        self.commit("dashboard/server.py", "broken")
        bad = self.head(self.author)
        self.listings[bad], self.unhealthy = GREEN, {bad}
        self.assertEqual(self.units.step(), [])
        self.assertEqual(self.head(self.checkout), before)
        self.assertEqual(len(self.restarts), 2)
        self.assertFalse((self.checkout / "dashboard/server.py").exists())
        self.assertEqual(self.units.bad_commit(), bad)
        self.assertEqual(self.units.step(), [])
        self.assertEqual((self.read, len(self.restarts)), ([bad], 2))
        self.commit("dashboard/server.py", "fixed")
        self.listings[self.head(self.author)] = GREEN
        self.assertEqual(self.units.step(), ["dashboard/server.py"])
        self.assertEqual(self.head(self.checkout), self.head(self.author))

    def test_a_failed_restart_counts_as_unhealthy(self):
        before = self.head(self.checkout)
        self.commit("bin/vibe-dashboard", "new")
        self.listings[self.head(self.author)] = GREEN
        self.units.restart = mock.Mock(side_effect=subprocess.CalledProcessError(1, ["systemctl"]))
        self.assertEqual(self.units.step(), [])
        self.assertEqual(self.head(self.checkout), before)
        self.assertEqual(self.health, [])

    def test_checks_are_read_from_the_github_repository_origin_names(self):
        git(self.checkout, "remote", "set-url", "origin", "https://github.com/Greenbauer/vibe-verifier.git")
        self.assertEqual(follow.github_repository(str(self.checkout)), "Greenbauer/vibe-verifier")
        git(self.checkout, "remote", "set-url", "origin", str(self.origin))
        with self.assertRaisesRegex(ValueError, "not an https://github.com/ repository"):
            follow.github_repository(str(self.checkout))


if __name__ == "__main__":
    unittest.main()
