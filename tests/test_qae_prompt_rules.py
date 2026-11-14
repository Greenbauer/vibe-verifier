"""The explorer prompt states the rules the gates judge by. The rules a gate holds as data are rendered into it from
that data (gates/_qae_prompt.py), so a gate that changes one changes the prompt (#218, #280); and every line form the
prompt shows (a step line, where a step log or screenshot goes, a record's file name, a verdict line), written the way
it says, is read by the gate pattern or gate run that reads it (#244). Rules only the prompt's prose states are not held
here."""
import argparse
import os
import re
import shutil
import tempfile
import types
import unittest
from unittest import mock
from pathlib import Path

from helpers import ROOT, png, write
from test_features import RegressionWalk

import _probe  # gates/, which helpers puts on the path
import _qae_prompt
import _refusals
import _retry
import acceptance_verdict
import qae_artifacts
import test_qae_artifacts
from _acceptance import REFERENCE_NAMED, VERDICT_RE, anchors_in
from _qae_rules import ENVIRONMENTAL

PLACEHOLDER = re.compile(r"<([^<>]*)>")


class Rendered(unittest.TestCase):
    def test_the_prompt_and_each_lanes_copy_are_rendered_from_the_gates(self):
        prompt = _qae_prompt.render()
        self.assertEqual(Path(_qae_prompt.PROMPT).read_text(), prompt, "run python3 gates/_qae_prompt.py")
        for lane, marker, fills in _qae_prompt.LANES:
            with self.subTest(lane=lane):
                self.assertEqual((ROOT / "harnesses" / "qae" / lane).read_text(),
                                 _qae_prompt.inlined(lane, marker, prompt, fills), "run python3 gates/_qae_prompt.py")

    def test_every_allowance_the_gate_takes_is_one_the_prompt_names(self):
        parser = argparse.ArgumentParser()
        qae_artifacts.add_arguments(parser)
        allowances = {option for action in parser._actions for option in action.option_strings if option.startswith("--allow-")}
        self.assertEqual(allowances, {option for _, option, _ in ENVIRONMENTAL})
        prompt = " ".join(Path(_qae_prompt.PROMPT).read_text().split())
        for kind, _, _ in ENVIRONMENTAL:
            self.assertIn("`%s: <pattern>`" % kind, prompt)

    def test_a_template_without_a_rule_does_not_render(self):
        work = tempfile.mkdtemp(prefix="vv-prompt-")
        self.addCleanup(shutil.rmtree, work, True)
        template = os.path.join(work, "prompt.in.md")
        write(work, {"prompt.in.md": Path(_qae_prompt.TEMPLATE).read_text().replace("{{refusals}}\n", "")})
        with self.assertRaisesRegex(ValueError, "refusals"):
            _qae_prompt.render(template)


    def test_a_source_of_declared_refusals_the_prompt_does_not_name_is_refused(self):
        # qae-artifacts gathers its sources through declared_refusals, which looks each up in DECLARED_BY.
        args = types.SimpleNamespace(features=None)
        self.assertEqual(_refusals.declared_refusals(args, [], [], [])[0], set())
        with mock.patch.dict(_refusals.DECLARED_BY), self.assertRaises(KeyError):
            del _refusals.DECLARED_BY["ticket"]
            _refusals.declared_refusals(args, [], [], [])


def fill(form):
    """A step line or file name the prompt shows, for AC1's step 1 (a retry is step 2), as admin, of reference home."""
    for name, value in (("ACn", "AC1"), ("step m", "step 2"), ("step k", "step 1"), ("step-k", "step-1"), ("<role>", "admin"), ("<key>", "home"), ("<id>", "auth")):
        form = form.replace(name, value)
    return PLACEHOLDER.sub("seen", form.strip())


def shown(prompt, start):
    """The one line of the prompt that starts `start`, filled in."""
    lines = [line for line in prompt.splitlines() if line.strip().startswith(start)]
    if len(lines) != 1:
        raise AssertionError("the prompt shows %d lines starting %r, not one" % (len(lines), start))
    return fill(lines[0])


class StepForms(unittest.TestCase):
    """Each step line form the prompt shows, and each record it says to save, as the gate that reads it reads it."""

    prompt = Path(_qae_prompt.PROMPT).read_text()

    def said(self, start):
        line = shown(self.prompt, start)
        self.assertRegex(line, acceptance_verdict.STEP_LINE)
        self.assertRegex(line, qae_artifacts.STEP_LINE)
        return acceptance_verdict.STEP_LINE.match(line).group("text")

    def test_the_marked_steps_are_the_gates_markers(self):
        self.assertTrue(_probe.PROBE.match(self.said("- step k: invalid input:")))
        self.assertTrue(_probe.PROBE.match(" as admin:" + self.said("- step k: invalid input:")))
        self.assertEqual(_retry.RETRY.match(self.said("- step m: retry of step k:")).group("k"), "1")
        # A retry counts only when it repeats its step's action, which the gate reads as the text before "->".
        plain = self.said("- step k: <what you did>").replace("seen", "opened the cart", 1)
        self.assertEqual(_retry.action(plain), "opened the cart")
        self.assertEqual(REFERENCE_NAMED.search(self.said("- step k: compared with reference")).group("key"), "home")
        self.assertRegex(shown(self.prompt, "- step k: as <role>:"), acceptance_verdict.STEP + r"[ \t]*as[ \t]+admin[ \t]*:")

    def written(self, pattern, line="- step 1: seen -> seen\n"):
        """Write a step log and its screenshot where the prompt's `pattern` says (groups: log, screenshot) in a fresh
        qae-artifacts directory, returned."""
        named = re.search(pattern, " ".join(self.prompt.split()))
        self.assertIsNotNone(named, pattern)
        root = tempfile.mkdtemp(prefix="vv-steps-")
        self.addCleanup(shutil.rmtree, root, True)
        log, shot = (fill(path)[len("qae-artifacts/"):] for path in named.group("log", "shot"))
        write(root, {log: line})
        Path(root, shot).write_bytes(png(1280))
        return root

    def test_a_step_logged_and_shot_where_the_prompt_says_is_a_step_the_gates_count(self):
        line = shown(self.prompt, "- step k: <what you did>") + "\n"
        root = self.written(r"save a screenshot to (?P<shot>qae-artifacts/\S+?\.png), then append one line to "
                            r"(?P<log>qae-artifacts/\S+?\.md)", line)
        self.assertEqual(qae_artifacts.steps_have_screenshots(root), [])
        os.remove(os.path.join(root, "qae", "AC1-step-1.png"))
        self.assertNotEqual(qae_artifacts.steps_have_screenshots(root), [], "the check reads the screenshot")
        root = self.written(r"step log is (?P<log>qae-artifacts/\S+?\.md) and each step's screenshot "
                            r"(?P<shot>qae-artifacts/\S+?\.png)", line)
        self.assertEqual(acceptance_verdict.walked_steps(root, "auth"), 1)

    def test_a_record_saved_by_the_name_the_prompt_gives_is_the_one_the_retry_reads(self):
        prompt = " ".join(self.prompt.split())
        root = tempfile.mkdtemp(prefix="vv-records-")
        self.addCleanup(shutil.rmtree, root, True)
        for tool, kind in _retry.RECORDS:
            with self.subTest(record=kind):
                named = re.search(r"%s with filename (qae-artifacts/\S+-%s\.txt)" % (tool, kind), prompt)
                self.assertIsNotNone(named, "the prompt says to save %s's record by name" % tool)
                saved = fill(named.group(1))
                write(root, {saved[len("qae-artifacts/"):]: "a record\n"})
                self.assertEqual(_retry.step_records(root, [(tool, saved, None, None)], "AC1", "1"), ["a record\n"])

    def test_a_fail_written_as_the_prompt_says_is_read_as_a_fail_of_its_step(self):
        line = follow(forms(self.prompt)["acceptance-check: ACn -- FAIL"], "AC1", "- step 1: wrong password -> a blank page")
        self.assertEqual(VERDICT_RE.match(line).group(1), "FAIL")
        self.assertEqual([(a.path, a.title) for a in anchors_in(line)], [("qae/AC1.md", "- step 1: wrong password -> a blank page")])


def follow(form, fid, line):
    """A verdict line written as the prompt's `form` says, for feature `fid` (or AC1) anchored to step log `line`."""
    def fill(match):
        said = match.group(1)
        if said.startswith("the step line text"):
            return line[2:] if 'after "- "' in said else line
        return "seen"
    return PLACEHOLDER.sub(fill, form.strip().replace("<id>", fid).replace("ACn", "AC1")) + "\n"


def forms(prompt):
    """The prompt's verdict line forms the re-walk's gate reads, by what each starts with."""
    found = {}
    for start in ("acceptance-check: ACn -- PASS", "acceptance-check: ACn -- FAIL", "regression-check: <id> -- PASS",
                  "regression-check: <id> -- FAIL", "regression-known: <id>", "regression-skip: <id>"):
        lines = [line for line in prompt.splitlines() if line.strip().startswith(start)]
        found[start] = lines[0] if len(lines) == 1 else None
    return found


class AccessFirst(unittest.TestCase):
    """The prompt has a role take every step that gives it access before it opens another role's record, because the
    gate fails the site's correct refusal of a role with no access yet when no criterion declares it (a consumer's
    walker opened a journey as a crew member before the owner confirmed the invitation, 2026-11-14)."""
    setUp, run_gate = test_qae_artifacts.QaeArtifacts.setUp, test_qae_artifacts.QaeArtifacts.run_gate

    def test_the_prompt_says_to_give_access_first_and_the_gate_fails_the_refusal_it_prevents(self):
        prompt = " ".join(Path(_qae_prompt.PROMPT).read_text().split())
        self.assertIn("Before you open, as one role, a record another role made, take every step that gives it access",
                      prompt)
        self.assertIn("that refusal fails the run unless the criterion declares it", prompt)
        write(self.root, {"network-1.log": "1. [GET] http://localhost:3000/api/journeys/77c67703-e430-46df-bc10-"
                                           "c189f95af34d => [403] Forbidden\n"})
        result = self.run_gate()
        self.assertEqual(result.returncode, 1)
        self.assertIn("request answered 403 outside the allowlist", result.stdout)


class Followed(RegressionWalk):
    """The same run as tests/test_regression_known.py: a re-walk FAIL the base branch shows too is a warning only."""

    def verdict_as_the_prompt_says(self, prompt):
        form = forms(prompt)
        self.assertNotIn(None, form.values(), "the prompt shows each verdict line once")
        return (follow(form["acceptance-check: ACn -- PASS"], "AC1", "- step 1: wrong password -> an error")
                + follow(form["regression-check: <id> -- FAIL"], "auth", "- step 1: signed in as the test user -> the dashboard")
                + follow(form["regression-known: <id>"], "auth", "- step 1: signed in as the test user -> an error page")
                + follow(form["regression-check: <id> -- PASS"], "projects", "- step 1: opened /projects -> two projects listed")
                + follow(form["regression-skip: <id>"], "projects", ""))

    def test_the_verdict_lines_the_prompt_shows_pass_the_gate(self):
        prompt = Path(_qae_prompt.PROMPT).read_text()
        # The base branch's walk, logged and shot where the prompt says.
        base = re.search(r"log those steps in (\S+) with screenshots (\S+?\.png)", " ".join(prompt.split()))
        log, shot = (fill(path)[len("qae-artifacts/"):] for path in base.groups())
        write(os.path.join(self.work, "qae-artifacts"), {log: "- step 1: signed in as the test user -> an error page\n"})
        Path(self.work, "qae-artifacts", shot).write_bytes(png(1280))
        result = self.rewalk(self.verdict_as_the_prompt_says(prompt))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("advisory: auth fails the same way on the base branch", result.stdout)
        self.assertIn("projects: states not walked, as the explorer reports: seen", result.stdout)


if __name__ == "__main__":
    unittest.main()
