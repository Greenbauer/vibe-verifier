"""Playwright runs for regression-test-fails-on-base: a fix's browser specs, run by a Playwright config the
manifest names (--playwright), against the site that config's webServer starts on this machine, once at the head
and once on the code before the fix.

The config's own webServer serves the code under test: Playwright starts it from the working tree as the gate
left it, so the site serves the head in the first run and the code before the fix in the second. The repository
provides the rest: a webServer that serves from the working tree (a dev server, or a build and a start), and the
job's own local stack (a local database started before the gate) for that site to read.

The gate never runs the config as it is. For each Playwright call it writes a wrapper beside the config, a .ts
file (Playwright 1.59 and 1.64 both load a .ts wrapper of a .ts, .mts, .js, .mjs or .cjs config, in a CommonJS or
an ES module package; an .mjs wrapper of a .ts config fails on 1.59), which imports the config and:
- gives every browser context a proxy that is a port on this machine the gate holds bound and never listens on,
  bypassed only for loopback names, so a page or the `request` fixture that reaches for any other host fails;
- starts every webServer itself (`reuseExistingServer: false`): a server already running serves whatever code it
  was started with, so Playwright fails on one rather than test the wrong code;
- reports through the gate's reporter (REPORTER), and writes the webServers it was given to a file, since a list
  of webServers is not in what a reporter sees.

Before each run, `playwright test --list`, which loads the config and the specs and starts no server, no global
setup and no browser, says which project collects which spec and with what baseURL. The gate refuses (exit 2)
before any server or browser starts: a credential for somewhere else in what the run inherits (_local_only.py),
a project that runs a changed spec, or one such a project depends on, whose baseURL is not set or not this
machine, a webServer url that is not this machine, and a config with no webServer. A preview is refused like any
other address: it serves the pull request's head, never the code before the fix.

Each run is one attempt (--retries=0) with its output in a scratch directory. A test that reported `expected` is
a pass, `unexpected` a failure with its error, `skipped` a skip. A file that does not load stops Playwright
collecting any file, so a listing that fails is listed again one file at a time to find which. A test that
reached for a blocked host, a failed test of another file (a setup project the spec depends on), and an error
no test owns (a webServer that did not start, a global setup that threw) are exit 2: the run cannot say whether
the bug shows.
"""
import json
import os
import re
import shutil
import socket
import tempfile

import _local_only
from _contract import CannotRun
from _regression_runners import ADVICE, Run, first_line, remaining
from _tools import run_tool

CLIS = (os.path.join("@playwright", "test", "cli.js"), os.path.join("playwright", "cli.js"))
BYPASS = "localhost,127.0.0.1,[::1],*.localhost"
ANSI = re.compile(r"\x1b\[[0-9;]*m")
REGEX_SPECIAL = re.compile(r"([\\^$.*+?()[\]{}|/])")
LOCAL_ONLY = ("This gate runs a spec only against a site the config's webServer starts on this machine, reading the "
              "job's own local stack: never production, and never a preview, which serves the pull request's head and "
              "not the code before the fix")
REFUSED = ("%s would run the pull request's code with a credential for somewhere other than this machine in what it "
           "inherits, so this gate does not start it: %s. Run the gate in a step whose environment holds only the "
           "job's own local stack, and declare a local secret the stack needs with --local-env NAME")
WRAPPER = """// Written by regression-test-fails-on-base for one Playwright call, and removed after it.
import * as loaded from "./%(config)s";
import { writeFileSync } from "node:fs";

const config = (loaded.default && loaded.default.default) || loaded.default || loaded;
const proxy = { server: "http://127.0.0.1:%(port)d", bypass: "%(bypass)s" };
const guard = (use) => Object.assign({}, use, { proxy });
const servers = [].concat(config.webServer || []);
writeFileSync(%(servers)s, JSON.stringify(servers.map((server) => ({
  url: server.url || null, port: server.port || null, cwd: server.cwd || null, env: server.env || {} }))));
const wrapped = Object.assign({}, config, { use: guard(config.use), reporter: [[%(reporter)s, { outputFile: %(report)s }]] });
if (config.projects) wrapped.projects = config.projects.map((project) => Object.assign({}, project, { use: guard(project.use) }));
if (servers.length) wrapped.webServer = servers.map((server) => Object.assign({}, server, { reuseExistingServer: false }));
export default wrapped;
"""
# Written beside the wrapper for each call, so the reporter and the wrapper that names it are always one pair.
REPORTER = """// regression-test-fails-on-base's Playwright reporter: the projects, and each test's outcome and error.
const fs = require("fs");
class Report {
  constructor(options) { this.outputFile = options.outputFile; this.errors = []; }
  onBegin(config, suite) { this.config = config; this.suite = suite; }
  onError(error) { this.errors.push(error.message || error.value || String(error)); }
  onEnd() {
    const projects = (this.config ? this.config.projects : []).map((project) => ({
      name: project.name, baseURL: (project.use || {}).baseURL || null,
      dependencies: project.dependencies || [], teardown: project.teardown || null }));
    const tests = (this.suite ? this.suite.allTests() : []).map((test) => {
      const last = test.results[test.results.length - 1];
      const error = last && (last.error || (last.errors || [])[0]);
      const project = test.parent.project();
      return { file: test.location.file, project: project ? project.name : "", titles: test.titlePath().slice(3),
               outcome: test.outcome(), error: error ? error.message || error.value || "" : null };
    });
    fs.writeFileSync(this.outputFile, JSON.stringify({ projects, tests, errors: this.errors }));
  }
  printsToStdio() { return false; }
}
module.exports = Report;
"""


def cli(directory):
    """The project's Playwright command line nearest `directory` (absolute), or None."""
    while True:
        for entry in CLIS:
            path = os.path.join(directory, "node_modules", entry)
            if os.path.isfile(path):
                return path
        parent = os.path.dirname(directory)
        if parent == directory:
            return None
        directory = parent


def pattern(path):
    """A Playwright file filter selecting exactly `path` (absolute): Playwright reads `/.../` as one regex."""
    return "/^%s$/" % REGEX_SPECIAL.sub(r"\\\1", path)


def described(message):
    """A Playwright error as one line: its first line, and the Expected and Received lines of an expect()."""
    lines = [line.strip() for line in ANSI.sub("", message or "").splitlines() if line.strip()]
    if not lines:
        return ""
    return " ".join([first_line(lines[0])] + [line for line in lines[1:8] if line.startswith(("Expected", "Received"))])


def refuse_credentials(where, environment, directories, local_names, servers=()):
    refused = _local_only.refusals(environment, directories, local_names, servers)
    if refused:
        raise CannotRun(REFUSED % (where, "; ".join(refused)))


def read_json(path):
    try:
        with open(path, encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return None


def call(repo, config, files, listing, local_names, deadline, timeout):
    """One `playwright test` of `files` (repository-relative) under `config`, through the wrapper: (the reporter's
    report, the webServers the config gave, the blocked port). The environment is checked first, every call."""
    root = os.path.realpath(repo)
    directory = os.path.dirname(os.path.join(root, config))
    if not os.path.isfile(os.path.join(root, config)):
        raise CannotRun("--playwright %s is not a file in the repository" % config)
    refuse_credentials("--playwright " + config, dict(os.environ), [root, directory], local_names)
    entry = cli(directory)
    if entry is None or not shutil.which("node"):
        raise CannotRun("no Playwright is installed for %s: install the project's dependencies, its browsers and its local "
                        "stack before this gate, which runs the project's own Playwright" % config)
    scratch = tempfile.mkdtemp(prefix="vv-regression-")
    wrapper = os.path.join(directory, ".vv-regression-%d.config.ts" % os.getpid())
    block = socket.socket()
    try:
        block.bind(("127.0.0.1", 0))  # bound and never listening: a connection to it is refused
        port = block.getsockname()[1]
        report, servers, reporter = (os.path.join(scratch, name) for name in ("report.json", "servers.json", "reporter.cjs"))
        with open(reporter, "w", encoding="utf-8") as handle:
            handle.write(REPORTER)
        with open(wrapper, "w", encoding="utf-8") as handle:
            handle.write(WRAPPER % {"config": os.path.basename(config), "port": port, "bypass": BYPASS, "servers": json.dumps(servers),
                                    "reporter": json.dumps(reporter), "report": json.dumps(report)})
        command = ["node", entry, "test", "--config", wrapper, "--output", os.path.join(scratch, "results"), "--retries=0"]
        command += ["--list", "--pass-with-no-tests"] if listing else []
        code, output = run_tool(command + [pattern(os.path.join(root, path)) for path in files], directory,
                                remaining(deadline, timeout), "Playwright", budget=timeout, advice=ADVICE)
        found = read_json(report)
        if not isinstance(found, dict):
            raise CannotRun("Playwright wrote no report for %s (exit %d): %s" % (config, code, output.strip()[-2000:]))
        return found, read_json(servers) or [], port
    finally:
        block.close()
        if os.path.lexists(wrapper):
            os.remove(wrapper)
        shutil.rmtree(scratch, ignore_errors=True)


def listed(repo, config, files, local_names, deadline, timeout):
    """({path: {the projects that collect it}}, {path: why it does not load}, {project: what it is}, webServers) of
    `files` under `config`, from `playwright test --list`."""
    report, servers, _ = call(repo, config, files, True, local_names, deadline, timeout)
    if report["errors"] and len(files) > 1:  # one file that does not load stops the whole collection
        collected, broken = {}, {}
        for path in files:
            one, why, projects, servers = listed(repo, config, [path], local_names, deadline, timeout)
            collected.update(one)
            broken.update(why)
        return collected, broken, projects, servers
    root = os.path.realpath(repo)
    wanted = {os.path.realpath(os.path.join(root, path)): path for path in files}
    collected = {}
    for test in report["tests"]:
        path = wanted.get(os.path.realpath(test["file"]))
        if path:
            collected.setdefault(path, set()).add(test["project"])
    broken = {files[0]: described(report["errors"][0])} if report["errors"] else {}
    return ({} if broken else collected), broken, {project["name"]: project for project in report["projects"]}, servers


def collected(repo, configs, paths, local_names, deadline, timeout):
    """Which of `paths` a --playwright config collects: ({path: the first config that does}, {path: why it does not
    load under one, for a path none collects})."""
    found, broken = {}, {}
    for config in configs:
        left = [path for path in paths if path not in found]
        if not left:
            break
        tests, errors, _, _ = listed(repo, config, left, local_names, deadline, timeout)
        found.update(dict.fromkeys(tests, config))
        for path, why in errors.items():
            broken.setdefault(path, why)
    return found, {path: why for path, why in broken.items() if path not in found}


def refuse_targets(config, files, tests, projects, servers, directory, local_names):
    """Exit 2 unless every project that runs `files`, and every project those depend on, goes to this machine, and the
    config starts a webServer here whose env and .env files hold no credential for anywhere else."""
    running = {project for path in files for project in tests[path]}
    pending = list(running)
    while pending:
        project = projects.get(pending.pop(), {})
        for name in project.get("dependencies", []) + ([project["teardown"]] if project.get("teardown") else []):
            if name not in running:
                running.add(name)
                pending.append(name)
    problems = []
    for name in sorted(running):
        label, url = "project %s" % name if name else "its project", projects.get(name, {}).get("baseURL")
        if not url:
            problems.append("%s sets no baseURL, so where its specs go cannot be checked" % label)
        elif not _local_only.local_url(url):
            problems.append("%s targets %s, which is not this machine" % (label, url))
    if not servers:
        problems.append("it starts no webServer, so nothing serves the code under test from this working tree")
    problems += ["webServer %d waits for %s, which is not this machine" % (index + 1, server["url"])
                 for index, server in enumerate(servers) if server.get("url") and not _local_only.local_url(server["url"])]
    if problems:
        raise CannotRun("--playwright %s: %s. %s" % (config, "; ".join(problems), LOCAL_ONLY))
    refuse_credentials("--playwright " + config, {}, [os.path.normpath(os.path.join(directory, server.get("cwd") or "."))
                                                       for server in servers], local_names, servers)


def read_run(report, wanted, config, port, repo):
    """{path: Run} of the wanted {real path: path} in one run's report."""
    if report["errors"]:
        raise CannotRun("the Playwright run of %s stopped before its tests could show anything: %s"
                        % (config, described(report["errors"][0])))
    blocked = re.compile(r"ERR_PROXY_CONNECTION_FAILED|NS_ERROR_PROXY_CONNECTION_REFUSED|ECONNREFUSED 127\.0\.0\.1:%d\b" % port)
    runs = {path: Run(None, {}) for path in wanted.values()}
    for test in report["tests"]:
        error = described(test["error"])
        name = " > ".join(([test["project"]] if test["project"] else []) + test["titles"])
        path = wanted.get(os.path.realpath(test["file"]))
        where = "%s: %s" % (path or os.path.relpath(test["file"], repo), name)
        if blocked.search(ANSI.sub("", test["error"] or "")):
            raise CannotRun("%s reached for a host that is not this machine, which this gate blocks: %s. %s"
                            % (where, error, LOCAL_ONLY))
        status = {"expected": "passed", "skipped": "skipped"}.get(test["outcome"], "failed")
        if path is None:
            if status == "failed":
                raise CannotRun("%s failed in the run, and a changed spec depends on it (a setup project, say), so the "
                                "run cannot show whether the bug shows: %s" % (where, error))
            continue
        runs[path].tests[name] = (status, error if status == "failed" else "")
    return runs


def run_playwright(repo, plans, local_names, deadline, timeout):
    """Each file in `plans` ({path: config}) under its config, against the site the config's webServer starts on this
    machine. A file that does not load, or that the config no longer collects, is a Run with that as its load error."""
    root = os.path.realpath(repo)
    runs = {}
    for config in sorted(set(plans.values())):
        files = sorted(path for path, which in plans.items() if which == config)
        tests, errors, projects, servers = listed(repo, config, files, local_names, deadline, timeout)
        for path in files:
            if path not in tests:
                runs[path] = Run(errors.get(path) or "%s does not collect it" % config, {})
        loadable = [path for path in files if path in tests]
        if loadable:
            refuse_targets(config, loadable, tests, projects, servers, os.path.dirname(os.path.join(root, config)), local_names)
            report, _, port = call(repo, config, loadable, False, local_names, deadline, timeout)
            runs.update(read_run(report, {os.path.realpath(os.path.join(root, path)): path for path in loadable}, config, port, root))
    return runs
