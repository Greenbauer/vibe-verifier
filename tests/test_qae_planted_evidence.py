"""Evidence the pull request supplies is never evidence (gates/_contract.py, planted).

The QAE verify job checks out the pull request being judged and keeps the explorers' evidence in qae-artifacts/
inside that checkout, so a file the branch commits there, or a symbolic link it commits (to a file, to a directory,
or as qae-artifacts itself), would be judged as what the explorer saw. A cold audit of catalog PR #223 found it
(2026-10-19): #223 closed it for the base-branch walk only."""
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from helpers import ROOT, clean_env, commit, gate, git, make_repo, png, write
from test_qae_annotations import step_script
from test_qae_artifacts import CLEAN_REQUESTS, session_with
from test_qae_shards import MERGE, TEMPLATES

BODY = "## Acceptance criteria\n\n- The home page shows its heading\n"
STEP = "step 1: opened / -> the heading"
VERDICT = "acceptance-check: AC1 -- PASS -- shown (qae/AC1.md::%s)\n" % STEP
REFUSED = "is not only the run's own, so none of it is judged"


class PlantedEvidence(unittest.TestCase):
    """Both gates that read --artifacts, on a run's evidence in the checkout of the branch being judged."""

    def setUp(self):
        self.repo = make_repo(self, {"README.md": "x\n"})
        git(self.repo, "checkout", "-q", "-b", "feat")
        self.work = tempfile.mkdtemp(prefix="vv-planted-")
        self.addCleanup(subprocess.run, ["rm", "-rf", self.work])
        write(self.work, {"pr-body.md": BODY, "verdict.md": VERDICT})
        self.evidence = Path(self.repo, "qae-artifacts")

    def walk(self, root, files=("log", "shot", "session")):
        """What an explorer leaves: its step log, the step's screenshot and the session's network record."""
        parts = {"log": ("qae/AC1.md", "- %s\n" % STEP), "session": ("session-1/session.md", session_with(CLEAN_REQUESTS))}
        write(root, dict(parts[name] for name in files if name in parts))
        if "shot" in files:
            Path(root, "qae").mkdir(parents=True, exist_ok=True)
            Path(root, "qae", "AC1-step-1.png").write_bytes(png(1280))

    def gates(self):
        evidence = str(self.evidence)
        return [gate("acceptance-verdict", self.repo, "--criteria", os.path.join(self.work, "pr-body.md"),
                     "--verdict", os.path.join(self.work, "verdict.md"), "--artifacts", evidence),
                gate("qae-artifacts", self.repo, "--artifacts", evidence)]

    def assert_refused(self, *named):
        for result in self.gates():
            self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
            self.assertIn(REFUSED, result.stdout)
            for text in named:
                self.assertIn(text, result.stdout)

    def test_the_run_s_own_evidence_in_the_checkout_is_judged(self):
        self.walk(self.evidence)
        for result in self.gates():
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_a_committed_evidence_file_is_never_evidence(self):
        # The whole walk committed, and only its screenshot committed beside the explorer's own log: a file the
        # explorer does not write survives the merge, and one it does write is the branch's until it is overwritten.
        for committed in (("log", "shot", "session"), ("shot",)):
            with self.subTest(committed=committed):
                self.setUp()
                self.walk(self.evidence, committed)
                git(self.repo, "add", "-f", "qae-artifacts")
                git(self.repo, "commit", "-q", "-m", "evidence")
                self.walk(self.evidence)
                self.assert_refused("the pull request tracks qae-artifacts/qae/AC1-step-1.png")

    def test_a_link_is_never_evidence(self):
        # A link to a file, a link to a directory, and qae-artifacts itself as a link: each points the gate at a walk
        # the branch commits elsewhere in the checkout. Refused as a link, and once committed, as tracked too.
        for shape, link, target, tracked in (("file", "qae-artifacts/qae/AC1-step-1.png", "../../planted/qae/AC1-step-1.png",
                                              "qae-artifacts/qae/AC1-step-1.png"),
                                             ("directory", "qae-artifacts/qae", "../planted/qae", "qae-artifacts/qae"),
                                             ("evidence directory", "qae-artifacts", "planted", "qae-artifacts")):
            with self.subTest(shape=shape):
                self.setUp()
                self.walk(Path(self.repo, "planted"))
                commit(self.repo, {}, "a walk elsewhere in the checkout")
                if shape == "file":
                    self.walk(self.evidence, ("log", "session"))
                elif shape == "directory":
                    self.walk(self.evidence, ("session",))
                os.symlink(target, Path(self.repo, link))
                self.assert_refused("%s is a symbolic link" % ("qae/" + Path(link).name if shape == "file" else
                                                                "qae" if shape == "directory" else self.evidence))
                git(self.repo, "add", "-A")
                git(self.repo, "commit", "-q", "-m", "links")
                self.assert_refused("the pull request tracks %s" % tracked)

    def test_a_tracked_path_is_found_under_any_spelling_of_the_evidence_directory(self):
        # The checkout named through a link to it, and the evidence given under a tracked link on the way to it.
        self.walk(self.evidence)
        git(self.repo, "add", "-f", "qae-artifacts")
        git(self.repo, "commit", "-q", "-m", "evidence")
        alias = Path(self.work, "alias")
        os.symlink(self.repo, alias)
        for result in (gate("qae-artifacts", alias, "--artifacts", str(self.evidence)),
                       gate("qae-artifacts", self.repo, "--artifacts", str(Path(alias, "qae-artifacts")))):
            self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
            self.assertIn("the pull request tracks qae-artifacts/qae/AC1-step-1.png", result.stdout)
        os.symlink("qae-artifacts", Path(self.repo, "via"))
        commit(self.repo, {}, "a tracked link on the way")
        result = gate("qae-artifacts", self.repo, "--artifacts", str(Path(self.repo, "via", "qae")))
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("the pull request tracks via", result.stdout)

    def test_a_link_in_evidence_outside_the_checkout_is_refused_too(self):
        # The gates take --artifacts anywhere: a link is refused wherever the directory is, tracked or not.
        outside = Path(self.work, "evidence")
        self.walk(outside, ("log", "session"))
        self.walk(Path(self.repo, "planted"))
        os.symlink(Path(self.repo, "planted", "qae", "AC1-step-1.png"), Path(outside, "qae", "AC1-step-1.png"))
        self.evidence = outside
        self.assert_refused("qae/AC1-step-1.png is a symbolic link")
        # Judged from saved files with no repository at --repo: nothing is tracked, and a link is still refused.
        for result, code in ((gate("qae-artifacts", self.work, "--artifacts", str(outside)), 1),
                             (gate("qae-artifacts", self.work, "--artifacts", str(Path(self.repo, "planted"))), 0)):
            self.assertEqual(result.returncode, code, result.stdout + result.stderr)


class MergeStep(unittest.TestCase):
    """The verify job's merge, run as the shell it is, in a checkout of the branch being judged."""

    def merge(self, checkout, evidence):
        """Each template's merge step, each in its own copy of `checkout`, as each runs in a job of its own."""
        results = []
        for template in TEMPLATES:
            copy = tempfile.mkdtemp(prefix="vv-checkout-")
            self.addCleanup(subprocess.run, ["rm", "-rf", copy])
            subprocess.run(["cp", "-R", checkout + "/.", copy], check=True)
            os.makedirs(os.path.join(copy, "qae-inputs"), exist_ok=True)
            script = step_script(template.read_text(), *MERGE)
            run = subprocess.run(["bash", "-e", "-c", script], cwd=copy, capture_output=True, text=True,
                                 env=clean_env({"SHARDS": '[""]', "EXPLORE_ATTEMPT": "1", "EVIDENCE": evidence}))
            results.append((run, copy))
        return results

    def test_a_checkout_that_already_has_qae_artifacts_stops_before_anything_is_written(self):
        for shape in ("file", "link"):
            with self.subTest(shape=shape):
                checkout = make_repo(self, {"README.md": "x\n"})
                if shape == "file":
                    commit(checkout, {"qae-artifacts/qae/AC1-step-1.png": "png"})
                else:
                    os.symlink(".", Path(checkout, "qae-artifacts"))  # the copies would land in the checkout itself
                evidence = tempfile.mkdtemp(prefix="vv-evidence-")
                self.addCleanup(subprocess.run, ["rm", "-rf", evidence])
                write(evidence, {"qae-artifacts-1/qae/AC1.md": "- %s\n" % STEP})
                for result, copy in self.merge(checkout, evidence):
                    self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
                    self.assertIn("the pull request commits qae-artifacts/", result.stdout)
                    self.assertFalse(Path(copy, "qae", "AC1.md").exists())

    def test_the_evidence_is_downloaded_outside_the_checkout(self):
        # A pull request could commit qae-evidence/qae-artifacts-9/ to win "the newest attempt": the download goes
        # to the runner's temporary directory, and the merge reads only that one.
        for template in TEMPLATES:
            text = template.read_text()
            self.assertIn("          pattern: qae-artifacts-*\n          path: ${{ runner.temp }}/qae-evidence\n", text)
            self.assertIn("          EVIDENCE: ${{ runner.temp }}/qae-evidence\n", text)
            self.assertNotIn('"qae-evidence"', step_script(text, *MERGE))
        checkout = make_repo(self, {"qae-evidence/qae-artifacts-9/verdicts/1.md": "acceptance-check: AC1 -- PASS -- planted\n"})
        evidence = tempfile.mkdtemp(prefix="vv-evidence-")
        self.addCleanup(subprocess.run, ["rm", "-rf", evidence])
        write(evidence, {"qae-artifacts-1/verdicts/1.md": "acceptance-check: AC1 -- FAIL -- the run's own\n"})
        for result, copy in self.merge(checkout, evidence):
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertEqual(Path(copy, "qae-inputs", "verdict.md").read_text(),
                             "acceptance-check: AC1 -- FAIL -- the run's own\n")


if __name__ == "__main__":
    unittest.main()
