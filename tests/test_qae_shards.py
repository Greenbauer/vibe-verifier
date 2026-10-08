"""A large pull request's criteria split across explorers that run in parallel (harnesses/qae/README.md,
"Splitting a large pull request across explorers"). `vibe-verifier criteria --max-shards` plans the split,
actions/criteria declares it as the explore job's matrix, actions/qae-inputs names each explorer's share
for its prompt, and the template's own steps keep each explorer's verdict and evidence apart and merge
them for the gates. Driven through the command line, the shipped actions and the template's steps, run
as the shell they are."""
import os
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from helpers import ROOT, clean_env, commit, gate, git, make_repo, runner, write
from test_qae_annotations import action_script, step_script
from test_qae_artifacts import BLOCKED, CLEAN_REQUESTS, page_record, session_saving_to

TEMPLATES = [ROOT / "harnesses" / "qae" / name for name in ("explore.yml", "explore-codex.yml")]
PROMPT = ROOT / "harnesses" / "qae" / "prompt.md"
QAE = "acceptance-verdict --criteria a --verdict b\nqae-artifacts --artifacts qae-artifacts --widths 1280,375\n"
KEEP = ("- name: Keep the verdict with the evidence", "- name: Keep the evidence\n")
MERGE = ("- name: Merge the evidence and read the verdict", "- name: Run the QA gates")
SHARE = ("Your share of the criteria is %s: you are explorer %d of %d. Walk only those, and write a verdict line for each "
         "of them and for no other criterion; the other criteria are walked by other explorers in parallel, which may "
         "use the same site, so expect data you did not create.")
NO_SHARE = ("None of the criteria is yours: you are explorer %d of %d, and other explorers walk them in parallel, which may "
            "use the same site, so expect data you did not create. Walk no criterion and write no acceptance-check line. "
            "Re-walk the features as described below, and write only their lines in the verdict.")
SESSION, CONSOLE = "session-1760000000000", "console-2026-10-08T12-00-00-000Z.log"


def body(*criteria):
    return "## Acceptance criteria\n\n" + "".join("- %s\n" % text for text in criteria)


THIRTEEN = body(*("Criterion %d holds" % number for number in range(1, 14)))


def workdir(test):
    path = tempfile.mkdtemp(prefix="vv-shards-")
    test.addCleanup(shutil.rmtree, path, True)
    return path


def run_step(script, work, env):
    return subprocess.run(["bash", "-e", "-c", script], cwd=work, capture_output=True, text=True, env=clean_env(env))


def outputs(path):
    return dict(line.split("=", 1) for line in Path(path).read_text().splitlines() if "=" in line)


def keep_evidence(work, shard=1, shards=1, template=TEMPLATES[0]):
    """The explore job's step after the explorer, run in a workspace that holds qae-artifacts/."""
    return run_step(step_script(template.read_text(), *KEEP), work, {"SHARD": str(shard), "SHARDS": str(shards)})


def hand_over(explore, verify, attempt=1, shard=1):
    """What the upload and the verify job's download do: the evidence under its artifact's name."""
    name = "qae-artifacts-%d%s" % (attempt, "" if shard == 1 else "-%d" % shard)
    shutil.copytree(os.path.join(explore, "qae-artifacts"), os.path.join(verify, "qae-evidence", name))


def merge_evidence(verify, shards='[""]', attempt="1", template=TEMPLATES[0]):
    """The verify job's merge step, run in a workspace that holds qae-evidence/."""
    os.makedirs(os.path.join(verify, "qae-inputs"), exist_ok=True)
    return run_step(step_script(template.read_text(), *MERGE), verify, {"SHARDS": shards, "EXPLORE_ATTEMPT": attempt})


def explorer_leaves(work, criterion, requests=CLEAN_REQUESTS, verdict="PASS"):
    """What one explorer leaves under qae-artifacts/. Every explorer names its session log, its console log
    and the network record it saved alike, so two explorers' evidence collides unless it is renamed."""
    step = "step 1: opened / -> the heading"
    session = session_saving_to("qae-artifacts/network.txt") + "- New console entries: qae-artifacts/%s#L1\n" % CONSOLE
    write(work, {"qae-artifacts/qae/%s.md" % criterion: "- %s\n" % step, "qae-artifacts/qae/%s-step-1.png" % criterion: "png",
                 "qae-artifacts/%s/session.md" % SESSION: session,
                 "qae-artifacts/network.txt": "".join("%d. %s\n" % (n, line) for n, line in enumerate(requests, 1)),
                 "qae-artifacts/%s" % CONSOLE: "[   606ms] [LOG] hello from %s @ http://localhost:3000/app.js:0\n" % criterion,
                 "qae-artifacts/final.md": "done with %s\n" % criterion, "qae-artifacts/server.log": "listening\n",
                 "qae-artifacts/references/home.png": "the image every explorer was handed\n"})
    if verdict:
        write(work, {"qae-artifacts/verdict.md": "acceptance-check: %s -- %s -- held (qae/%s.md::%s)\n" % (criterion, verdict, criterion, step)})


def tree(root):
    return sorted(os.path.relpath(os.path.join(folder, name), root) for folder, _, files in os.walk(root) for name in files)


class Plan(unittest.TestCase):
    """`criteria --max-shards`: how many explorers, from the walks of the criteria alone."""

    def plan(self, text, most, ticket="", manifest="", code=0):
        work = workdir(self)
        write(work, {"pr-body.md": text, "ticket.md": ticket, "manifest": manifest})
        args = ["criteria", os.path.join(work, "pr-body.md"), "--max-shards", str(most), "--repo", work]
        args += ["--ticket", os.path.join(work, "ticket.md")] if ticket else []
        args += ["--manifest", os.path.join(work, "manifest")] if manifest else []
        result = runner(*args)
        self.assertEqual(result.returncode, code, result.stdout + result.stderr)
        return [line for line in result.stdout.splitlines() if line.startswith(("shard", "walks", "::warning"))]

    def test_a_small_pull_request_is_one_explorer_whatever_is_allowed(self):
        # Three criteria at two widths are six walks, what one explorer is sized for.
        self.assertEqual(self.plan(body("a", "b", "c"), 4, manifest=QAE), ["shards: [1]", "walks: 6"])

    def test_the_pull_request_one_explorer_could_not_finish_is_shared_out_evenly(self):
        # A consumer's pull request: 13 criteria at two widths, 26 walks. Four runners share them, and the
        # plan says up front that 26 walks are more than four explorers are sized for. Who walks which
        # criterion is the explore job's to say (Share, below): it knows the feature re-walk.
        lines = self.plan(THIRTEEN, 4, manifest=QAE)
        self.assertEqual(lines, ["shards: [1,2,3,4]", "walks: 26",
                                 "::warning title=vibe-verifier criteria::26 walks (each criterion once per role and width) for 4 "
                                 "explorers sized for 24: expect criteria left unfinished. List fewer criteria on one pull request, "
                                 "or raise max-shards (harnesses/qae/README.md)."])
        self.assertEqual(self.plan(THIRTEEN, 4, manifest=QAE), lines)
        self.assertEqual(self.plan(THIRTEEN, 8, manifest=QAE), ["shards: [1,2,3,4,5]", "walks: 26"])

    def test_each_role_and_each_ticket_criterion_is_counted(self):
        # Three roles are three walks of one criterion; a ticket's criteria count with the pull request's.
        self.assertEqual(self.plan(body("a [as: admin, rep, viewer]", "b", "c", "d", "e"), 3), ["shards: [1,2]", "walks: 7"])
        ticket = "- t1\n- t2 [as: admin, rep]\n- t3\n"
        self.assertEqual(self.plan(body("a", "b", "c", "d"), 2, ticket=ticket), ["shards: [1,2]", "walks: 8"])
        self.assertEqual(self.plan(body("None: a refactor"), 2, ticket=ticket + "- t4\n- t5\n- t6\n"), ["shards: [1,2]", "walks: 7"])

    def test_one_explorer_is_the_default_and_an_oversized_pull_request_says_so(self):
        self.assertEqual(self.plan(THIRTEEN, 1), [
            "shards: [1]", "walks: 13", "::warning title=vibe-verifier criteria::13 walks (each criterion once per role and width) "
            "for 1 explorer sized for 6: expect criteria left unfinished. List fewer criteria on one pull request, or raise "
            "max-shards (harnesses/qae/README.md)."])

    def test_there_are_never_more_explorers_than_criteria(self):
        # One criterion as four roles at two widths is eight walks, and still one explorer's.
        lines = self.plan(body("a [as: admin, rep, viewer, guest]"), 3, manifest=QAE)
        self.assertEqual(lines[:2], ["shards: [1]", "walks: 8"])
        self.assertIn("8 walks (each criterion once per role and width) for 1 explorer sized for 6", lines[2])

    def test_the_widths_are_the_manifests_as_the_base_has_it(self):
        repo = make_repo(self, {".vibe-verifier-qae": QAE, "pr-body.md": body("a", "b", "c", "d")})
        git(repo, "checkout", "-q", "-b", "feat")
        commit(repo, {".vibe-verifier-qae": QAE.replace(" --widths 1280,375", "")})
        args = ["criteria", os.path.join(repo, "pr-body.md"), "--max-shards", "3", "--repo", repo]
        counted = runner(*args, "--manifest", os.path.join(repo, ".vibe-verifier-qae"))
        self.assertIn("shards: [1,2]\nwalks: 8\n", counted.stdout)
        self.assertIn("shards: [1]\nwalks: 4\n", runner(*args).stdout)

    def test_a_count_below_one_or_a_manifest_that_is_not_there_cannot_run(self):
        self.assertEqual(self.plan(body("a"), 0, code=2), [])
        missing = runner("criteria", os.path.join(make_repo(self, {"pr-body.md": body("a")}), "pr-body.md"), "--max-shards", "2",
                         "--manifest", "/nonexistent/manifest")
        self.assertEqual(missing.returncode, 2)
        self.assertIn("no manifest at", missing.stderr)


class CriteriaAction(unittest.TestCase):
    """actions/criteria declares the plan as the explore job's matrix, its first value blank."""

    def run_action(self, text, most="1", entries="", changed=""):
        work = workdir(self)
        write(work, {"pr-body.md": text, "changed": changed, "github-output": ""})
        result = run_step(action_script("criteria"), work, {
            "GITHUB_ACTION_PATH": str(ROOT / "actions" / "criteria"), "GITHUB_OUTPUT": os.path.join(work, "github-output"),
            "RUNNER_TEMP": work, "VV_PATH": "pr-body.md", "VV_CHANGED": "changed" if changed else "", "VV_TICKET": "",
            "VV_MAX_SHARDS": most, "VV_MANIFEST": "", "VV_ENTRIES": entries})
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return result.stdout, outputs(os.path.join(work, "github-output"))

    def test_one_explorer_is_a_matrix_of_one_blank_value_so_the_job_keeps_its_name(self):
        # GitHub names a matrix job "<name> (<value>)" and leaves a blank value out (measured, 2026-10-08).
        stdout, declared = self.run_action(body("a", "b"))
        self.assertEqual((declared["count"], declared["shards"]), ("2", '[""]'))
        self.assertNotIn("::warning", stdout)

    def test_a_split_pull_request_is_one_matrix_value_an_explorer_and_the_warning_reaches_the_log(self):
        stdout, declared = self.run_action(THIRTEEN, most="3")
        self.assertEqual(declared["shards"], '["",2,3]')
        self.assertNotIn("::warning", stdout)
        stdout, declared = self.run_action(THIRTEEN, most="2")
        self.assertEqual(declared["shards"], '["",2]')
        self.assertIn("\n::warning title=vibe-verifier criteria::13 walks (each criterion once per role and width) for 2 explorers "
                      "sized for 12", stdout)

    def test_a_wrappers_gate_list_supplies_the_widths(self):
        self.assertEqual(self.run_action(body("a", "b", "c", "d"), most="3")[1]["shards"], '[""]')
        self.assertEqual(self.run_action(body("a", "b", "c", "d"), most="3", entries=QAE)[1]["shards"], '["",2]')

    def test_a_pull_request_with_nothing_to_walk_still_declares_a_matrix(self):
        _, declared = self.run_action("## Why\n\ndocs\n", most="3", changed="README.md\n")
        self.assertEqual((declared["count"], declared["shards"]), ("0", '[""]'))


class Share(unittest.TestCase):
    """actions/qae-inputs shares the criteria out, the same way in every explore job, tells each explorer
    its share and sizes its turn cap to it."""

    def run_action(self, files, shard, shards, manifest=QAE):
        repo = make_repo(self, {".vibe-verifier-qae": manifest})
        write(repo, files)
        output = os.path.join(repo, ".git", "github-output")
        Path(output).write_text("")
        result = run_step(action_script("qae-inputs"), repo, {
            "GITHUB_ACTION_PATH": str(ROOT / "actions" / "qae-inputs"), "GITHUB_OUTPUT": output,
            "RUNNER_TEMP": os.path.join(repo, ".git"), "VV_MANIFEST": ".vibe-verifier-qae", "VV_ENTRIES": "",
            "VV_REFERENCES": "qae-inputs/references", "VV_EVIDENCE": "qae-artifacts", "VV_SHARD": str(shard), "VV_SHARDS": str(shards)})
        return result, outputs(output), repo

    def shares(self, files, shards, manifest=QAE):
        """[(the criteria the explore job's log names, its turn cap)] for each explorer, each in a workspace of its own."""
        found = []
        for shard in range(1, shards + 1):
            result, declared, _ = self.run_action(files, shard, shards, manifest)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            share = re.search(r"^share: (.*) \(explorer %d of %d\)$" % (shard, shards), result.stdout, re.MULTILINE).group(1)
            self.assertEqual(declared["share"], SHARE % (share, shard, shards) if share != "no criterion" else NO_SHARE % (shard, shards))
            found.append((share, int(declared["max-turns"])))
        return found

    def test_each_explorer_is_named_its_share_and_sized_to_it(self):
        # Thirteen criteria at two widths, taken in turn: no criterion twice, none left out.
        self.assertEqual(self.shares({"qae-inputs/pr-body.md": THIRTEEN}, 4), [
            ("AC1, AC5, AC9, AC13", 240), ("AC2, AC6, AC10", 220), ("AC3, AC7, AC11", 220), ("AC4, AC8, AC12", 220)])

    def test_a_criterion_goes_whole_to_the_explorer_with_the_fewest_walks_and_a_tie_to_the_first(self):
        # Three roles at two widths are six walks of one criterion, which is never split between explorers.
        files = {"qae-inputs/pr-body.md": body("a [as: admin, rep, viewer]", "b", "c", "d", "e")}
        self.assertEqual([share for share, _ in self.shares(files, 2)], ["AC1, AC5", "AC2, AC3, AC4"])

    def test_the_tickets_criteria_are_shared_out_after_the_pull_requests(self):
        files = {"qae-inputs/pr-body.md": body("a", "b", "c", "d"), "qae-inputs/ticket.md": "- t1\n- t2 [as: admin, rep]\n- t3\n"}
        self.assertEqual([share for share, _ in self.shares(files, 2)], ["AC1, AC3, TC1, TC3", "AC2, AC4, TC2"])

    def test_one_explorer_walks_every_criterion_and_is_told_nothing(self):
        result, declared, _ = self.run_action({"qae-inputs/pr-body.md": body("a", "b")}, 1, 1)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual((declared["share"], declared["max-turns"]), ("", "160"))
        self.assertNotIn("share:", result.stdout)

    def test_the_first_explorer_starts_with_the_feature_re_walk_and_takes_fewer_criteria(self):
        # A selected feature with no walked step fails the gate, so the explorer that carries the re-walk
        # must reach it: its load starts at the re-walk's walks, counted as the turn cap counts them (one
        # a feature at three states each). Without the re-walk it had four criteria; with it, two.
        files = {"qae-inputs/pr-body.md": THIRTEEN}
        files.update({"qae-inputs/features/%s.md" % name: "# %s\n" % name for name in ("cart", "search", "sign-in")})
        files["qae-inputs/features.md"] = "# Features to re-walk\n"
        self.assertEqual(self.shares(files, 4), [("AC7, AC11", 240), ("AC1, AC4, AC8, AC12", 240), ("AC2, AC5, AC9, AC13", 240),
                                                 ("AC3, AC6, AC10", 220)])
        # Six states of each feature are two walks a feature: six walks before the first criterion.
        more = QAE.replace("--verdict b", "--verdict b --max-states 6")
        self.assertEqual([share for share, _ in self.shares(files, 4, more)], ["AC10", "AC1, AC4, AC7, AC11", "AC2, AC5, AC8, AC12",
                                                                                "AC3, AC6, AC9, AC13"])
        # Every explorer counts the features, to make the same assignment. Only the first is handed them.
        for shard, handed in ((1, True), (2, False)):
            _, _, repo = self.run_action(files, shard, 4)
            self.assertEqual(os.path.isdir(os.path.join(repo, "qae-inputs", "features")), handed)
            self.assertEqual(os.path.isfile(os.path.join(repo, "qae-inputs", "features.md")), handed)

    def test_an_explorer_whose_whole_load_is_the_re_walk_is_told_to_walk_no_criterion(self):
        # Two criteria, of two walks and of six, and three features: both criteria go to the second explorer.
        files = {"qae-inputs/pr-body.md": body("a", "b [as: admin, rep, viewer]"), "qae-inputs/features.md": "# Features to re-walk\n"}
        files.update({"qae-inputs/features/%s.md" % name: "# %s\n" % name for name in ("cart", "search", "sign-in")})
        self.assertEqual(self.shares(files, 2), [("no criterion", 130), ("AC1, AC2", 240)])

    def test_an_explorer_left_with_nothing_to_walk_fails_before_a_model_runs(self):
        # The criteria job counted two criteria and the body now lists one: it was edited between the jobs.
        result, declared, _ = self.run_action({"qae-inputs/pr-body.md": body("a")}, 2, 2)
        self.assertEqual(result.returncode, 2)
        self.assertIn("explorer 2 of 2 has no criterion to walk", result.stderr)
        self.assertEqual(declared, {})
        self.assertEqual(self.run_action({"qae-inputs/pr-body.md": body("a")}, 3, 2)[0].returncode, 2)


class Templates(unittest.TestCase):
    def parts(self, template):
        text = template.read_text()
        return (text[text.index("jobs:\n  criteria:\n"):text.index("\n  explore:\n")],
                text[text.index("\n  explore:\n"):text.index("\n  verify:\n")], text[text.index("\n  verify:\n"):])

    def test_the_explore_job_is_a_matrix_the_criteria_job_plans(self):
        for template in TEMPLATES:
            criteria, explore, _ = self.parts(template)
            self.assertIn("      shards: ${{ steps.criteria.outputs.shards }}\n", criteria)
            self.assertIn("          manifest: qae-inputs/manifest\n          max-shards: 1   # CONSUMER", criteria)
            self.assertIn("    strategy:\n      fail-fast: false\n      matrix:\n"
                          "        shard: ${{ fromJSON(needs.criteria.outputs.shards) }}\n", explore)
            # A fixed name: an expression there shows unevaluated on a skipped job (measured, 2026-10-08).
            self.assertIn("\n  explore:\n    name: qae-explore\n", explore)

    def test_each_explorer_is_given_its_number_and_counts_the_features_before_its_share(self):
        # Every explore job runs the selection, so all of them start the first explorer at the same
        # re-walk and make the same assignment. The references step then leaves the features to the first.
        for template in TEMPLATES:
            _, explore, _ = self.parts(template)
            step = explore[explore.index("- name: Prepare the explorer's references and widths"):explore.index("- name: Install the browser")]
            self.assertIn("        with:\n          shard: ${{ matrix.shard || 1 }}\n          shards: ${{ strategy.job-total }}\n", step)
            rewalk = explore[explore.index("- name: Select the features to re-walk"):explore.index("- uses: actions/setup-node@")]
            self.assertNotIn("        if:", rewalk)
            self.assertLess(explore.index("- name: Select the features to re-walk"), explore.index("- name: Prepare the explorer's references"))

    def test_the_share_reaches_the_prompt_of_both_lanes(self):
        self.assertIn("   post nothing, and stop.\n   SHARD_SHARE\n2. For each criterion", PROMPT.read_text())
        claude, codex = (template.read_text() for template in TEMPLATES)
        self.assertIn("post nothing, and stop.\n               ${{ steps.references.outputs.share }}\n", claude)
        self.assertIn("          SHARD_SHARE: ${{ steps.references.outputs.share }}\n", codex)
        script = step_script(codex, "- name: Write the explorer's prompt", "- name: Explore the acceptance criteria in a real browser")
        for share in (SHARE % ("AC1, AC4, TC2", 2, 3), ""):
            work = workdir(self)
            os.mkdir(os.path.join(work, "qae-inputs"))
            result = run_step(script, work, {"SITE_URL": "http://localhost:3000", "SHARD_SHARE": share})
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            written = Path(work, "qae-inputs", "prompt.md").read_text()
            self.assertIn("post nothing, and stop.\n     %s\n  2. For each criterion" % share, written)
            self.assertNotIn("SHARD_SHARE", written)

    def test_each_explorer_uploads_under_its_own_names_and_the_first_keeps_todays(self):
        past_the_first = "${{ matrix.shard && format('-{0}', matrix.shard) || '' }}\n"
        claude, codex = (self.parts(template)[1] for template in TEMPLATES)
        for explore in (claude, codex):
            self.assertIn("          name: qae-artifacts-${{ github.run_attempt }}" + past_the_first, explore)
        self.assertIn("          name: vv-usage-qae-explorer-${{ github.run_attempt }}" + past_the_first, claude)
        self.assertIn("          shard: ${{ matrix.shard }}\n", codex)
        self.assertIn("        name: vv-usage-qae-explorer-${{ github.run_attempt }}${{ inputs.shard && format('-{0}', inputs.shard) || '' }}\n",
                      (ROOT / "actions" / "qae-codex" / "action.yml").read_text())

    def test_the_criteria_job_reads_the_bases_manifest_and_the_heads_where_the_base_has_none(self):
        script = step_script(TEMPLATES[0].read_text(), "- name: Write the criteria inputs", "- name: Write the ticket's criteria")
        for at_base, expected in ((True, "base manifest\n"), (False, "head manifest\n")):
            work, bin_dir = workdir(self), workdir(self)
            Path(bin_dir, "gh").write_text('#!/bin/sh\ncase "$*" in\n  *ref=BASE*) %s ;;\n  *ref=HEAD*) echo "head manifest" ;;\n'
                                           '  *) echo x ;;\nesac\n' % ('echo "base manifest"' if at_base else 'echo "{}"; exit 1'))
            os.chmod(os.path.join(bin_dir, "gh"), 0o755)
            result = run_step(script, work, {"PATH": bin_dir + os.pathsep + os.environ["PATH"], "GH_TOKEN": "x", "PR_NUMBER": "7",
                                             "REPO": "o/r", "BASE_SHA": "BASE", "HEAD_SHA": "HEAD"})
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertEqual(Path(work, "qae-inputs", "manifest").read_text(), expected)

    def test_a_split_run_fails_its_verify_job_unless_every_explorers_job_succeeded(self):
        # No branch rule can name qae-explore (2): it exists only on a large pull request. The verify job
        # stands in for it, after the gates and before the review, and only when the run was split.
        required = "- name: Require every explorer of a split run"
        for template in TEMPLATES:
            _, _, verify = self.parts(template)
            self.assertLess(verify.index("- name: Run the QA gates"), verify.index(required))
            self.assertIn("        if: ${{ !cancelled() && needs.criteria.outputs.shards != '[\"\"]' && needs.explore.result != 'success' "
                          "&& needs.explore.result != 'skipped' }}\n", verify[verify.index(required):verify.index("- name: Post the QA review")])
        failed = run_step(step_script(TEMPLATES[0].read_text(), required, "- name: Post the QA review"), workdir(self), {"EXPLORE_RESULT": "failure"})
        self.assertEqual(failed.returncode, 1)
        self.assertIn("::error::an explorer's job did not succeed (the explore job ended failure)", failed.stdout)

    def test_the_verify_job_reads_no_pull_request_comment(self):
        # The verdict was the newest comment the workflow's identity had posted: after a close and reopen, one
        # run's verify job read another run's. It is the file this run's explorers left, and nothing else.
        for template in TEMPLATES:
            _, _, verify = self.parts(template)
            for gone in ("/comments", "jq -r", "github-actions[bot]"):
                self.assertNotIn(gone, verify)
            self.assertIn("          : > qae-inputs/verdict.md\n", verify)
            self.assertIn("          pattern: qae-artifacts-*\n          path: qae-evidence\n", verify)
            self.assertLess(verify.index("- uses: actions/download-artifact@"), verify.index(MERGE[0]))
            self.assertLess(verify.index(MERGE[0]), verify.index(MERGE[1]))


class Evidence(unittest.TestCase):
    """The explore job's step after the explorer, and the verify job's merge, on what explorers leave."""

    def setUp(self):
        self.repo = make_repo(self, {"README.md": "x\n"})
        self.verify = workdir(self)
        write(self.verify, {"qae-inputs/pr-body.md": body("The home page loads", "The cart shows its total")})

    def explore(self, criterion, shard, shards=2, attempt=1, **left):
        work = workdir(self)
        explorer_leaves(work, criterion, **left)
        kept = keep_evidence(work, shard, shards)
        self.assertEqual(kept.returncode, 0, kept.stdout + kept.stderr)
        hand_over(work, self.verify, attempt, shard)
        return work

    def verdicts(self):
        return re.findall(r"^acceptance-check: (\S+) -- (\S+)", Path(self.verify, "qae-inputs", "verdict.md").read_text(), re.MULTILINE)

    def gates(self):
        evidence, inputs = os.path.join(self.verify, "qae-artifacts"), os.path.join(self.verify, "qae-inputs")
        return (gate("acceptance-verdict", self.repo, "--criteria", os.path.join(inputs, "pr-body.md"), "--verdict",
                     os.path.join(inputs, "verdict.md"), "--artifacts", evidence),
                gate("qae-artifacts", self.repo, "--artifacts", evidence, "--criteria", os.path.join(inputs, "pr-body.md"),
                     "--site", "http://localhost:3000"))

    def test_one_explorer_keeps_every_name_it_wrote_and_its_verdict_is_copied(self):
        for template in TEMPLATES:
            work = workdir(self)
            explorer_leaves(work, "AC1")
            before = tree(os.path.join(work, "qae-artifacts"))
            kept = keep_evidence(work, template=template)
            self.assertEqual(kept.returncode, 0, kept.stdout + kept.stderr)
            self.assertEqual(tree(os.path.join(work, "qae-artifacts")), sorted(before + ["verdicts/1.md"]))
            self.assertEqual(Path(work, "qae-artifacts", "verdicts", "1.md").read_text(), Path(work, "qae-artifacts", "verdict.md").read_text())

    def test_two_explorers_evidence_merges_without_a_collision_and_passes_the_gates(self):
        self.explore("AC1", 1)
        second = self.explore("AC2", 2)
        merged = merge_evidence(self.verify, '["",2]')
        self.assertEqual(merged.returncode, 0, merged.stdout + merged.stderr)
        self.assertEqual(tree(os.path.join(self.verify, "qae-artifacts")), sorted(
            ["qae/AC1.md", "qae/AC1-step-1.png", "qae/AC2.md", "qae/AC2-step-1.png", "references/home.png", "verdicts/1.md", "verdicts/2.md"]
            + [name % shard for shard in (1, 2) for name in ("session-%d-1760000000000/session.md", "console-%d-2026-10-08T12-00-00-000Z.log",
                                                              "shard-%d/network.txt", "shard-%d/final.md", "shard-%d/server.log",
                                                              "shard-%d/verdict.md")]))
        self.assertEqual(self.verdicts(), [("AC1", "PASS"), ("AC2", "PASS")])
        # The session log names the files it saved by their paths, and still finds them.
        log = Path(second, "qae-artifacts", "session-2-1760000000000", "session.md").read_text()
        self.assertIn('"filename": "qae-artifacts/shard-2/network.txt"', log)
        self.assertIn("qae-artifacts/console-2-2026-10-08T12-00-00-000Z.log#L1", log)
        for result in self.gates():
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_a_500_in_one_explorers_saved_network_record_still_fails_the_merged_evidence(self):
        self.explore("AC1", 1)
        self.explore("AC2", 2, requests=CLEAN_REQUESTS + ["[POST] http://localhost:3000/api/cart => [500] Internal Server Error"])
        self.assertEqual(merge_evidence(self.verify, '["",2]').returncode, 0)
        verdict, artifacts = self.gates()
        self.assertEqual(verdict.returncode, 0, verdict.stdout + verdict.stderr)
        self.assertEqual(artifacts.returncode, 1, artifacts.stdout + artifacts.stderr)
        self.assertIn("shard-2/network.txt: request answered 500 outside the allowlist: [POST] http://localhost:3000/api/cart", artifacts.stdout)

    def test_every_explorers_page_record_reaches_the_one_file_the_gate_reads(self):
        # Each explorer followed a link to a hosted page that failed to reach its own analytics. The gate
        # reads which page logged an error from console-pages.jsonl, under that one name, so each
        # explorer's lines are appended to it and neither error is the site's.
        for shard, criterion in ((1, "AC1"), (2, "AC2")):
            work, error = workdir(self), BLOCKED.replace("id=1", "id=%d" % shard)
            explorer_leaves(work, criterion)
            write(work, {"qae-artifacts/" + CONSOLE: "[   -4ms] [ERROR] %s\n" % error,
                         "qae-artifacts/console-pages.jsonl": page_record(("https://pay.example/checkout", error))})
            self.assertEqual(keep_evidence(work, shard, 2).returncode, 0)
            hand_over(work, self.verify, 1, shard)
        self.assertEqual(merge_evidence(self.verify, '["",2]').returncode, 0)
        self.assertEqual(len(Path(self.verify, "qae-artifacts", "console-pages.jsonl").read_text().splitlines()), 2)
        _, artifacts = self.gates()
        self.assertEqual(artifacts.returncode, 0, artifacts.stdout + artifacts.stderr)

    def test_an_explorer_that_wrote_no_verdict_leaves_its_criteria_without_one(self):
        self.explore("AC1", 1)
        self.explore("AC2", 2, verdict="")
        merged = merge_evidence(self.verify, '["",2]')
        self.assertEqual(merged.returncode, 0, merged.stdout + merged.stderr)
        self.assertEqual(self.verdicts(), [("AC1", "PASS")])
        verdict, _ = self.gates()
        self.assertEqual(verdict.returncode, 1)
        self.assertIn("AC2 has no `acceptance-check: AC2` line", verdict.stdout)

    def test_a_path_two_explorers_both_left_is_refused_never_overwritten(self):
        # The second explorer walked a criterion of the first's share: two step logs, two verdict lines.
        self.explore("AC1", 1)
        work = workdir(self)
        explorer_leaves(work, "AC2")
        write(work, {"qae-artifacts/qae/AC1.md": "- step 1: opened /cart -> nothing\n"})
        self.assertEqual(keep_evidence(work, 2, 2).returncode, 0)
        hand_over(work, self.verify, 1, 2)
        merged = merge_evidence(self.verify, '["",2]')
        self.assertEqual(merged.returncode, 1)
        self.assertIn("::error::the evidence cannot be merged: explorers 1 and 2 both left qae/AC1.md", merged.stdout)

    def test_only_the_workflow_puts_a_verdict_in_an_explorers_name(self):
        work = workdir(self)
        explorer_leaves(work, "AC1")
        write(work, {"qae-artifacts/verdicts/2.md": "acceptance-check: AC2 -- PASS -- pasted (qae/AC1.md::step 1: opened /)\n"})
        self.assertEqual(keep_evidence(work, 1, 2).returncode, 0)
        self.assertEqual(os.listdir(os.path.join(work, "qae-artifacts", "verdicts")), ["1.md"])

    def test_each_explorer_is_judged_on_the_newest_attempt_it_ran_in(self):
        # A re-run of one failed explorer (measured, 2026-10-08): the verify job sees every attempt's
        # artifacts, the explore job reports the attempt of the explorer that ran again, and the others'
        # evidence is still the first attempt's.
        self.explore("AC1", 1)
        self.explore("AC2", 2, verdict="FAIL")
        self.explore("AC2", 2, attempt=2)
        self.explore("AC2", 3)
        merged = merge_evidence(self.verify, '["",2]', attempt="2")
        self.assertEqual(merged.returncode, 0, merged.stdout + merged.stderr)
        self.assertIn("explorer 1: qae-artifacts-1\nexplorer 2: qae-artifacts-2-2\n", merged.stdout)
        self.assertNotIn("explorer 3", merged.stdout)
        self.assertEqual(self.verdicts(), [("AC1", "PASS"), ("AC2", "PASS")])
        for result in self.gates():
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_an_attempt_that_left_no_evidence_is_refused(self):
        # As before the split: an explore job that reports an attempt and left nothing in it has nothing to judge.
        self.assertEqual(merge_evidence(self.verify).returncode, 1)
        self.explore("AC1", 1, shards=1)
        for reported in ("2", ""):
            merged = merge_evidence(self.verify, attempt=reported)
            self.assertEqual(merged.returncode, 1, reported)
            self.assertIn("::error::the explore job reported attempt %r and left no evidence in it" % reported, merged.stdout)
        self.assertEqual(merge_evidence(self.verify).returncode, 0)


if __name__ == "__main__":
    unittest.main()
