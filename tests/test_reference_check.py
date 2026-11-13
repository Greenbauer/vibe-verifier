"""The reference check: the second look at a QAE FAIL that claims a difference from a design reference.

`Grader` runs `bin/vibe-verifier reference-check` with a fake Codex CLI: which FAILs it asks about, what the model is
given (the two images, the claim, an empty directory) and the record it writes. `Gate` holds the acceptance-verdict
gate to those records. `Model` asks the real model about the made-up pages in tests/fixtures/reference-check/ (drawn with Pillow, a planner
page with a date-range modal at 1440 by 900): `close-x` is the shape of a FAIL a consumer's explorer invented (the
reference has the same close X), `no-modal` is a real difference, and `close-x-timeout` is the invented X plus a save
that could not be checked, which no look at two images settles. It needs a Codex login, so it runs only where REFERENCE_CHECK_CODEX_HOME names one."""
import hashlib
import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path

from helpers import ROOT, gate, make_repo, runner, write

import _reference_check
import _references

FIXTURES = ROOT / "tests" / "fixtures" / "reference-check"
KEY = "date-range-modal"
STEP = "step 24: compared with reference %s -> The modal adds a close X absent from the reference" % KEY
FAIL = "acceptance-check: AC1 -- FAIL -- The date modal adds a close control. (qae/AC1.md::%s)" % STEP
CLAIM = "The date modal adds a close control. (qae/AC1.md::%s)" % STEP

FAKE_CODEX = r'''#!/usr/bin/env python3
import json, os, sys
args = sys.argv[1:]
images = [args[i + 1] for i, arg in enumerate(args) if arg == "--image"]
with open(os.environ["FAKE_LOG"], "a") as log:
    log.write(json.dumps([args, images, sorted(os.listdir(args[args.index("--cd") + 1]))]) + "\n")
if os.environ.get("FAKE_BEHAVIOUR") == "limit":
    print(json.dumps({"type": "turn.failed", "error": {"message": "You've hit your usage limit. Try again at 3:05 PM."}}), flush=True)
    sys.exit(1)
seen = os.environ.get("FAKE_BEHAVIOUR") == "confirm"
open(args[args.index("-o") + 1], "w").write(json.dumps({"confirmed": seen, "other": False, "reason": "Both images show a close X."}))
'''


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def evidence(root, verdict=FAIL, item="AC1"):
    """An explorer's artifact directory: the reference, the comparison's screenshot and step log, and the verdict."""
    root = Path(root)
    (root / "references").mkdir(parents=True, exist_ok=True)
    (root / "qae").mkdir(parents=True, exist_ok=True)
    shutil.copy(FIXTURES / "close-x" / "references" / (KEY + ".png"), root / "references" / (KEY + ".png"))
    shutil.copy(FIXTURES / "close-x" / "qae" / "TC11-step-24.png", root / "qae" / ("%s-step-24.png" % item))
    write(root, {"qae/%s.md" % item: "- %s\n" % STEP, "verdict.md": verdict + "\n"})


class Comparison(unittest.TestCase):
    def test_only_a_fail_whose_one_anchor_is_a_step_naming_one_reference_is_a_comparison(self):
        self.assertEqual(_reference_check.comparison("AC1", FAIL), ("24", KEY, CLAIM))
        # The reference is read from what the step did; what it saw may say "the reference" in passing.
        for line in (FAIL.replace("FAIL", "PASS", 1), FAIL.replace("qae/AC1.md", "qae/AC2.md"),
                     "acceptance-check: AC1 -- FAIL -- Save shows nothing. (qae/AC1.md::step 3: pressed Save -> nothing)",
                     FAIL.replace("compared with reference %s" % KEY, "compared with reference a and reference b"),
                     FAIL + " (qae/AC1.md::step 25: closed it -> closed)"):
            self.assertIsNone(_reference_check.comparison("AC1", line), line)

    def test_a_fail_that_is_no_comparison_counts_without_a_second_look(self):
        line = "acceptance-check: AC1 -- FAIL -- Save shows nothing. (qae/AC1.md::step 3: pressed Save -> nothing)"
        self.assertEqual(_references.second_look("AC1", "The modal opens [ref: %s]" % KEY, line, "/nonexistent"), (True, ""))


class Grader(unittest.TestCase):
    def setUp(self):
        self.work = Path(tempfile.mkdtemp(prefix="vv-reference-check-"))
        self.addCleanup(shutil.rmtree, self.work, True)
        evidence(self.work / "art", FAIL + "\nacceptance-check: AC2 -- FAIL -- Save shows no message. (qae/AC2.md::step 3: "
                 "pressed Save -> nothing happens)\nacceptance-check: AC3 -- PASS -- It matches. (qae/AC3.md::step 1: "
                 "compared with reference %s -> it matches)" % KEY)
        write(self.work, {"home/auth.json": json.dumps({"auth_mode": "chatgpt", "tokens": {"refresh_token": "r"}}),
                          "codex": FAKE_CODEX, "art/qae/AC9-reference-check.json": "{}"})
        os.chmod(self.work / "codex", 0o755)

    def run_grader(self, behaviour=""):
        return runner("reference-check", "--artifacts", str(self.work / "art"), "--codex", str(self.work / "codex"),
                      "--codex-home", str(self.work / "home"),
                      env={"FAKE_LOG": str(self.work / "calls.jsonl"), "FAKE_BEHAVIOUR": behaviour})

    def calls(self):
        return [json.loads(line) for line in (self.work / "calls.jsonl").read_text().splitlines()]

    def test_only_a_comparison_fail_is_asked_about_with_the_two_images_and_its_claim_in_an_empty_directory(self):
        result = self.run_grader()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        (call,) = self.calls()
        args, images, listed = call
        art = self.work / "art"
        self.assertEqual(images, [os.path.abspath(art / "references" / (KEY + ".png")), os.path.abspath(art / "qae" / "AC1-step-24.png")])
        self.assertEqual(listed, [".git"])
        self.assertIn(CLAIM, args[-1])
        self.assertIn(str(ROOT / "harnesses" / "reference-check" / "schema.json"), args)
        record = json.loads((art / "qae" / "AC1-reference-check.json").read_text())
        self.assertEqual(record, {"step": "24", "reference": KEY, "claim": CLAIM,
                                  "reference_sha256": sha(art / "references" / (KEY + ".png")),
                                  "screenshot_sha256": sha(art / "qae" / "AC1-step-24.png"),
                                  "output": {"confirmed": False, "other": False, "reason": "Both images show a close X."}, "error": None})
        # Only this run's records are in the evidence: one left from before is removed.
        self.assertEqual(sorted(path.name for path in (art / "qae").glob("*-reference-check.json")), ["AC1-reference-check.json"])

    def test_a_check_that_did_not_run_is_recorded_with_its_error_and_exits_2(self):
        result = self.run_grader("limit")
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        record = json.loads((self.work / "art" / "qae" / "AC1-reference-check.json").read_text())
        self.assertIsNone(record["output"])
        self.assertIn("usage limit", record["error"])

    def test_a_missing_screenshot_is_recorded_without_asking_the_model(self):
        os.remove(self.work / "art" / "qae" / "AC1-step-24.png")
        self.assertEqual(self.run_grader().returncode, 2)
        self.assertFalse((self.work / "calls.jsonl").exists())
        record = json.loads((self.work / "art" / "qae" / "AC1-reference-check.json").read_text())
        self.assertIn("qae/AC1-step-24.png", record["error"])


class Gate(unittest.TestCase):
    def setUp(self):
        self.repo = make_repo(self, {"README.md": "x\n"})
        self.work = Path(tempfile.mkdtemp(prefix="vv-reference-gate-"))
        self.addCleanup(shutil.rmtree, self.work, True)
        self.art = self.work / "art"
        evidence(self.art)
        reference = self.art / "references" / (KEY + ".png")
        write(self.work, {"criteria.md": "## Acceptance criteria\n- The date modal matches the design [ref: %s]\n" % KEY,
                          "references.json": json.dumps({KEY: sha(reference)})})

    def record(self, **changes):
        record = {"step": "24", "reference": KEY, "claim": CLAIM, "reference_sha256": sha(self.art / "references" / (KEY + ".png")),
                  "screenshot_sha256": sha(self.art / "qae" / "AC1-step-24.png"),
                  "output": {"confirmed": False, "other": False, "reason": "Both images show a close X."}, "error": None}
        record.update(changes)
        write(self.art, {"qae/AC1-reference-check.json": json.dumps(record)})

    def run_gate(self, criteria="criteria.md"):
        return gate("acceptance-verdict", self.repo, "--criteria", str(self.work / criteria), "--verdict",
                    str(self.art / "verdict.md"), "--artifacts", str(self.art), "--references", str(self.work / "references.json"))

    def test_a_fail_the_second_look_refutes_is_judged_as_a_pass_at_its_comparison_step(self):
        self.record()
        result = self.run_gate()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("AC1's FAIL does not count: a second look at the reference and step 24's screenshot did not see "
                      "the difference: Both images show a close X.", result.stdout)

    def test_a_fail_stands_unless_its_record_refutes_exactly_this_claim_about_these_images(self):
        cases = {"confirmed": dict(output={"confirmed": True, "reason": "Only the screenshot has an X."}),
                 "did not run": dict(output=None, error="the grader ran past --timeout (600 seconds)"),
                 "another claim": dict(claim=CLAIM + " Also the title differs."),
                 "another step": dict(step="23"),
                 "other images": dict(screenshot_sha256="0" * 64),
                 "no answer": dict(output={"reason": "?"}),
                 "more than a look": dict(output={"confirmed": False, "other": True, "reason": "The X is in both; the timeout is not."})}
        for name, changes in cases.items():
            self.record(**changes)
            result = self.run_gate()
            self.assertEqual(result.returncode, 1, name)
            self.assertIn("AC1 is not a PASS: " + FAIL, result.stdout, name)
        os.remove(self.art / "qae" / "AC1-reference-check.json")
        self.assertIn("no second look at it ran", self.run_gate().stdout)

    def test_a_refuted_fail_on_a_reference_the_criterion_does_not_name_still_counts(self):
        self.record()
        write(self.work, {"plain.md": "## Acceptance criteria\n- The date modal opens\n"})
        result = self.run_gate("plain.md")
        self.assertEqual(result.returncode, 1)
        self.assertIn("the reference it was compared with is not one this criterion names", result.stdout)

    def test_a_fail_on_anything_but_a_comparison_is_never_put_to_a_second_look(self):
        write(self.art, {"verdict.md": "acceptance-check: AC1 -- FAIL -- Save shows nothing. (qae/AC1.md::step 3: pressed "
                                       "Save -> nothing happens)\n"})
        self.record()
        result = self.run_gate()
        self.assertEqual(result.returncode, 1)
        self.assertNotIn("second look", result.stdout)


@unittest.skipUnless(os.environ.get("REFERENCE_CHECK_CODEX_HOME"), "asks the real model: needs REFERENCE_CHECK_CODEX_HOME, a Codex login")
class Model(unittest.TestCase):
    def test_the_model_rejects_the_invented_close_x_and_confirms_a_missing_modal(self):
        for name, confirmed, other in (("close-x", False, False), ("no-modal", True, False), ("close-x-timeout", False, True)):
            folder = Path(tempfile.mkdtemp(prefix="vv-reference-model-"))
            self.addCleanup(shutil.rmtree, folder, True)
            shutil.copytree(FIXTURES / name, folder, dirs_exist_ok=True)
            result = runner("reference-check", "--artifacts", str(folder), "--codex-home", os.environ["REFERENCE_CHECK_CODEX_HOME"],
                            "--model", "gpt-6.1-sol", "--reasoning-effort", "high")
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            record = json.loads((folder / "qae" / "TC11-reference-check.json").read_text())
            self.assertIs(record["output"]["confirmed"], confirmed, (name, record["output"]))
            self.assertIs(record["output"]["other"], other, (name, record["output"]))


if __name__ == "__main__":
    unittest.main()
