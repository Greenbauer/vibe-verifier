"""The osv-scanner ratchet, on the real pinned osv-scanner against osv.dev. The vulnerable pins are old releases
with long-published advisories (requests 2.19.0, urllib3 1.24.1, lodash 4.17.15), so the answers do not move."""
import json
import unittest

from helpers import commit, gate, git, make_repo

VULNERABLE = "requests==2.19.0\n"
SAFE = "PyYAML==6.0.3\n"
LOCK = json.dumps({"name": "x", "lockfileVersion": 3,
                   "packages": {"": {"name": "x"}, "node_modules/lodash": {"version": "4.17.15"}}})


def findings(result):
    return json.loads(result.stdout)["findings"]


class OsvScanner(unittest.TestCase):
    def run_gate(self, repo, *args):
        return gate("osv-scanner", repo, "--base-ref", "HEAD~1", "--format", "json", *args)

    def branch(self, base, head):
        repo = make_repo(self, base)
        commit(repo, head, "change")
        return repo

    def test_adding_a_vulnerable_pin_fails_at_its_line(self):
        result = self.run_gate(self.branch({"README.md": "x\n"}, {"requirements.txt": SAFE + VULNERABLE}))
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        hits = findings(result)
        self.assertTrue(hits)
        self.assertTrue(all(f["path"] == "requirements.txt" and f["line"] == 2 for f in hits), hits)
        self.assertTrue(any("requests 2.19.0 (PyPI): known vulnerability" in f["message"] and "PYSEC-2018-28" in f["message"]
                            for f in hits), hits)

    def test_a_pin_already_vulnerable_at_the_base_never_blocks_but_a_new_one_does(self):
        repo = self.branch({"requirements.txt": VULNERABLE}, {"requirements.txt": VULNERABLE + SAFE})
        self.assertEqual(self.run_gate(repo).returncode, 0)
        commit(repo, {"requirements.txt": VULNERABLE + SAFE + "urllib3==1.24.1\n"}, "another")
        result = self.run_gate(repo)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertEqual({f["message"].split(" ")[0] for f in findings(result)}, {"urllib3"})

    def test_any_requirements_file_name_and_an_npm_lockfile_are_read(self):
        repo = self.branch({"README.md": "x\n"}, {"requirements/dev.txt": VULNERABLE, "web/package-lock.json": LOCK})
        result = self.run_gate(repo)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertEqual({f["path"] for f in findings(result)}, {"requirements/dev.txt", "web/package-lock.json"})
        self.assertTrue(any(f["message"].startswith("lodash 4.17.15 (npm)") for f in findings(result)))

    def test_a_vulnerability_the_repository_ignores_with_a_reason_is_not_a_finding(self):
        ignores = "".join('[[IgnoredVulns]]\nid = "%s"\nreason = "not reachable here"\n\n' % vid for vid in (
            "PYSEC-2018-28", "PYSEC-2023-74", "PYSEC-2026-1872", "PYSEC-2026-1873", "PYSEC-2026-2275"))
        repo = self.branch({"README.md": "x\n"}, {"requirements.txt": VULNERABLE, "osv-scanner.toml": ignores})
        result = self.run_gate(repo)
        self.assertNotIn("PYSEC-2018-28", result.stdout)

    def test_moving_a_file_is_not_a_finding(self):
        repo = make_repo(self, {"old/requirements.txt": VULNERABLE + SAFE})
        git(repo, "mv", "old", "new")
        git(repo, "commit", "-q", "-m", "move")
        self.assertEqual(self.run_gate(repo).returncode, 0)

    def test_all_reports_every_vulnerability(self):
        repo = self.branch({"requirements.txt": VULNERABLE}, {"README.md": "y\n"})
        self.assertEqual(self.run_gate(repo).returncode, 0)
        result = gate("osv-scanner", repo, "--all", "--format", "json")
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("PYSEC-2018-28", result.stdout)

    def test_a_file_the_scanner_cannot_read_cannot_run(self):
        result = self.run_gate(self.branch({"README.md": "x\n"}, {"package-lock.json": "{not json"}))
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn("osv-scanner exited 127", result.stderr)


if __name__ == "__main__":
    unittest.main()
