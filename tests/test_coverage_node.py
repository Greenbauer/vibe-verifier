"""coverage-not-lost on node:test files (gates/_coverage_node.py): the Node on PATH (CI's 22, and locally 22 and 24) with
its own coverage, `--experimental-test-coverage`, on synthetic repositories that run their tests with `node --test`
from a package.json script or a --node-test glob. Nothing is installed: node:test is Node's own."""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from helpers import ROOT, clean_env, commit, gate, git, make_repo
from test_local_guard_gates import marker

PACKAGE = json.dumps({"type": "module", "scripts": {"lint": "eslint .", "test": "node --test test/*.test.mjs"}}) + "\n"
PRICE = """export function discount(total, member) {
  if (member && total > 100) {
    return total * 0.9;
  }
  return total;
}

export function rounded(value) {
  return Math.round(value * 100) / 100;
}
"""
HEADER = 'import { test } from "node:test";\nimport assert from "node:assert/strict";\n'
MEMBER_TEST = HEADER + 'import { discount, rounded } from "../src/price.mjs";\n' \
    'test("gives members 10% off over 100", () => { assert.equal(rounded(discount(200, true)), 180); });\n'
# The member test weakened: it still runs, but no longer reaches the discount or rounded().
WEAK_MEMBER_TEST = HEADER + 'import { discount } from "../src/price.mjs";\n' \
    'test("gives members 10% off over 100", () => { assert.equal(discount(50, true), 50); });\n'
PLAIN_TEST = HEADER + 'import { discount } from "../src/price.mjs";\n' \
    'test("charges everyone else full price", () => { assert.equal(discount(50, false), 50); });\n'
# Covers line 3 of settle.mjs in every run but the second one the gate makes (the first at the head), counted in a file
# outside the repository: coverage that changes from run to run.
SETTLE = 'export function settle(fast) {\n  if (fast) {\n    return "fast";\n  }\n  return "slow";\n}\n'
FLAKY_TEST = HEADER + """import { readFileSync, writeFileSync } from "node:fs";
import { settle } from "../src/settle.mjs";
const run = Number(readFileSync(process.env.VV_RUN_COUNTER, "utf8"));
writeFileSync(process.env.VV_RUN_COUNTER, String(run + 1));
test("settles", () => { assert.equal(settle(run !== 1), run !== 1 ? "fast" : "slow"); });
"""
# Code only a child process, a worker thread or a child killed by a signal runs. The first child gets an environment of
# its own, into which Node copies NODE_V8_COVERAGE; the killed one writes no coverage, so it is measured at no side.
APP = 'export function greeting(name) {\n  return "hello " + name;\n}\nconsole.log(greeting("world"));\n'
WORK = ('import { parentPort } from "node:worker_threads";\nfunction double(value) {\n  return value * 2;\n}\n'
        'parentPort.postMessage(double(21));\n')
DAEMON = 'function serve() {\n  return "up";\n}\nconsole.log(serve());\nsetInterval(() => {}, 1000);\n'
CHILD_TEST = HEADER + """import { execFileSync, spawn } from "node:child_process";
import { Worker } from "node:worker_threads";
test("prints", () => {
  assert.equal(execFileSync(process.execPath, ["src/app.mjs"], { env: { PATH: process.env.PATH } }).toString(), "hello world\\n");
});
test("doubles in a worker", async () => {
  const worker = new Worker(new URL("../src/work.mjs", import.meta.url));
  const [value] = await Promise.all([new Promise((done) => worker.once("message", done)), new Promise((done) => worker.once("exit", done))]);
  assert.equal(value, 42);
});
test("serves until stopped", async () => {
  const daemon = spawn(process.execPath, ["src/daemon.mjs"]);
  assert.equal(String(await new Promise((done) => daemon.stdout.once("data", done))), "up\\n");
  daemon.kill("SIGTERM");
  await new Promise((done) => daemon.once("exit", done));
});
"""
# A stand-in compiler, as ts-node is one: a require hook for .lang files that puts three lines of its own above the code
# and appends a source map of every column back to the file, so without the map every line would be read three down.
HOOK = r"""const fs = require("fs");
const { pathToFileURL } = require("url");
const B64 = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";
const vlq = (n) => {
  let value = n < 0 ? (-n << 1) | 1 : n << 1, out = "";
  do { let digit = value & 31; value >>>= 5; if (value) digit |= 32; out += B64[digit]; } while (value);
  return out;
};
require.extensions[".lang"] = function (module, filename) {
  const source = fs.readFileSync(filename, "utf8");
  let line0 = 0, col0 = 0;
  const mappings = source.replace(/\n$/, "").split("\n").map((text, line) => {
    const segments = [];
    for (let col = 0, generated = 0; col < text.length; generated = col, col++) {
      segments.push(vlq(col - generated) + "A" + vlq(line - line0) + vlq(col - col0));
      line0 = line; col0 = col;
    }
    return segments.join(",");
  });
  const map = { version: 3, sources: [pathToFileURL(filename).href], names: [], mappings: ";;;" + mappings.join(";") };
  module._compile('"use strict";\n// compiled\n// by the stand-in\n' + source + "\n//# sourceMappingURL=data:application/json;base64," +
                  Buffer.from(JSON.stringify(map)).toString("base64") + "\n", filename);
};
"""
LANG_PRICE = PRICE.replace("export function", "function").replace("\n\nfunction", "\nfunction") + \
    "module.exports = { discount, rounded };\n"
LANG_HEADER = 'const test = require("node:test");\nconst assert = require("node:assert/strict");\n' \
    'const { discount, rounded } = require("../src/price.lang");\n'
LANG_PLAIN = LANG_HEADER + 'test("charges everyone else full price", () => { assert.equal(discount(50, false), 50); });\n'
LANG_MEMBER = LANG_HEADER + 'test("gives members 10% off over 100", () => { assert.equal(rounded(discount(200, true)), 180); });\n'
SPAN = re.compile(r"(?:^intended, .*?: )?Lines? (\d+)(?:-(\d+))? \(")


def findings(result):
    return json.loads(result.stdout)["findings"]


def lines_found(result):
    """{(path, line)} of every line the findings name, each span spelled out."""
    found = set()
    for finding in findings(result):
        span = SPAN.search(finding["message"])
        found.update((finding["path"], number) for number in range(int(span.group(1)), int(span.group(2) or span.group(1)) + 1))
    return found


def project(test, base, head, package=PACKAGE):
    repo = make_repo(test, dict({"package.json": package}, **base))
    for path, text in head.items():
        if text is None:
            git(repo, "rm", "-q", path)
    commit(repo, {path: text for path, text in head.items() if text is not None})
    return repo


def run_gate(repo, *args, env=None):
    return gate("coverage-not-lost", repo, "--runner", "node-test", "--base-ref", "HEAD~1", "--format", "json", *args, env=env)


def worktrees(repo):
    return subprocess.run(["git", "-C", repo, "worktree", "list", "--porcelain"], capture_output=True, text=True).stdout.count("worktree ")


class NodeTestCoverage(unittest.TestCase):
    """The rule on node:test files: lines that ran at the merge base and do not at the head, two runs agreeing."""

    def test_a_deleted_node_test_that_was_the_only_cover_flags_its_lines(self):
        repo = project(self, {"src/price.mjs": PRICE, "test/member.test.mjs": MEMBER_TEST, "test/plain.test.mjs": PLAIN_TEST},
                       {"test/member.test.mjs": None})
        result = run_gate(repo)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertEqual([(f["path"], f["line"], f["advisory"]) for f in findings(result)],
                         [("src/price.mjs", 3, False), ("src/price.mjs", 9, False)])
        first = findings(result)[0]["message"]
        self.assertIn("Lines 3-4 (`return total * 0.9;`) ran under the project's tests at the merge base and does not at the head", first)
        self.assertIn("Run at the merge base by the test files this pull request deleted or changed: test/member.test.mjs (deleted).",
                      first)
        self.assertIn("`coverage-lost: <reason>`", first)
        self.assertRegex(result.stderr, r"the first runs found 4 that no longer run at the head, and the second runs agree on 4")
        self.assertEqual(worktrees(repo), 1)

    def test_a_weakened_node_test_flags_the_lines_it_no_longer_runs(self):
        repo = project(self, {"src/price.mjs": PRICE, "test/member.test.mjs": MEMBER_TEST, "test/plain.test.mjs": PLAIN_TEST},
                       {"test/member.test.mjs": WEAK_MEMBER_TEST})
        result = run_gate(repo)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertEqual(lines_found(result), {("src/price.mjs", line) for line in (3, 4, 9, 10)})
        self.assertIn("Run at the merge base by the test files this pull request deleted or changed: test/member.test.mjs (changed).",
                      findings(result)[0]["message"])

    def test_an_unchanged_suite_does_not_flag(self):
        # A test moved to another directory the script still runs, and a README: every line still runs.
        repo = project(self, {"src/price.mjs": PRICE, "test/member.test.mjs": MEMBER_TEST, "test/plain.test.mjs": PLAIN_TEST,
                              "README.md": "a\n"},
                       {"README.md": "b\n", "test/member.test.mjs": None,
                        "test/pricing/member.test.mjs": MEMBER_TEST.replace('"../src/', '"../../src/')},
                       package=PACKAGE.replace("test/*.test.mjs", "test/*.test.mjs test/pricing/*.test.mjs"))
        result = run_gate(repo)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(findings(result), [])
        self.assertIn("every one the pull request left unchanged still runs at the head", result.stderr)
        self.assertRegex(result.stderr, r"the run at the head: 9 line\(s\) ran \(9 in src/\), in \d+ seconds")

    def test_coverage_that_changes_between_runs_is_not_reported(self):
        counter = tempfile.NamedTemporaryFile("w", delete=False, suffix=".count")
        counter.write("0")
        counter.close()
        self.addCleanup(os.unlink, counter.name)
        repo = project(self, {"src/settle.mjs": SETTLE, "test/settle.test.mjs": FLAKY_TEST, "README.md": "a\n"}, {"README.md": "b\n"})
        result = run_gate(repo, env={"VV_RUN_COUNTER": counter.name})
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(findings(result), [])
        self.assertIn("the first runs found 2 that no longer run at the head, and the second runs agree on 0", result.stderr)
        self.assertIn("not reported, because the two runs disagree (coverage that changes from run to run): src/settle.mjs:3, "
                      "src/settle.mjs:4", result.stderr)
        self.assertEqual(Path(counter.name).read_text(), "4")  # merge base, head, merge base, head

    def test_a_coverage_lost_line_in_the_body_marks_the_loss_intended(self):
        repo = project(self, {"src/price.mjs": PRICE, "test/member.test.mjs": MEMBER_TEST, "test/plain.test.mjs": PLAIN_TEST},
                       {"test/member.test.mjs": None})
        body = Path(tempfile.mkdtemp(prefix="vv-coverage-body-"), "body.md")
        self.addCleanup(shutil.rmtree, body.parent, True)
        body.write_text("## Summary\n\n- coverage-lost: the member discount is retired next week\n")
        result = run_gate(repo, "--body", str(body))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual([(f["line"], f["advisory"]) for f in findings(result)], [(3, True), (9, True)])
        self.assertTrue(findings(result)[0]["message"].startswith(
            "intended, the pull request body says `coverage-lost: the member discount is retired next week`: Lines 3-4"))

    def test_a_suite_that_fails_at_either_side_cannot_run_even_under_soak(self):
        failing = PLAIN_TEST.replace("50);", "51);")
        for side, base, head in (("merge base", failing, PLAIN_TEST), ("head", PLAIN_TEST, failing)):
            with self.subTest(side=side):
                repo = project(self, {"src/price.mjs": PRICE, "test/plain.test.mjs": base}, {"test/plain.test.mjs": head})
                result = run_gate(repo, "--soak")
                self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
                self.assertRegex(result.stderr, r"the suite of \. fails at the %s(?: [0-9a-f]{12})? \(node --test exited 1; its "
                                                r"failures are above\)" % side)
                self.assertIn("test/plain.test.mjs > charges everyone else full price: AssertionError: Expected values to be "
                              "strictly equal: 50 !== 51", result.stderr)
                self.assertEqual(worktrees(repo), 1)

    def test_a_node_before_22_cannot_run(self):
        fake = tempfile.mkdtemp(prefix="vv-old-node-")
        self.addCleanup(shutil.rmtree, fake, True)
        Path(fake, "node").write_text("#!/bin/sh\necho v20.18.0\n")
        os.chmod(os.path.join(fake, "node"), 0o755)
        repo = project(self, {"src/price.mjs": PRICE, "test/plain.test.mjs": PLAIN_TEST}, {"test/plain.test.mjs": None})
        result = run_gate(repo, "--soak", env={"PATH": fake + os.pathsep + os.environ["PATH"]})
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn("the node on PATH is v20.18.0, and coverage of node:test files needs Node 22 or later", result.stderr)
        self.assertEqual(worktrees(repo), 1)

    def test_a_line_only_a_child_process_or_a_worker_ran_is_measured(self):
        repo = project(self, {"src/price.mjs": PRICE, "src/app.mjs": APP, "src/work.mjs": WORK, "src/daemon.mjs": DAEMON,
                              "test/app.test.mjs": CHILD_TEST, "test/plain.test.mjs": PLAIN_TEST}, {"test/app.test.mjs": None})
        result = run_gate(repo)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        found = lines_found(result)
        self.assertEqual({path for path, _ in found}, {"src/app.mjs", "src/work.mjs"})
        self.assertTrue({("src/app.mjs", 2), ("src/app.mjs", 4), ("src/work.mjs", 3), ("src/work.mjs", 5)} <= found, found)

    def test_a_route_directory_with_brackets_runs_its_tests(self):
        # Node reads a --test path with [ or ] as a glob: app/[id]/page.test.mjs, given as it is, ran no test.
        page = {"app/[id]/page.mjs": "export function title(id) {\n  return `#${id}`;\n}\n",
                "app/[id]/page.test.mjs": HEADER + 'import { title } from "./page.mjs";\ntest("titles", () => { assert.equal(title(1), "#1"); });\n'}
        repo = project(self, dict(page, **{"src/price.mjs": PRICE, "test/plain.test.mjs": PLAIN_TEST}), {"app/[id]/page.test.mjs": None},
                       package=PACKAGE.replace("test/*.test.mjs", "test/*.test.mjs app/*/*.test.mjs"))
        result = run_gate(repo)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        # Nothing loads the page at the head, so each of its lines that ran at the merge base is lost.
        self.assertEqual(lines_found(result), {("app/[id]/page.mjs", line) for line in (1, 2, 3)})


    def test_a_credential_in_any_scripts_assignments_stops_the_run_before_any_test_starts(self):
        # Two scripts run the suite, so two commands: the first's file would run first, the second's script assigns a
        # credential. Every command's environment is checked before any of them starts (gates/_local_guard.py).
        ran = marker(self)
        first = 'import { appendFileSync } from "node:fs";\nappendFileSync(process.env.VV_RAN, "ran\\n");\n' + PLAIN_TEST
        package = json.dumps({"type": "module", "scripts": {"test:a": "node --test test/a.test.mjs",
                                                            "test:b": "API_TOKEN=tok_in_script node --test test/b.test.mjs"}})
        repo = project(self, {"src/price.mjs": PRICE, "test/a.test.mjs": first, "test/b.test.mjs": MEMBER_TEST, "README.md": "a\n"},
                       {"README.md": "b\n"}, package=package + "\n")
        result = run_gate(repo, "--soak", env={"VV_RAN": ran})
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn("the node --test run of package.json script `test:b` in the base checkout would run the pull request's "
                      "code with a credential for somewhere other than this machine", result.stderr)
        self.assertIn("the environment: API_TOKEN is named as a credential", result.stderr)
        self.assertNotIn("tok_in_script", result.stdout + result.stderr)
        self.assertFalse(Path(ran).exists())  # no test process started, not even the first command's

    def test_a_file_an_env_file_option_of_args_loads_is_checked_too(self):
        ran = marker(self)
        first = 'import { appendFileSync } from "node:fs";\nappendFileSync(process.env.VV_RAN, "ran\\n");\n' + PLAIN_TEST
        repo = project(self, {"src/price.mjs": PRICE, "test/a.test.mjs": first, "README.md": "a\n",
                              "config/test.env": "DATABASE_URL=postgresql://app:pw-in-env-file@db.example.com:5432/app\n"},
                       {"README.md": "b\n"})
        result = run_gate(repo, "--soak", "--args=--env-file=config/test.env", env={"VV_RAN": ran})
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn("config/test.env: DATABASE_URL holds a connection string for db.example.com", result.stderr)
        self.assertNotIn("pw-in-env-file", result.stdout + result.stderr)
        self.assertFalse(Path(ran).exists())


class CompiledFiles(unittest.TestCase):
    """A file a loader compiles is measured at its own lines through the loader's source map (--enable-source-maps), and
    the runs take --node-test and --args: here `--require ./hook.cjs` and a glob no package.json script runs."""

    def project(self):
        return project(self, {"hook.cjs": HOOK, "src/price.lang": LANG_PRICE, "test/plain.test.cjs": LANG_PLAIN,
                              "test/member.test.cjs": LANG_MEMBER}, {"test/member.test.cjs": None}, package="{}\n")

    def test_a_file_a_loader_compiles_is_measured_at_its_own_lines(self):
        result = run_gate(self.project(), "--node-test", "test/*.test.cjs", "--args", "--require ./hook.cjs")
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        found = lines_found(result)
        # Lines 3 and 8 stopped running; 1, 2, 5 and 10 still run. Read three lines down, 3 and 8 would not be found.
        self.assertTrue({("src/price.lang", 3), ("src/price.lang", 8)} <= found, found)
        self.assertFalse({("src/price.lang", line) for line in (1, 2, 5, 10)} & found, found)

    def test_without_source_maps_a_compiled_file_is_measured_at_the_wrong_lines(self):
        copy = tempfile.mkdtemp(prefix="vv-coverage-copy-")
        self.addCleanup(shutil.rmtree, copy, True)
        shutil.copytree(ROOT / "gates", os.path.join(copy, "gates"))
        runs = Path(copy, "gates", "_coverage_node.py")
        self.assertEqual(runs.read_text().count('"--enable-source-maps", '), 1)
        runs.write_text(runs.read_text().replace('"--enable-source-maps", ', ""))
        result = subprocess.run([sys.executable, os.path.join(copy, "gates", "coverage_not_lost.py"), "--repo", self.project(),
                                 "--runner", "node-test", "--base-ref", "HEAD~1", "--format", "json", "--node-test",
                                 "test/*.test.cjs", "--args", "--require ./hook.cjs"], capture_output=True, text=True, env=clean_env())
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        found = lines_found(result)
        self.assertFalse({("src/price.lang", 3), ("src/price.lang", 8)} & found, found)
        self.assertIn(("src/price.lang", 10), found)


if __name__ == "__main__":
    unittest.main()
