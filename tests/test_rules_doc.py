"""`bin/vibe-verifier rules-doc` and the repo-rules gate's --doc check, on the real pinned ast-grep: the
generated block lists every enabled rule from the rule files, and a document whose block is missing or
differs from what the rules at HEAD render is a finding."""
import json
import subprocess
import unittest
from pathlib import Path

from helpers import commit, gate, make_repo, runner

BEGIN = ("<!-- BEGIN vibe-verifier rules-doc: generated from the rule files by `bin/vibe-verifier rules-doc`; "
         "edit the rules and run it with --write, never this block -->")
END = "<!-- END vibe-verifier rules-doc -->"
INTRO = ("`repo-rules` checks every pull request against these rules. A blocking rule fails a pull request that adds a "
         "finding to a file; an advisory one only reports it. A rule applies to the files of its language (`with` and `without` "
         "list the globs its directory's sgconfig.yml maps to that language and to others), narrowed by its `files` and "
         "`ignores` globs.")
# a.yml holds the rules whose ids sort last, so the order below comes from the ids, not the files.
LATE = """id: zz-no-alert
language: Tsx
severity: hint
message: >
  Show a toast
  instead of alert().
files:
  - src/**
  - 'app/**'
ignores: ["legacy/**"]
rule:
  pattern: alert($$$A)
---
id: zz-switched-off
language: TypeScript
severity: "off"
message: Never runs.
rule:
  pattern: eval($$$A)
"""
EARLY = """id: aa-no-console-log
language: TypeScript
message: Use `log.info` from src/log instead of console.log.
rule:
  pattern: console.log($$$ARGS)
"""
TESTS = """id: aa-no-console-log
valid: [log.info(1)]
invalid: [console.log(1)]
---
id: zz-no-alert
valid: [toast(1)]
invalid: [alert(1)]
---
id: zz-switched-off
valid: [run(1)]
invalid: [eval(1)]
"""
MAPPING = "# more extensions for this directory's rules\nlanguageGlobs:\n  tsx: ['*.ts', \"*.mts\"]\n"
RULES = {".vibe-verifier-rules/a.yml": LATE, ".vibe-verifier-rules/b.yml": EARLY, ".vibe-verifier-rules/tests/all.yml": TESTS,
         ".vibe-verifier-rules/sgconfig.yml": MAPPING}
LISTED = [
    # The mapping moves .ts and .mts files to tsx, so this TypeScript rule no longer sees them.
    "- `aa-no-console-log` (blocking; TypeScript without `*.ts`, `*.mts`): Use `log.info` from src/log instead of console.log.",
    "- `no-debugger` (blocking; TypeScript): Remove the `debugger` statement; it stops every run that has a debugger attached.",
    "- `zz-no-alert` (advisory; Tsx with `*.ts`, `*.mts`; files `src/**`, `app/**`; ignores `legacy/**`): Show a toast instead of alert().",
]
BLOCK = "\n".join([BEGIN, "", INTRO, ""] + LISTED + ["", END])


def doc_repo(test, manifest="repo-rules --pack example\n", files=None):
    return make_repo(test, dict(RULES, **{".vibe-verifier": manifest, "src/a.ts": "export const a = 1\n"}, **(files or {})))


def rules_doc(repo, *args):
    return runner("rules-doc", "--repo", repo, "--manifest", str(Path(repo, ".vibe-verifier")), *args)


def check(repo, *args):
    result = gate("repo-rules", repo, "--base-ref", "HEAD~1", "--format", "json", "--pack", "example", *args)
    findings = json.loads(result.stdout)["findings"] if result.returncode in (0, 1) else None
    return result, findings


class RulesDoc(unittest.TestCase):
    def test_lists_every_enabled_rule_by_id_with_whether_it_blocks_its_scope_and_its_message(self):
        repo = doc_repo(self, "# the rules\nrepo-rules --pack example --soak --doc AGENTS.md\n")
        result = rules_doc(repo)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(result.stdout, BLOCK + "\n")  # zz-switched-off is `off`: ast-grep never runs it
        again = make_repo(self, {".vibe-verifier": "repo-rules --rules lint --pack example\n", "lint/one.yml": EARLY + "---\n" + LATE,
                                 "lint/tests/all.yml": TESTS, "lint/sgconfig.yml": MAPPING, "README.md": "x\n"})
        self.assertEqual(rules_doc(again).stdout, BLOCK + "\n")  # same rules in other files and another order: same block
        unmapped = doc_repo(self)
        Path(unmapped, ".vibe-verifier-rules/sgconfig.yml").unlink()  # the example pack maps nothing either
        self.assertIn("- `zz-no-alert` (advisory; Tsx; files `src/**`", rules_doc(unmapped).stdout)

    def test_write_appends_the_block_then_replaces_it_in_place(self):
        repo = doc_repo(self, files={"AGENTS.md": "# Agents\n\nRead this first."})
        agents = Path(repo, "AGENTS.md")
        self.assertEqual(rules_doc(repo, "--write", str(agents)).returncode, 0)
        self.assertEqual(agents.read_text(), "# Agents\n\nRead this first.\n\n" + BLOCK + "\n")
        agents.write_text(agents.read_text() + "\n## After\n\nKept.\n")
        Path(repo, ".vibe-verifier-rules/b.yml").write_text(EARLY.replace("instead of console.log", "instead"))
        result = rules_doc(repo, "--write", str(agents))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("replaced the rules block in", result.stdout)
        replaced = BLOCK.replace("from src/log instead of console.log.", "from src/log instead.")
        self.assertEqual(agents.read_text(), "# Agents\n\nRead this first.\n\n" + replaced + "\n\n## After\n\nKept.\n")
        created = Path(repo, "docs/RULES.md")
        created.parent.mkdir()
        self.assertEqual(rules_doc(repo, "--write", str(created)).returncode, 0)
        self.assertEqual(created.read_text(), replaced + "\n")

    def test_a_manifest_without_repo_rules_or_with_an_unreadable_rule_cannot_render(self):
        result = rules_doc(doc_repo(self, "gitleaks\n"))
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn("has no repo-rules line", result.stderr)
        result = rules_doc(doc_repo(self, files={".vibe-verifier-rules/b.yml": EARLY.replace("message: Use", "note: Use")}))
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn("rule aa-no-console-log (.vibe-verifier-rules/b.yml) has no message", result.stderr)


class DocCheck(unittest.TestCase):
    def test_a_missing_block_or_file_is_a_finding_naming_the_command(self):
        repo = doc_repo(self, files={"AGENTS.md": "# Agents\n"})
        commit(repo, {"src/b.ts": "export const b = 1\n"})
        result, findings = check(repo, "--doc", "AGENTS.md", "--doc", "docs/RULES.md")
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertEqual([(f["path"], f["line"], f["advisory"]) for f in findings],
                         [("AGENTS.md", None, False), ("docs/RULES.md", None, False)])
        self.assertIn("AGENTS.md has no generated rules block; regenerate it from the repository root with "
                      "`<catalog>/bin/vibe-verifier rules-doc --repo . --manifest <manifest> --write AGENTS.md`", findings[0]["message"])

    def test_a_stale_block_is_a_finding_on_its_first_line_and_soak_reports_it_only(self):
        stale = "# Agents\n\n" + BLOCK.replace("instead of console.log.", "instead.") + "\n"
        repo = doc_repo(self, files={"AGENTS.md": stale})
        commit(repo, {"src/b.ts": "export const b = 1\n"})
        result, findings = check(repo, "--doc", "AGENTS.md")
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertEqual([(f["path"], f["line"]) for f in findings], [("AGENTS.md", 3)])
        self.assertIn("not what the rule files at HEAD generate", findings[0]["message"])
        result, findings = check(repo, "--doc", "AGENTS.md", "--soak")
        self.assertEqual((result.returncode, len(findings)), (0, 1), result.stdout + result.stderr)

    def test_blank_lines_a_formatter_adds_or_removes_never_make_the_block_stale(self):
        # The block sits between blank lines so Prettier leaves it alone; one that a formatter reflowed
        # anyway (blank lines added or dropped) is still fresh, while any changed text is not.
        self.assertTrue(BLOCK.split("\n")[1] == "" and BLOCK.split("\n")[-2] == "", BLOCK)
        squeezed = "\n".join(line for line in BLOCK.split("\n") if line.strip())
        spread = BLOCK.replace("\n", "\n\n")
        for variant in (squeezed, spread):
            repo = doc_repo(self, files={"AGENTS.md": "# Agents\n\n" + variant + "\n"})
            commit(repo, {"src/b.ts": "export const b = 1\n"})
            result, findings = check(repo, "--doc", "AGENTS.md")
            self.assertEqual((result.returncode, findings), (0, []), result.stdout + result.stderr)

    def test_a_fresh_block_passes_and_follows_the_rules_at_head_not_the_base(self):
        repo = doc_repo(self, "repo-rules --pack example --doc AGENTS.md\n", {"AGENTS.md": "# Agents\n\n" + BLOCK + "\n"})
        base = subprocess.run(["git", "-C", repo, "rev-parse", "HEAD"], capture_output=True, text=True, check=True).stdout.strip()
        commit(repo, {"src/b.ts": "export const b = 1\n"})
        self.assertEqual(check(repo, "--doc", "AGENTS.md")[0].returncode, 0)
        commit(repo, {".vibe-verifier-rules/b.yml": EARLY.replace("instead of console.log", "instead")})  # the base keeps judging code
        result, findings = check(repo, "--doc", "AGENTS.md")
        self.assertEqual((result.returncode, [f["path"] for f in findings]), (1, ["AGENTS.md"]), result.stdout + result.stderr)
        self.assertEqual(rules_doc(repo, "--write", str(Path(repo, "AGENTS.md"))).returncode, 0)
        commit(repo, {}, "regenerate")
        replayed = runner("run", "--repo", repo, "--manifest", str(Path(repo, ".vibe-verifier")), "--base-ref", base)
        self.assertEqual(replayed.returncode, 0, replayed.stdout + replayed.stderr)
        self.assertRegex(replayed.stdout, r"repo-rules\s+PASS")

    def test_a_pull_request_that_changes_the_manifests_rules_regenerates_the_block_with_it(self):
        # On a pull request the gate's own arguments are the base's line; the block follows the head manifest.
        repo = doc_repo(self, "repo-rules --doc AGENTS.md\n", {"AGENTS.md": "# Agents\n"})
        manifest, agents = str(Path(repo, ".vibe-verifier")), str(Path(repo, "AGENTS.md"))
        self.assertEqual(rules_doc(repo, "--write", agents).returncode, 0)
        commit(repo, {}, "block")
        base = subprocess.run(["git", "-C", repo, "rev-parse", "HEAD"], capture_output=True, text=True, check=True).stdout.strip()
        commit(repo, {".vibe-verifier": "repo-rules --pack example --doc AGENTS.md\n"}, "subscribe to a pack")
        stale = runner("run", "--repo", repo, "--manifest", manifest, "--base-ref", base)
        self.assertEqual(stale.returncode, 1, stale.stdout + stale.stderr)
        self.assertIn("repo-rules: AGENTS.md:3: rules-doc: this rules block is not what the rule files at HEAD generate", stale.stdout)
        self.assertEqual(rules_doc(repo, "--write", agents).returncode, 0)
        commit(repo, {}, "regenerate")
        fresh = runner("run", "--repo", repo, "--manifest", manifest, "--base-ref", base)
        self.assertEqual(fresh.returncode, 0, fresh.stdout + fresh.stderr)
        self.assertIn("- `no-debugger` (blocking; TypeScript)", Path(agents).read_text())


if __name__ == "__main__":
    unittest.main()
