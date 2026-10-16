"""The suite-scope engine and its action's step, through their command lines, against a stand-in `gh`
that serves one pull request, its files and the inert list at each ref from a JSON file, and
records every call. Each way the decision could wrongly say "not applicable" has a test that makes
it say "run"."""
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from helpers import ROOT, clean_env

ENGINE = ROOT / "actions" / "suite-scope" / "suite_scope.py"
ACTION = ROOT / "actions" / "suite-scope" / "action.yml"
REPO, HEAD, BASE = "acme/app", "a" * 40, "b" * 40
LIST = ".github/suite-inert"
# The only ref the engine may read the list at: the default branch by its full name, never a tag of that name.
MAIN = "refs/heads/main"
WHERE = "%s on main" % LIST

# Serves $FAKE/state.json: "pull" is the pull request's record, "files" its files (as GitHub lists
# them, a hundred to a page, "listed" of them when GitHub cuts the list short) and "lists" the inert
# list's text by ref. "fail" fails every read whose endpoint holds one of its strings, "garbage" answers
# it with a page that is not JSON, "answers" with the JSON given for it, and
# "refuse_writes" every write. Every call's argv goes to $FAKE/argv, every write to $FAKE/writes. A read
# of the list that does not ask for the raw file is refused, so no test passes on JSON read as patterns.
FAKE_GH = '''#!/usr/bin/env python3
import json, os, re, sys
root, argv = os.environ["FAKE"], sys.argv[1:]
state = json.load(open(os.path.join(root, "state.json")))
with open(os.path.join(root, "argv"), "a") as handle:
    handle.write(json.dumps(argv) + "\\n")
endpoint = argv[1]
if "-X" in argv:
    if state.get("refuse_writes"):
        sys.stderr.write("gh: Resource not accessible by integration (HTTP 403)\\n")
        sys.exit(1)
    with open(os.path.join(root, "writes"), "a") as handle:
        handle.write(json.dumps([endpoint, json.load(sys.stdin)]) + "\\n")
    sys.exit(0)
if any(part in endpoint for part in state.get("fail", [])):
    sys.stderr.write("gh: Bad Gateway (HTTP 502)\\n")
    sys.exit(1)
if any(part in endpoint for part in state.get("garbage", [])):
    sys.stdout.write("<html>Bad gateway</html>\\n")
    sys.exit(0)
for part, answer in state.get("answers", {}).items():
    if part in endpoint:
        print(json.dumps(answer))
        sys.exit(0)
if "/contents/" in endpoint:
    if "Accept: application/vnd.github.raw" not in argv:
        sys.stderr.write("the list was not asked for as a raw file\\n")
        sys.exit(9)
    text = state.get("lists", {}).get(endpoint.split("/contents/", 1)[1])
    if text is None:
        sys.stderr.write("gh: Not Found (HTTP 404)\\n")
        sys.exit(1)
    sys.stdout.write(text)
elif "/files?" in endpoint:
    per, page = (int(re.search(r"[?&]" + name + r"=(\\d+)", endpoint).group(1)) for name in ("per_page", "page"))
    files = state["files"][:state.get("listed", len(state["files"]))]
    print(json.dumps(files[(page - 1) * per:page * per]))
else:
    print(json.dumps(state["pull"]))
'''

# A list in the shape a consumer writes: each entry with its reason, the exceptions as `!` lines.
INERT = """# What a pull request can change without changing what the browser suite verifies.
docs/**                            # documentation no build ships
*.md                               # the top-level README
.github/**                         # other workflows and repository settings
!.github/workflows/preview.yml     # the pipeline that deploys the preview and runs the suite
tests/**                           # CI self-tests no spec imports
'.vibe-verifier*'                  # gate manifests
"""
# No `!**/e2e/**` line: a path under a directory named e2e is never inert whatever the list says.
INERT_PATHS = ("docs/guide.md", "docs/parity/extract.txt", "README.md", ".github/workflows/ci.yml", ".github/CODEOWNERS",
               "tests/unit/test_pipeline.py", ".vibe-verifier-qae",
               # "e2e" in a name, not a directory.
               "tests/e2e-lane-coverage.test.mjs", "docs/operations/preview-e2e.md")
ACTIVE_PATHS = ("src/app.ts", "package.json", "infra/README.md", "scripts/deploy.sh",
                # Near misses of the list's own entries: `*` stays inside one path segment.
                "docs", "docsite/guide.md", "nested/NOTES.md", ".github", ".vibe-verifier-rules/tests/a.yml",
                # The `!` line wins over the line that matches.
                ".github/workflows/preview.yml")
# A segment named e2e at any depth, inert by the list's own lines or not: Playwright finds a spec under one. The
# last two are the directory itself, a submodule or a link, which GitHub lists as one path.
E2E_PATHS = ("e2e/fixtures.ts", "tests/e2e/new.spec.ts", "docs/examples/e2e/helper.ts", "apps/web/e2e/README.md",
             ".github/e2e/setup.ts", "e2e", "docs/e2e")


def files(*names):
    return [{"filename": name} for name in names]


class SuiteScope(unittest.TestCase):
    def setUp(self):
        self.fake = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.fake, True)
        bin_dir = self.fake / "bin"
        bin_dir.mkdir()
        gh = bin_dir / "gh"
        gh.write_text(FAKE_GH)
        gh.chmod(gh.stat().st_mode | stat.S_IEXEC)
        self.output = self.fake / "output"
        self.env = clean_env({"FAKE": str(self.fake), "PATH": "%s:%s" % (bin_dir, os.environ["PATH"]),
                              "GITHUB_OUTPUT": str(self.output)})

    def serve(self, changed, base_ref="main", head=HEAD, lists=None, **state):
        """One open pull request into `base_ref` whose diff is `changed`, with the inert list on main."""
        pull = {"number": 7, "head": {"sha": head}, "changed_files": len(changed),
                "base": {"ref": base_ref, "sha": BASE, "repo": {"default_branch": "main"}}}
        pull.update(state.pop("pull", {}))
        lists = {MAIN: INERT} if lists is None else lists
        state.update(pull=pull, files=changed, lists={"%s?ref=%s" % (LIST, ref): text for ref, text in lists.items()})
        (self.fake / "state.json").write_text(json.dumps(state))
        for name in ("argv", "writes"):
            (self.fake / name).unlink(missing_ok=True)

    def engine(self, *args):
        return subprocess.run([sys.executable, str(ENGINE), *args], capture_output=True, text=True, env=self.env)

    def decide(self, *args, pr="7", sha=HEAD):
        """(not-applicable, reason, stdout) of a decision, read from the step outputs it wrote."""
        self.output.unlink(missing_ok=True)
        result = self.engine("decide", "--repo", REPO, "--list", LIST, "--pr", pr, "--sha", sha, *args)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        outputs = dict(line.split("=", 1) for line in self.output.read_text().splitlines())
        self.assertEqual(sorted(outputs), ["not-applicable", "reason"])
        return outputs["not-applicable"], outputs["reason"], result.stdout

    def calls(self):
        path = self.fake / "argv"
        return [json.loads(line)[1] for line in path.read_text().splitlines()] if path.exists() else []

    def writes(self):
        path = self.fake / "writes"
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def assert_runs(self, reason, warned=False, **decide):
        verdict, said, stdout = self.decide(**decide)
        self.assertEqual((verdict, said), ("false", reason))
        self.assertEqual(stdout, "::%s title=Suite scope::The suite runs: %s\n" % ("warning" if warned else "notice", reason))

    def test_a_diff_that_is_inert_throughout_is_not_applicable_and_says_why(self):
        self.serve(files(*INERT_PATHS))
        verdict, reason, stdout = self.decide()
        said = "Not applicable: every changed path (%d) is on the inert list (%s)" % (len(INERT_PATHS), WHERE)
        self.assertEqual((verdict, reason), ("true", said))
        self.assertLessEqual(len(reason), 140)
        self.assertEqual(stdout, "::notice title=Suite scope::%s\n" % said)
        self.assertEqual(self.calls(), ["repos/acme/app/pulls/7", "repos/acme/app/contents/%s?ref=%s" % (LIST, MAIN),
                                        "repos/acme/app/pulls/7/files?per_page=100&page=1"])

    def test_any_path_off_the_list_runs_the_suite_and_names_it(self):
        for path in ACTIVE_PATHS:
            with self.subTest(path):
                self.serve(files(*INERT_PATHS, path))
                self.assert_runs("%s is not on the inert list (%s)" % (path, WHERE))
        self.serve(files("README.md", "src/app.ts", "package.json", "e2e/a.spec.ts"))
        self.assert_runs("src/app.ts is not on the inert list (%s), and 2 more changed paths are not inert" % WHERE)
        self.serve(files("README.md", "src/app.ts", "package.json"))
        self.assert_runs("src/app.ts is not on the inert list (%s), and 1 more changed path is not inert" % WHERE)

    def test_a_path_under_a_directory_named_e2e_is_never_inert_whatever_the_list_says(self):
        # Playwright matches testMatch at any depth, so a list that forgets `!**/e2e/**` must not skip a new spec.
        for lists in ({MAIN: INERT}, {MAIN: "**\n"}):
            for path in E2E_PATHS:
                with self.subTest(path, lists=lists):
                    self.serve(files("docs/guide.md", path), lists=lists)
                    self.assert_runs("%s has a path segment named e2e, which is never inert" % path)
        # A name that only holds e2e is not a segment named e2e.
        self.serve(files("tests/e2e-lane-coverage.test.mjs", "docs/operations/preview-e2e.md", "E2E/a.md", "docs/e2e.md"),
                   lists={MAIN: "**\n"})
        self.assertEqual(self.decide()[0], "true")
        # check judges a list with the same rule, so a path the suite reads under e2e/ is never flagged inert.
        result = self.check("**\n", "tests/e2e/new.spec.ts")
        self.assertEqual((result.returncode, result.stdout), (0, "1 path the suite reads, 0 on the inert list %s\n" % (self.fake / "list")))

    def test_a_never_line_wins_wherever_it_is_written(self):
        first = "!docs/schema.json\ndocs/**\n"
        self.serve(files("docs/guide.md", "docs/schema.json"), lists={MAIN: first})
        self.assert_runs("docs/schema.json is not on the inert list (%s)" % WHERE)

    def test_both_names_of_a_rename_count(self):
        # Moving a file out of the app removes it there; GitHub reports that as the old name.
        self.serve([{"filename": "docs/old-api.ts", "previous_filename": "src/api/old-api.ts"}])
        self.assert_runs("src/api/old-api.ts is not on the inert list (%s)" % WHERE)
        self.serve([{"filename": "docs/b.md", "previous_filename": "docs/a.md"}])
        self.assertEqual(self.decide()[0], "true")

    def test_a_file_list_github_cut_short_runs_the_suite(self):
        self.serve(files("docs/a.md", "docs/b.md", "src/app.ts"), listed=2)
        self.assert_runs("GitHub listed 2 of the pull request's 3 changed files", warned=True)
        # The pull request's own count is the only proof the list is whole.
        self.serve(files("docs/a.md"), pull={"changed_files": 2})
        self.assert_runs("GitHub listed 1 of the pull request's 2 changed files", warned=True)
        # A file listed twice is one file: two pages read while the list changed would otherwise hide another.
        self.serve(files("docs/b.md", "docs/b.md"))
        self.assert_runs("GitHub listed a changed file twice", warned=True)
        self.serve(files("docs/a.md", "docs/a.md", "docs/b.md"), pull={"changed_files": 2})
        self.assert_runs("GitHub listed 3 of the pull request's 2 changed files", warned=True)

    def test_a_file_list_github_does_not_return_runs_the_suite(self):
        self.serve(files("docs/a.md"), fail=["/files"])
        self.assert_runs("repos/acme/app/pulls/7/files: gh: Bad Gateway (HTTP 502)", warned=True)
        for count in (0, None):
            with self.subTest(count):
                self.serve([], pull={"changed_files": count})
                self.assert_runs("the pull request reports no changed file")
        # A file GitHub counts and does not name is a path nobody judged.
        for nameless in ({"status": "added"}, {"filename": ""}, {"filename": None, "previous_filename": "docs/b.md"}):
            with self.subTest(nameless):
                self.serve([{"filename": "docs/a.md"}, nameless])
                self.assert_runs("GitHub listed a changed file with no name", warned=True)

    def test_every_page_of_the_file_list_is_read(self):
        docs = ["docs/page-%03d.md" % number for number in range(250)]
        self.serve(files(*docs))
        self.assertEqual(self.decide()[0], "true")
        self.assertEqual([call for call in self.calls() if "/files" in call],
                         ["repos/acme/app/pulls/7/files?per_page=100&page=%d" % page for page in (1, 2, 3)])
        self.serve(files(*docs, "src/app.ts"))
        self.assert_runs("src/app.ts is not on the inert list (%s)" % WHERE)

    def test_a_pull_request_too_large_to_list_runs_the_suite_without_listing_it(self):
        self.serve(files("docs/a.md"), pull={"changed_files": 3001})
        self.assert_runs("the pull request changes 3001 files and GitHub lists only 3000 of them")
        self.assertEqual(self.calls(), ["repos/acme/app/pulls/7"])

    def test_no_pull_request_runs_the_suite_and_reads_nothing(self):
        # A push to the default branch, a schedule, a dispatch: the event names no pull request.
        self.serve(files("docs/a.md"))
        self.assert_runs("no pull request is named, and only a pull request's diff can be inert", pr="")
        self.assertEqual(self.calls(), [])

    def test_a_pull_request_into_another_branch_runs_the_suite(self):
        self.serve(files("docs/a.md"), base_ref="feat/base")
        self.assert_runs("the pull request targets feat/base, not the default branch main")

    def test_a_head_that_moved_runs_the_suite(self):
        # The file list GitHub returns is the current head's, so it proves nothing about another commit.
        self.serve(files("docs/a.md"), head="c" * 40)
        self.assert_runs("this run is for aaaaaaaa and the pull request's head is cccccccc")
        self.serve(files("docs/a.md"))
        self.assert_runs("this run is for no commit and the pull request's head is aaaaaaaa", sha="")

    def test_the_list_is_read_on_the_default_branch_so_a_pull_request_cannot_widen_its_own(self):
        self.serve(files("src/app.ts", LIST), lists={MAIN: "docs/**\n", "main": "**\n", HEAD: "**\n", BASE: "**\n"})
        self.assert_runs("src/app.ts is not on the inert list (%s), and 1 more changed path is not inert" % WHERE)
        self.assertEqual([call for call in self.calls() if "/contents/" in call],
                         ["repos/acme/app/contents/%s?ref=%s" % (LIST, MAIN)])

    def test_a_default_branch_with_no_list_runs_the_suite_so_the_pull_request_that_adds_one_is_not_skipped_by_it(self):
        none = "%s names no inert path, so nothing is inert until a list merges" % WHERE
        for lists in ({HEAD: "**\n"}, {MAIN: ""}, {MAIN: "# nothing yet\n!src/**\n"}):
            with self.subTest(lists):
                self.serve(files(LIST), lists=lists)
                self.assert_runs(none)
                self.assertFalse([call for call in self.calls() if "/files" in call])

    def test_a_list_that_cannot_be_read_runs_the_suite_with_a_warning(self):
        for text, said in (("docs/** tests/**\n", "line 1 is not one pattern (quote a path that holds a space): docs/** tests/**"),
                           ("docs/**\n'tests/**\n", "line 2: No closing quotation"),
                           ("docs/**\n!\n", "line 2 is not one pattern (quote a path that holds a space): !"),
                           ("docs/**\n''\n", "line 2 is not one pattern (quote a path that holds a space): ''")):
            with self.subTest(text):
                self.serve(files("docs/a.md"), lists={MAIN: text})
                self.assert_runs("the default branch's inert list %s is malformed (%s)" % (LIST, said), warned=True)
        self.serve(files("docs/a.md"), fail=["/contents/"])
        self.assert_runs("repos/acme/app/contents/%s: gh: Bad Gateway (HTTP 502)" % LIST, warned=True)

    def test_a_pull_request_github_cannot_answer_for_runs_the_suite_with_a_warning(self):
        self.serve(files("docs/a.md"), fail=["pulls/7"])
        self.assert_runs("repos/acme/app/pulls/7: gh: Bad Gateway (HTTP 502)", warned=True)
        self.serve(files("docs/a.md"))
        (self.fake / "state.json").write_text(json.dumps({"pull": {"number": 7}}))
        self.assert_runs("GitHub's answer could not be read (KeyError: 'head')", warned=True)
        (self.fake / "state.json").write_text(json.dumps({"pull": ["not", "a", "record"]}))
        self.assertEqual(self.decide()[0], "false")

    def test_a_gh_that_cannot_run_or_answers_with_no_json_runs_the_suite_with_a_warning(self):
        for where in ("pulls/7", "/files"):
            with self.subTest(where):
                self.serve(files("docs/a.md"), garbage=[where])
                self.assert_runs("GitHub's answer could not be read (JSONDecodeError: Expecting value: line 1 column 1 "
                                 "(char 0))", warned=True)
        # A page of files GitHub answers with an object is not a list of files.
        self.serve(files("docs/a.md"), answers={"/files": {"message": "Not Found"}})
        self.assert_runs("GitHub's answer could not be read (AttributeError: 'str' object has no attribute 'get')",
                         warned=True)
        empty = self.fake / "no-gh"
        empty.mkdir()
        self.env["PATH"] = str(empty)
        self.assert_runs("gh could not be run: [Errno 2] No such file or directory: 'gh'", warned=True)

    def test_a_hash_inside_a_pattern_is_malformed_not_a_comment(self):
        # shlex would end the pattern at the `#`: this `!` line would quietly mean `!docs/`, and a pattern like
        # `docs/*#x` would widen to `docs/*`.
        for listed in ("docs/**\n!docs/#internal/**\n", "docs/*#x\n"):
            with self.subTest(listed):
                self.serve(files("docs/#internal/a.ts"), lists={MAIN: listed})
                line = listed.splitlines()[-1]
                self.assert_runs("the default branch's inert list %s is malformed (line %d has a `#` inside a pattern, "
                                 "which would start a comment there (quote the pattern, or put a space before the "
                                 "comment): %s)" % (LIST, listed.count("\n"), line), warned=True)
                self.assertEqual(self.check(listed, "scripts/deploy.sh").returncode, 2)
        # Quoted, or escaped, it is part of the pattern; a comment after a space is still a comment.
        quoted = "docs/**   # documentation\n'!docs/#internal/**'   # read by the suite\n!docs/\\#raw/**\n"
        for path in ("docs/#internal/a.ts", "docs/#raw/b.ts"):
            with self.subTest(path):
                self.serve(files("docs/guide.md", path), lists={MAIN: quoted})
                self.assert_runs("%s is not on the inert list (%s)" % (path, WHERE))
        self.serve(files("docs/guide.md"), lists={MAIN: quoted})
        self.assertEqual(self.decide()[0], "true")

    def test_a_pattern_that_would_quietly_match_less_is_malformed(self):
        # A gitignore-style `/` and whitespace other than a space would leave a `!` line matching nothing.
        for line in ("!/docs/internal/**", "!docs/internal/", "/docs/**", "docs/"):
            with self.subTest(line):
                self.serve(files("docs/internal/a.ts"), lists={MAIN: "docs/**\n%s\n" % line})
                self.assert_runs("the default branch's inert list %s is malformed (line 2: a pattern starts at the "
                                 "repository root with no leading or trailing `/` (a directory is `dir/**`): %s)"
                                 % (LIST, line), warned=True)
        for line in ("'!docs/internal/**\u00a0'", "'!docs/internal/**\t'"):
            with self.subTest(line):
                self.serve(files("docs/internal/a.ts"), lists={MAIN: "docs/**\n%s\n" % line})
                # The reason is one line, so the whitespace it names reads as a space there.
                self.assert_runs("the default branch's inert list %s is malformed (line 2: a pattern holds whitespace "
                                 "other than a space: %s)" % (LIST, " ".join(line.split())), warned=True)
        # A non-breaking space before a `#` is part of the pattern to shlex, so the `#` is inside it.
        self.serve(files("docs/internal/a.ts"), lists={MAIN: "docs/**\n!docs/internal/**\u00a0# kept\n"})
        self.assertEqual(self.decide()[0], "false")
        self.assertIn("has a `#` inside a pattern", self.decide()[1])
        self.assertEqual(self.check("docs/**\n!docs/internal/\n", "scripts/deploy.sh").returncode, 2)

    def test_a_never_line_covers_everything_under_the_path_it_names(self):
        listed = "docs/**\n!docs/internal\n"
        for path in ("docs/internal", "docs/internal/a.ts", "docs/internal/deep/b.md"):
            with self.subTest(path):
                self.serve(files("docs/guide.md", path), lists={MAIN: listed})
                self.assert_runs("%s is not on the inert list (%s)" % (path, WHERE))
        self.serve(files("docs/guide.md", "docs/internalx.md"), lists={MAIN: listed})
        self.assertEqual(self.decide()[0], "true")

    def test_a_reason_of_the_callers_own_runs_the_suite_and_reads_nothing(self):
        self.serve(files("docs/a.md"))
        verdict, reason, _ = self.decide("--also-run", "the body claims a proof\nonly the suite scores")
        self.assertEqual((verdict, reason), ("false", "the body claims a proof only the suite scores"))
        self.assertEqual(self.calls(), [])
        self.assertEqual(self.decide("--also-run", " \n")[0], "true")

    def test_a_path_with_a_line_break_cannot_write_an_output_of_its_own(self):
        # A file name is the pull request's to choose, and it reaches the reason.
        self.serve(files("docs/a.md", "x\nnot-applicable=true\nz=%0A::error::"))
        _, _, stdout = self.decide()
        said = "x not-applicable=true z=%%0A::error:: is not on the inert list (%s)" % WHERE
        self.assertEqual(self.output.read_text(), "not-applicable=false\nreason=%s\n" % said)
        self.assertEqual(stdout, "::notice title=Suite scope::The suite runs: %s\n" % said.replace("%", "%25"))

    def post(self, *contexts, reason="Not applicable: every changed path (2) is on the inert list (%s)" % WHERE):
        return self.engine("post", "--repo", REPO, "--sha", HEAD, "--interval", "0", "--reason", reason,
                           "--url", "https://github.com/acme/app/actions/runs/1", *contexts)

    def test_post_writes_each_context_as_a_success_status_with_the_reason(self):
        self.serve([])
        result = self.post("preview-e2e", "browser-regressions", reason="Not applicable: " + "x" * 200)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.writes(), [["repos/acme/app/statuses/" + HEAD, {
            "state": "success", "context": context, "description": ("Not applicable: " + "x" * 200)[:140],
            "target_url": "https://github.com/acme/app/actions/runs/1"}] for context in ("preview-e2e", "browser-regressions")])

    def test_a_post_github_refuses_three_times_fails_naming_the_permission(self):
        self.serve([], refuse_writes=True)
        result = self.post("preview-e2e", "browser-regressions")
        self.assertEqual(result.returncode, 1)
        self.assertIn("could not post the `preview-e2e` status", result.stdout)
        self.assertIn("statuses: write", result.stdout)
        self.assertEqual(len(self.calls()), 3)

    def check(self, listed, *reads):
        (self.fake / "list").write_text(listed)
        (self.fake / "reads").write_text("".join(path + "\n" for path in reads))
        return self.engine("check", "--list", str(self.fake / "list"), "--reads", str(self.fake / "reads"))

    def test_check_fails_when_the_list_calls_a_path_the_suite_reads_inert(self):
        result = self.check(INERT, "scripts/deploy.sh", "docs/parity/extract.txt", ".github/workflows/preview.yml")
        self.assertEqual(result.returncode, 1)
        self.assertEqual(len(re.findall("::error ", result.stdout)), 1)
        self.assertIn("docs/parity/extract.txt is on the inert list %s and the suite reads it" % (self.fake / "list"), result.stdout)
        self.assertIn("add `!docs/parity/extract.txt`", result.stdout)
        result = self.check(INERT + "!docs/parity/extract.txt\n", "scripts/deploy.sh", "docs/parity/extract.txt")
        self.assertEqual((result.returncode, result.stdout), (0, "2 paths the suite reads, 0 on the inert list %s\n" % (self.fake / "list")))

    def test_check_cannot_run_on_a_list_or_reads_file_that_is_not_there(self):
        for args in (("--list", str(self.fake / "missing"), "--reads", str(self.fake / "missing-too")),):
            result = self.engine("check", *args)
            self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
            self.assertIn("cannot be checked", result.stdout)
        (self.fake / "list").write_text(INERT)
        result = self.engine("check", "--list", str(self.fake / "list"), "--reads", str(self.fake / "missing"))
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn("cannot be checked", result.stdout)

    def test_check_cannot_run_on_a_malformed_list_or_with_no_path(self):
        result = self.check("docs/** tests/**\n", "scripts/deploy.sh")
        self.assertEqual(result.returncode, 2)
        self.assertIn("line 1 is not one pattern", result.stdout)
        result = self.check(INERT)
        self.assertEqual(result.returncode, 2)
        self.assertIn("names no path, so nothing was checked", result.stdout)

    def action_step(self, **inputs):
        """Run the action's one step as GitHub would, with these inputs as its env."""
        script = re.search(r"      run: \|\n(.*)", ACTION.read_text(), re.S).group(1)
        script = "\n".join(line[8:] for line in script.splitlines())
        env = dict(self.env, GITHUB_ACTION_PATH=str(ACTION.parent), REPO=REPO, LIST=LIST, PR="", SHA="", ALSO_RUN="",
                   POST="", REASON="", READS="", RUN_URL="https://github.com/acme/app/actions/runs/1")
        env.update(inputs)
        self.output.unlink(missing_ok=True)
        result = subprocess.run(["bash", "-c", script], capture_output=True, text=True, env=env)
        return result, self.output.read_text() if self.output.exists() else ""

    def test_each_input_reaches_the_step_under_the_name_its_script_reads(self):
        # action_step hands the script its env by name, so a misspelt input here would pass every test
        # below and decide "run" for every consumer.
        declared, _, rest = ACTION.read_text().partition("\noutputs:\n")
        inputs = re.findall(r"^  ([a-z-]+):\n    description:", declared, re.M)
        env = dict(re.findall(r"^        ([A-Z_]+): \$\{\{ inputs\.([a-z-]+) \}\}$", rest, re.M))
        self.assertEqual(env, {"GH_TOKEN": "token", "LIST": "list", "PR": "pull-request", "SHA": "sha", "ALSO_RUN": "also-run",
                               "POST": "post", "REASON": "reason", "READS": "reads", "REPO": "repository"})
        self.assertEqual(sorted(inputs), sorted(env.values()))
        script = rest[rest.index("      run: |"):]
        self.assertEqual(set(re.findall(r"\$\{?([A-Z_]+)", script)) - {"GITHUB_ACTION_PATH"}, set(env) - {"GH_TOKEN"} | {"RUN_URL"})
        # Strict by default: only a pull request event names a pull request, and its head.
        self.assertIn("  pull-request:\n", declared)
        self.assertEqual(re.findall(r"default: \$\{\{ github\.event\.pull_request\.([a-z_.]+) \}\}", declared), ["number", "head.sha"])

    def test_each_output_is_the_steps_output_under_its_own_name(self):
        # A consumer's job-level `if:` reads these. Wired to the wrong step or name, they would be empty, so no pull
        # request would skip and no post job would run, and every test of the step itself would still pass.
        declared = ACTION.read_text().partition("\noutputs:\n")[2]
        block, _, runs = declared.partition("\nruns:\n")
        self.assertEqual(re.findall(r"^  ([a-z-]+):$", block, re.M), ["not-applicable", "reason"])
        self.assertEqual(re.findall(r"^    value: \$\{\{ steps\.(\S+)\.outputs\.(\S+) \}\}$", block, re.M),
                         [("scope", "not-applicable"), ("scope", "reason")])
        self.assertEqual(re.findall(r"^      id: (\S+)$", runs, re.M), ["scope"])

    def test_the_action_decides_for_the_events_pull_request(self):
        self.serve(files("docs/a.md", "README.md"))
        result, output = self.action_step(PR="7", SHA=HEAD)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(output, "not-applicable=true\nreason=Not applicable: every changed path (2) is on the inert "
                         "list (%s)\n" % WHERE)
        # Any other event names no pull request, and a reason that starts like an option is still a reason.
        self.serve(files("docs/a.md"))
        self.assertEqual(self.action_step()[1].splitlines()[0], "not-applicable=false")
        self.serve(files("docs/a.md"))
        self.assertEqual(self.action_step(PR="7", SHA=HEAD, ALSO_RUN="--proof claimed")[1],
                         "not-applicable=false\nreason=--proof claimed\n")
        # One word that looks like an option still reaches the engine as the reason (`--also-run=`).
        self.assertEqual(self.action_step(PR="7", SHA=HEAD, ALSO_RUN="--proof")[1], "not-applicable=false\nreason=--proof\n")

    def test_the_action_posts_the_contexts_it_is_given(self):
        self.serve([])
        result, _ = self.action_step(POST="preview-e2e\nbrowser-regressions", SHA=HEAD, REASON="-- not applicable")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual([(write[1]["context"], write[1]["description"]) for write in self.writes()],
                         [("preview-e2e", "-- not applicable"), ("browser-regressions", "-- not applicable")])
        result, _ = self.action_step(POST="preview-e2e", REASON="x")
        self.assertEqual(result.returncode, 1)
        self.assertIn("::error::no commit to post on", result.stdout)

    def test_the_action_checks_the_workspaces_list_and_needs_one_named(self):
        (self.fake / "list").write_text(INERT)
        (self.fake / "reads").write_text("docs/guide.md\n")
        result, _ = self.action_step(LIST=str(self.fake / "list"), READS=str(self.fake / "reads"))
        self.assertEqual(result.returncode, 1)
        self.assertIn("docs/guide.md is on the inert list", result.stdout)
        self.assertEqual(result.stdout.count("\n"), 2)
        result, output = self.action_step(LIST="", PR="7", SHA=HEAD)
        self.assertEqual((result.returncode, output), (1, ""))
        self.assertIn("::error::name the inert list", result.stdout)


if __name__ == "__main__":
    unittest.main()
