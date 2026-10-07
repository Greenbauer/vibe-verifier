"""The explore template: its write-scope, site and verify-input steps run as the shell they are, the
one site URL it carries from the site step to the prompt and the gate, and the order of its steps.
All read harnesses/qae/explore.yml itself, so the test judges what a consumer copies."""
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from helpers import ROOT, clean_env, commit, git, make_repo, write
from test_qae_annotations import action_script
from test_qae_artifacts import CLEAN_REQUESTS, session_with
from test_review_harness import prompt_block

TEMPLATE = ROOT / "harnesses" / "qae" / "explore.yml"
PROMPT = ROOT / "harnesses" / "qae" / "prompt.md"
MANIFEST = ROOT / "harnesses" / "qae" / "manifest"


def step_script(start, end):
    text = TEMPLATE.read_text()
    step = text[text.index(start):text.index(end)]
    match = re.search(r"^( +)run: \|\n((?:\1 .*\n|\n)+)", step, re.MULTILINE)
    indent = len(match.group(1)) + 2
    return "".join(line[indent:] if line.strip() else "\n" for line in match.group(2).splitlines(True))


def verify_inputs_script():
    return step_script("- name: Write the declared inputs", "- name: Run the QA gates")


def scope_script():
    return step_script("- name: Enforce the write scope", "- name: Keep the evidence")


def stub_bin(test, commands):
    """A PATH directory holding one shell script per command name."""
    path = tempfile.mkdtemp(prefix="vv-bin-")
    test.addCleanup(shutil.rmtree, path, True)
    for name, body in commands.items():
        with open(os.path.join(path, name), "w") as handle:
            handle.write("#!/bin/sh\n" + body)
        os.chmod(os.path.join(path, name), 0o755)
    return path


class WriteScope(unittest.TestCase):
    """A clone of a PR branch after claude-code-action reset the config paths it distrusts:
    the PR changed CLAUDE.md and added .mcp.json, so the runner shows both as changed."""

    def setUp(self):
        origin = tempfile.mkdtemp(prefix="vv-origin-")
        self.addCleanup(lambda: subprocess.run(["rm", "-rf", origin]))
        git(origin, "init", "-q", "-b", "main")
        git(origin, "config", "user.email", "t@example.com")
        git(origin, "config", "user.name", "t")
        commit(origin, {"CLAUDE.md": "base rules\n", "app.js": "1\n"}, "base")
        git(origin, "checkout", "-q", "-b", "pr")
        commit(origin, {"CLAUDE.md": "pr rules\n", ".mcp.json": "{}\n", "app.js": "2\n"}, "pr")
        self.repo = tempfile.mkdtemp(prefix="vv-clone-")
        self.addCleanup(lambda: subprocess.run(["rm", "-rf", self.repo]))
        subprocess.run(["git", "clone", "-q", "-b", "pr", origin, self.repo], check=True, env=clean_env())
        # what restore-config.ts does: base's copy where base has one, gone where it does not
        git(self.repo, "checkout", "origin/main", "--", "CLAUDE.md")
        os.remove(os.path.join(self.repo, ".mcp.json"))
        git(self.repo, "reset", "-q", "--", "CLAUDE.md", ".mcp.json")

    def run_step(self):
        return subprocess.run(["bash", "-c", scope_script()], cwd=self.repo, capture_output=True, text=True,
                              env=clean_env({"BASE": "origin/main"}))

    def test_the_restore_alone_is_not_a_write(self):
        status = subprocess.run(["git", "status", "--porcelain"], cwd=self.repo, capture_output=True, text=True).stdout
        self.assertEqual(sorted(status.splitlines()), [" D .mcp.json", " M CLAUDE.md"])
        result = self.run_step()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("write scope held", result.stdout)

    def test_artifacts_are_in_scope(self):
        write(self.repo, {"qae-artifacts/qae/AC1.md": "- step 1: x -> y\n", "qae-inputs/pr-body.md": "x\n"})
        self.assertEqual(self.run_step().returncode, 0)

    def test_a_restored_path_the_explorer_then_edits_still_fails(self):
        write(self.repo, {"CLAUDE.md": "base rules\nand mine\n"})
        result = self.run_step()
        self.assertEqual(result.returncode, 1)
        self.assertIn("CLAUDE.md", result.stdout)
        self.assertNotIn(".mcp.json", result.stdout)

    def test_a_write_outside_the_scope_fails(self):
        write(self.repo, {"app.js": "3\n", "qae-artifacts/ok.png": ""})
        result = self.run_step()
        self.assertEqual(result.returncode, 1)
        self.assertIn(" M app.js", result.stdout)
        self.assertNotIn("CLAUDE.md", result.stdout)

    def test_an_unknown_base_excuses_nothing(self):
        result = subprocess.run(["bash", "-c", scope_script()], cwd=self.repo, capture_output=True, text=True,
                                env=clean_env({"BASE": "origin/nope"}))
        self.assertEqual(result.returncode, 1)
        self.assertIn("CLAUDE.md", result.stdout)


class SiteOrigins(unittest.TestCase):
    """A site step may declare `origins`, further hosts the site is served from: the verify job
    writes them after the site, and never without one."""

    def site_file(self, site, origins):
        work = tempfile.mkdtemp(prefix="vv-work-")
        self.addCleanup(shutil.rmtree, work, True)
        bin_dir = stub_bin(self, {"gh": "exit 0\n"})
        result = subprocess.run(["bash", "-e", "-c", verify_inputs_script()], cwd=work, capture_output=True, text=True,
                                env=clean_env({"PATH": bin_dir + os.pathsep + os.environ["PATH"], "GH_TOKEN": "x", "PR_NUMBER": "7",
                                               "REPO": "o/r", "SITE_URL": site, "SITE_ORIGINS": origins, "REFERENCES": "", "TICKET": ""}))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return Path(work, "qae-inputs", "site-url").read_text()

    def test_declared_origins_follow_the_site(self):
        self.assertEqual(self.site_file("https://site.example", "https://api.example https://files.example").split(),
                         ["https://site.example", "https://api.example", "https://files.example"])

    def test_an_origin_without_a_site_is_not_read_as_the_site(self):
        # The gate reads the first URL as the site, so an origin alone must leave the file empty,
        # which the gate refuses.
        self.assertEqual(self.site_file("", "https://api.example"), "\n")


class SiteUrl(unittest.TestCase):
    """One URL, declared by the site step's `url` output: the prompt names it, the job output carries
    it to the verify job, which writes qae-inputs/site-url, which the manifest's gate line reads."""

    def setUp(self):
        self.work = tempfile.mkdtemp(prefix="vv-work-")
        self.addCleanup(shutil.rmtree, self.work, True)

    def test_the_default_site_step_declares_the_local_site(self):
        bin_dir = stub_bin(self, {"npm": "exit 0\n", "curl": "exit 0\n"})
        output = Path(self.work) / "github_output"
        output.write_text("")
        script = step_script("- name: Build and start the site under test", "- name: Install the browser toolchain")
        result = subprocess.run(["bash", "-e", "-c", script], cwd=self.work, capture_output=True, text=True,
                                env=clean_env({"PATH": bin_dir + os.pathsep + os.environ["PATH"], "GITHUB_OUTPUT": str(output)}))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(output.read_text(), "url=http://localhost:3000\n")
        self.assertIn("        id: site\n", TEMPLATE.read_text())

    def test_the_prompt_is_the_harness_prompt_naming_the_declared_url(self):
        expected = (PROMPT.read_text().replace("PR_NUMBER", "${{ github.event.pull_request.number }}")
                    .replace("REPOSITORY", "${{ github.repository }}")
                    .replace("SITE_URL", "${{ steps.site.outputs.url }}"))
        self.assertEqual(prompt_block(TEMPLATE.read_text()), expected)

    def test_the_declared_url_reaches_the_gate_through_the_verify_job(self):
        text = TEMPLATE.read_text()
        self.assertIn("    outputs:\n      site-url: ${{ steps.site.outputs.url }}\n      site-origins: ${{ steps.site.outputs.origins }}\n", text)
        self.assertIn("          SITE_URL: ${{ needs.explore.outputs.site-url }}\n          SITE_ORIGINS: ${{ needs.explore.outputs.site-origins }}\n", text)
        preview = "https://site-git-feat-team.vercel.app"
        bin_dir = stub_bin(self, {"gh": 'case "$1 $2" in\n'
                                        '  pr*) printf "## Acceptance criteria\\n\\n- The quote page loads\\n" ;;\n'
                                        '  *pulls*) printf "app/quote/page.tsx\\n" ;;\n'
                                        '  api*) printf %s "[{\\"user\\":{\\"login\\":\\"github-actions[bot]\\"},\\"body\\":\\"acceptance-check: AC1 -- PASS -- loaded (qae/AC1.md::step 1: x)\\"}]" ;;\n'
                                        'esac\n'})
        script = verify_inputs_script()
        result = subprocess.run(["bash", "-e", "-c", script], cwd=self.work, capture_output=True, text=True,
                                env=clean_env({"PATH": bin_dir + os.pathsep + os.environ["PATH"], "GH_TOKEN": "x",
                                               "PR_NUMBER": "7", "REPO": "o/r", "SITE_URL": preview, "SITE_ORIGINS": "", "REFERENCES": "", "TICKET": ""}))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(Path(self.work, "qae-inputs", "site-url").read_text(), preview + "\n")
        # The run's artifacts: the preview answered 500, the local default 404; only the declared site counts.
        write(self.work, {
            "qae-artifacts/qae/AC1.md": "- step 1: x\n", "qae-artifacts/qae/AC1-step-1.png": "png",
            "qae-artifacts/session-1/session.md": session_with(CLEAN_REQUESTS + [
                "[GET] %s/api/quote => [500] Internal Server Error" % preview,
                "[GET] http://localhost:3000/gone => [404] Not Found"]),
        })
        line = [entry for entry in MANIFEST.read_text().splitlines() if entry.startswith("qae-artifacts ")]
        self.assertEqual(len(line), 1)
        self.assertIn("--site-file qae-inputs/site-url", line[0])
        gate = subprocess.run([sys.executable, str(ROOT / "gates" / "qae_artifacts.py"), *shlex.split(line[0])[1:]],
                              cwd=self.work, capture_output=True, text=True, env=clean_env())
        self.assertEqual(gate.returncode, 1, gate.stdout + gate.stderr)
        self.assertIn("[GET] %s/api/quote" % preview, gate.stdout)
        self.assertNotIn("localhost:3000/gone", gate.stdout)


class Steps(unittest.TestCase):
    def test_the_explorer_runs_only_when_there_is_a_criterion(self):
        text = TEMPLATE.read_text()
        self.assertIn("  explore:\n    name: qae-explore\n    needs: criteria\n"
                      "    if: needs.criteria.outputs.count != '0'\n", text)
        self.assertIn("    outputs:\n      count: ${{ steps.criteria.outputs.count }}\n", text[:text.index("\n  explore:\n")])

    def test_the_only_comment_the_explorer_may_post_is_the_verdict_file(self):
        # gh opens --body-file itself, so a wildcard rule would let a hostile PR body post any file.
        text = TEMPLATE.read_text()
        tools = re.search(r'--allowed-tools "([^"]+)"', text).group(1).split(",")
        comment_rules = [rule for rule in tools if "gh pr comment" in rule]
        self.assertEqual(comment_rules, ["Bash(gh pr comment ${{ github.event.pull_request.number }} --body-file qae-artifacts/verdict.md)"])
        self.assertIn("gh pr comment ${{ github.event.pull_request.number }} --body-file qae-artifacts/verdict.md\n", text)

    def test_dependabot_is_the_one_bot_whose_pull_requests_reach_the_explorer(self):
        # claude-code-action refuses a run a bot started unless allowed_bots names it, so no Dependabot
        # pull request could be walked (a consumer's four security updates, 2026-10-06). Named, never
        # "*": that lets any App able to open a pull request start the explorer with a body it wrote.
        text = TEMPLATE.read_text()
        explorer = text[text.index("- name: Explore the acceptance criteria in a real browser"):text.index("- name: Collect numeric usage")]
        self.assertEqual(re.findall(r"^ +allowed_bots: (.*)$", explorer, re.MULTILINE), ["dependabot[bot]"])

    def test_a_consumer_can_tell_the_explorer_how_to_sign_in(self):
        # The file is written by the consumer's build step, which runs before the model, and the
        # explorer may read qae-inputs/ and nothing else outside its artifacts.
        text = TEMPLATE.read_text()
        self.assertIn("If qae-inputs/site.md exists, read it before anything else.", text)
        self.assertIn("Read(qae-inputs/**)", text)
        self.assertLess(text.index("- name: Build and start the site under test"),
                        text.index("- name: Explore the acceptance criteria in a real browser"))

    def test_the_browser_toolchain_is_installed_from_the_lockfile_not_resolved_at_run_time(self):
        text = TEMPLATE.read_text()
        self.assertNotIn("npx -y", text)
        self.assertIn("uses: Greenbauer/vibe-verifier/actions/qae-browser@", text)
        self.assertIn('"command":"node","args":["${{ steps.browser.outputs.mcp }}"', text)
        self.assertLess(text.index("id: browser"), text.index("- name: Explore the acceptance criteria"))

    def test_every_catalog_pin_is_the_placeholder_a_consumer_replaces(self):
        pins = re.findall(r"vibe-verifier/actions/[\w-]+@(\S+)( #[^\n]*)?", TEMPLATE.read_text())
        self.assertEqual(len(pins), 8)
        for sha, comment in pins:
            self.assertEqual(sha, "0" * 40)
            self.assertIn("CONSUMER: pin the commit you subscribe to", comment)


class FeatureRewalk(unittest.TestCase):
    """The explore job hands the explorer the features its changes touch (docs/feature-map.md), before the
    model runs; the verify job's gate re-derives the same selection, so this only informs."""

    def explore(self):
        text = TEMPLATE.read_text()
        return text[text.index("\n  explore:\n"):text.index("\n  verify:\n")]

    def test_the_selection_runs_after_the_inputs_and_before_the_explorer(self):
        explore = self.explore()
        order = [explore.index(step) for step in ("- name: Write the explorer's input", "- name: Select the features to re-walk",
                                                  "- name: Explore the acceptance criteria in a real browser")]
        self.assertEqual(order, sorted(order))
        step = explore[explore.index("- name: Select the features to re-walk"):explore.index("- uses: actions/setup-node@")]
        self.assertIn("uses: Greenbauer/vibe-verifier/actions/features@", step)
        self.assertIn("          changed-files: qae-inputs/changed-files\n", step)

    def test_the_explore_checkout_has_the_base_the_selection_is_judged_from(self):
        explore = self.explore()
        checkout = explore[explore.index("- uses: actions/checkout@"):explore.index("- name: Write the explorer's input")]
        self.assertIn("          fetch-depth: 0\n", checkout)
        self.assertIn("          persist-credentials: false\n", checkout)

    def test_the_explore_inputs_step_writes_the_changed_paths(self):
        work = tempfile.mkdtemp(prefix="vv-work-")
        self.addCleanup(shutil.rmtree, work, True)
        bin_dir = stub_bin(self, {"gh": 'case "$1" in\n  pr) printf "## Acceptance criteria\\n\\n- x\\n" ;;\n'
                                        '  api) printf "app/auth/login.ts\\napp/old.ts\\n" ;;\nesac\n'})
        script = step_script("- name: Write the explorer's input", "- name: Select the features to re-walk")
        result = subprocess.run(["bash", "-e", "-c", script], cwd=work, capture_output=True, text=True,
                                env=clean_env({"PATH": bin_dir + os.pathsep + os.environ["PATH"], "GH_TOKEN": "x",
                                               "PR_NUMBER": "7", "REPO": "o/r", "TICKET": ""}))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(Path(work, "qae-inputs", "changed-files").read_text(), "app/auth/login.ts\napp/old.ts\n")

    def test_the_prompt_tells_the_explorer_where_the_features_are_and_how_to_answer(self):
        prompt = PROMPT.read_text()
        for phrase in ("If the directory qae-inputs/features/ exists", "qae-artifacts/qae/features/<id>.md",
                       "qae-artifacts/qae/features/<id>-step-k.png",
                       "regression-check: <id> -- PASS -- <one sentence> (qae/features/<id>.md::<the step line text>)"):
            self.assertIn(phrase, prompt)

    def test_the_prompt_bounds_the_re_walk_and_keeps_a_fail_for_a_step_that_was_walked(self):
        # The gate reads these tokens and file names, so the prompt and the gate must name the same ones: the
        # list actions/features writes, the skip line the gate prints, and what a FAIL may and may not mean.
        prompt = " ".join(PROMPT.read_text().split())
        for phrase in ("as this pull request has that file",
                       "Walk at most as many states of each feature as qae-inputs/features.md says (three when that file is absent)",
                       "walk one state of every feature before a second state of any",
                       "and nothing more: the checks beyond a criterion, above, are for criteria only",
                       "is not a failure: log no step for it and name it on the feature's regression-skip line",
                       "FAIL only when a step you walked showed that the feature no longer works the way its file describes, "
                       "never because states were left unwalked or time ran out",
                       "regression-skip: <id> -- <the states not walked, for example 4, 5, 7>",
                       "A feature of which you walked no state gets its regression-skip line and no regression-check line"):
            self.assertIn(phrase, prompt)
        step = self.explore()
        step = step[step.index("- name: Select the features to re-walk"):step.index("- uses: actions/setup-node@")]
        self.assertIn("qae-inputs/features.md tells the explorer how many states of each to walk (--max-states, default 3)", step)
        action = (ROOT / "actions" / "features" / "action.yml").read_text()
        self.assertIn('--out "$VV_OUT" --listing "$VV_OUT.md"', action)
        self.assertIn("    default: qae-inputs/features\n", action)


class Applicability(unittest.TestCase):
    """A pull request that needs no browser check builds nothing and is not judged, and both jobs
    decide that with the same action, from the same two inputs."""

    def test_a_criteria_job_on_any_runner_decides_before_the_explore_job_takes_its_runner(self):
        # On the Codex lane the explore job's runner holds the one login, so a pull request with
        # nothing to walk must never queue for it: the criteria job is first, holds no model token,
        # builds nothing and never checks out the pull request's code.
        text = TEMPLATE.read_text()
        criteria = text[text.index("jobs:\n  criteria:\n"):text.index("\n  explore:\n")]
        self.assertIn("    runs-on: ubuntu-latest   # CONSUMER: any runner. It holds no model login and builds nothing.\n", criteria)
        self.assertIn("      pull-requests: read\n", criteria)
        self.assertLess(criteria.index("- name: Write the criteria inputs"), criteria.index("- name: Read the criteria"))
        for absent in ("secrets.CLAUDE", "actions/checkout@", "npm ", "write"):
            self.assertNotIn(absent, criteria.replace("- name: Write the criteria inputs", ""), absent)
        explore = text[text.index("\n  explore:\n"):text.index("\n  verify:\n")]
        self.assertNotIn("steps.criteria", explore)

    def test_the_verify_job_refuses_a_criteria_job_that_did_not_finish(self):
        # A job whose need failed is skipped, exactly like an explore job with nothing to walk, so the
        # verify job tells the two apart itself, first, before anything can read as a pass.
        text = TEMPLATE.read_text()
        verify = text[text.index("\n  verify:\n"):]
        self.assertIn("    needs: [criteria, explore]\n    if: always()\n", verify)
        self.assertLess(verify.index("- name: Require a finished criteria job"), verify.index("- uses: actions/checkout@"))
        script = step_script("- name: Require a finished criteria job", "- uses: actions/checkout@fbc6f3992d24b796d5a048ff273f7fcc4a7b6c09 # v5.1.0\n        with:\n          fetch-depth: 0")
        for result, code in (("success", 0), ("failure", 1), ("cancelled", 1), ("skipped", 1), ("", 1)):
            ran = subprocess.run(["bash", "-e", "-c", script], capture_output=True, text=True,
                                 env=clean_env({"CRITERIA_RESULT": result}))
            self.assertEqual(ran.returncode, code, result)

    def test_every_job_reads_the_changed_paths_the_same_way(self):
        # The criteria and verify jobs pass them to the criteria action; the explore job to the feature selection.
        text = TEMPLATE.read_text()
        self.assertEqual(text.count("uses: Greenbauer/vibe-verifier/actions/criteria@"), 2)
        self.assertEqual(text.count("uses: Greenbauer/vibe-verifier/actions/features@"), 1)
        self.assertEqual(text.count("          changed-files: qae-inputs/changed-files\n"), 3)
        self.assertEqual(text.count("--jq '.[] | .filename, (.previous_filename // empty)' > qae-inputs/changed-files"), 3)

    def test_the_explore_inputs_step_writes_the_body_and_every_changed_name(self):
        work = tempfile.mkdtemp(prefix="vv-work-")
        self.addCleanup(shutil.rmtree, work, True)
        bin_dir = stub_bin(self, {"gh": 'case "$1" in\n'
                                        '  pr) printf "## Why\\n\\ndocs\\n" ;;\n'
                                        '  api) printf "README.md\\ndocs/about.tsx\\napp/about.tsx\\n" ;;\n'
                                        'esac\n'})
        script = step_script("- name: Write the criteria inputs", "- name: Read the criteria")
        result = subprocess.run(["bash", "-e", "-c", script], cwd=work, capture_output=True, text=True,
                                env=clean_env({"PATH": bin_dir + os.pathsep + os.environ["PATH"], "GH_TOKEN": "x",
                                               "PR_NUMBER": "7", "REPO": "o/r"}))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(Path(work, "qae-inputs", "changed-files").read_text(), "README.md\ndocs/about.tsx\napp/about.tsx\n")
        self.assertTrue(Path(work, "qae-inputs", "pr-body.md").is_file())

    def test_the_verify_job_runs_the_gates_unless_no_check_is_needed(self):
        text = TEMPLATE.read_text()
        verify = text[text.index("\n  verify:\n"):]
        download = verify.index("- uses: actions/download-artifact@")
        self.assertLess(verify.index("- name: Read the criteria"), download)
        self.assertIn("if: steps.criteria.outputs.count != '0'", verify[download:download + 200])
        gates = verify.index("- name: Run the QA gates")
        self.assertIn("        id: gates\n        if: steps.criteria.outputs.declared-none == ''\n", verify[gates:gates + 200])


class Evidence(unittest.TestCase):
    """Each run attempt keeps its own evidence, and the verify job reads the explore job's."""

    def test_a_re_run_never_reads_an_earlier_attempts_evidence(self):
        # The upload runs always, so a failed or cancelled attempt uploads too. Under one name for
        # every attempt, a consumer's third attempt passed its explore job and the verify job was
        # handed the cancelled second attempt's artifact (2026-10-07): no step log, no session log.
        text = TEMPLATE.read_text()
        explore = text[text.index("\n  explore:\n"):text.index("\n  verify:\n")]
        upload = explore[explore.index("- name: Keep the evidence"):]
        self.assertIn("        if: always()\n", upload)
        self.assertIn("          name: qae-artifacts-${{ github.run_attempt }}\n          path: qae-artifacts\n", upload)
        uploads = re.findall(r"uses: actions/upload-artifact@[^\n]*\n        with:\n          name: ([^\n]*)\n", text)
        self.assertEqual(len(uploads), 2)
        for name in uploads:
            self.assertTrue(name.endswith("-${{ github.run_attempt }}"), name)

    def test_the_verify_job_downloads_the_attempt_the_explore_job_ran_in(self):
        # Not its own attempt: when only the verify job is re-run, the explore job's evidence is an
        # earlier attempt's, and that job's outputs are the ones it reported then.
        text = TEMPLATE.read_text()
        explore = text[text.index("\n  explore:\n"):text.index("\n  verify:\n")]
        self.assertIn("      attempt: ${{ github.run_attempt }}\n", explore[explore.index("    outputs:\n"):explore.index("    steps:\n")])
        verify = text[text.index("\n  verify:\n"):]
        download = verify[verify.index("- uses: actions/download-artifact@"):verify.index("- name: Run the QA gates")]
        self.assertIn("          name: qae-artifacts-${{ needs.explore.outputs.attempt }}\n          path: qae-artifacts\n", download)
        self.assertNotIn("github.run_attempt", verify)


class Review(unittest.TestCase):
    """The verify job posts the one review comment, after the gates, whatever they concluded."""

    def test_the_review_is_the_last_step_and_runs_unless_cancelled(self):
        text = TEMPLATE.read_text()
        verify = text[text.index("\n  verify:\n"):]
        review = verify[verify.index("- name: Post the QA review"):]
        self.assertNotIn("\n      - ", review[1:])
        self.assertIn("        if: ${{ !cancelled() }}\n", review)
        self.assertIn("uses: Greenbauer/vibe-verifier/actions/qa-review@", review)
        for line in ("explore-result: ${{ needs.explore.result }}", "gates-outcome: ${{ steps.gates.outcome }}",
                     "not-required: ${{ steps.criteria.outputs.declared-none }}", "verdict: qae-inputs/verdict.md",
                     "criteria: qae-inputs/pr-body.md", "token: ${{ secrets.GITHUB_TOKEN }}"):
            self.assertIn("          %s\n" % line, review)

    def test_only_the_verify_job_may_comment_besides_the_explorer(self):
        text = TEMPLATE.read_text()
        verify = text[text.index("\n  verify:\n"):]
        self.assertIn("      pull-requests: write   # the QA review comment, and nothing else\n", verify)
        self.assertNotIn("issues: write", text)

    def test_the_verdict_lookup_skips_the_review_comment(self):
        # The review is posted by the same identity, after the verdict, and can quote a criterion that
        # reads like a check line. The gate must still be fed the explorer's verdict.
        work = tempfile.mkdtemp(prefix="vv-work-")
        self.addCleanup(shutil.rmtree, work, True)
        verdict = "acceptance-check: AC1 -- PASS -- loaded (qae/AC1.md::step 1: x)"
        comments = json.dumps([
            {"user": {"login": "github-actions[bot]"}, "body": verdict},
            {"user": {"login": "github-actions[bot]"},
             "body": "<!-- vibe-verifier:qa-review -->\n## QA review: passed\n\n- AC1, explorer says PASS: acceptance-check: AC1 -- PASS"},
        ])
        Path(work, "comments.json").write_text(comments)
        bin_dir = stub_bin(self, {"gh": 'case "$1 $2" in\n'
                                        '  pr*) printf "## Acceptance criteria\\n\\n- x\\n" ;;\n'
                                        '  *pulls*) printf "app/page.tsx\\n" ;;\n'
                                        '  api*) cat "%s" ;;\n'
                                        'esac\n' % Path(work, "comments.json")})
        result = subprocess.run(["bash", "-e", "-c", verify_inputs_script()], cwd=work, capture_output=True, text=True,
                                env=clean_env({"PATH": bin_dir + os.pathsep + os.environ["PATH"], "GH_TOKEN": "x",
                                               "PR_NUMBER": "7", "REPO": "o/r", "SITE_URL": "http://localhost:3000",
                                               "SITE_ORIGINS": "", "REFERENCES": "", "TICKET": ""}))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(Path(work, "qae-inputs", "verdict.md").read_text(), verdict + "\n")
        self.assertEqual(Path(work, "qae-inputs", "changed-files").read_text(), "app/page.tsx\n")


class TurnCap(unittest.TestCase):
    """The Claude lane's --max-turns, sized by actions/qae-inputs to what the explorer walks: each criterion
    of the pull request and its ticket once per role it names and per width, plus each feature re-walk.
    40 plus 30 a walk, never below 80 nor above 240 (bin/vibe-verifier, TURNS_BASE)."""

    def run_action(self, files, widths=None, verdict_line=""):
        manifest = verdict_line + "qae-artifacts --artifacts qae-artifacts%s\n" % (" --widths " + widths if widths else "")
        repo = make_repo(self, {".vibe-verifier-qae": manifest})
        write(repo, files)
        output = Path(repo, ".git", "github-output")
        output.write_text("")
        result = subprocess.run(["bash", "-e", "-c", action_script("qae-inputs")], cwd=repo, capture_output=True, text=True,
                                env=clean_env({"GITHUB_ACTION_PATH": str(ROOT / "actions" / "qae-inputs"), "GITHUB_OUTPUT": str(output),
                                               "RUNNER_TEMP": os.path.join(repo, ".git"), "VV_MANIFEST": ".vibe-verifier-qae",
                                               "VV_ENTRIES": "", "VV_REFERENCES": "qae-inputs/references",
                                               "VV_EVIDENCE": "qae-artifacts"}))
        return result, output.read_text()

    def cap(self, files, widths=None, verdict_line=""):
        result, output = self.run_action(files, widths, verdict_line)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return int(re.search(r"^max-turns=([0-9]+)$", output, re.MULTILINE).group(1)), result.stdout

    def test_the_run_a_fixed_80_cut_off_gets_twice_that(self):
        # A consumer's live run (2026-10-07): a pull request declaring None, its ticket's two criteria (one naming a
        # design reference) at two widths. It took 54 turns, then ran out at 80 on a re-run of the same head.
        ticket = ("## Acceptance criteria\n\n- The portfolio page lists its sections\n"
                  "- At 375 pixels wide, the home page matches its mobile design reference [ref: home-mobile]\n")
        turns, stdout = self.cap({"qae-inputs/pr-body.md": "## Acceptance criteria\n\n- None: a workflow change\n",
                                  "qae-inputs/ticket.md": ticket}, "1280,375")
        self.assertEqual(turns, 160)
        self.assertIn("max turns: 160 (4 walks)", stdout)

    def test_each_role_and_each_feature_to_re_walk_is_a_walk(self):
        turns, _ = self.cap({"qae-inputs/pr-body.md": "## Acceptance criteria\n\n- Only an admin deletes [as: admin, read-only]\n",
                             "qae-inputs/features/sign-in.md": "# Sign-in\n"}, "1280,375")
        self.assertEqual(turns, 40 + 30 * (2 * 2 + 1))

    def test_a_feature_is_one_walk_for_each_three_states_the_manifest_lets_the_explorer_walk(self):
        # One criterion and two features: three walks at the default three states a feature, as before the bound.
        files = {"qae-inputs/pr-body.md": "## Acceptance criteria\n\n- The home page loads\n",
                 "qae-inputs/features/sign-in.md": "# Sign-in\n", "qae-inputs/features/projects.md": "# Projects\n"}
        line = "acceptance-verdict --criteria a --verdict b --features docs/features --changed-files c%s\n"
        for states, walks in (("", 3), (" --max-states 1", 3), (" --max-states 3", 3), (" --max-states 4", 5), (" --max-states 6", 5)):
            turns, stdout = self.cap(files, verdict_line=line % states)
            self.assertEqual(turns, 40 + 30 * walks, states)
            self.assertIn("(%d walks)" % walks, stdout)

    def test_a_run_with_one_walk_keeps_the_floor_of_80(self):
        self.assertEqual(self.cap({"qae-inputs/pr-body.md": "## Acceptance criteria\n\n- The home page loads\n"})[0], 80)

    def test_the_cap_is_bounded_whatever_the_body_lists(self):
        body = "## Acceptance criteria\n\n" + "".join("- Criterion %d holds\n" % n for n in range(12))
        self.assertEqual(self.cap({"qae-inputs/pr-body.md": body}, "1280,375")[0], 240)

    def test_without_the_pr_body_the_cap_cannot_be_sized(self):
        result, output = self.run_action({"README.md": "x\n"})
        self.assertEqual(result.returncode, 2)
        self.assertIn("could not read", result.stderr)
        self.assertEqual(output, "")

    def test_the_claude_lane_passes_the_cap_to_the_explorer(self):
        text = TEMPLATE.read_text()
        self.assertIn("            --max-turns ${{ steps.references.outputs.max-turns }}\n", text)
        self.assertNotIn("--max-turns 8", text)
        self.assertIn("        id: references\n        uses: Greenbauer/vibe-verifier/actions/qae-inputs@", text)
        self.assertIn("    value: ${{ steps.prepare.outputs.max-turns }}\n", (ROOT / "actions" / "qae-inputs" / "action.yml").read_text())


if __name__ == "__main__":
    unittest.main()
