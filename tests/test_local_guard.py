"""gates/_local_guard.py on its own, both ways: a Node and a Python process started with Guard.env() reach this
machine and nothing else, children and worker threads included, and each refusal is in the run's record; and a
credential for anywhere else in what a test process would inherit stops it before it starts. Every outside address
here is reserved (192.0.2.0/24, TEST-NET-1, and example.com), and each is refused before anything is sent: an address
given as numbers before the connection is made, and a name (Node only: Python is handed addresses) before it is looked
up. The gates that use the guard are tests/test_local_guard_gates.py."""
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path

from helpers import ROOT, clean_env

sys.path.insert(0, str(ROOT / "gates"))
import _local_guard  # noqa: E402  (gates/ is not a package)
from _contract import CannotRun  # noqa: E402

SAID = "refused by vibe-verifier, whose test runs reach only this machine"
NODE_OUTSIDE = r"""
const net = require("net"), https = require("https"), { execFileSync } = require("child_process"), { Worker } = require("worker_threads");
const attempt = (options) => new Promise((resolve) => {
  const socket = net.connect(options).on("connect", () => { socket.destroy(); resolve("connected"); });
  socket.on("error", (error) => resolve(error.code + ": " + error.message));
});
const lookup = (host, options, callback) => { console.log("looked up " + host); callback(null, "192.0.2.5", 4); };
(async () => {
  console.log("address:", await attempt({ host: "192.0.2.1", port: 80 }));
  console.log("name:", await attempt({ host: "example.com", port: 443, lookup }));
  console.log("fetch:", await fetch("http://192.0.2.2/").then(() => "reached", (error) => error.cause.code));
  console.log("https:", await new Promise((resolve) => https.get("https://example.com/", () => resolve("reached")).on("error", (error) => resolve(error.code))));
  // A child given an environment of its own that keeps only the guard: refused, and still in the run's record.
  console.log("child:", execFileSync(process.execPath, ["-e", "require('net').connect(80, '192.0.2.3').on('error', (e) => console.log(e.code))"],
                                     { env: { NODE_OPTIONS: process.env.NODE_OPTIONS } }).toString().trim());
  await new Promise((resolve) => new Worker("require('net').connect(80, '192.0.2.4').on('error', (e) => console.log('worker: ' + e.code))", { eval: true }).on("exit", resolve));
  console.log("localhost elsewhere:", await attempt({ host: "db.localhost", port: 5432, lookup: (host, options, callback) => callback(null, [{ address: "192.0.2.6", family: 4 }]) }));
})();
"""
NODE_HERE = r"""
const net = require("net");
const read = (options) => new Promise((resolve) => {
  let said = "";
  net.connect(options, function () { this.write("hi\n"); }).on("data", (data) => { said += data; })
    .on("end", () => resolve(said)).on("error", (error) => resolve(error.code));
});
(async () => {
  console.log("127.0.0.1:", await read({ host: "127.0.0.1", port: PORT }));
  console.log("localhost:", await read({ host: "localhost", port: PORT }));
  console.log("fetch:", await fetch("http://localhost:PORT/").then((response) => response.status, (error) => error.cause.code));
})();
"""
PY_OUTSIDE = r"""
import os, socket, subprocess, sys
for host in ("192.0.2.1", "::ffff:192.0.2.2"):
    try:
        socket.socket(socket.AF_INET6 if ":" in host else socket.AF_INET).connect((host, 80))
        print("reached")
    except ConnectionRefusedError as error:
        print(host, error.strerror)
print("connect_ex:", socket.socket().connect_ex(("192.0.2.3", 80)) == __import__("errno").ECONNREFUSED)
child = "import socket\ntry:\n    socket.create_connection(('192.0.2.4', 80), timeout=5)\nexcept ConnectionRefusedError as error:\n    print(error.strerror)"
# A child given an environment of its own that keeps only the guard: refused, and still in the run's record.
print("child:", subprocess.run([sys.executable, "-c", child], capture_output=True, text=True,
                               env={"PYTHONPATH": os.environ["PYTHONPATH"]}).stdout.strip())
"""
PY_HERE = r"""
import os, socket, sys, tempfile
for host in ("127.0.0.1", "localhost"):
    with socket.create_connection((host, PORT), timeout=5) as client:
        client.sendall(b"hi\n")
        print(host, client.recv(2).decode())
server, path = socket.socket(socket.AF_UNIX), os.path.join(tempfile.mkdtemp(), "s")
server.bind(path)
server.listen(1)
socket.socket(socket.AF_UNIX).connect(path)
print("unix: connected", "marked" if getattr(sys, "vv_marked", False) else "")
"""


class Here:
    """A server on 127.0.0.1 for as long as the test runs: ok to a client that writes a line, an HTTP 200 to a request."""

    def __init__(self, test):
        self.server = socket.socket()
        self.server.bind(("127.0.0.1", 0))
        self.server.listen(8)
        self.port = self.server.getsockname()[1]
        threading.Thread(target=self.serve, daemon=True).start()
        test.addCleanup(self.server.close)

    def serve(self):
        while True:
            try:
                connection, _ = self.server.accept()
            except OSError:
                return
            with connection:
                said = connection.recv(1024)
                connection.sendall(b"HTTP/1.0 200 OK\r\nContent-Length: 2\r\n\r\nok" if said.startswith(b"GET ") else b"ok")


class Guarded(unittest.TestCase):
    def setUp(self):
        self.repo = tempfile.mkdtemp(prefix="vv-guard-repo-")
        self.addCleanup(shutil.rmtree, self.repo, True)
        self.guard = _local_guard.Guard("test", self.repo, {"SESSION_SECRET"})
        self.addCleanup(self.guard.__exit__)

    def run_with(self, command, base=None):
        return subprocess.run(command, env=self.guard.env("the test", base=clean_env(base)), capture_output=True, text=True, timeout=120)

    def test_node_refuses_every_other_address_and_name_before_anything_is_sent_and_records_each(self):
        result = self.run_with(["node", "-e", NODE_OUTSIDE])
        self.assertEqual(result.returncode, 0, result.stderr)
        lines = result.stdout.splitlines()
        self.assertIn("address: ECONNREFUSED: connect ECONNREFUSED 192.0.2.1:80: " + SAID, lines)
        self.assertIn("name: ECONNREFUSED: connect ECONNREFUSED example.com:443: " + SAID, lines)
        self.assertNotIn("looked up example.com", lines)  # refused before it was looked up
        self.assertIn("fetch: ECONNREFUSED", lines)
        self.assertIn("https: ECONNREFUSED", lines)
        self.assertIn("child: ECONNREFUSED", lines)
        self.assertIn("worker: ECONNREFUSED", lines)
        self.assertIn("localhost elsewhere: ECONNREFUSED: connect ECONNREFUSED db.localhost:5432 (192.0.2.6): " + SAID, lines)
        self.assertEqual(self.guard.refused(), ["192.0.2.1:80", "example.com:443", "192.0.2.2:80", "192.0.2.3:80", "192.0.2.4:80",
                                                "db.localhost:5432 (192.0.2.6)"])
        records = [json.loads(line) for line in Path(self.guard.record).read_text().splitlines()]
        self.assertEqual({record["by"] for record in records}, {"node"})
        self.assertEqual(len({record["pid"] for record in records}), 2)  # the child is a process of its own

    def test_node_reaches_this_machine(self):
        here = Here(self)
        result = self.run_with(["node", "-e", NODE_HERE.replace("PORT", str(here.port))])
        self.assertEqual(result.stdout.splitlines(), ["127.0.0.1: ok", "localhost: ok", "fetch: 200"], result.stderr)
        self.assertEqual(self.guard.refused(), [])

    def test_python_refuses_every_other_address_and_records_each(self):
        result = self.run_with([sys.executable, "-c", PY_OUTSIDE])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.splitlines(), ["192.0.2.1 " + SAID + ": 192.0.2.1:80", "::ffff:192.0.2.2 " + SAID + ": ::ffff:192.0.2.2:80",
                                                      "connect_ex: True", "child: " + SAID + ": 192.0.2.4:80"])
        self.assertEqual(self.guard.refused(), ["192.0.2.1:80", "::ffff:192.0.2.2:80", "192.0.2.3:80", "192.0.2.4:80"])
        self.assertEqual({json.loads(line)["by"] for line in Path(self.guard.record).read_text().splitlines()}, {"python"})

    def test_python_reaches_this_machine_and_the_interpreters_own_sitecustomize_still_runs(self):
        here, site = Here(self), tempfile.mkdtemp(prefix="vv-guard-site-")
        self.addCleanup(shutil.rmtree, site, True)
        Path(site, "sitecustomize.py").write_text("import sys\nsys.vv_marked = True\n")
        result = self.run_with([sys.executable, "-c", PY_HERE.replace("PORT", str(here.port))], {"PYTHONPATH": site})
        self.assertEqual(result.stdout.splitlines(), ["127.0.0.1 ok", "localhost ok", "unix: connected marked"], result.stderr)
        self.assertEqual(self.guard.refused(), [])

    def test_a_guard_inside_a_guarded_run_records_in_its_own_run(self):
        # A gate a test runs builds its environment from the guarded one: the refusal is the inner run's to judge.
        with _local_guard.Guard("inner", self.repo, ()) as inner:
            env = inner.env("the inner test", base=self.guard.env("the outer test", base=clean_env()))
            for command in (["node", "-e", "require('net').connect(80, '192.0.2.7').on('error', () => {})"],
                            [sys.executable, "-c", "import socket\ntry:\n    socket.create_connection(('192.0.2.8', 80))\nexcept OSError:\n    pass"]):
                subprocess.run(command, env=env, check=True, timeout=60)
            self.assertEqual(inner.refused(), ["192.0.2.7:80", "192.0.2.8:80"])
        self.assertEqual(self.guard.refused(), [])

    def test_a_credential_for_anywhere_else_is_refused_before_anything_starts_and_never_printed(self):
        Path(self.repo, "apps", "web").mkdir(parents=True)
        Path(self.repo, "apps", "web", ".env.local").write_text("DATABASE_URL=postgresql://app:pw-in-dotenv@db.example.com:5432/app\n")
        with self.assertRaises(CannotRun) as refused:
            self.guard.env("Vitest", base={"PATH": os.environ["PATH"], "RESEND_API_KEY": "re_value_in_env"})
        said = str(refused.exception)
        self.assertIn("Vitest would run the pull request's code with a credential for somewhere other than this machine", said)
        self.assertIn("the environment: RESEND_API_KEY is named as a credential, and the manifest does not declare it", said)
        self.assertIn("apps/web/.env.local: DATABASE_URL holds a connection string for db.example.com, which is not this machine", said)
        for value in ("re_value_in_env", "pw-in-dotenv"):
            self.assertNotIn(value, said)
        # A secret of the job's own stack that the manifest declares with --local-env starts.
        os.remove(Path(self.repo, "apps", "web", ".env.local"))
        env = self.guard.env("Vitest", base={"PATH": os.environ["PATH"], "SESSION_SECRET": "local-only"})
        self.assertEqual(env["SESSION_SECRET"], "local-only")

    def test_a_failure_in_a_run_that_refused_anything_cannot_be_judged(self):
        self.guard.judge("a.test.ts: t (AssertionError: x)", "the run")  # nothing refused: nothing to say
        Path(self.guard.record).write_text(json.dumps({"to": "192.0.2.1:80", "by": "node", "pid": 1}) + "\n")
        with self.assertRaisesRegex(CannotRun, r"a.test.ts: t \(AssertionError: x\) failed in the run, which tried to reach "
                                               r"192.0.2.1:80: the gate refused it.*cannot be told from the refusal"):
            self.guard.judge("a.test.ts: t (AssertionError: x)", "the run")
        self.guard.judge(None, "the run")  # passed all the same: noted, not refused


if __name__ == "__main__":
    unittest.main()
