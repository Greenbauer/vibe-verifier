"""qae-artifacts counts a Next.js App Router `_rsc` fetch of a declared refused page as that page: a navigation
to it also requests `<path>?_rsc=<token>`, which answers the same refusal, in the network record and in
Chromium's console line. Only `_rsc` is dropped; any other query still has to be declared. Driven through the
gate's command line."""
import os
import shutil
import tempfile
import unittest

from helpers import gate, make_repo, write
from test_qae_artifacts import CLEAN_REQUESTS, session_with

SITE = "https://site-git-fix-auth-gate.vercel.app"
DECLARED = "## Acceptance criteria\n\n- A missing customer is not found (expected-refusal: 404 /customers/{id})\n"


class RscRefusal(unittest.TestCase):
    def setUp(self):
        self.repo = make_repo(self, {"README.md": "x\n"})
        self.work = tempfile.mkdtemp(prefix="vv-rsc-refusal-")
        self.addCleanup(shutil.rmtree, self.work, True)

    def artifacts_gate(self, *paths, declared=DECLARED):
        artifacts = os.path.join(self.work, "qae-artifacts")
        shutil.rmtree(artifacts, True)
        write(artifacts, {
            "qae/AC1.md": "- step 1: opened /customers/9 -> not found\n", "qae/AC1-step-1.png": "png",
            "console-1.log": "".join("[  9%02dms] [ERROR] Failed to load resource: the server responded with a status of 404 () @ %s%s:0\n"
                                     % (n, SITE, path) for n, path in enumerate(paths)),
            "session-1/session.md": session_with(CLEAN_REQUESTS + ["[GET] %s%s => [404] Not Found" % (SITE, path) for path in paths])})
        write(self.work, {"pr-body.md": declared, "site-url": SITE + "\n"})
        return gate("qae-artifacts", self.repo, "--artifacts", artifacts, "--criteria", os.path.join(self.work, "pr-body.md"),
                    "--site-file", os.path.join(self.work, "site-url"))

    def test_the_rsc_fetch_of_a_declared_page_is_expected(self):
        result = self.artifacts_gate("/customers/9", "/customers/9?_rsc=j5eJ_0D-TpGjlyyb")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_only_rsc_is_dropped_beside_a_declared_query(self):
        declared = DECLARED.replace("/customers/{id})", "/customers/{id}?tab=a)")
        result = self.artifacts_gate("/customers/9?tab=a&_rsc=x1", "/customers/9?_rsc=x1&tab=a", declared=declared)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_another_path_or_another_query_is_still_not_expected(self):
        paths = ("/orders/9?_rsc=j5eJ_0D-TpGjlyyb", "/customers/9?tab=a", "/customers/9?tab=a&_rsc=x1")
        result = self.artifacts_gate(*paths)
        self.assertEqual(result.returncode, 1)
        for path in paths:
            self.assertIn("request answered 404 outside the allowlist: [GET] %s%s" % (SITE, path), result.stdout)
            self.assertIn("status of 404 () @ %s%s:0" % (SITE, path), result.stdout)


if __name__ == "__main__":
    unittest.main()
