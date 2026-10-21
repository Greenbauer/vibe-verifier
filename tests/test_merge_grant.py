"""`bin/vibe-verifier merge-grant`: the default policy's categories, exclusions and content checks, a
policy's grants and scopes, its validation, and the command's reading of a pull request through gh."""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from helpers import ROOT

sys.path.insert(0, str(ROOT / "gates"))
import _merge_grant  # noqa: E402
import _merge_grant_cli  # noqa: E402

DEFAULT = _merge_grant.load_policy()
WF = ".github/workflows/ci.yml"
WF_BASE = """name: ci
on: [pull_request]
permissions:
  contents: read
jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@abc # v4
      - run: npm test"""


def decide(paths, base=None, head=None, policy=DEFAULT, repo="o/r", title="", renamed=(), unreadable=()):
    """The decision for a pull request changing `paths` (plus `renamed` (old, new) pairs). `base` and `head`
    map a path to its text on that side (absent: not there); a path in `unreadable` cannot be read."""
    base, head = base or {}, head or {}
    rows = [("modified", path, path) for path in paths] + [("renamed", old, new) for old, new in renamed]

    def read(path, side):
        if path in unreadable:
            raise _merge_grant.Unreadable("%s could not be read" % path)
        return (base if side == "base" else head).get(path)
    return _merge_grant.decide(policy, repo, files=lambda: rows, title=lambda: title, read=read)


def policy(**raw):
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as handle:
        json.dump({"version": 1, **raw}, handle)
    try:
        return _merge_grant.load_policy(handle.name)
    finally:
        os.unlink(handle.name)


class DefaultCategories(unittest.TestCase):
    def assert_eligible(self, paths, **kwargs):
        grant, why = decide(paths, **kwargs)
        self.assertEqual(grant, "non-product grant", why)

    def assert_not(self, paths, **kwargs):
        grant, why = decide(paths, **kwargs)
        self.assertIsNone(grant)
        return why

    def test_each_category_alone_and_mixed(self):
        for paths in (["WORKFLOW.md"], ["Dockerfile"], ["pnpm-lock.yaml"], ["go.mod", "go.sum", "requirements-dev.txt"],
                      ["tests/unit/cart.test.ts"], [".github/workflows/vibe-verifier.yml", ".vibe-verifier"],
                      [".gitleaks.toml", "zizmor.yml", "osv-scanner.toml", "scripts/ci/semgrep/osv-scanner.toml"],
                      ["apps/web/tsconfig.build.json", ".prettierrc", "compose.yaml", ".nvmrc", "eslint.config.mjs"],
                      ["playwright.preview.config.ts", "e2e/checkout.spec.ts", "src/__tests__/cart.ts",
                       "packages/web/tests/fixtures/orders.json"],
                      ["docs/plan.md", "tests/unit/cart.test.ts", "Dockerfile", WF, "pnpm-lock.yaml"]):
            self.assert_eligible(paths)

    def test_product_and_left_out_files(self):
        self.assertIn("src/cart.ts", self.assert_not(["docs/plan.md", "Dockerfile", "src/cart.ts"]))
        for path in (".npmrc", "next.config.mjs", "pyproject.toml", "Gemfile"):
            self.assert_not([path])

    def test_a_rename_is_judged_at_its_old_path(self):
        self.assert_not([], renamed=[("src/session.ts", "docs/session.md")])
        self.assert_not([], renamed=[("tests/auth.test.ts", "tests/login.test.ts")])

    def test_exclusions(self):
        for path in ("tests/fixtures/migrations/0001.json", "tests/fixtures/seed.sql", "supabase/functions/tsconfig.json",
                     "tests/auth/login.spec.ts", "e2e/rls.spec.ts", "tests/checkRls.test.ts", "tests/middleware.test.ts",
                     ".github/workflows/secret-scan.yml", ".github/CODEOWNERS", "CODEOWNERS", "docs/CODEOWNERS"):
            self.assert_not([path])
        self.assert_eligible(["tests/test_urls.py"])
        self.assert_eligible(["docs/auth.md", "docs/security/rls-policies.md"])

    def test_package_json_dependency_keys_only(self):
        pkg = '{"name":"app","scripts":{"build":"next build"},"dependencies":{"next":"15.0.0"}}'
        deps = ('{"name":"app","scripts":{"build":"next build"},"dependencies":{"next":"15.1.0"},'
                '"devDependencies":{"vitest":"3.0.0"},"engines":{"node":">=22"},"packageManager":"pnpm@9.1.0"}')
        script = '{"name":"app","scripts":{"build":"next build","postinstall":"node x.js"},"dependencies":{"next":"15.1.0"}}'
        self.assert_eligible(["package.json", "pnpm-lock.yaml"], base={"package.json": pkg}, head={"package.json": deps})
        self.assert_eligible(["packages/web/package.json"], base={"packages/web/package.json": pkg},
                             head={"packages/web/package.json": deps})
        self.assertIn("more than", self.assert_not(["package.json"], base={"package.json": pkg}, head={"package.json": script}))
        self.assert_not(["package.json"], head={"package.json": pkg})          # a new package with scripts
        self.assert_not(["package.json"], base={"package.json": pkg}, head={"package.json": "not json"})
        self.assert_not([], renamed=[("package.json", "docs/package.md")], base={"package.json": pkg},
                        head={"docs/package.md": "notes"})

    def test_a_content_condition_holds_even_where_another_category_takes_the_path(self):
        # e2e/package.json is a test path, and its scripts still run on install; tests/vercel.json is too.
        self.assert_not(["e2e/package.json"], base={"e2e/package.json": '{"scripts":{"a":"x"}}'},
                        head={"e2e/package.json": '{"scripts":{"a":"curl x | sh"}}'})
        self.assert_not(["tests/vercel.json"], base={"tests/vercel.json": '{"headers":[1]}'},
                        head={"tests/vercel.json": '{"headers":[]}'})
        self.assert_eligible(["e2e/package.json"], base={"e2e/package.json": '{"devDependencies":{"a":"1"}}'},
                             head={"e2e/package.json": '{"devDependencies":{"a":"2"}}'})

    def test_markdown_is_exempt_only_by_its_lowercase_extension(self):
        self.assert_not([".github/SECURITY.MD"])
        self.assert_eligible([".github/SECURITY.md", "src/notes.md", WF], base={WF: WF_BASE}, head={WF: WF_BASE + "\n      - run: x"})

    def test_pnpm_overrides_only(self):
        def pkg(value):
            return {"package.json": json.dumps(value)}
        built = ["esbuild"]
        self.assert_eligible(["package.json"], base=pkg({"pnpm": {"onlyBuiltDependencies": built, "overrides": {"a": "1"}}}),
                             head=pkg({"pnpm": {"onlyBuiltDependencies": built, "overrides": {"a": "2", "b": "1"}}}))
        self.assert_eligible(["package.json"], base=pkg({"name": "app"}),
                             head=pkg({"name": "app", "pnpm": {"overrides": {"a": "2"}}}))
        self.assert_not(["package.json"], base=pkg({"pnpm": {"onlyBuiltDependencies": built}}),
                        head=pkg({"pnpm": {"onlyBuiltDependencies": built + ["sharp"]}}))
        self.assert_not(["package.json"], base=pkg({"pnpm": "x", "dependencies": {"a": "1"}}),
                        head=pkg({"pnpm": "x", "dependencies": {"a": "2"}}))

    def test_unreadable_is_a_no(self):
        deps = {"package.json": '{"dependencies":{"next":"15.0.0"}}'}
        self.assertIn("could not be read", self.assert_not(["package.json"], base=deps, head=deps, unreadable=["package.json"]))
        grant, why = _merge_grant.decide(DEFAULT, "o/r", files=self._unreadable, title=lambda: "", read=None)
        self.assertIsNone(grant)
        self.assertIn("listed", why)

    @staticmethod
    def _unreadable():
        raise _merge_grant.Unreadable("GitHub listed 2 of its 3 changed files")

    def test_vercel_headers_unchanged(self):
        frame = [{"source": "/(.*)", "headers": [{"key": "X-Frame-Options", "value": "DENY"}]}]
        self.assert_eligible(["vercel.json"], base={"vercel.json": json.dumps({"cleanUrls": False, "headers": frame})},
                             head={"vercel.json": json.dumps({"cleanUrls": True, "headers": frame})})
        self.assertIn("headers", self.assert_not(["vercel.json"], base={"vercel.json": json.dumps({"headers": frame})},
                                                 head={"vercel.json": '{"headers":[]}'}))


class Grants(unittest.TestCase):
    POLICY = {"extends": "default", "grants": [
        {"name": "whitelist", "repos": ["acme/tools", "acme/infra-*"], "exclusions": ["codeowners"]},
        {"name": "title grant", "repos": ["acme/app"], "title": "^(fix|ci|test|docs)(\\([^)]*\\))?: ",
         "exclusions": ["database", "codeowners", "security-names", "rls", "workflow-security"]},
        {"name": "non-product grant", "categories": ["ci", "docs", "build", "dependencies", "tests"],
         "exclusions": ["database", "codeowners", "security-names", "rls", "workflow-security"]}]}

    def setUp(self):
        self.policy = policy(**self.POLICY)

    def run_grant(self, paths, repo, title="", base=None, head=None):
        return decide(paths, repo=repo, title=title, policy=self.policy, base=base, head=head)[0]

    def test_whitelist_reads_nothing_and_matches_patterns_case_blind(self):
        grant, _ = _merge_grant.decide(self.policy, "Acme/Infra-Dns", files=lambda: [("modified", "src/a.ts", "src/a.ts")],
                                       title=self.fail, read=self.fail)
        self.assertEqual(grant, "whitelist")
        self.assertIsNone(self.run_grant([".github/CODEOWNERS"], "acme/tools"))

    def test_title_grant_allows_product_code_not_exclusions(self):
        self.assertEqual(self.run_grant(["apps/api/src/routes.ts", "e2e/fixtures.ts"], "acme/app", "fix(e2e): races"), "title grant")
        self.assertIsNone(self.run_grant(["apps/web/src/gallery.ts"], "acme/app", "feat(web): gallery"))
        self.assertIsNone(self.run_grant(["apps/web/src/a.ts"], "acme/app", "fixup: something"))
        self.assertIsNone(self.run_grant(["apps/api/src/a.ts"], "acme/other", "fix(api): x"))
        self.assertIsNone(self.run_grant(["apps/api/src/trips.ts", "supabase/migrations/0042_trips.sql"], "acme/app", "fix(db): trips"))
        self.assertIsNone(self.run_grant(["apps/api/src/auth/session.ts"], "acme/app", "fix(api): session"))
        for owners in (".github/CODEOWNERS", "CODEOWNERS", "docs/CODEOWNERS"):
            self.assertIsNone(self.run_grant([owners], "acme/app", "chore: owners"))
        self.assertEqual(self.run_grant(["package.json", "apps/api/src/a.ts"], "acme/app", "fix(api): scripts",
                                        base={"package.json": '{"scripts":{"a":"x"}}'}, head={"package.json": '{"scripts":{"a":"y"}}'}),
                         "title grant")
        self.assertIsNone(self.run_grant(["apps/api/src/migrations/run.ts"], "acme/app", "fix(api): migrations runner"))
        self.assertEqual(self.run_grant([WF, "apps/web/src/a.ts"], "acme/app", "ci: lint", base={WF: "jobs: {}"},
                                        head={WF: "permissions:\n  contents: read\njobs: {}"}), "title grant")
        self.assertIsNone(self.run_grant([WF], "acme/app", "ci: deploy", base={WF: "jobs: {}"},
                                         head={WF: "permissions:\n  contents: write\njobs: {}"}))

    def test_grants_compose(self):
        self.assertEqual(self.run_grant(["docs/onboarding.md", "e2e/onboarding.spec.ts"], "acme/app", "feat(docs): guide"),
                         "non-product grant")
        self.assertIsNone(self.run_grant(["docs/trips.md", "e2e/fixtures/trips.sql"], "acme/app", "feat(e2e): seed"))

    def test_reasons_name_each_grant_in_scope(self):
        grant, why = decide(["src/a.ts"], repo="acme/app", title="fix: x", policy=self.policy,
                            renamed=[("src/auth.ts", "src/login.ts")])
        self.assertIsNone(grant)
        self.assertIn("title grant: it touches an auth", why)
        self.assertIn("non-product grant: it changes files outside", why)
        self.assertIn("no grant covers", decide(["src/a.ts"], policy=policy(grants=[{"name": "w", "repos": ["x/y"]}]))[1])

    def test_a_team_can_replace_and_add_categories(self):
        custom = policy(extends="default", categories={"docs": [{"path": "^docs/"}], "scripts": [{"path": "^scripts/"}]},
                        grants=[{"name": "docs and scripts", "categories": ["docs", "scripts"]}])
        self.assertEqual(decide(["docs/a.txt", "scripts/x.sh"], policy=custom)[0], "docs and scripts")
        self.assertIsNone(decide(["README.md"], policy=custom)[0])


class NestedKeys(unittest.TestCase):
    def test_json_unchanged_reads_a_dotted_key_as_nested(self):
        frozen = policy(extends="default", categories={"deps": [{"path": "(^|/)package\\.json$", "json_unchanged": ["pnpm.overrides"]}]},
                        grants=[{"name": "deps", "categories": ["deps"]}])
        def pkg(value):
            return {"package.json": json.dumps(value)}
        self.assertIsNone(decide(["package.json"], policy=frozen, base=pkg({"pnpm": {"overrides": {"a": "1"}}}),
                                 head=pkg({"pnpm": {"overrides": {"a": "2"}}}))[0])
        self.assertEqual(decide(["package.json"], policy=frozen, base=pkg({"pnpm": {"overrides": {"a": "1"}}, "x": 1}),
                                head=pkg({"pnpm": {"overrides": {"a": "1"}}, "x": 2}))[0], "deps")


class PolicyValidation(unittest.TestCase):
    def test_rejects(self):
        for raw in ({"version": 2, "grants": [{"name": "a"}]},
                    {"version": 1, "grants": []},
                    {"version": 1, "extends": "other", "grants": [{"name": "a"}]},
                    {"version": 1, "grants": [{"name": "a", "categories": ["nope"]}]},
                    {"version": 1, "grants": [{"name": "a", "exclusions": ["nope"]}]},
                    {"version": 1, "grants": [{"name": "a", "repos": ["no-slash"]}]},
                    {"version": 1, "grants": [{"name": "a", "title": "("}]},
                    {"version": 1, "grants": [{"name": "a", "surprise": 1}]},
                    {"version": 1, "categories": {"c": [{"path": "x", "json_changes_only": "dependencies"}]}, "grants": [{"name": "a"}]},
                    {"version": 1, "exclusions": {"e": {"path": "x"}}, "grants": [{"name": "a"}]}):
            with self.subTest(raw=raw), tempfile.NamedTemporaryFile("w", suffix=".json") as handle:
                json.dump(raw, handle)
                handle.flush()
                with self.assertRaises(_merge_grant.PolicyError):
                    _merge_grant.load_policy(handle.name)


def done(stdout="", code=0, stderr=""):
    return subprocess.CompletedProcess([], code, stdout, stderr)


class Command(unittest.TestCase):
    """The command reads the pull request through gh: its view, the files API (counted against
    changedFiles, renames with their old path), and each file's text at the base and the head."""

    def answer(self, args):
        joined = " ".join(args)
        if args[:2] == ["pr", "view"]:
            return done('{"baseRefOid":"b1","headRefOid":"h1","changedFiles":%d}' % self.count)
        if "/pulls/7/files" in joined:
            old = (lambda name: "src/old.ts") if self.renamed else (lambda name: name)
            status = "renamed" if self.renamed else "modified"
            return done("".join("%s\t%s\t%s\n" % (status, old(name), name) for name in self.names))
        if "/contents/" in joined:
            self.reads.append(args[1])
            return done('{"dependencies":{"a":"2"}}' if "ref=h1" in joined else '{"dependencies":{"a":"1"}}')
        return done(code=1, stderr="unexpected")

    def run_command(self, names, count=None, renamed=False):
        self.names, self.count, self.renamed, self.reads = names, len(names) if count is None else count, renamed, []
        args = mock.Mock(repo="o/r", pr="7", policy=None, gh="gh")
        with mock.patch.object(_merge_grant_cli, "_gh", side_effect=lambda gh, *a: self.answer(list(a))), \
                mock.patch("builtins.print") as printed:
            code = _merge_grant_cli.command(args)
        return code, printed.call_args[0][0]

    def test_eligible_and_not(self):
        self.assertEqual(self.run_command(["docs/a.md", "package.json"]), (0, "eligible: non-product grant"))
        self.assertEqual(self.reads, ["repos/o/r/contents/package.json?ref=b1", "repos/o/r/contents/package.json?ref=h1"])
        code, line = self.run_command(["src/a.ts"])
        self.assertEqual(code, 1)
        self.assertTrue(line.startswith("not eligible: non-product grant: it changes files outside"))

    def test_short_list_and_rename(self):
        self.assertEqual(self.run_command(["docs/a.md"], count=2)[0], 1)
        code, line = self.run_command(["docs/new.md"], renamed=True)
        self.assertEqual(code, 1)
        self.assertIn("src/old.ts", line)

    def test_a_binary_file_is_read_not_a_crash(self):
        def answer(gh, *args):
            if "/contents/" in " ".join(args):
                return subprocess.CompletedProcess([], 0, b"\x89PNG\xff\xfe".decode("utf-8", "replace"), "")
            return self.answer(list(args))
        self.names, self.count, self.renamed, self.reads = [".github/logo.png"], 1, False, []
        with mock.patch.object(_merge_grant_cli.subprocess, "run",
                               return_value=subprocess.CompletedProcess([], 0, b"\x89PNG\xff", b"")):
            self.assertEqual(_merge_grant_cli._gh("gh", "api", "x").stdout, "\ufffdPNG\ufffd")
        args = mock.Mock(repo="o/r", pr="7", policy=None, gh="gh")
        with mock.patch.object(_merge_grant_cli, "_gh", side_effect=answer), mock.patch("builtins.print") as printed:
            self.assertEqual(_merge_grant_cli.command(args), 0)
        self.assertEqual(printed.call_args[0][0], "eligible: non-product grant")

    def test_cli_rejects_a_bad_policy(self):
        with tempfile.NamedTemporaryFile("w", suffix=".json") as handle:
            handle.write("{}")
            handle.flush()
            result = subprocess.run([sys.executable, str(ROOT / "bin" / "vibe-verifier"), "merge-grant", "--repo", "o/r",
                                     "--pr", "1", "--policy", handle.name], capture_output=True, text=True)
        self.assertEqual(result.returncode, 2)
        self.assertIn("cannot run: policy version must be 1", result.stdout)


if __name__ == "__main__":
    unittest.main()
