"""The suite-scope inert list as a list: what each line means, what makes one malformed, and `check`, which
judges a list against the paths a suite reads. Driven through the engine's command line with the stand-in `gh`
of test_suite_scope.py, so a list is read exactly as a decision reads it."""
import re
import unittest

from test_suite_scope import INERT, LIST, MAIN, WHERE, Harness, files


class SuiteScopeList(Harness):
    def test_a_never_line_wins_wherever_it_is_written(self):
        first = "!docs/schema.json\ndocs/**\n"
        self.serve(files("docs/guide.md", "docs/schema.json"), lists={MAIN: first})
        self.assert_runs("docs/schema.json is not on the inert list (%s)" % WHERE)

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
        # Quoted, it is part of the pattern; a comment after a space is still a comment.
        quoted = "docs/**   # documentation\n'!docs/#internal/**'   # read by the suite\n"
        self.serve(files("docs/guide.md", "docs/#internal/a.ts"), lists={MAIN: quoted})
        self.assert_runs("docs/#internal/a.ts is not on the inert list (%s)" % WHERE)
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
        # Gitignore's sets would match only themselves here, and shlex drops a backslash as an escape.
        for line in ("!docs/{internal,private}/**", "![d]ocs/internal/**"):
            with self.subTest(line):
                self.serve(files("docs/internal/a.ts"), lists={MAIN: "docs/**\n%s\n" % line})
                self.assert_runs("the default branch's inert list %s is malformed (line 2: a pattern holds `[`, `]`, "
                                 "`{` or `}`, which match only themselves here, not a set of names: %s)" % (LIST, line),
                                 warned=True)
        for line in ("!docs\\internal", "'!docs\\internal/**'"):
            with self.subTest(line):
                self.serve(files("docs/internal/a.ts"), lists={MAIN: "docs/**\n%s\n" % line})
                self.assert_runs("the default branch's inert list %s is malformed (line 2 has a backslash, which shlex "
                                 "would read as an escape and drop: %s)" % (LIST, line), warned=True)
        # A non-breaking space before a `#` is part of the pattern to shlex, so the `#` is inside it.
        self.serve(files("docs/internal/a.ts"), lists={MAIN: "docs/**\n!docs/internal/**\u00a0# kept\n"})
        self.assertEqual(self.decide()[0], "false")
        self.assertIn("has a `#` inside a pattern", self.decide()[1])
        self.assertEqual(self.check("docs/**\n!docs/internal/\n", "scripts/deploy.sh").returncode, 2)

    def test_a_byte_order_mark_or_a_trailing_line_break_cannot_make_a_path_inert(self):
        # An editor's byte-order mark before a first `!` line would leave it an inert line matching nothing.
        listed = "\ufeff!docs/internal/**\ndocs/**\n"
        self.serve(files("docs/internal/a.ts"), lists={MAIN: listed})
        self.assert_runs("docs/internal/a.ts is not on the inert list (%s)" % WHERE)
        self.assertEqual(self.check(listed, "docs/internal/a.ts").returncode, 0)
        # `$` alone matches before a trailing line break; the whole path has to match.
        self.serve(files("docs/a.md\n"))
        self.assert_runs("docs/a.md is not on the inert list (%s)" % WHERE)

    def test_a_never_line_covers_everything_under_the_path_it_names(self):
        listed = "docs/**\n!docs/internal\n"
        for path in ("docs/internal", "docs/internal/a.ts", "docs/internal/deep/b.md"):
            with self.subTest(path):
                self.serve(files("docs/guide.md", path), lists={MAIN: listed})
                self.assert_runs("%s is not on the inert list (%s)" % (path, WHERE))
        self.serve(files("docs/guide.md", "docs/internalx.md"), lists={MAIN: listed})
        self.assertEqual(self.decide()[0], "true")

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


if __name__ == "__main__":
    unittest.main()
