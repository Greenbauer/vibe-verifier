"""Carrying a passing QAE walk across an update no browser can see: the decision in `vibe-verifier
criteria --carry`, the facts actions/criteria/carry_facts.py reads from GitHub for it, the criteria
action's `carried-from` output, and the QA review it leads to."""
import hashlib
import json
import os
import re
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from helpers import ROOT, clean_env, runner

FACTS = ROOT / "actions" / "criteria" / "carry_facts.py"
CRITERIA_ACTION = ROOT / "actions" / "criteria" / "action.yml"
BODY = "## Why\n\nx\n\n## Acceptance criteria\n\n- The page shows the total\n"
HEAD, WALKED = "b" * 40, "a" * 40
RUN = {"id": 7, "head": WALKED, "created_at": "2026-10-12T10:00:00Z", "url": "https://github.com/o/r/actions/runs/7",
       "path": ".github/workflows/qae-explore.yml"}


def facts(files=(".github/workflows/ci.yml", "docs/a.md"), status="ahead", edited=None, cut=False, run=RUN, behind=0,
          opened="2026-10-11T08:00:00Z", body=BODY):
    return {"head": HEAD, "run": run, "compare": {"status": status, "files": list(files), "cut": cut},
            "base_behind": behind, "pull_created_at": opened, "body_edited_at": edited,
            "body_sha256": hashlib.sha256(body.encode()).hexdigest()}


class Decision(unittest.TestCase):
    def file(self, text):
        handle, path = tempfile.mkstemp(prefix="vv-carry-")
        os.write(handle, text.encode()); os.close(handle)
        self.addCleanup(os.remove, path)
        return path

    def criteria(self, carry, body=BODY, ticket=None):
        args = ["criteria", self.file(body), "--carry", self.file(json.dumps(carry))]
        if ticket is not None:
            args += ["--ticket", self.file(ticket)]
        return runner(*args)

    def test_a_walk_followed_only_by_unrendered_changes_is_carried(self):
        result = self.criteria(facts())
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.splitlines(), [
            "none: QA passed on `aaaaaaaa` (https://github.com/o/r/actions/runs/7), and every file changed since (2) "
            "is CI configuration or documentation no site renders",
            "carried: %s https://github.com/o/r/actions/runs/7" % WALKED])

    def test_anything_a_browser_could_see_needs_a_new_walk(self):
        for carry in (facts(files=("docs/a.md", "src/page.tsx")),          # one rendered file
                      facts(status="diverged"),                              # the walked head is not an ancestor
                      facts(status="identical"),                             # nothing new: not this head's update
                      facts(files=()),                                       # an empty compare proves nothing
                      facts(cut=True),                                       # GitHub may have cut the list
                      facts(edited="2026-10-12T10:00:01Z"),                  # the criteria may have changed
                      facts(run=None),                                       # no passing walk on record
                      facts(behind=2),                                       # the walk tested a base this head lacks
                      facts(files=(".github/workflows/qae-explore.yml",)),   # the walked run's own workflow changed
                      facts(files=(".vibe-verifier-qae",)),                  # the gate list the verify job reads changed
                      facts(files=("app/.vibe-verifier",)),
                      facts(opened="2026-10-12T11:00:00Z"),                  # a run older than this pull request
                      facts(opened=None),
                      facts(body=BODY + "- A criterion added between the jobs\n"),   # the body the facts were read with
                      {"head": HEAD, "run": None}):
            with self.subTest(carry=carry):
                self.assertEqual(self.criteria(carry).stdout, "criteria: 1\n")

    def test_a_body_edited_before_the_walk_still_carries(self):
        self.assertIn("carried: ", self.criteria(facts(edited="2026-10-12T09:59:59Z")).stdout)

    def test_a_ticket_with_criteria_is_never_carried(self):
        result = self.criteria(facts(), ticket="## Acceptance criteria\n\n- The ticket's item\n")
        self.assertEqual(result.stdout.splitlines()[0], "criteria: 2")

    def test_a_declared_none_keeps_its_own_reason_and_carries_nothing(self):
        result = self.criteria(facts(), body="## Acceptance criteria\n\n- None: a CI step\n")
        self.assertEqual(result.stdout, "none: a CI step\n")

    def test_an_unreadable_carry_file_cannot_run(self):
        result = runner("criteria", self.file(BODY), "--carry", self.file("{not json"))
        self.assertEqual(result.returncode, 2)


# A stand-in `gh` answering from $FAKE/answers.json ({endpoint prefix: JSON answer}); an endpoint with no
# answer fails as GitHub would. Every call is appended to $FAKE/calls.
FAKE_GH = '''#!/usr/bin/env python3
import json, os, sys
root, argv = os.environ["FAKE"], sys.argv[1:]
endpoint = argv[1] if argv[1] != "graphql" else "graphql"
with open(os.path.join(root, "calls"), "a") as handle:
    handle.write(endpoint + "\\n")
answers = json.load(open(os.path.join(root, "answers.json")))
for prefix, answer in answers.items():
    if endpoint.startswith(prefix):
        print(json.dumps(answer))
        sys.exit(0)
sys.stderr.write("gh: Not Found (HTTP 404)\\n")
sys.exit(1)
'''


def run_row(rid, sha, workflow=11, created="2026-10-12T10:00:00Z", pr=5):
    return {"id": rid, "head_sha": sha, "workflow_id": workflow, "created_at": created, "path": ".github/workflows/qae.yml",
            "html_url": "https://github.com/o/r/actions/runs/%d" % rid, "pull_requests": [{"number": pr}] if pr else []}


class Facts(unittest.TestCase):
    def setUp(self):
        self.fake = tempfile.mkdtemp(prefix="vv-carry-facts-")
        self.addCleanup(lambda: subprocess.run(["rm", "-rf", self.fake]))
        stub = Path(self.fake, "bin", "gh")
        stub.parent.mkdir()
        stub.write_text(FAKE_GH)
        stub.chmod(stub.stat().st_mode | stat.S_IXUSR)

    def read(self, runs, artifacts, compare=None, edited=None):
        answers = {"repos/o/r/actions/runs/99": {"workflow_id": 11},
                   "repos/o/r/actions/runs?": {"workflow_runs": runs},
                   "repos/o/r/compare/main...": {"behind_by": 3},
                   "repos/o/r/compare/": compare or {"status": "ahead", "files": [{"filename": "docs/a.md"},
                                                                                   {"filename": "README.md", "previous_filename": "src/old.md"}]},
                   "graphql": {"data": {"repository": {"pullRequest": {"createdAt": "2026-10-11T08:00:00Z", "lastEditedAt": edited}}}}}
        for rid, names in artifacts.items():
            answers["repos/o/r/actions/runs/%d/artifacts" % rid] = {"artifacts": [{"name": n, "expired": n.endswith("!")} for n in names]}
        Path(self.fake, "answers.json").write_text(json.dumps(answers))
        out = Path(self.fake, "facts.json")
        Path(self.fake, "body.md").write_text(BODY)
        env = clean_env({"FAKE": self.fake, "PATH": "%s%s%s" % (Path(self.fake, "bin"), os.pathsep, os.environ["PATH"])})
        result = subprocess.run([sys.executable, str(FACTS), "--repo", "o/r", "--pr", "5", "--head", HEAD,
                                 "--branch", "feat/x y", "--base", "main", "--run-id", "99",
                                 "--body", str(Path(self.fake, "body.md")), "--out", str(out)],
                                capture_output=True, text=True, env=env)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(out.read_text()), Path(self.fake, "calls").read_text().splitlines()

    def test_the_newest_walked_run_of_the_same_workflow_is_the_one(self):
        runs = [run_row(98, HEAD),               # this head: never its own carry
                run_row(97, "c" * 40, workflow=12),  # another workflow
                run_row(92, "1" * 40, pr=6),     # another pull request from a branch of the same name
                run_row(91, "2" * 40, pr=None),  # a run GitHub tied to no pull request (a fork's)
                run_row(96, "d" * 40),           # walked nothing (a carried or not-required run)
                run_row(95, "e" * 40),           # its evidence expired
                run_row(94, WALKED),             # walked and passed
                run_row(93, "f" * 40)]
        found, calls = self.read(runs, {96: ["usage"], 95: ["qae-artifacts-1!"], 94: ["qae-artifacts-1"], 93: ["qae-artifacts-1"]},
                                 edited="2026-10-12T09:00:00Z")
        self.assertEqual(found["run"], {"id": 94, "head": WALKED, "created_at": "2026-10-12T10:00:00Z",
                                        "url": "https://github.com/o/r/actions/runs/94", "path": ".github/workflows/qae.yml"})
        self.assertEqual(found["base_behind"], 3)
        self.assertFalse(any("/runs/92/" in c or "/runs/91/" in c for c in calls), "other pull requests' runs are never read")
        self.assertEqual(found["compare"], {"status": "ahead", "files": ["docs/a.md", "README.md", "src/old.md"], "cut": False})
        self.assertEqual(found["body_edited_at"], "2026-10-12T09:00:00Z")
        self.assertEqual(found["pull_created_at"], "2026-10-11T08:00:00Z")
        self.assertEqual(found["body_sha256"], hashlib.sha256(BODY.encode()).hexdigest())
        listing = next(c for c in calls if c.startswith("repos/o/r/actions/runs?"))
        self.assertIn("status=success", listing)
        self.assertIn("branch=feat%2Fx%20y", listing)
        self.assertIn("repos/o/r/compare/%s...%s" % (WALKED, HEAD), calls)

    def test_no_walk_among_the_newest_five_is_no_run(self):
        runs = [run_row(90 - i, "%x" % i * 40) for i in range(6)]
        found, calls = self.read(runs, {90 - i: ["usage"] for i in range(5)} | {84: ["qae-artifacts-1"]})
        self.assertEqual(found, {"head": HEAD, "run": None})
        self.assertFalse(any("/runs/84/" in c for c in calls))

    def test_a_cut_compare_is_marked(self):
        files = [{"filename": "docs/%d.md" % i} for i in range(300)]
        found, _ = self.read([run_row(94, WALKED)], {94: ["qae-artifacts-1"]}, compare={"status": "ahead", "files": files})
        self.assertTrue(found["compare"]["cut"])

    def test_a_read_that_fails_is_no_run(self):
        Path(self.fake, "answers.json").write_text(json.dumps({"repos/o/r/actions/runs/99": {"workflow_id": 11}}))
        out = Path(self.fake, "facts.json")
        env = clean_env({"FAKE": self.fake, "PATH": "%s%s%s" % (Path(self.fake, "bin"), os.pathsep, os.environ["PATH"])})
        result = subprocess.run([sys.executable, str(FACTS), "--repo", "o/r", "--pr", "5", "--head", HEAD,
                                 "--branch", "b", "--base", "main", "--run-id", "99", "--body", "missing.md", "--out", str(out)],
                               capture_output=True, text=True, env=env)
        self.assertEqual(result.returncode, 0)
        self.assertIn("::warning::no earlier QAE walk can be carried forward", result.stdout)
        self.assertEqual(json.loads(out.read_text()), {"head": HEAD, "run": None})


class CriteriaAction(unittest.TestCase):
    def script(self):
        match = re.search(r"\n      run: \|\n((?:        [^\n]*\n|\n)+)", CRITERIA_ACTION.read_text())
        return "".join(line[8:] for line in match.group(1).splitlines(True))

    def run_step(self, token, carry, carry_file=None):
        work = tempfile.mkdtemp(prefix="vv-criteria-action-")
        self.addCleanup(lambda: subprocess.run(["rm", "-rf", work]))
        Path(work, "body.md").write_text(BODY)
        # carry_facts.py replaced by a stub that writes the given facts, so the step is what is tested.
        action = Path(work, "action")
        action.mkdir()
        Path(action, "carry_facts.py").write_text(
            "import sys, json\nopen(%r, 'w').write('ran')\nout = sys.argv[sys.argv.index('--out') + 1]\njson.dump(%r, open(out, 'w'))\n"
            % (str(Path(work, "facts-ran")), carry))
        self.ran = Path(work, "facts-ran")
        if carry_file is not None:
            Path(work, "carry.json").write_text(carry_file)
        Path(work, "out").write_text("")
        env = clean_env({"VV_PATH": "body.md", "VV_CHANGED": "", "VV_TICKET": "", "VV_MAX_SHARDS": "1", "VV_MANIFEST": "",
                         "VV_ENTRIES": "", "GH_TOKEN": token, "VV_REPO": "o/r", "VV_PR": "5", "VV_HEAD": HEAD,
                         "VV_BRANCH": "b", "VV_BASE": "main", "VV_RUN_ID": "99",
                         "VV_CARRY": "carry.json" if carry_file is not None else "", "RUNNER_TEMP": work, "GITHUB_OUTPUT": str(Path(work, "out")),
                         "GITHUB_ACTION_PATH": str(action)})
        script = self.script().replace('"$GITHUB_ACTION_PATH/../../bin/vibe-verifier"', '"%s"' % (ROOT / "bin" / "vibe-verifier"))
        result = subprocess.run(["bash", "-c", script], cwd=work, capture_output=True, text=True, env=env)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return dict(line.split("=", 1) for line in Path(work, "out").read_text().splitlines() if "=" in line and "<<" not in line)

    def test_a_carried_walk_is_the_outputs_and_its_facts_are_passed_on(self):
        out = self.run_step("t", facts())
        self.assertEqual((out["count"], out["carried-from"]), ("0", WALKED))
        self.assertTrue(out["declared-none"].startswith("QA passed on `aaaaaaaa`"))
        self.assertEqual(json.loads(out["carry"]), facts())

    def test_a_later_job_decides_on_the_facts_it_is_given_and_reads_none(self):
        out = self.run_step("t", {"head": HEAD, "run": None}, carry_file=json.dumps(facts()))
        self.assertEqual((out["count"], out["carried-from"]), ("0", WALKED))
        self.assertFalse(self.ran.exists(), "given the earlier job's facts, the step must not read its own")

    def test_an_empty_facts_file_carries_nothing(self):
        out = self.run_step("t", facts(), carry_file="")
        self.assertEqual((out["count"], out["carried-from"], out["carry"]), ("1", "", ""))
        self.assertFalse(self.ran.exists())

    def test_without_a_token_nothing_is_read_or_carried(self):
        out = self.run_step("", facts())
        self.assertEqual((out["count"], out["carried-from"], out["declared-none"], out["carry"]), ("1", "", "", ""))
        self.assertFalse(self.ran.exists())


class CarriedWalk(unittest.TestCase):
    """Both templates let a passing walk's verdict be carried across an update no browser can see: the
    criteria job's action gets a token that can read the runs, the verify job decides on the facts that
    job read, and the QA review is told which head it carries."""

    def test_both_templates_wire_the_carry(self):
        for name in ("explore.yml", "explore-codex.yml"):
            with self.subTest(template=name):
                text = (ROOT / "harnesses" / "qae" / name).read_text()
                criteria, rest = text.split("\n  explore:\n", 1)
                verify = rest.split("\n  verify:\n", 1)[1]
                self.assertIn("      actions: read", criteria)
                self.assertIn("          token: ${{ secrets.GITHUB_TOKEN }}   # carries", criteria)
                self.assertIn("      carry: ${{ steps.criteria.outputs.carry }}", criteria)
                # The verify job decides on the criteria job's facts and reads none of its own: its criteria
                # step is given no token. (Its `actions: read` is for qa-review's usage-limit check.)
                step = verify[verify.index("      - name: Read the criteria\n"):]
                step = step[:step.index("\n      - ", 1)]
                self.assertIn("actions/criteria@", step)
                self.assertNotIn("token:", step)
                self.assertIn("CARRY: ${{ needs.criteria.outputs.carry }}", verify)
                self.assertIn("          carry: qae-inputs/carry.json", verify)
                self.assertIn("carried-from: ${{ steps.criteria.outputs.carried-from }}", verify)

if __name__ == "__main__":
    unittest.main()
