"""The review-receipt gate over its declared input files."""
import json
import os
import tempfile
import unittest

from helpers import gate

HEAD = "a" * 40
OLD = "b" * 40


class ReviewReceipt(unittest.TestCase):
    def files(self, receipt=None, head=HEAD + "\n", threads=None):
        root = tempfile.mkdtemp(prefix="vv-rr-")
        self.addCleanup(lambda: __import__("shutil").rmtree(root, True))
        paths = {}
        for name, text in (("receipt.md", receipt), ("head.txt", head), ("threads.json", threads)):
            if text is not None:
                with open(os.path.join(root, name), "w") as handle:
                    handle.write(text)
                paths[name] = os.path.join(root, name)
        return root, paths

    def run_gate(self, paths, *extra):
        args = ["--receipt", paths["receipt.md"], "--head", paths["head.txt"]]
        if "threads.json" in paths:
            args += ["--threads", paths["threads.json"]]
        return gate("review-receipt", ".", *args, *extra)

    def test_a_receipt_for_this_head_passes(self):
        root, paths = self.files(receipt="review-receipt: %s -- full -- run 1\n" % HEAD, threads=json.dumps({"unresolved": 0}))
        result = self.run_gate(paths)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_a_receipt_for_an_older_commit_fails_and_names_it(self):
        root, paths = self.files(receipt="review-receipt: %s -- delta -- run 1\n" % OLD)
        result = self.run_gate(paths)
        self.assertEqual(result.returncode, 1)
        self.assertIn("no review receipt for %s" % HEAD[:10], result.stdout)
        self.assertIn("newest receipt is for %s" % OLD[:10], result.stdout)

    def test_no_receipt_at_all_fails(self):
        root, paths = self.files(receipt="")
        result = self.run_gate(paths)
        self.assertEqual(result.returncode, 1)
        self.assertIn("did not complete", result.stdout)

    def test_unresolved_threads_fail_when_wired(self):
        root, paths = self.files(receipt="review-receipt: %s -- full -- run 1\n" % HEAD, threads=json.dumps({"unresolved": 2}))
        result = self.run_gate(paths)
        self.assertEqual(result.returncode, 1)
        self.assertIn("2 unresolved review threads", result.stdout)
        del paths["threads.json"]
        self.assertEqual(self.run_gate(paths).returncode, 0)

    def test_soak_reports_and_passes(self):
        root, paths = self.files(receipt="")
        result = self.run_gate(paths, "--soak")
        self.assertEqual(result.returncode, 0)
        self.assertIn("did not complete", result.stdout)

    def test_bad_inputs_cannot_run(self):
        root, paths = self.files(receipt="x", head="not-a-sha\n")
        self.assertEqual(self.run_gate(paths).returncode, 2)
        root, paths = self.files(receipt="review-receipt: %s -- full -- run 1\n" % HEAD, threads="{}")
        self.assertEqual(self.run_gate(paths).returncode, 2)
        root, paths = self.files(receipt="x")
        paths["receipt.md"] = os.path.join(root, "missing.md")
        self.assertEqual(self.run_gate(paths).returncode, 2)


if __name__ == "__main__":
    unittest.main()
