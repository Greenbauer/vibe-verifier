"""qae-artifacts with the feature map on: a re-walked feature's Verify step declares the refusal it
walks into (docs/feature-map.md, "Expected refusals in a re-walk"), driven through the gate's command
line against a repository with a base branch and a pull request branch."""
import os
import shutil
import tempfile
import unittest

from helpers import commit, gate, git, make_repo, write
from test_qae_artifacts import session_with

PREVIEW = "https://site-git-fix-auth-gate.vercel.app"
SUPABASE = "https://abcdefghijklmnop.supabase.co"  # the API host this preview's branch got: no file can name it


def feature_file(title, glob, *steps, gotchas="- none"):
    return ("# %s\n\n## Surfaces\n\n- source: `%s`\n\n## Reach\n\nOpen it.\n\n## Verify\n\n%s\n\n## Gotchas\n\n%s\n"
            % (title, glob, "\n".join("%d. %s" % (n, step) for n, step in enumerate(steps, 1)), gotchas))


SIGN_IN = feature_file("Sign-in", "app/auth/**", "Signed out, open `/login`: the form shows.",
                       "Sign in with a wrong password: the form says so (expected-refusal: 400 /auth/v1/token?grant_type=password).")
AGENT_API = feature_file("Agent API", "app/api/**", "Signed out, GET `/api/v1/orders` is refused (expected-refusal: 401 /api/v1/orders).")
# Two of the three refusals a consumer's re-walk walked by design on 2026-10-09, each of which failed the run.
REFUSED = [("POST", SUPABASE + "/auth/v1/token?grant_type=password", "400"), ("GET", PREVIEW + "/api/v1/orders", "401")]


class FeatureStepRefusals(unittest.TestCase):
    """A re-walked feature's Verify step declares the refusal it walks into, as a criterion does."""

    def setUp(self):
        self.repo = make_repo(self, {"app/auth/login.ts": "1\n", "app/api/orders.ts": "1\n", "docs/features/README.md": "# Map\n",
                                     "docs/features/sign-in.md": SIGN_IN, "docs/features/agent-api.md": AGENT_API})
        git(self.repo, "checkout", "-q", "-b", "feat")
        self.root = tempfile.mkdtemp(prefix="vv-qae-")
        self.inputs = tempfile.mkdtemp(prefix="vv-inputs-")
        self.addCleanup(shutil.rmtree, self.root, True)
        self.addCleanup(shutil.rmtree, self.inputs, True)
        write(self.inputs, {"site-url": "%s\n%s\n" % (PREVIEW, SUPABASE),
                            "body.md": "## Acceptance criteria\n\n- The orders page lists orders\n"})

    def walk(self, changed, requests, *args, files=None):
        """The gate over a run whose pull request changed `changed` and whose browser saw `requests`
        ((method, URL, status)), each logged by Chromium too, with the feature map on."""
        commit(self.repo, files or {path: "2\n" for path in changed})
        write(self.inputs, {"changed-files": "".join(path + "\n" for path in changed)})
        write(self.root, {
            "qae/AC1.md": "- step 1: opened /orders -> saw the list\n", "qae/AC1-step-1.png": "png",
            "console-1.log": "".join("[  9%02dms] [ERROR] Failed to load resource: the server responded with a status of %s () @ %s:0\n"
                                     % (n, status, url) for n, (_, url, status) in enumerate(requests)),
            "session-1/session.md": session_with(["[GET] %s/orders => [200] OK" % PREVIEW]
                                                 + ["[%s] %s => [%s] Error" % request for request in requests])})
        return gate("qae-artifacts", self.repo, "--artifacts", self.root, "--criteria", os.path.join(self.inputs, "body.md"),
                    "--site-file", os.path.join(self.inputs, "site-url"), *args)

    def features(self):
        return ("--features", "docs/features", "--changed-files", os.path.join(self.inputs, "changed-files"))

    def test_a_selected_features_step_declares_its_refusal_on_the_site_and_on_a_further_origin(self):
        changed = ["app/auth/login.ts", "app/api/orders.ts"]
        result = self.walk(changed, REFUSED, *self.features())
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_without_the_feature_map_the_same_run_fails_as_before(self):
        result = self.walk(["app/auth/login.ts", "app/api/orders.ts"], REFUSED)
        self.assertEqual(result.returncode, 1)
        self.assertIn("request answered 400 outside the allowlist: [POST] %s/auth/v1/token" % SUPABASE, result.stdout)
        self.assertIn("request answered 401 outside the allowlist: [GET] %s/api/v1/orders" % PREVIEW, result.stdout)

    def test_a_feature_the_re_walk_did_not_select_declares_nothing(self):
        # Only sign-in's files changed: agent-api was not re-walked, so its 401 is unexplained.
        result = self.walk(["app/auth/login.ts"], REFUSED, *self.features())
        self.assertEqual(result.returncode, 1)
        self.assertIn("request answered 401 outside the allowlist: [GET] %s/api/v1/orders" % PREVIEW, result.stdout)
        self.assertIn("status of 401 () @ %s/api/v1/orders:0" % PREVIEW, result.stdout)
        self.assertNotIn("auth/v1/token", result.stdout)

    def test_only_a_steps_own_line_outside_a_comment_declares(self):
        hidden = feature_file("Sign-in", "app/auth/**", "Sign in with a wrong password <!-- expected-refusal: 400 /auth/v1/token?grant_type=password -->",
                              gotchas="- expected-refusal: 400 /auth/v1/token?grant_type=password")
        result = self.walk(["app/auth/login.ts"], REFUSED[:1], *self.features(),
                           files={"app/auth/login.ts": "2\n", "docs/features/sign-in.md": hidden})
        self.assertEqual(result.returncode, 1)
        self.assertIn("request answered 400 outside the allowlist: [POST] %s/auth/v1/token" % SUPABASE, result.stdout)

    def test_a_step_declaring_an_unhandled_status_or_another_url_is_still_judged(self):
        broken = feature_file("Sign-in", "app/auth/**", "Sign in: the server errors (expected-refusal: 500 /auth/v1/token?grant_type=password).")
        result = self.walk(["app/auth/login.ts"], [("POST", SUPABASE + "/auth/v1/token?grant_type=password", "500"),
                                                   ("POST", SUPABASE + "/auth/v1/token?grant_type=refresh_token", "400")],
                           *self.features(), files={"app/auth/login.ts": "2\n", "docs/features/sign-in.md": broken})
        self.assertEqual(result.returncode, 1)
        self.assertIn("docs/features/sign-in.md step 1 declares expected-refusal 500: only a handled refusal", result.stdout)
        self.assertIn("request answered 500 outside the allowlist", result.stdout)
        self.assertIn("grant_type=refresh_token", result.stdout)

    def test_the_pull_requests_own_copy_of_a_step_is_read(self):
        # The explorer walks the pull request's copy, so a step the pull request adds declares for its run.
        added = SIGN_IN.replace("## Gotchas", "3. Open a missing file: it says so (expected-refusal: 404 /api/v1/files/download?path=none.png).\n\n## Gotchas")
        missing = ("GET", PREVIEW + "/api/v1/files/download?path=none.png", "404")
        result = self.walk(["app/auth/login.ts"], [missing], *self.features(),
                           files={"app/auth/login.ts": "2\n", "docs/features/sign-in.md": added})
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)



if __name__ == "__main__":
    unittest.main()
