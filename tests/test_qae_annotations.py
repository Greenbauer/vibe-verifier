"""Criterion annotations in the QAE harness: `[ref: <key>]` (a design reference the workflow supplies)
and `[as: <role>, ...]` (the roles to walk a criterion as). `vibe-verifier criteria` names them,
actions/qae-inputs copies the supplied references into the evidence and declares their digests, and the
acceptance-verdict gate holds a criterion to them. Every part is driven through its command line or the
shipped action and template, so the test judges what a consumer runs."""
import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from helpers import ROOT, clean_env, gate, make_repo, png, runner, write

TEMPLATE = ROOT / "harnesses" / "qae" / "explore.yml"
MANIFEST = ROOT / "harnesses" / "qae" / "manifest"
BODY = ("## Summary\n\nA new home page.\n\n## Acceptance criteria\n\n"
        "- The home page matches the design [ref: home-desktop]\n"
        "- Only an admin can delete a post [as: admin, read-only]\n")
HOME = png(1440, 900)
DIGESTS = {"home-desktop": hashlib.sha256(HOME).hexdigest()}
LOG_AC1 = ("- step 1: opened / at 1440 wide -> the hero and the nav\n"
           "- step 2: compared with reference home-desktop -> same nav, hero and footer\n")
LOG_AC2 = ("- step 1: as admin: opened a post -> a Delete button\n"
           "- step 2: as admin: clicked Delete -> the post is gone\n"
           "- step 3: as read-only: opened a post -> no Delete button\n")
VERDICT = ("acceptance-check: AC1 -- PASS -- it matches (qae/AC1.md::compared with reference home-desktop)\n"
           "acceptance-check: AC2 -- PASS -- held for both (qae/AC2.md::as read-only: opened a post)\n")


def action_script(name):
    text = (ROOT / "actions" / name / "action.yml").read_text()
    script = re.search(r"^      run: \|\n((?:        .*\n|\n)+)", text, re.MULTILINE).group(1)
    return "".join(line[8:] if line.strip() else "\n" for line in script.splitlines(True))


def step_script(text, start, end):
    step = text[text.index(start):text.index(end)]
    match = re.search(r"^( +)run: \|\n((?:\1 .*\n|\n)+)", step, re.MULTILINE)
    indent = len(match.group(1)) + 2
    return "".join(line[indent:] if line.strip() else "\n" for line in match.group(2).splitlines(True))


def workdir(test):
    path = tempfile.mkdtemp(prefix="vv-qae-in-")
    test.addCleanup(shutil.rmtree, path, True)
    return path


class Criteria(unittest.TestCase):
    """`vibe-verifier criteria` names each criterion's annotations after its count line, and
    actions/criteria still decides on that first line alone."""

    def body(self, text):
        path = os.path.join(workdir(self), "pr-body.md")
        Path(path).write_text(text)
        return path

    def test_each_annotation_is_named_after_the_count(self):
        result = runner("criteria", self.body(BODY + "- A typo [ref: two words]\n"))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "criteria: 3\nAC1 references: home-desktop\nAC2 roles: admin, read-only\n"
                                        "AC3 malformed: [ref: two words]\n")

    def test_a_declared_none_prints_only_its_reason(self):
        self.assertEqual(runner("criteria", self.body("## Acceptance criteria\n\n- None: a pin bump [ref: x]\n")).stdout,
                         "none: a pin bump [ref: x]\n")

    def test_the_action_counts_from_the_first_line(self):
        work = workdir(self)
        Path(work, "pr-body.md").write_text(BODY)
        output = Path(work, "github-output")
        output.write_text("")
        result = subprocess.run(["bash", "-e", "-c", action_script("criteria")], cwd=work, capture_output=True, text=True,
                                env=clean_env({"GITHUB_ACTION_PATH": str(ROOT / "actions" / "criteria"),
                                               "GITHUB_OUTPUT": str(output), "VV_PATH": "pr-body.md", "VV_CHANGED": "",
                                               "VV_TICKET": ""}))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue(output.read_text().startswith("count=2\ndeclared-none=\n"), output.read_text())
        self.assertIn("AC2 roles: admin, read-only", result.stdout)


class QaeInputs(unittest.TestCase):
    """actions/qae-inputs: each supplied reference copied into the evidence, listed with its width for
    the explorer, and its digest declared as the step's output."""

    def run_action(self, work):
        output = Path(work, "github-output")
        output.write_text("")
        result = subprocess.run(["bash", "-e", "-c", action_script("qae-inputs")], cwd=work, capture_output=True, text=True,
                                env=clean_env({"GITHUB_ACTION_PATH": str(ROOT / "actions" / "qae-inputs"),
                                               "GITHUB_OUTPUT": str(output), "RUNNER_TEMP": work, "VV_MANIFEST": "", "VV_ENTRIES": "",
                                               "VV_REFERENCES": "qae-inputs/references", "VV_EVIDENCE": "qae-artifacts"}))
        return result, output.read_text()

    def test_supplied_references_are_copied_listed_and_declared(self):
        work = workdir(self)
        os.makedirs(os.path.join(work, "qae-inputs", "references"))
        Path(work, "qae-inputs", "references", "home-desktop.png").write_bytes(HOME)
        Path(work, "qae-inputs", "references", "notes.txt").write_text("not an image")
        result, output = self.run_action(work)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("references: 1\nhome-desktop: 1440x900\n", result.stdout)
        self.assertEqual(output, "references=%s\n" % json.dumps(DIGESTS))
        self.assertEqual(Path(work, "qae-artifacts", "references", "home-desktop.png").read_bytes(), HOME)
        self.assertIn("- home-desktop: qae-inputs/references/home-desktop.png, 1440 pixels wide (1440 x 900)",
                      Path(work, "qae-inputs", "references.md").read_text())

    def test_none_supplied_declares_an_empty_object_and_copies_nothing(self):
        work = workdir(self)
        result, output = self.run_action(work)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(output, "references={}\n")
        self.assertFalse(os.path.exists(os.path.join(work, "qae-artifacts")))
        self.assertFalse(os.path.exists(os.path.join(work, "qae-inputs", "references.md")))

    def test_a_file_that_is_not_a_png_or_not_a_key_fails_the_step(self):
        for name, data in (("home.png", b"GIF89a not a png"), ("home page.png", HOME)):
            work = workdir(self)
            write(work, {"qae-inputs/references/.keep": ""})
            Path(work, "qae-inputs", "references", name).write_bytes(data)
            result, output = self.run_action(work)
            self.assertEqual(result.returncode, 2, name)
            self.assertIn("is not a PNG named <key>.png", result.stderr)
            self.assertEqual(output, "")


class Gate(unittest.TestCase):
    """acceptance-verdict holds each annotated criterion to the evidence."""

    def setUp(self):
        self.repo = make_repo(self, {"README.md": "x\n"})
        self.inputs = workdir(self)
        self.artifacts = os.path.join(self.inputs, "qae-artifacts")
        write(self.artifacts, {"qae/AC1.md": LOG_AC1, "qae/AC2.md": LOG_AC2})
        os.makedirs(os.path.join(self.artifacts, "references"))
        Path(self.artifacts, "references", "home-desktop.png").write_bytes(HOME)

    def run_gate(self, body=BODY, verdict=VERDICT, references=DIGESTS, *extra):
        write(self.inputs, {"pr-body.md": body, "verdict.md": verdict})
        args = ["--criteria", os.path.join(self.inputs, "pr-body.md"), "--verdict", os.path.join(self.inputs, "verdict.md"),
                "--artifacts", self.artifacts]
        if references is not None:
            write(self.inputs, {"references.json": json.dumps(references) if isinstance(references, dict) else references})
            args += ["--references", os.path.join(self.inputs, "references.json")]
        return gate("acceptance-verdict", self.repo, *args, *extra)

    def test_a_supplied_reference_compared_and_every_role_walked_passes(self):
        result = self.run_gate()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_a_reference_the_workflow_did_not_supply_is_the_operators_to_supply(self):
        result = self.run_gate(references={})
        self.assertEqual(result.returncode, 1)
        self.assertIn("AC1 names reference `home-desktop`, but the workflow supplied no qae-inputs/references/home-desktop.png",
                      result.stdout)
        self.assertIn("needs-operator-reference", result.stdout)
        # An empty file, which the verify job writes when the explore job never declared any, is none supplied.
        self.assertIn("supplied no qae-inputs/references/home-desktop.png", self.run_gate(references="\n").stdout)

    def test_a_manifest_line_without_references_cannot_pass_a_reference(self):
        result = self.run_gate(BODY, VERDICT, None)
        self.assertEqual(result.returncode, 1)
        self.assertIn("declares no supplied references: add --references qae-inputs/references.json", result.stdout)

    def test_an_image_the_explorer_replaced_is_not_the_reference(self):
        Path(self.artifacts, "references", "home-desktop.png").write_bytes(png(1440, 901))
        result = self.run_gate()
        self.assertEqual(result.returncode, 1)
        self.assertIn("references/home-desktop.png in the evidence is not the image the workflow supplied", result.stdout)
        os.remove(os.path.join(self.artifacts, "references", "home-desktop.png"))
        self.assertIn("the evidence holds no references/home-desktop.png", self.run_gate().stdout)

    def test_a_pass_with_no_step_naming_the_reference_is_refused(self):
        write(self.artifacts, {"qae/AC1.md": LOG_AC1.replace("compared with reference home-desktop", "looked at the design")})
        verdict = VERDICT.replace("compared with reference home-desktop", "looked at the design")
        result = self.run_gate(BODY, verdict)
        self.assertEqual(result.returncode, 1)
        self.assertIn("AC1 is a PASS, but no step line in qae/AC1.md names `reference home-desktop`", result.stdout)

    def test_a_pass_missing_a_role_is_refused(self):
        write(self.artifacts, {"qae/AC2.md": LOG_AC2.replace("as read-only: ", "")})
        result = self.run_gate(BODY, VERDICT.replace("as read-only: opened a post", "as admin: opened a post"))
        self.assertEqual(result.returncode, 1)
        self.assertIn("AC2 is a PASS, but no step line in qae/AC2.md starts `as read-only:`", result.stdout)
        self.assertNotIn("`as admin:`", result.stdout)

    def test_a_fail_is_not_held_to_steps_but_a_missing_reference_is_still_named(self):
        verdict = VERDICT.replace("AC1 -- PASS", "AC1 -- FAIL")
        write(self.artifacts, {"qae/AC1.md": "- step 1: opened / -> the hero\n"})
        result = self.run_gate(BODY, verdict, {})
        self.assertEqual(result.returncode, 1)
        self.assertIn("AC1 is not a PASS", result.stdout)
        self.assertIn("supplied no qae-inputs/references/home-desktop.png", result.stdout)
        self.assertNotIn("names `reference home-desktop`", result.stdout)

    def test_a_malformed_annotation_is_refused_not_skipped(self):
        body = BODY + "- The footer links work [as: ]\n"
        verdict = VERDICT + "acceptance-check: AC3 -- PASS -- ok (qae/AC1.md::opened / at 1440 wide)\n"
        result = self.run_gate(body, verdict)
        self.assertEqual(result.returncode, 1)
        self.assertIn("AC3 has a malformed annotation [as: ]", result.stdout)

    def test_annotations_need_the_evidence(self):
        write(self.inputs, {"pr-body.md": BODY, "verdict.md": VERDICT, "references.json": json.dumps(DIGESTS)})
        result = gate("acceptance-verdict", self.repo, "--criteria", os.path.join(self.inputs, "pr-body.md"),
                      "--verdict", os.path.join(self.inputs, "verdict.md"),
                      "--references", os.path.join(self.inputs, "references.json"))
        self.assertEqual(result.returncode, 1)
        self.assertIn("AC1 carries annotations, which are checked in the run's evidence: give the gate --artifacts", result.stdout)

    def test_a_missing_or_malformed_references_file_cannot_run_even_under_soak(self):
        self.assertEqual(self.run_gate(BODY, VERDICT, "{not json", "--soak").returncode, 2)
        self.assertEqual(self.run_gate(BODY, VERDICT, '["home-desktop"]', "--soak").returncode, 2)
        result = gate("acceptance-verdict", self.repo, "--criteria", os.path.join(self.inputs, "pr-body.md"),
                      "--verdict", os.path.join(self.inputs, "verdict.md"), "--references", "/nonexistent/refs.json")
        self.assertEqual(result.returncode, 2)

    def test_criteria_without_annotations_need_no_references_file_contents(self):
        body = "## Acceptance criteria\n\n- The home page loads\n"
        verdict = "acceptance-check: AC1 -- PASS -- it loads (qae/AC1.md::opened / at 1440 wide)\n"
        self.assertEqual(self.run_gate(body, verdict, "").returncode, 0)


class Template(unittest.TestCase):
    """The template copies the references after the site step and before the explorer, and the digests
    reach the gate as a declared input, never through the explorer's artifacts."""

    def test_the_references_step_runs_after_the_site_step_and_before_the_explorer(self):
        text = TEMPLATE.read_text()
        explore = text[text.index("\n  explore:\n"):text.index("\n  verify:\n")]
        order = [explore.index(step) for step in ("        id: site\n", "- name: Prepare the explorer's references and widths",
                                                  "- name: Explore the acceptance criteria in a real browser")]
        self.assertEqual(order, sorted(order))
        self.assertIn("        id: references\n        uses: Greenbauer/vibe-verifier/actions/qae-inputs@", explore)
        self.assertIn("      references: ${{ steps.references.outputs.references }}\n", explore)
        self.assertIn("          REFERENCES: ${{ needs.explore.outputs.references }}\n", text[text.index("\n  verify:\n"):])

    def test_the_declared_digests_reach_the_gate_on_the_manifest_line(self):
        work = workdir(self)
        os.makedirs(os.path.join(work, "qae-inputs", "references"))
        Path(work, "qae-inputs", "references", "home-desktop.png").write_bytes(HOME)
        output = Path(work, "github-output")
        output.write_text("")
        prepare = subprocess.run(["bash", "-e", "-c", action_script("qae-inputs")], cwd=work, capture_output=True, text=True,
                                 env=clean_env({"GITHUB_ACTION_PATH": str(ROOT / "actions" / "qae-inputs"), "GITHUB_OUTPUT": str(output),
                                                "RUNNER_TEMP": work, "VV_REFERENCES": "qae-inputs/references",
                                                "VV_EVIDENCE": "qae-artifacts", "VV_MANIFEST": "", "VV_ENTRIES": ""}))
        self.assertEqual(prepare.returncode, 0, prepare.stdout + prepare.stderr)
        declared = output.read_text().split("=", 1)[1].strip()
        # The verify job's runner has none of the explore job's files: only the output and the artifact.
        verify = workdir(self)
        shutil.copytree(os.path.join(work, "qae-artifacts"), os.path.join(verify, "qae-artifacts"))
        write(verify, {"qae-artifacts/qae/AC1.md": LOG_AC1, "qae-artifacts/qae/AC2.md": LOG_AC2})
        bin_dir = workdir(self)
        Path(bin_dir, "gh").write_text('#!/bin/sh\ncase "$1 $2" in\n  pr*) cat "%s" ;;\n  *pulls*) printf "app/page.tsx\\n" ;;\n'
                                       '  api*) cat "%s" ;;\nesac\n' % (Path(work, "body.md"), Path(work, "comments.json")))
        os.chmod(os.path.join(bin_dir, "gh"), 0o755)
        Path(work, "body.md").write_text(BODY)
        Path(work, "comments.json").write_text(json.dumps([{"user": {"login": "github-actions[bot]"}, "body": VERDICT}]))
        text = TEMPLATE.read_text()
        script = step_script(text, "- name: Write the declared inputs", "- name: Run the QA gates")
        result = subprocess.run(["bash", "-e", "-c", script], cwd=verify, capture_output=True, text=True,
                                env=clean_env({"PATH": bin_dir + os.pathsep + os.environ["PATH"], "GH_TOKEN": "x", "PR_NUMBER": "7",
                                               "REPO": "o/r", "SITE_URL": "http://localhost:3000", "REFERENCES": declared,
                                               "TICKET": ""}))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(json.loads(Path(verify, "qae-inputs", "references.json").read_text()), DIGESTS)
        line = [entry for entry in MANIFEST.read_text().splitlines() if entry.startswith("acceptance-verdict ")]
        self.assertEqual(len(line), 1)
        self.assertIn("--references qae-inputs/references.json", line[0])
        repo = make_repo(self, {"README.md": "x\n"})
        ran = subprocess.run([sys.executable, str(ROOT / "gates" / "acceptance_verdict.py"), "--repo", repo,
                              *shlex.split(line[0])[1:]], cwd=verify, capture_output=True, text=True, env=clean_env())
        self.assertEqual(ran.returncode, 0, ran.stdout + ran.stderr)
        # The explorer overwriting the copy it was handed is caught by the digest the workflow declared.
        Path(verify, "qae-artifacts", "references", "home-desktop.png").write_bytes(png(1440, 2))
        ran = subprocess.run([sys.executable, str(ROOT / "gates" / "acceptance_verdict.py"), "--repo", repo,
                              *shlex.split(line[0])[1:]], cwd=verify, capture_output=True, text=True, env=clean_env())
        self.assertEqual(ran.returncode, 1, ran.stdout + ran.stderr)


if __name__ == "__main__":
    unittest.main()
