"""The follower moves its checkout to merged code and restarts dashboards only when they need it."""

import importlib.machinery
import importlib.util
import subprocess
import tempfile
import unittest
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


class Follow(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
