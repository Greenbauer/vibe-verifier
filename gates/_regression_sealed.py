"""The sealed run of regression-test-fails-on-base's browser specs: a Docker network the kernel keeps to itself.

Every Playwright call of the gate (the listing, the run at the head, the run on the code before the fix) runs in a
container of the project's Playwright image (mcr.microsoft.com/playwright:v<its version>-noble, or --browser-image)
on one Docker network created `--internal` with no address on its bridge (com.docker.network.bridge.inhibit_ipv4):
a container there has a route to the network's other containers and to nothing else, not a cloud metadata service
and, for a container with no capability such as the run's, not this machine, and Docker's DNS answers it no outside
name. Whatever runs in the run's container (the site's server, the browser, a spec, a global setup, any process they
start, over TCP or UDP) can reach only the network. The limit: the stack's own containers keep Docker's default
capabilities and this machine keeps an interface on the network (with no IPv4 address), so a root process in one
of them could send raw traffic to this machine itself.

The job's local stack (--stack-network NAME, the Docker network its containers run on, such as Supabase's
`supabase_network_<project>`) is moved onto that network at the first run (a listing starts no site, so it does not
wait for that): each container leaves every network it was on, joins the sealed one under the same names and
aliases, and is restarted so that nothing in it keeps an address from before (an API gateway caches the addresses
of the services behind it). A container on an internal network publishes no port, so while the stack is sealed a
relay (RELAY_JS, a container of its own on an ordinary network, joined to the sealed one) publishes the same ports
on this machine again and forwards each to its container: the gate's own runs on this machine (its node:test and
Python runs are tested) reach the stack as before. The relay runs only this code and connects only to the stack. After the
runs the relay goes, and each container is moved back and restarted again, so the job's later steps find the stack
as they left it. The relay forwards no IP packet (net.ipv4.ip_forward=0), so a process on the sealed network that
sends one to it, raw, gets nowhere; it and the run's container hold no capability (--cap-drop ALL), so no process in
them opens a raw socket or changes a route, even when the gate runs as root.

Inside the run's container the gate's entry (SEAL_JS) forwards each port the stack publishes on this machine
(127.0.0.1:<host port>) to its container on the network, so the site's settings for the stack work unchanged, and
records what the network refused: every outside name a process looked up (Docker's DNS hands a name it does not
know to a recorder at 127.0.0.1:53, set with --dns; --dns-search . keeps the runner's own search domain off it) and the kernel's count of IPv4 packets that had no route (Ip
OutNoRoutes in the container's own /proc/net/snmp, for an address given as numbers, from any process). An IPv6
address given as numbers is refused the same way but not counted: Chromium's own check of whether IPv6 reaches the
internet counts there on every run. The project's own files are mounted at the same path, and the run's user is this process's.
"""
import ipaddress
import json
import os
import secrets
import subprocess
import sys
import time
from contextlib import contextmanager

from _contract import CannotRun
from _regression_runners import ADVICE, remaining
from _tools import run_tool

IMAGE = "mcr.microsoft.com/playwright:v%s-noble"
SAID = "regression-test-fails-on-base: "
# The gate's own environment goes into the run, but not what describes this machine rather than the job.
NOT_PASSED = {"PATH", "HOME", "TMPDIR", "TMP", "TEMP", "PWD", "OLDPWD", "SHLVL", "_", "USER", "LOGNAME", "SHELL",
              "HOSTNAME", "TERM", "NODE_OPTIONS", "PLAYWRIGHT_BROWSERS_PATH", "VV_SEAL_RECORD"}
SEAL_JS = r"""// Written by regression-test-fails-on-base: the entry of a sealed run's container.
const { spawn } = require("child_process");
const dgram = require("dgram"), fs = require("fs"), net = require("net");
const [forwards, ...command] = process.argv.slice(2);
const names = new Set();
// IPv4 packets the kernel found no route for: the network refusing an address given as numbers, from any process.
// (Not IPv6: Chromium checks whether IPv6 reaches the internet on its first navigation, which counts there every run.)
const noRoutes = () => {
  const rows = fs.readFileSync("/proc/net/snmp", "utf8").split("\n").filter((line) => line.startsWith("Ip:"));
  const keys = rows[0].split(/\s+/), values = rows[1].split(/\s+/);
  return Number(values[keys.indexOf("OutNoRoutes")]);
};
const before = noRoutes();
// Docker's DNS hands this every name it does not know: each is recorded and answered "no such name".
const dns = dgram.createSocket("udp4").on("message", (query, from) => {
  const labels = [];
  for (let at = 12; at < query.length && query[at]; at += query[at] + 1) labels.push(query.subarray(at + 1, at + 1 + query[at]).toString());
  const name = labels.join(".").toLowerCase();
  if (name && name !== "localhost" && !name.endsWith(".localhost")) names.add(name);
  const answer = Buffer.from(query);
  answer[2] = 0x80 | (query[2] & 0x01);
  answer[3] = 0x83;
  dns.send(answer, from.port, from.address);
});
const listening = [new Promise((resolve, reject) => dns.once("error", reject).bind(53, "127.0.0.1", resolve))];
// Each port the stack publishes on the machine, at the same loopback address here, to its container on the network.
for (const [port, host, target] of JSON.parse(forwards)) {
  const server = net.createServer((client) => {
    const upstream = net.connect(target, host);
    const close = () => { client.destroy(); upstream.destroy(); };
    client.on("error", close).pipe(upstream).on("error", close).pipe(client);
  });
  listening.push(new Promise((resolve, reject) => server.once("error", reject).listen(port, "127.0.0.1", resolve)));
}
Promise.all(listening).then(() => {
  const child = spawn(command[0], command.slice(1), { stdio: "inherit" });
  child.on("exit", (code, signal) => {
    fs.writeFileSync(process.env.VV_SEAL_RECORD, JSON.stringify({ names: [...names].sort(), noRoutes: noRoutes() - before }));
    process.exit(signal ? 1 : code);
  });
}, (error) => { console.error("regression-test-fails-on-base: the sealed run could not start: " + error.message); process.exit(70); });
"""
RELAY_JS = r"""// Written by regression-test-fails-on-base: the sealed stack's ports, published on this machine again.
const net = require("net"), os = require("os");
// Its own network's address only: the sealed network, joined after this starts, gets no listener.
const own = Object.values(os.networkInterfaces()).flat().find((entry) => entry.family === "IPv4" && !entry.internal).address;
const servers = JSON.parse(process.argv[1]).map(([port, host, target]) => new Promise((resolve) => net.createServer((client) => {
  const upstream = net.connect(target, host);
  const close = () => { client.destroy(); upstream.destroy(); };
  client.on("error", close).pipe(upstream).on("error", close).pipe(client);
}).listen(port, own, resolve)));
Promise.all(servers).then(() => console.log("ready"));
"""


def took(started, what):
    print("%s%s: %.1fs" % (SAID, what, time.monotonic() - started), file=sys.stderr, flush=True)


def docker(*args, timeout=300):
    """The output of one docker command; a failure, or no Docker at all, is CannotRun."""
    try:
        result = subprocess.run(["docker", *args], capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError:
        raise CannotRun("no docker command: this gate runs browser specs in a Docker network sealed off from everything "
                        "but the job's own containers, so the runner needs Docker")
    except subprocess.TimeoutExpired:
        raise CannotRun("docker %s did not finish within %ds" % (args[0], timeout))
    if result.returncode:
        raise CannotRun("docker %s failed: %s" % (" ".join(args[:3]), (result.stderr or result.stdout).strip()[-1000:]))
    return result.stdout


def image_for(directory, given):
    """The image a config in `directory` runs in: `given`, or the official one of the project's Playwright version."""
    if given:
        return given
    while True:
        manifest = os.path.join(directory, "node_modules", "playwright-core", "package.json")
        if os.path.isfile(manifest):
            with open(manifest, encoding="utf-8") as handle:
                return IMAGE % json.load(handle)["version"]
        parent = os.path.dirname(directory)
        if parent == directory:
            raise CannotRun("no playwright-core is installed above %s, so the Playwright image to run in is not known: "
                            "install the project's dependencies, or name one with --browser-image" % directory)
        directory = parent


def bound(address, port):
    """docker run's -p for `port` on `address`, as the stack published it: "" or an unspecified address is every interface."""
    if not address or ipaddress.ip_address(address).is_unspecified:
        return "%d:%d" % (port, port)
    return ("[%s]:%d:%d" if ":" in address else "%s:%d:%d") % (address, port, port)


def members(network):
    """{container: {network: aliases}} of every container on `network`, and {container: [[host address, host port,
    port]]} of the TCP ports each publishes on this machine."""
    found = json.loads(docker("network", "inspect", network))[0].get("Containers") or {}
    joined, ports = {}, {}
    for name in sorted(member["Name"] for member in found.values()):
        info = json.loads(docker("inspect", name))[0]
        joined[name] = {net: sorted(set(conf.get("Aliases") or [])) for net, conf in info["NetworkSettings"]["Networks"].items()}
        ports[name] = [[binding.get("HostIp") or "", int(binding["HostPort"]), int(port.split("/")[0])]
                       for port, bindings in (info["HostConfig"].get("PortBindings") or {}).items() if port.endswith("/tcp")
                       for binding in bindings or [] if binding.get("HostPort")]
    return joined, ports


class Seal:
    """The sealed network, the stack moved onto it, and the runs in it. Opened at the first run, closed once."""

    def __init__(self, stacks, image):
        self.stacks, self.image, self.network, self.relay, self.pulled = stacks, image, None, None, set()
        self.entered, self.joined, self.forwards, self.published = False, {}, [], {}

    def open(self, deadline, timeout):
        started = time.monotonic()
        docker("version", "--format", "{{.Server.Version}}", timeout=30)
        network = "vv-regression-%d-%s" % (os.getpid(), secrets.token_hex(3))
        docker("network", "create", "--internal", "-o", "com.docker.network.bridge.inhibit_ipv4=true", network)
        self.network = network
        took(started, "the sealed network is ready")

    def enter(self, image, deadline, timeout):
        """Move the stack onto the sealed network, restart it, and publish its ports again through the relay."""
        started, self.entered = time.monotonic(), True
        for stack in self.stacks:
            self.gather(stack)
        for name, networks in self.joined.items():
            aliases = sorted({alias for values in networks.values() for alias in values} | {name})
            docker("network", "connect", *[arg for alias in aliases for arg in ("--alias", alias)], self.network, name)
            for network in networks:
                docker("network", "disconnect", network, name)
        self.restart(deadline, timeout)
        if self.published:
            self.publish(image, deadline, timeout)
        took(started, "%d stack container(s) moved onto it and restarted%s" % (len(self.joined), ", its %d port(s) published "
                                                                             "again through a relay" % len(self.published) if self.published else ""))

    def gather(self, stack):
        """Read the containers of `stack`, their networks and names, and the ports they publish on this machine."""
        joined, ports = members(stack)
        if not joined:
            raise CannotRun("--stack-network %s has no containers: start the job's local stack before this gate" % stack)
        self.joined.update(joined)
        for name, published in ports.items():
            for address, port, target in published:  # one relay listener a port; one on every interface wins
                self.forwards.append([port, name, target])
                known = self.published.get(port)
                self.published[port] = [name, target, "" if known and known[2] != address else address]

    def publish(self, image, deadline, timeout):
        """The relay: on an ordinary network of its own, with the stack's ports, then joined to the sealed network."""
        self.relay = self.network + "-relay"
        docker("network", "create", self.relay)
        ports = [arg for port, (_, _, address) in sorted(self.published.items()) for arg in ("-p", bound(address, port))]
        targets = [[port, name, target] for port, (name, target, _) in sorted(self.published.items())]
        # A container forwards IP packets by default, and this one sits on the sealed network and an ordinary one: it
        # forwards none, and holds no capability with which a process could change that.
        docker("run", "-d", "--init", "--name", self.relay, "--network", self.relay, "--user", "%d:%d" % (os.getuid(), os.getgid()),
               "--cap-drop", "ALL", "--sysctl", "net.ipv4.ip_forward=0", *ports, image, "node", "-e", RELAY_JS, json.dumps(targets))
        docker("network", "connect", self.network, self.relay)
        while "ready" not in docker("logs", self.relay):
            if remaining(deadline, timeout) < 1:
                raise CannotRun("the relay for the sealed stack's ports did not start. %s" % ADVICE)
            time.sleep(0.2)

    def restart(self, deadline, timeout):
        """Restart the stack's containers and wait until each is running and, where it has a health check, healthy."""
        if not self.joined:
            return
        docker("restart", "-t", "10", *self.joined)
        while True:
            states = [json.loads(docker("inspect", name))[0]["State"] for name in self.joined]
            if all(state["Running"] and state.get("Health", {}).get("Status", "healthy") == "healthy" for state in states):
                return
            if remaining(deadline, timeout) < 1:
                raise CannotRun("the job's stack was not healthy again after the gate moved it. %s" % ADVICE)
            time.sleep(1)

    def close(self, deadline, timeout):
        """Move the stack back onto its own networks and restart it there, then remove the sealed network. Each step
        reads where a container is first, so a seal that stopped halfway is undone as far as it went."""
        started = time.monotonic()
        try:
            if self.relay:  # first, so the ports are free for the stack again
                subprocess.run(["docker", "rm", "-f", self.relay], capture_output=True)
                subprocess.run(["docker", "network", "rm", self.relay], capture_output=True)
            for name, networks in self.joined.items():
                now = json.loads(docker("inspect", name))[0]["NetworkSettings"]["Networks"]
                for network, aliases in networks.items():
                    if network not in now:
                        docker("network", "connect", *[arg for alias in aliases for arg in ("--alias", alias)], network, name)
                if self.network in now:
                    docker("network", "disconnect", self.network, name)
            self.restart(deadline + 120, timeout)
        finally:
            docker("network", "rm", self.network)
        if self.joined:
            took(started, "the stack is back on its own network and restarted")

    def run(self, command, directory, root, scratch, image, deadline, timeout):
        """(exit code, output, what the network refused) of `command` run in `image` on the sealed network, from
        `directory`, with the repository and `scratch` mounted where they are here."""
        if image not in self.pulled:
            exists = subprocess.run(["docker", "image", "inspect", image], capture_output=True).returncode == 0
            if not exists:
                started = time.monotonic()
                docker("pull", "--quiet", image, timeout=max(int(remaining(deadline, timeout)), 1))
                took(started, "pulled " + image)
            self.pulled.add(image)
        if "--list" not in command and not self.entered:
            self.enter(image, deadline, timeout)
        record, entry = os.path.join(scratch, "sealed.json"), os.path.join(scratch, "seal.cjs")
        with open(entry, "w", encoding="utf-8") as handle:
            handle.write(SEAL_JS)
        name, started = "vv-regression-run-%s" % secrets.token_hex(4), time.monotonic()
        passed = [arg for key in sorted(os.environ) if key not in NOT_PASSED and not key.startswith("DOCKER_")
                  for arg in ("-e", key)]
        try:
            code, output = run_tool(
                ["docker", "run", "--rm", "--init", "--name", name, "--network", self.network, "--dns", "127.0.0.1", "--dns-search", ".",
                 "--user", "%d:%d" % (os.getuid(), os.getgid()), "--cap-drop", "ALL", "--shm-size", "1g", "-e", "HOME=/tmp",
                 "-e", "VV_SEAL_RECORD=" + record, *passed, "-v", "%s:%s" % (root, root), "-v", "%s:%s" % (scratch, scratch),
                 "-w", directory, image, "node", entry, json.dumps(self.forwards), *command],
                directory, remaining(deadline, timeout), "Playwright", budget=timeout, advice=ADVICE)
        finally:
            subprocess.run(["docker", "rm", "-f", name], capture_output=True)
        took(started, "a %s in the sealed network" % ("listing" if "--list" in command else "Playwright run"))
        try:
            with open(record, encoding="utf-8") as handle:
                refused = json.load(handle)
        except (OSError, ValueError):
            raise CannotRun("the sealed Playwright run wrote no record of what its network refused (exit %d): %s"
                            % (code, output.strip()[-2000:]))
        return code, output, refused["names"] + (
            ["%d attempt(s) to reach an address off the network" % refused["noRoutes"]] if refused["noRoutes"] else [])


CURRENT = []


@contextmanager
def sealed(stacks, image, deadline, timeout):
    """Every Playwright call inside shares one sealed network, opened at the first (a fix with no spec opens none)."""
    seal = Seal(stacks, image)
    CURRENT.append(seal)
    try:
        yield seal
    except BaseException:
        if seal.network:  # the error that stopped the run is the one reported; undoing the seal is still tried
            try:
                seal.close(deadline, timeout)
            except CannotRun as error:
                print(SAID + str(error), file=sys.stderr, flush=True)
        raise
    else:
        if seal.network:
            seal.close(deadline, timeout)
    finally:
        CURRENT.pop()


@contextmanager
def session(deadline, timeout):
    """The seal of the gate's run, opened if this is its first call; or, called on its own, a seal of its own."""
    if CURRENT:
        seal = CURRENT[-1]
        if not seal.network:
            seal.open(deadline, timeout)
        yield seal
        return
    with sealed([], None, deadline, timeout) as seal:
        seal.open(deadline, timeout)
        yield seal
