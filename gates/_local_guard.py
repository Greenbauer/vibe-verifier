"""What every test process changed-code-mutation, coverage-not-lost and regression-test-fails-on-base start runs under.
They run the pull request's own tests, so before each test process starts the gate refuses (exit 2) a credential for
anywhere else in what it would inherit (gates/_local_only.py: the environment it gets, and every .env file a framework
would load in the repository), and the process starts with a guard that refuses a connection to anything that is not
this machine: loopback, as _local_only reads it (127.0.0.0/8, ::1, an IPv4 loopback address mapped into IPv6, and the
names localhost and *.localhost, whose addresses are looked up and must all be loopback).

- Node: NODE_GUARD, preloaded with NODE_OPTIONS=--require into every Node process and worker thread that inherits the
  variable (Vitest's workers, a node:test file's process, StrykerJS's, a child a test starts). It replaces
  net.Socket.prototype.connect, which every TCP connection made through Node's net module goes through (net, tls, http,
  https, http2, fetch): an address that is not loopback, or a name that is not localhost, is refused before anything
  is sent, a name before it is looked up.
- Python: PYTHON_GUARD, the sitecustomize.py first on PYTHONPATH, which every Python process that inherits the variable
  imports at start (pytest, unittest, mutmut's workers, a child a test starts). socket.socket's connect and connect_ex
  refuse an IPv4 or IPv6 address that is not loopback, TCP or UDP, the same way; a name socket.create_connection looks
  up first has already been sent to the resolver by then. It then runs the sitecustomize it hides (HIDDEN).

A refused connection fails in the test as ECONNREFUSED and is written to the run's record (VV_LOCAL_ONLY_RECORD, a JSON
line naming where it went). After each run the gate reads the record: a run in which a test failed and anything was
refused cannot be judged (exit 2), since the failure cannot be told from the refusal; a run that passed says what it
refused.

Not covered: a process that is neither Node nor Python (curl, psql, the Supabase CLI, bun), a socket opened in native
code (a driver such as libpq under psycopg, or Prisma's query engine), a process started with an environment that drops
NODE_OPTIONS or PYTHONPATH, or with python -I, -E or -S, code that replaces the guarded connect with its own, and UDP
sent without connect (Node's dgram, the DNS queries of dns.resolve, Python's sendto).
"""
import json
import os
import shutil
import sys
import tempfile

import _local_only
from _contract import CannotRun

RECORD = "VV_LOCAL_ONLY_RECORD"
UNJUDGED = ("%s failed in %s, which tried to reach %s: the gate refused it, because a gate's test runs reach only this "
            "machine, so the failure cannot be told from the refusal. Stub that call in the test, or point it at the job's "
            "own local stack")
NODE_GUARD = r"""// vibe-verifier's local-only guard (gates/_local_guard.py), preloaded with NODE_OPTIONS=--require into every
// Node process and worker thread of a gate's test run: a TCP connection to anything but this machine is refused.
"use strict";
const net = require("net");
const dns = require("dns");
const { appendFileSync } = require("fs");
const { constants } = require("os");
const MARK = Symbol.for("vibe-verifier.local-only");
const connect = net.Socket.prototype.connect;
if (!connect[MARK]) {
  const record = process.env.VV_LOCAL_ONLY_RECORD;
  // true for 127.0.0.0/8 and ::1 (an IPv4 loopback address mapped into IPv6 too), false for any other address, null for a name.
  const loopback = (host) => {
    if (net.isIPv4(host)) return host.startsWith("127.");
    if (!net.isIPv6(host)) return null;
    let address;
    try { address = new URL("http://[" + host + "]/").hostname; } catch (error) { return false; }
    return address === "[::1]" || /^\[::ffff:7f[0-9a-f]{2}:[0-9a-f]{1,4}\]$/.test(address);
  };
  const refusal = (to) => {
    if (record) {
      try { appendFileSync(record, JSON.stringify({ to, by: "node", pid: process.pid }) + "\n"); } catch (error) { /* the run is over */ }
    }
    return Object.assign(new Error("connect ECONNREFUSED " + to + ": refused by vibe-verifier, whose test runs reach only this machine"),
                         { code: "ECONNREFUSED", errno: -constants.errno.ECONNREFUSED, syscall: "connect" });
  };
  // Where a connect() call goes ({options, host, port}), or null for a Unix socket or a named pipe. net.connect() hands
  // its parsed arguments on as an array; a direct call passes options, or a port and a host.
  const target = (args) => {
    let options = Array.isArray(args[0]) ? args[0][0] : args[0];
    if (options === null || typeof options !== "object") {
      if (typeof options === "string" && !(Number(options) >= 0)) return null;
      options = { port: options, host: typeof args[1] === "string" ? args[1] : undefined };
    }
    if (options.path) return null;
    const host = String(options.host || "localhost").toLowerCase().replace(/^\[|\]$/g, "").replace(/\.$/, "");
    return { options, host, port: options.port };
  };
  // A localhost name is looked up as the caller would, and refused when any address it gives is not loopback.
  const checked = (lookup, to) => function (hostname, options, callback) {
    if (typeof options === "function") { callback = options; options = {}; }
    lookup(hostname, options, (error, address, family) => {
      const all = error ? [] : Array.isArray(address) ? address.map((entry) => entry.address) : [address];
      const away = all.find((each) => !loopback(String(each)));
      if (away === undefined) callback(error, address, family);
      else callback(refusal(to + " (" + away + ")"));
    });
  };
  const guarded = function (...args) {
    const where = target(args);
    if (where) {
      const to = where.host + ":" + where.port, local = loopback(where.host);
      if (local === false || (local === null && where.host !== "localhost" && !where.host.endsWith(".localhost"))) {
        const error = refusal(to);
        process.nextTick(() => this.destroy(error));
        return this;
      }
      if (local === null) {
        const options = Object.assign({}, where.options, { lookup: checked(where.options.lookup || dns.lookup, to) });
        const callback = Array.isArray(args[0]) ? args[0][1] : args.find((arg) => typeof arg === "function");
        return callback ? connect.call(this, options, callback) : connect.call(this, options);
      }
    }
    return connect.apply(this, args);
  };
  guarded[MARK] = true;
  net.Socket.prototype.connect = guarded;
}
"""
PYTHON_GUARD = r'''# vibe-verifier's local-only guard (gates/_local_guard.py), the sitecustomize every Python process of a gate's test run
# imports at start: a socket connection to anything but this machine is refused.


def _vv_local_only():
    import errno
    import os
    import socket
    if getattr(socket.socket.connect, "_vv_local_only", False):
        return
    record = os.environ.get("VV_LOCAL_ONLY_RECORD")
    connect, connect_ex, lookup = socket.socket.connect, socket.socket.connect_ex, socket.getaddrinfo
    opened, write, close = os.open, os.write, os.close

    def loopback(host):
        """True for 127.0.0.0/8 and ::1 (an IPv4 loopback address mapped into IPv6 too), False for another address,
        None for a name."""
        import ipaddress
        try:
            address = ipaddress.ip_address(host.split("%")[0])
        except ValueError:
            return None
        return (getattr(address, "ipv4_mapped", None) or address).is_loopback

    def away(sock, address):
        """host:port when connecting `sock` to `address` would leave this machine, else None."""
        if sock.family not in (socket.AF_INET, socket.AF_INET6) or not isinstance(address, tuple) or len(address) < 2:
            return None
        host = address[0].decode("ascii", "replace") if isinstance(address[0], (bytes, bytearray)) else str(address[0])
        host = host.strip("[]").rstrip(".").lower()
        to, local = "%s:%s" % (host, address[1]), loopback(host)
        if local is not None:
            return None if local else to
        if host != "localhost" and not host.endswith(".localhost"):
            return to
        try:
            found = [info[4][0] for info in lookup(host, address[1], sock.family, sock.type)]
        except OSError:
            return to + " (which does not resolve)"
        outside = [each for each in found if not loopback(each)]
        return "%s (%s)" % (to, outside[0]) if outside else None

    def refused(to):
        if record:
            import json
            try:
                handle = opened(record, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
                try:
                    write(handle, (json.dumps({"to": to, "by": "python", "pid": os.getpid()}) + "\n").encode())
                finally:
                    close(handle)
            except OSError:
                pass
        return ConnectionRefusedError(errno.ECONNREFUSED, "refused by vibe-verifier, whose test runs reach only this machine: " + to)

    def guarded_connect(self, address):
        to = away(self, address)
        if to:
            raise refused(to)
        return connect(self, address)

    def guarded_connect_ex(self, address):
        to = away(self, address)
        if to:
            refused(to)
            return errno.ECONNREFUSED
        return connect_ex(self, address)

    guarded_connect._vv_local_only = True
    socket.socket.connect, socket.socket.connect_ex = guarded_connect, guarded_connect_ex


_vv_local_only()
'''
# The end of a sitecustomize put first on PYTHONPATH: it runs the sitecustomize it hides, the next one on sys.path (the
# interpreter's own, which Homebrew's and Ubuntu's Python ship, or another of a gate's), if there is one.
HIDDEN = '''

def _hidden():
    import importlib.machinery
    import importlib.util
    import os
    import sys
    here = os.path.dirname(os.path.abspath(__file__))
    entries = [os.path.abspath(entry or os.curdir) for entry in sys.path]
    after = sys.path[entries.index(here) + 1:] if here in entries else []
    spec = importlib.machinery.PathFinder.find_spec("sitecustomize", after)
    if spec is not None and spec.loader is not None:
        spec.loader.exec_module(importlib.util.module_from_spec(spec))


_hidden()
'''


def add_local_env(parser):
    """The --local-env flag every gate that runs the project's tests takes, as the Playwright path reads it."""
    parser.add_argument("--local-env", action="append", metavar="NAME",
                        help="a variable named like a credential that holds the job's own local stack's secret (repeatable)")


class Guard:
    """One test run's guard: a scratch directory holding the Node preload, the Python sitecustomize and the record of
    what the run's processes were refused. `gate` names the notes it prints."""

    def __init__(self, gate, repo, local_names):
        self.gate, self.repo, self.local_names = gate, repo, set(local_names or ())
        self.scratch = tempfile.mkdtemp(prefix="vv-guard-")
        self.node, self.site = os.path.join(self.scratch, "guard.cjs"), os.path.join(self.scratch, "site")
        self.record = os.path.join(self.scratch, "refused.jsonl")
        os.makedirs(self.site)
        for path, text in ((self.node, NODE_GUARD), (os.path.join(self.site, "sitecustomize.py"), PYTHON_GUARD + HIDDEN)):
            with open(path, "w", encoding="utf-8") as handle:
                handle.write(text)

    def __enter__(self):
        return self

    def __exit__(self, *_):
        shutil.rmtree(self.scratch, ignore_errors=True)

    def env(self, where, base=None, extra=None):
        """The environment `where` (what starts, for a refusal) runs with: `base` (default this process's) and `extra`,
        refused (exit 2) when it or a .env file in the repository holds a credential for anywhere else, with the guard
        first on NODE_OPTIONS and PYTHONPATH."""
        environment = dict(os.environ if base is None else base, **(extra or {}))
        _local_only.refuse(where, environment, _local_only.env_files_under(self.repo), self.local_names)
        environment[RECORD] = self.record
        environment["NODE_OPTIONS"] = " ".join(filter(None, ["--require " + json.dumps(self.node), environment.get("NODE_OPTIONS")]))
        environment["PYTHONPATH"] = os.pathsep.join(filter(None, [self.site, environment.get("PYTHONPATH")]))
        return environment

    def refused(self):
        """Where the run's processes were refused, each once, in the order first refused."""
        try:
            with open(self.record, encoding="utf-8", errors="replace") as handle:
                lines = handle.read().splitlines()
        except OSError:
            return []
        found = []
        for line in lines:
            try:
                found.append(str(json.loads(line)["to"]))
            except (ValueError, KeyError, TypeError):
                found.append("an address the record could not name")
        return list(dict.fromkeys(found))

    def judge(self, failed, run, output=""):
        """After `run` (what ran, for the message): exit 2 when `failed`, the first test that failed in it (None when none
        did), failed in a run in which anything was refused, printing the run's `output` first; else each refusal is
        noted."""
        refused = self.refused()
        if refused and failed:
            if output:
                print(output, file=sys.stderr, flush=True)
            raise CannotRun(UNJUDGED % (failed, run, ", ".join(refused)))
        if refused:
            print("%s: %s tried to reach %s, which the gate refused; its tests passed all the same"
                  % (self.gate, run, ", ".join(refused)), file=sys.stderr, flush=True)
