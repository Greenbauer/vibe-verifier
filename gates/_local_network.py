"""What a browser run of regression-test-fails-on-base may reach on the network: this machine only. Two pieces, one
for each side of the run, both recording the host each blocked attempt asked for, so the gate knows the run reached
for somewhere else even when no test's own error says so:

- Refuser, a proxy on this machine that every browser context the config creates, the request fixture and Node's
  global agents (proxy_env) send every host but loopback to; it answers each request 403.
- GUARD, preloaded into every Node process of the run through NODE_OPTIONS (node_guard): the Playwright runner and
  its workers, the site's server, a global setup. It refuses any TCP connection or UDP datagram to an address that
  is not loopback, whatever opens it, judging a name by every address it resolves to.

What the credentials a run inherits may hold is gates/_local_only.py.
"""
import os
import socket
import threading
import urllib.parse


class Refuser:
    """A proxy on this machine that every request of a run goes to, for any host but loopback: it answers each one
    403 and records the host it asked for (`CONNECT host:443`, or the URL of a plain `GET`), so the gate knows the run
    reached for somewhere else even when no test's own error says so (a page's script, an in-page fetch, the site's
    server-side fetch)."""

    def __init__(self):
        self.hosts = []
        self.server = socket.socket()
        self.server.bind(("127.0.0.1", 0))
        self.server.listen(16)
        self.server.settimeout(0.2)
        self.port = self.server.getsockname()[1]
        self.done = threading.Event()
        self.thread = threading.Thread(target=self.serve, daemon=True)
        self.thread.start()

    def serve(self):
        while not self.done.is_set():
            try:
                connection, _ = self.server.accept()
            except OSError:  # the accept timed out, or the server closed
                continue
            with connection:
                try:
                    connection.settimeout(2)
                    line = connection.recv(4096).split(b"\r\n", 1)[0].decode("latin-1").split()
                    target = line[1] if len(line) > 1 else "?"
                    self.hosts.append(urllib.parse.urlsplit(target).netloc or target)
                    connection.sendall(b"HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
                except OSError:
                    pass

    def close(self):
        self.done.set()
        self.thread.join()
        self.server.close()
        return sorted(set(self.hosts))


# What the Node side of a run is told, so its fetch and http go to the gate's proxy for every host but this machine.
NO_PROXY = "localhost,127.0.0.1,::1,[::1],.localhost"


def proxy_env(port):
    """The environment that sends Node's global fetch and http, a library that reads HTTP_PROXY, and on Linux a
    Chromium launched without a proxy of its own (a global setup's sign-in browser), to the proxy at `port` for every
    host but loopback. GUARD stops whatever in Node does not follow it."""
    proxy = "http://127.0.0.1:%d" % port
    return {"HTTP_PROXY": proxy, "HTTPS_PROXY": proxy, "http_proxy": proxy, "https_proxy": proxy,
            "NO_PROXY": NO_PROXY, "no_proxy": NO_PROXY, "NODE_USE_ENV_PROXY": "1"}


# Preloaded into every Node process of a run (NODE_OPTIONS --require): the Playwright runner and its workers, the
# site's server, a global setup. A TCP connection to a host that is not this machine fails as ECONNREFUSED and its
# host is recorded, whatever opens it: fetch, http with its own Agent, a database driver, a raw net.connect.
GUARD = r"""// Written by regression-test-fails-on-base: this process may reach only this machine.
const dgram = require("dgram");
const dns = require("dns");
const fs = require("fs");
const net = require("net");
const loopback = (address) => /^127(\.\d{1,3}){3}$/.test(address) || address === "::1" || /^::ffff:127(\.\d{1,3}){3}$/i.test(address);
const named = (host) => host === "localhost" || /\.localhost\.?$/.test(host);
const record = (where) => { try { fs.appendFileSync(process.env.VV_REGRESSION_REACHED, where + "\n"); } catch (error) {} };
const refused = (where) => Object.assign(new Error("connect ECONNREFUSED " + where + " (regression-test-fails-on-base allows "
  + "only this machine)"), { code: "ECONNREFUSED", syscall: "connect" });
const bare = (host) => String(host).toLowerCase().replace(/^\[|\]$/g, "");
// A name is checked by what it resolves to, through whatever lookup the caller gave: every address must be loopback.
// One that does not resolve is recorded too: the process reached for it, and its answer shaped what happened.
const checked = (own, port) => function (hostname, options, callback) {
  if (typeof options === "function") [callback, options] = [options, {}];
  (own || dns.lookup)(hostname, options, (error, address, family) => {
    const addresses = Array.isArray(address) ? address.map((entry) => entry.address) : [address];
    if (!error && addresses.every((one) => loopback(bare(one)))) return callback(error, address, family);
    record(hostname + ":" + port);
    callback(error || refused(hostname + ":" + port));
  });
};
const connect = net.Socket.prototype.connect;
net.Socket.prototype.connect = function (...args) {
  const options = Array.isArray(args[0]) ? args[0][0] : args[0];
  if (options === null || typeof options !== "object") {  // (port, host, callback) or (path, callback)
    const given = typeof options === "string" && !/^\d+$/.test(options) ? { path: options } : { port: options, host: args[1] };
    return net.Socket.prototype.connect.call(this, given, ...args.filter((value) => typeof value === "function"));
  }
  if (options.path) return connect.apply(this, args);
  const host = bare(typeof options.host === "string" && options.host ? options.host : "localhost");
  if (net.isIP(host) ? loopback(host) : named(host) && !options.lookup) return connect.apply(this, args);
  if (!net.isIP(host)) {
    options.lookup = checked(options.lookup, options.port);
    return connect.apply(this, args);
  }
  const where = (host.includes(":") ? "[" + host + "]" : host) + ":" + options.port;
  record(where);
  process.nextTick(() => this.destroy(refused(where)));
  return this;
};
// UDP: a datagram to an address that is not this machine is dropped and recorded.
const allowed = (address) => address === undefined || named(bare(address)) || loopback(bare(address));
const send = dgram.Socket.prototype.send;
dgram.Socket.prototype.send = function (...args) {
  const address = args.find((value, index) => index > 0 && typeof value === "string");
  if (allowed(address)) return send.apply(this, args);
  const ports = args.filter((value, index) => index > 0 && typeof value === "number");
  record(bare(address) + ":" + ports[ports.length - 1] + "/udp");
  const callback = args.find((value) => typeof value === "function");
  if (callback) process.nextTick(callback, refused(address));
};
const udpConnect = dgram.Socket.prototype.connect;
dgram.Socket.prototype.connect = function (port, address, callback) {
  if (allowed(typeof address === "string" ? address : undefined)) return udpConnect.apply(this, arguments);
  record(bare(address) + ":" + port + "/udp");
  process.nextTick(() => this.emit("error", refused(address)));
};
"""


def node_guard(scratch):
    """({NODE_OPTIONS, VV_REGRESSION_REACHED} that preload GUARD into a run's Node processes, the file it records each
    blocked connection's host in, the `--require` option itself, for a webServer env that sets NODE_OPTIONS)."""
    guard, reached = os.path.join(scratch, "local-only.cjs"), os.path.join(scratch, "reached.txt")
    with open(guard, "w", encoding="utf-8") as handle:
        handle.write(GUARD)
    require = '--require "%s"' % guard
    return {"NODE_OPTIONS": (os.environ.get("NODE_OPTIONS", "") + " " + require).strip(), "VV_REGRESSION_REACHED": reached}, \
        reached, require


def reached_by_node(path):
    """The hosts GUARD refused in a run, from its record."""
    try:
        with open(path, encoding="utf-8") as handle:
            return [line.strip() for line in handle if line.strip()]
    except OSError:
        return []
