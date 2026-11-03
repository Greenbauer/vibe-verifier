"""gates/_descriptors.py, the runner of tool descriptors: its file filter, exit contract and parsers on a stand-in
tool, a repository's own descriptors judged from the base, and the block on two gates running one tool. The catalog's
own descriptor (actionlint) keeps the test cases its Python gate had, in tests/test_workflow_lint.py."""
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from helpers import ROOT, clean_env, commit, gate, make_repo, runner

sys.path.insert(0, str(ROOT / "gates"))
import _descriptors  # noqa: E402
import _parsers  # noqa: E402

sys.path.remove(str(ROOT / "gates"))

# A stand-in linter: a finding for every file it is given that holds BAD, in the shape STANDIN_MODE names.
STANDIN = r'''#!/usr/bin/env python3
import json, os, sys
files = [a for a in sys.argv[1:] if os.path.isfile(a)]
if os.environ.get("STANDIN_LOG"):
    with open(os.environ["STANDIN_LOG"], "a") as log:
        log.write(" ".join(sys.argv[1:]) + "\n")
mode = os.environ.get("STANDIN_MODE", "json")
if mode == "tree":  # a tool that reads the whole tree, whatever files it is given
    files = sorted(os.path.normpath(os.path.join(d, n)) for d, _, names in os.walk(".") if ".git" not in d
                   for n in names if n.endswith(".txt"))
bad = [f for f in files if "BAD" in open(f).read()]
if mode == "absolute":  # reports each file by its real absolute path
    bad, mode = [os.path.abspath(f) for f in bad], "json"
if mode == "crash":
    sys.stderr.write("warming up\nboom\n"); sys.exit(4)
if mode == "garbage":
    print("not json"); sys.exit(1)
if mode == "sarif":
    def result(f, **extra):
        return dict({"ruleId": "no-bad", "message": {"text": "holds BAD"},
                     "locations": [{"physicalLocation": {"artifactLocation": {"uri": f.replace("é", "%C3%A9")},
                                                         "region": {"startLine": 1}}}]}, **extra)
    results = [result(f) for f in bad] + [result(f, level="note") for f in files] + [result(f, suppressions=[{"kind": "inSource"}]) for f in files]
    print(json.dumps({"version": "2.1.0", "runs": [{"results": results}]})); sys.exit(0)
print(json.dumps([{"msg": "holds BAD", "rule": "no-bad", "file": f, "ln": 1} for f in bad]))
sys.exit(1 if bad or mode == "empty" else 0)
'''


def descriptor(gate_name="lint-text", tool="vv-standin", parser="json", args=None, files=None, options=None):
    output = {"parser": "sarif"} if parser == "sarif" else {"parser": "json", "message": "{rule}: {msg} ({missing})",
                                                              "path": "file", "line": "ln"}
    found = {"gate": gate_name, "description": "Fail on BAD.", "tool": tool, "args": args or ["--check"],
             "files": files or ["**/*.txt"], "output": output}
    return json.dumps(dict(found, options=options) if options else found)


class StandIn(unittest.TestCase):
    def setUp(self):
        self.bin = tempfile.mkdtemp(prefix="vv-desc-")
        self.addCleanup(shutil.rmtree, self.bin, True)
        tool = Path(self.bin, "vv-standin")
        tool.write_text(STANDIN.replace("#!/usr/bin/env python3", "#!" + sys.executable))
        tool.chmod(tool.stat().st_mode | stat.S_IXUSR)
        self.log = Path(self.bin, "log")

    def env(self, mode="json"):
        return {"PATH": "%s:%s" % (self.bin, os.environ["PATH"]), "STANDIN_MODE": mode, "STANDIN_LOG": str(self.log)}

    def repo(self, base, change, **kwargs):
        repo = make_repo(self, dict({".vibe-verifier-tools/lint-text.json": descriptor(**kwargs), "README.md": "x\n"}, **base))
        commit(repo, change)
        return repo

    def run_gate(self, repo, *args, name="lint-text", mode="json"):
        return gate(name, repo, *args, env=self.env(mode))


class Contract(StandIn):
    def test_a_bad_file_the_pr_changes_fails_with_the_mapped_fields(self):
        result = self.run_gate(self.repo({}, {"src/a.txt": "BAD\n"}), "--base-ref", "HEAD~1", "--format", "json")
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertEqual(json.loads(result.stdout)["findings"],
                         [{"message": "no-bad: holds BAD (?)", "path": "src/a.txt", "line": 1, "advisory": False}])

    def test_only_changed_files_its_globs_match_are_passed(self):
        repo = self.repo({"old.txt": "BAD\n"}, {"new.txt": "fine\n", "new.md": "BAD\n"})
        result = self.run_gate(repo, "--base-ref", "HEAD~1")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.log.read_text(), "--check new.txt\n")

    def test_no_judged_file_passes_without_running_the_tool(self):
        result = self.run_gate(self.repo({}, {"notes.md": "BAD\n"}), "--base-ref", "HEAD~1")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertFalse(self.log.exists())

    def test_a_pre_existing_problem_is_not_this_prs_finding_unless_all(self):
        repo = self.repo({"old.txt": "BAD\n"}, {"README.md": "y\n"})
        self.assertEqual(self.run_gate(repo, "--base-ref", "HEAD~1").returncode, 0)
        self.assertEqual(self.run_gate(repo, "--all").returncode, 1)

    def test_a_finding_on_a_file_it_was_not_given_is_dropped(self):
        repo = self.repo({"old.txt": "BAD\n"}, {"new.txt": "BAD\n"})
        result = self.run_gate(repo, "--base-ref", "HEAD~1", "--format", "json", mode="tree")
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertEqual([f["path"] for f in json.loads(result.stdout)["findings"]], ["new.txt"])

    def test_an_absolute_path_it_reports_is_read_as_the_repositorys(self):
        # The fixture lives under a linked temporary directory on macOS, so the tool's real path differs from --repo.
        result = self.run_gate(self.repo({}, {"src/a.txt": "BAD\n"}), "--base-ref", "HEAD~1", "--format", "json", mode="absolute")
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertEqual([f["path"] for f in json.loads(result.stdout)["findings"]], ["src/a.txt"])

    def test_a_declared_option_is_passed_to_the_tool_only_when_given(self):
        repo = self.repo({}, {"a.txt": "fine\n"}, options={"--level": "how strict"})
        self.assertEqual(self.run_gate(repo, "--base-ref", "HEAD~1", "--level", "high").returncode, 0)
        self.assertEqual(self.run_gate(repo, "--base-ref", "HEAD~1").returncode, 0)
        self.assertEqual(self.log.read_text(), "--check --level high a.txt\n--check a.txt\n")
        self.assertEqual(self.run_gate(repo, "--base-ref", "HEAD~1", "--other", "x").returncode, 2)  # undeclared

    def test_soak_reports_and_passes(self):
        result = self.run_gate(self.repo({}, {"a.txt": "BAD\n"}), "--base-ref", "HEAD~1", "--soak")
        self.assertEqual(result.returncode, 0)
        self.assertIn("a.txt:1: no-bad: holds BAD", result.stdout)

    def test_a_crash_an_unreadable_report_or_exit_one_with_none_cannot_run(self):
        repo = self.repo({}, {"a.txt": "fine\n"})
        crash = self.run_gate(repo, "--base-ref", "HEAD~1", mode="crash")
        self.assertEqual(crash.returncode, 2)
        self.assertIn("vv-standin exited 4: boom", crash.stderr)
        garbage = self.run_gate(self.repo({}, {"a.txt": "BAD\n"}), "--base-ref", "HEAD~1", "--soak", mode="garbage")
        self.assertEqual(garbage.returncode, 2)
        self.assertIn("could not be read as json", garbage.stderr)
        empty = self.run_gate(repo, "--base-ref", "HEAD~1", mode="empty")
        self.assertEqual(empty.returncode, 2)
        self.assertIn("exited 1 with an empty report", empty.stderr)

    def test_a_tool_not_on_path_cannot_run(self):
        result = gate("lint-text", self.repo({}, {"a.txt": "x\n"}, tool="vv-not-installed"), "--base-ref", "HEAD~1")
        self.assertEqual(result.returncode, 2)
        self.assertIn("vv-not-installed is not on PATH", result.stderr)

    def test_a_malformed_descriptor_cannot_run(self):
        for text, why in ((descriptor(gate_name="other"), "`gate` must be the file's own name"),
                          (descriptor(tool="/opt/homebrew/bin/zizmor"), "a bare command name, not a path"),
                          (descriptor(options={"--soak": "x"}), "`options` cannot declare --soak"),
                          (json.dumps({"gate": "lint-text"}), "a descriptor has the keys"),
                          ("{", "is not JSON")):
            with self.subTest(why=why):
                repo = make_repo(self, {".vibe-verifier-tools/lint-text.json": text})
                commit(repo, {"a.txt": "BAD\n"})
                result = self.run_gate(repo, "--base-ref", "HEAD~1")
                self.assertEqual(result.returncode, 2, result.stdout)
                self.assertIn(why, result.stderr)

    def test_an_unresolvable_base_cannot_run(self):
        self.assertEqual(self.run_gate(self.repo({}, {"a.txt": "x\n"}), "--base-ref", "no-such-branch").returncode, 2)


class Sarif(StandIn):
    def test_results_count_whatever_the_exit_notes_advise_and_suppressions_drop(self):
        repo = self.repo({}, {"café.txt": "BAD\n"}, parser="sarif")
        result = self.run_gate(repo, "--base-ref", "HEAD~1", "--format", "json", mode="sarif")
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        findings = json.loads(result.stdout)["findings"]
        self.assertEqual([(f["path"], f["line"], f["advisory"]) for f in findings], [("café.txt", 1, False), ("café.txt", 1, True)])
        self.assertEqual(findings[0]["message"], "no-bad: holds BAD")

    def test_notes_alone_pass(self):
        result = self.run_gate(self.repo({}, {"a.txt": "fine\n"}, parser="sarif"), "--base-ref", "HEAD~1", mode="sarif")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("1 advisory finding(s)", result.stdout)


class FromTheBase(StandIn):
    def test_a_descriptor_the_base_has_runs_as_the_base_has_it(self):
        repo = self.repo({}, {".vibe-verifier-tools/lint-text.json": descriptor(args=["--loose"], files=["nothing/*"]),
                              "a.txt": "BAD\n"}, args=["--strict"])
        result = self.run_gate(repo, "--base-ref", "HEAD~1")
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertEqual(self.log.read_text(), "--strict a.txt\n")
        self.assertIn("is judged as it is at HEAD~1", result.stderr)

    def test_a_descriptor_the_pr_adds_runs_at_once(self):
        repo = make_repo(self, {"README.md": "x\n"})
        commit(repo, {".vibe-verifier-tools/lint-text.json": descriptor(), "a.txt": "BAD\n"})
        self.assertEqual(self.run_gate(repo, "--base-ref", "HEAD~1").returncode, 1)

    def test_the_runner_runs_a_repositorys_own_descriptor(self):
        repo = self.repo({".vibe-verifier": "lint-text\n"}, {"a.txt": "BAD\n"})
        result = runner("run", "--repo", repo, "--manifest", os.path.join(repo, ".vibe-verifier"), "--base-ref", "HEAD~1",
                        env=self.env())
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertRegex(result.stdout, r"lint-text +FAIL")


class NoDuplicates(StandIn):
    def test_the_catalog_has_no_conflict(self):
        self.assertEqual(_descriptors.conflicts(_descriptors.catalog()), {})
        self.assertIn("actionlint", _descriptors.catalog_names())

    def test_the_tools_python_gates_run_are_found(self):
        tools = _descriptors.gate_tools()
        self.assertTrue({"gitleaks", "bandit", "osv-scanner", "ast-grep", "vale"} <= set(tools), tools)
        self.assertEqual(tools["bandit"], "gates/bandit.py")  # passed as the constant GATE
        self.assertFalse({"actionlint", "zizmor"} & set(tools))  # their descriptors run them now

    def test_every_output_has_a_parser(self):
        self.assertEqual(set(_descriptors.OUTPUTS), set(_parsers.PARSERS))

    def test_a_descriptor_running_a_python_gates_tool_cannot_run(self):
        result = self.run_gate(self.repo({}, {"a.txt": "BAD\n"}, tool="bandit"), "--base-ref", "HEAD~1")
        self.assertEqual(result.returncode, 2, result.stdout)
        self.assertIn("tool 'bandit' is in gates/bandit.py and HEAD~1:.vibe-verifier-tools/lint-text.json", result.stderr)

    def test_a_descriptor_running_a_catalog_descriptors_tool_stops_both(self):
        repo = self.repo({}, {"a.txt": "BAD\n"}, tool="actionlint")
        for name in ("lint-text", "actionlint"):
            with self.subTest(gate=name):
                result = self.run_gate(repo, "--base-ref", "HEAD~1", name=name)
                self.assertEqual(result.returncode, 2, result.stdout)
                self.assertIn("tool 'actionlint' is in", result.stderr)

    def test_a_descriptor_named_like_a_python_gate_cannot_run(self):
        repo = make_repo(self, {".vibe-verifier-tools/bandit.json": descriptor(gate_name="bandit"), ".vibe-verifier": "bandit\n"})
        commit(repo, {"a.txt": "BAD\n"})
        direct = subprocess.run([sys.executable, str(ROOT / "gates" / "_descriptors.py"), "bandit", "--repo", repo,
                                 "--base-ref", "HEAD~1"], capture_output=True, text=True, env=clean_env(self.env()))
        self.assertEqual(direct.returncode, 2, direct.stdout)
        self.assertIn("gate 'bandit' is in gates/bandit.py and HEAD~1:.vibe-verifier-tools/bandit.json", direct.stderr)
        result = runner("run", "--repo", repo, "--manifest", os.path.join(repo, ".vibe-verifier"), "--base-ref", "HEAD~1",
                        env=self.env())
        self.assertEqual(result.returncode, 2, result.stdout)
        self.assertIn(".vibe-verifier-tools/bandit.json names the catalog's gate 'bandit'", result.stderr)


if __name__ == "__main__":
    unittest.main()
