"""The gates composite action's own step, run under bash as a runner would: it runs a manifest file,
or the gate list a wrapper passes as `entries`. Its script is read from the shipped action.yml, so
the test judges what a consumer pins."""
import json
import os
import re
import subprocess
import tempfile
import unittest

from helpers import ROOT, clean_env, commit, make_repo

ACTION = ROOT / "actions" / "gates" / "action.yml"
BAD_PKG = json.dumps({"dependencies": {"typescript": "5"}})


def script():
    match = re.search(r"\n      run: \|\n((?:        [^\n]*\n|\n)+)", ACTION.read_text())
    return "".join(line[8:] for line in match.group(1).splitlines(True))


class GatesAction(unittest.TestCase):
    def run_action(self, repo, manifest=".vibe-verifier", entries="", base_ref=""):
        temp = tempfile.mkdtemp(prefix="vv-runner-temp-")
        env = clean_env({"VV_MANIFEST": manifest, "VV_ENTRIES": entries, "VV_BASE_REF": base_ref,
                         "RUNNER_TEMP": temp, "GITHUB_ACTION_PATH": str(ACTION.parent)})
        return subprocess.run(["bash", "-c", script()], cwd=repo, capture_output=True, text=True, env=env), temp

    def test_the_manifest_file_is_the_default(self):
        repo = make_repo(self, {"a.txt": "x", ".vibe-verifier": "no-duplicate-package-json-keys\n"})
        result, _ = self.run_action(repo)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertRegex(result.stdout, r"no-duplicate-package-json-keys\s+PASS")
        missing, _ = self.run_action(make_repo(self, {"a.txt": "x"}))
        self.assertEqual(missing.returncode, 2, missing.stdout + missing.stderr)
        self.assertIn("no manifest at .vibe-verifier", missing.stderr)

    def test_entries_are_the_gate_list_and_the_repository_needs_no_manifest(self):
        repo = make_repo(self, {"package.json": BAD_PKG})
        result, temp = self.run_action(repo, entries="# why\nno-duplicate-package-json-keys\nbuild-tools-in-devdependencies --soak")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertRegex(result.stdout, r"no-duplicate-package-json-keys\s+PASS")
        self.assertRegex(result.stdout, r"build-tools-in-devdependencies\s+PASS \(soak\)")
        self.assertTrue(os.path.isfile(os.path.join(temp, "vibe-verifier-entries")))
        self.assertFalse(os.path.exists(os.path.join(repo, ".vibe-verifier")))

    def test_entries_are_never_judged_from_the_base(self):
        # The base's manifest would fail this branch; the entries come from the wrapper's workflow,
        # which a pull request of the target cannot edit, so the repository's own file is not read.
        repo = make_repo(self, {"package.json": BAD_PKG, ".vibe-verifier": "build-tools-in-devdependencies\n"})
        commit(repo, {"README.md": "x\n"})
        result, _ = self.run_action(repo, entries="no-duplicate-package-json-keys", base_ref="HEAD~1")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertNotIn("judged by the manifest at", result.stdout)
        self.assertNotIn("build-tools-in-devdependencies", result.stdout)


if __name__ == "__main__":
    unittest.main()
