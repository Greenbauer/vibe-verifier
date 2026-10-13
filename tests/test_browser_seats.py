"""The browser-seats contract, both halves: the action's step checks a seat list and writes its
outputs, and select.mjs gives a suite's worker k seat k from those outputs. Driven through the
action's own step and through node, because those are what a consumer runs."""
import json
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from helpers import ROOT, clean_env

ACTION = ROOT / "actions" / "browser-seats" / "action.yml"
SELECT = ROOT / "actions" / "browser-seats" / "select.mjs"


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


@unittest.skipUnless(shutil.which("node"), "node runs the suite side of the contract")
class SelectTest(unittest.TestCase):
    ROLES = {"traveler": ["E2E_TRAVELER_EMAIL", "E2E_TRAVELER_PASSWORD"],
             "partner": ["E2E_PARTNER_EMAIL", "E2E_PARTNER_PASSWORD"]}

    def node(self, body):
        script = f"import * as seats from {json.dumps(SELECT.as_uri())};\n{body}"
        result = subprocess.run(["node", "--input-type=module", "-e", script],
                                capture_output=True, text=True, env=clean_env())
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def select(self, index, extra):
        environment = {"E2E_TRAVELER_EMAIL": "traveler+s0@preview.example.test",
                       "E2E_TRAVELER_PASSWORD": "pw-traveler-0", "E2E_PARTNER_EMAIL": "partner+s0@preview.example.test",
                       "E2E_PARTNER_PASSWORD": "pw-partner-0"}
        if index is not None:
            environment["TEST_PARALLEL_INDEX"] = str(index)
        return self.node(
            f"const env = {json.dumps(environment)};\n"
            "let seat, error = null;\n"
            f"try {{ seat = seats.selectSeat(env, {json.dumps(extra)}, {json.dumps(self.ROLES)}); }}"
            " catch (e) { error = e.message; }\n"
            "console.log(JSON.stringify({ seat, env, error }));")

    def test_the_action_output_gives_worker_k_seat_k(self):
        extra = json.dumps([seat(1), seat(2), seat(3)], separators=(",", ":"))
        self.assertEqual(self.node(f"console.log(seats.workersFor({json.dumps(extra)}));"), 4)
        for index in range(4):
            with self.subTest(worker=index):
                picked = self.select(index, extra)
                self.assertEqual(picked["seat"], index)
                self.assertEqual(picked["env"]["E2E_TRAVELER_EMAIL"], f"traveler+s{index}@preview.example.test")
                self.assertEqual(picked["env"]["E2E_PARTNER_PASSWORD"], f"pw-partner-{index}")

    def test_a_run_without_extra_seats_keeps_every_worker_on_seat_0(self):
        self.assertEqual(self.node("console.log(seats.workersFor(''));"), 1)
        for index in (None, 0, 3):
            with self.subTest(worker=index):
                picked = self.select(index, "")
                self.assertEqual(picked["seat"], 0)
                self.assertEqual(picked["env"]["E2E_TRAVELER_EMAIL"], "traveler+s0@preview.example.test")

    def test_a_worker_without_a_seat_refuses_to_share_one(self):
        extra = json.dumps([seat(1)])
        self.assertIn("worker 2 has no seat", self.select(2, extra)["error"])
        broken = json.dumps([{"traveler": {"email": "x@preview.example.test"}}])
        self.assertIn("seat 1 needs an email and password", self.select(1, broken)["error"])


if __name__ == "__main__":
    unittest.main()
