"""The browser-seats contract, both halves: the action's step checks a seat list and writes its
outputs, and select.mjs gives a suite's worker k seat k from those outputs (its own tests are
select.test.mjs, run here on the action's real output). Driven through the action's own step, the
engine's command line and node, because those are what a consumer runs."""
import json
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from helpers import ROOT, clean_env

ACTION = ROOT / "actions" / "browser-seats" / "action.yml"
ENGINE = ROOT / "actions" / "browser-seats" / "seats.py"
SELECT_TEST = ROOT / "actions" / "browser-seats" / "select.test.mjs"


def seat(number, domain="preview.example.test"):
    return {
        role: {"email": f"{role}+s{number}@{domain}", "password": f"pw-{role}-{number}"}
        for role in ("traveler", "partner")
    }


class ActionTest(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.dir)

    def step(self, seats, **inputs):
        """Runs the action's step on a seats file; returns the result and the outputs written."""
        script = re.search(r"      run: \|\n(.*)", ACTION.read_text(), re.S).group(1)
        script = "\n".join(line[8:] for line in script.splitlines())
        (self.dir / "seats.json").write_text(seats if isinstance(seats, str) else json.dumps(seats))
        output = self.dir / "output"
        output.write_text("")
        env = clean_env({"GITHUB_ACTION_PATH": str(ACTION.parent), "GITHUB_OUTPUT": str(output),
                         "SEATS_FILE": str(self.dir / "seats.json"), "EMAIL_DOMAIN": "",
                         "MAX_SEATS": "8"})
        env.update(inputs)
        result = subprocess.run(["bash", "-c", script], capture_output=True, text=True, env=env)
        return result, dict(line.split("=", 1) for line in output.read_text().splitlines())

    def test_four_seats_make_four_workers_and_hand_on_seats_1_to_3_masked(self):
        seats = [seat(n) for n in range(4)]
        result, outputs = self.step(seats, EMAIL_DOMAIN="preview.example.test")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(outputs["workers"], "4")
        self.assertEqual(json.loads(outputs["extra-seats"]), seats[1:])
        masks = re.findall(r"^::add-mask::(.*)$", result.stdout, re.M)
        for number in range(4):
            for role in ("traveler", "partner"):
                self.assertIn(f"pw-{role}-{number}", masks)
        self.assertIn(outputs["extra-seats"], masks)
        # Every mask is printed before the step reports anything else.
        self.assertTrue(result.stdout.startswith("::add-mask::"))

    def test_one_seat_runs_one_worker_and_masks_no_empty_list(self):
        result, outputs = self.step([seat(0)])
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertEqual(outputs, {"workers": "1", "extra-seats": "[]"})
        self.assertNotIn("::add-mask::[]", result.stdout)

    def test_a_list_a_suite_must_not_run_on_fails_before_any_browser(self):
        shared = seat(1)
        shared["partner"] = seat(0)["partner"]
        missing = seat(1)
        missing["partner"] = {"email": "partner+s1@preview.example.test", "password": ""}
        cases = {
            "an empty list": ([], "non-empty JSON list"),
            "not a list": ({"traveler": seat(0)["traveler"]}, "non-empty JSON list"),
            "not JSON": ("[{", "not JSON"),
            "other roles": ([seat(0), {"traveler": seat(1)["traveler"]}], "seat 1 has roles"),
            "no password": ([seat(0), missing], "seat 1's partner has no password"),
            "a shared login": ([seat(0), shared], "seat 1's partner is the same login as seat 0's"),
            "over the cap": ([seat(n) for n in range(3)], "3 seats, over the 2 allowed"),
            "off the domain": ([seat(0), seat(1, "example.com")], "not an @preview.example.test login"),
        }
        for name, (seats, error) in cases.items():
            with self.subTest(name):
                result, outputs = self.step(
                    seats, EMAIL_DOMAIN="preview.example.test",
                    MAX_SEATS="2" if name == "over the cap" else "8")
                self.assertEqual(result.returncode, 1)
                self.assertIn(error, result.stdout)
                self.assertEqual(outputs, {})
                self.assertNotIn("pw-", result.stdout)


    def test_from_a_clone_the_engine_checks_a_seat_list(self):
        """Without GITHUB_OUTPUT (a clone) the outputs print; with it (a workflow) they go to the file."""
        (self.dir / "seats.json").write_text(json.dumps([seat(0), seat(1)]))
        command = [sys.executable, str(ENGINE), "--file", str(self.dir / "seats.json")]
        clone = {key: value for key, value in clean_env().items() if key != "GITHUB_OUTPUT"}
        printed = subprocess.run(command, capture_output=True, text=True, env=clone)
        self.assertEqual(printed.returncode, 0, printed.stdout + printed.stderr)
        self.assertIn("workers=2\n", printed.stdout)
        output = self.dir / "output"
        written = subprocess.run(command, capture_output=True, text=True, env=dict(clone, GITHUB_OUTPUT=str(output)))
        self.assertEqual(written.returncode, 0, written.stdout + written.stderr)
        self.assertTrue(output.read_text().startswith("workers=2\nextra-seats=["))
        self.assertNotIn("workers=", written.stdout)
        missing = subprocess.run([sys.executable, str(ENGINE), "--file", str(self.dir / "absent.json")],
                                 capture_output=True, text=True, env=clone)
        self.assertEqual(missing.returncode, 1)
        self.assertIn("::error::the seats file is missing or is not JSON", missing.stdout)

    @unittest.skipUnless(shutil.which("node"), "node runs the suite side of the contract")
    def test_the_suite_side_gives_worker_k_seat_k_from_the_action_output(self):
        """select.test.mjs, handed the `extra-seats` value this action really wrote."""
        result, outputs = self.step([seat(n) for n in range(4)])
        self.assertEqual(result.returncode, 0, result.stdout)
        run = subprocess.run(["node", "--test", str(SELECT_TEST)], capture_output=True, text=True,
                             env=clean_env({"EXTRA_SEATS": outputs["extra-seats"]}))
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
        self.assertRegex(run.stdout, r"# pass 3\b")
        self.assertRegex(run.stdout, r"# fail 0\b")


if __name__ == "__main__":
    unittest.main()
