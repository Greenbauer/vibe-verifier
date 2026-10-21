"""node:test runs for the coverage-not-lost gate, under Node's own coverage (`--experimental-test-coverage`).

Which files run, and how, is read the way regression-test-fails-on-base reads it (gates/_regression_node.py): a tracked
JS or TS file that a package.json script's `node ... --test ...` command selects runs from that package with the
command's options and its `NAME=value` assignments, and one a --node-test glob declares runs from the repository root.
The files that run the same way run as one command, in the checkout of the side, with the gate's own coverage options
after the project's (the project's own coverage, reporter, isolation, sharding and watch options are dropped):

    node <the script's options> <--args> --enable-source-maps --test --experimental-test-coverage
         --test-coverage-exclude=**/node_modules/** --test-reporter=<the gate's reporter>
         --test-reporter-destination=<a file> <the files>

The reporter is a module of the gate's (REPORTER) that writes, as JSON lines, each test that passed or failed and the
coverage report Node sends it when the tests are done (the `test:coverage` event): every line of every file the run
loaded, with how many times it ran. A line counts as run when its count is above 0; a blank line, and every line of a
file either side runs with `node --test` (test code, as regression-test-fails-on-base reads it), never counts.

Node's test runner points NODE_V8_COVERAGE at a directory of its own before it starts the test files, and merges every
coverage file written there, so a Node process a test starts and a worker thread are measured too: Node's
child_process copies the variable into the child's environment, even into one the test gives it of its own. A child
killed by a signal writes nothing, and one still running when Node reads the files has written nothing yet, so neither
is measured. `--enable-source-maps` makes Node map a file a loader compiled (ts-node, say) back to the file's own lines
through the source map the loader wrote. Node leaves out its own modules, and the exclude leaves out node_modules: it
replaces Node 24's default, which also leaves out every file named like a default test file (src/test-money.mjs), a
source file the gate must measure. Measured on Node 22.23 and 24.21; a Node before 22.5, the first with
--test-coverage-exclude, is refused (exit 2), and the releases from 22.5 to 22.22 are accepted without having been run
here.

Every command is guarded (gates/_local_guard.py), as the other runners' runs are: a credential for anywhere else in
what it inherits (with the script's assignments and the files an --env-file option of the script or of --args loads)
is exit 2, checked for every command of the --project at the side before the first one starts (projects and sides run
in turn, so a later one can be refused after an earlier one ran), and a connection to anything but this machine is
refused in every Node process that keeps NODE_OPTIONS.
"""
import contextlib
import json
import os
import re
import shlex
import shutil
import tempfile

from _contract import CannotRun, git, matches
from _coverage_runs import ADVICE, ENV, NOT_SOURCE, Coverage, Red, repository_path, time_left
from _local_guard import Guard
from _regression_frameworks import JS
from _regression_node import env_files, plan
from _tools import run_tool

# The first Node with --test-coverage-exclude, which the gate sets so that Node 24's default (leaving out every file named
# like a default test file, test-*.mjs say) cannot hide a source file; node_modules stays out as before.
MINIMUM = (22, 5)
EXCLUDE = "--test-coverage-exclude=**/node_modules/**"
# Node 22 and later read a --test path with a glob character as a glob, so a Next.js route such as `app/[id]/a.test.ts`
# matched nothing: each such character is given as a class that holds only it, which matches the file.
GLOB = re.compile(r"[][*?]")
REPORTER = r"""// vibe-verifier's coverage-not-lost reporter (gates/_coverage_node.py): each test that passed or failed, and the
// coverage report Node sends when the tests are done, as JSON lines.
import { relative } from "node:path";
const said = (error) => {
  const cause = error && error.cause !== undefined ? error.cause : error;  // node:test wraps what a test threw
  const text = String(cause && cause.message !== undefined ? cause.message : cause);
  const lines = text.split("\n").map((line) => line.trim()).filter(Boolean);
  const message = lines.length > 1 && lines[0].endsWith(":") ? lines[0] + " " + lines[1] : (lines[0] || "");
  return ((cause && cause.name) || "Error") + ": " + message.slice(0, 200);
};
export default async function* report(source) {
  for await (const { type, data } of source) {
    if ((type === "test:pass" || type === "test:fail") && data.details.type !== "suite") {
      const failed = type === "test:fail" && !data.todo;  // a todo test that fails does not fail the run
      const where = data.file ? relative(process.cwd(), data.file) + " > " : "";
      yield JSON.stringify(failed ? { failed: where + data.name + ": " + said(data.details.error) } : { passed: data.name }) + "\n";
    } else if (type === "test:coverage") {
      yield JSON.stringify({ coverage: data.summary.files.map((file) => ({
        path: file.path,
        ran: file.lines.filter((line) => line.count > 0).map((line) => line.line),
        missed: file.lines.filter((line) => line.count === 0).map((line) => line.line),
      })) }) + "\n";
    }
  }
}
"""


def literal(path):
    """`path` as a --test argument that selects exactly that file."""
    return GLOB.sub(lambda found: "[%s]" % found.group(0), path)


def node_version():
    """Exit 2 unless the node on PATH is MINIMUM or later."""
    if not shutil.which("node"):
        raise CannotRun("the node-test runner needs node on PATH")
    code, output = run_tool(["node", "--version"], None, 60, "node --version")
    found = re.match(r"v(\d+)\.(\d+)\.", output.strip())
    if code != 0 or not found:
        raise CannotRun("`node --version` printed no version: %s" % output.strip()[-200:])
    if (int(found.group(1)), int(found.group(2))) < MINIMUM:
        raise CannotRun("the node on PATH is %s, and coverage of node:test files needs Node %d.%d or later: run the gate "
                        "with the project's Node %d.%d or later first on PATH" % (output.strip(), *MINIMUM, *MINIMUM))


def events(path):
    """The reporter's JSON lines, or [] when it wrote none."""
    try:
        with open(path, encoding="utf-8", errors="replace") as handle:
            return [json.loads(line) for line in handle if line.strip()]
    except (OSError, ValueError):
        return []


class NodeTest:
    """The files a repository runs with `node --test`, under the Node on PATH, with Node's own coverage."""

    def __init__(self, args, repo, scratch):
        self.args, self.repo, self.scratch, self.local = shlex.split(args.args), repo, scratch, args.local_env
        self.declared = tuple(args.node_test or ())
        node_version()
        self.reporter = os.path.join(scratch, "vibe-verifier-coverage-reporter.mjs")
        with open(self.reporter, "w", encoding="utf-8") as handle:
            handle.write(REPORTER)

    def plans(self, tree, paths):
        """{(package, options, assignments): (Plan, [paths])} for the paths `tree` runs with `node --test`."""
        groups = {}
        for path in sorted(paths):
            how = plan(tree, path, self.declared) if path.endswith(JS) else None
            if how:
                key = (how.cwd, tuple(how.flags), tuple(sorted(how.env.items())))
                groups.setdefault(key, (how, []))[1].append(path)
        return groups

    def run(self, tree, project, deadline, budget, files=()):
        root = os.path.realpath(tree)
        paths = [os.path.relpath(path, root) for path in files] or \
            [path for path in git(tree, "ls-files", "-z", "--", project).split("\0") if path]
        lines, tested, groups = {}, False, self.plans(tree, paths).values()
        with contextlib.ExitStack() as guards:
            runs = []
            for how, chosen in groups:  # every command's guard first: a refusal runs no test
                what = "the node --test run of %s in the %s checkout" % (how.source, os.path.basename(tree))
                guard = guards.enter_context(Guard("coverage-not-lost", self.repo, self.local, more=(tree,)))
                loads = env_files(how._replace(flags=[*how.flags, *self.args]))  # --env-file from the script or --args
                runs.append((how, chosen, what, guard, guard.env(what, base=ENV, extra=how.env, files=loads)))
            for run in runs:
                ran, any_test = self.run_files(tree, *run, deadline, budget)
                tested = tested or any_test
                for path, numbers in ran.items():
                    merged = lines.setdefault(path, {})
                    for number, hit in numbers.items():
                        merged[number] = merged.get(number, False) or hit
        tests = [path for _, chosen in groups for path in chosen]
        for path in tests:  # test code
            lines.pop(path, None)
        return Coverage(lines, tested, tests=tests)

    def run_files(self, tree, how, paths, what, guard, env, deadline, budget):
        """({path: {line: ran}}, whether any test ran) of one guarded `node --test` command over `paths`, run as `how` says."""
        report = os.path.join(tempfile.mkdtemp(prefix="run-", dir=self.scratch), "report.jsonl")
        given = [literal(os.path.relpath(os.path.join(os.path.realpath(tree), path), how.cwd)) for path in paths]
        command = ["node", *how.flags, *self.args, "--enable-source-maps", "--test", "--experimental-test-coverage", EXCLUDE,
                   "--test-reporter=" + self.reporter, "--test-reporter-destination=" + report, *given]
        code, output = run_tool(command, how.cwd, time_left(deadline), "node --test", env=env, budget=budget, advice=ADVICE)
        said = events(report)
        failed = [event["failed"] for event in said if "failed" in event]
        red = code != 0 or bool(failed)
        guard.judge((failed or ["the suite (node --test exited %d)" % code])[0] if red else None, what)
        if red:
            raise Red("node --test exited %d" % code, "\n".join(failed[:20]) or output[-3000:])
        tested = any("passed" in event for event in said)
        reports = [event["coverage"] for event in said if "coverage" in event]
        if tested and not reports:
            raise CannotRun("node --test ran the tests of %s and wrote no coverage report: %s" % (how.source, output.strip()[-500:]))
        return self.parse([entry for found in reports for entry in found], how.cwd, tree), tested

    def parse(self, files, cwd, tree):
        """{repository path: {line: ran}} of the reporter's coverage, its blank lines left out."""
        lines = {}
        for entry in files:
            path = repository_path(entry["path"], cwd, tree, self.repo)
            if path is None or matches(path, NOT_SOURCE):
                continue
            try:
                with open(os.path.join(tree, path), encoding="utf-8", errors="replace") as handle:
                    text = handle.read().splitlines()
            except OSError:
                continue  # a file a test wrote, ran and removed: not the project's source
            ran = lines.setdefault(path, {})
            for number, hit in [(number, False) for number in entry["missed"]] + [(number, True) for number in entry["ran"]]:
                if number <= len(text) and text[number - 1].strip():
                    ran[number] = ran.get(number, False) or hit
        return lines

    def test_files(self, paths, tree):
        """The files among `paths` that the merge base's checkout, `tree`, runs with `node --test`."""
        return [path for _, chosen in self.plans(tree, paths).values() for path in chosen]
