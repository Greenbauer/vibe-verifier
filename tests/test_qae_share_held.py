"""One of several QAE explorers is held to its share by the step after it (harnesses/qae/README.md, "What each
explorer does"): actions/qae-inputs declares the share as its `share-ids` output, and the template's keep step sets
aside what the explorer left for any other criterion, so it never reaches the merge in the place of the explorer
whose share it is. Driven through the shipped action and the template's steps, run as the shell they are."""
import json
import os
import unittest
from pathlib import Path

import test_qae_shards as shards
from helpers import write
from test_qae_artifacts import session_saving_to
from test_qae_shards import CONSOLE, KEEP, SESSION, TEMPLATES, THIRTEEN, body, explorer_leaves, hand_over, keep_evidence, \
    merge_evidence, tree, workdir


class DeclaredShare(unittest.TestCase):
    """actions/qae-inputs declares the criteria an explorer walks as data, beside the sentence its prompt gets."""

    run_action = shards.Share.run_action

    def test_each_explorers_share_is_declared_as_the_log_names_it(self):
        for shard, expected in ((1, ["AC1", "AC5", "AC9", "AC13"]), (2, ["AC2", "AC6", "AC10"])):
            result, declared, _ = self.run_action({"qae-inputs/pr-body.md": THIRTEEN}, shard, 4)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn("share: %s (explorer %d of 4)" % (", ".join(expected), shard), result.stdout)
            self.assertEqual(json.loads(declared["share-ids"]), expected)

    def test_an_explorer_that_only_re_walks_the_features_is_declared_no_criterion(self):
        files = {"qae-inputs/pr-body.md": body("a", "b"), "qae-inputs/features.md": "# Features to re-walk\n"}
        files.update({"qae-inputs/features/%s.md" % name: "# %s\n" % name for name in ("cart", "search", "sign-in")})
        result, declared, _ = self.run_action(files, 1, 2)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(declared["share-ids"], "[]")

    def test_the_step_after_the_explorer_is_handed_the_declared_share(self):
        # A step output, which the explorer cannot change: qae-inputs/ is excused by the write scope.
        for template in TEMPLATES:
            text = template.read_text()
            keep = text[text.index(KEEP[0]):text.index(KEEP[1])]
            self.assertIn("          SHARE_IDS: ${{ steps.references.outputs.share-ids }}\n", keep)


class HeldToItsShare(unittest.TestCase):
    """The keep step and the verify job's merge on an explorer that walked a criterion outside its share."""

    setUp, explore, verdicts, gates = shards.Evidence.setUp, shards.Evidence.explore, shards.Evidence.verdicts, shards.Evidence.gates

    def test_a_criterion_walked_outside_the_explorers_share_is_set_aside_and_its_owner_judges_it(self):
        # A consumer's split run (2026-10-15): the explorer given AC1 and AC3 also walked TC1 and TC3, the shares of
        # two others. It and the explorer given TC3 both left qae/TC3-step-2.png, and the merge refused the run
        # although every criterion had a verdict from the explorer it was given to. The prompt's "walk only those"
        # is not what holds the share: the step after the explorer is.
        for template in TEMPLATES:
            self.setUp()
            self.explore("AC1", 1, template=template)
            work = workdir(self)
            explorer_leaves(work, "AC2")
            step = "step 1: opened /cart -> nothing"
            session = session_saving_to("qae-artifacts/qae/AC1-network.txt") + "- New console entries: qae-artifacts/%s#L1\n" % CONSOLE
            write(work, {"qae-artifacts/qae/AC1.md": "- %s\n" % step, "qae-artifacts/qae/AC1-step-1.png": "another png",
                         "qae-artifacts/qae/AC1-network.txt": "1. [GET] http://localhost:3000/cart => [200] OK\n",
                         "qae-artifacts/%s/session.md" % SESSION: session,
                         "qae-artifacts/verdict.md": "acceptance-check: AC2 -- PASS -- held (qae/AC2.md::step 1: opened / -> the heading)\n"
                                                     "- acceptance-check: ac1 -- FAIL -- not its own (qae/AC1.md::%s)\n" % step})
            kept = keep_evidence(work, 2, 2, template, share='["AC2"]')
            self.assertEqual(kept.returncode, 0, kept.stdout + kept.stderr)
            self.assertIn("::warning::explorer 2 walked AC1 outside its share (AC2): its evidence for them is kept under shard-2/qae/ "
                          "and its verdict lines for them are not counted", kept.stdout)
            evidence = os.path.join(work, "qae-artifacts")
            self.assertEqual(Path(evidence, "verdicts", "2.md").read_text(),
                             "acceptance-check: AC2 -- PASS -- held (qae/AC2.md::step 1: opened / -> the heading)\n")
            # What the explorer wrote stays whole beside its evidence; only the copy the verify job reads is held to the share.
            self.assertIn("ac1 -- FAIL", Path(evidence, "shard-2", "verdict.md").read_text())
            self.assertIn('"filename": "qae-artifacts/shard-2/qae/AC1-network.txt"',
                          Path(evidence, "session-2-1760000000000", "session.md").read_text())
            hand_over(work, self.verify, 1, 2)
            merged = merge_evidence(self.verify, '["",2]', template=template)
            self.assertEqual(merged.returncode, 0, merged.stdout + merged.stderr)
            self.assertEqual(self.verdicts(), [("AC1", "PASS"), ("AC2", "PASS")])
            merged_tree = tree(os.path.join(self.verify, "qae-artifacts"))
            for path in ("shard-2/qae/AC1.md", "shard-2/qae/AC1-step-1.png", "shard-2/qae/AC1-network.txt"):
                self.assertIn(path, merged_tree)
            self.assertEqual(Path(self.verify, "qae-artifacts", "qae", "AC1.md").read_text(), "- step 1: opened / -> the heading\n")
            for result in self.gates():
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_evidence_set_aside_is_still_judged(self):
        # Setting a walk aside takes its verdict line out of the count, never its requests out of the network gate.
        self.explore("AC1", 1)
        work = workdir(self)
        explorer_leaves(work, "AC2")
        write(work, {"qae-artifacts/qae/AC1-network.txt": "1. [POST] http://localhost:3000/api/cart => [500] Internal Server Error\n",
                     "qae-artifacts/%s/session.md" % SESSION: session_saving_to("qae-artifacts/qae/AC1-network.txt")})
        self.assertEqual(keep_evidence(work, 2, 2, share='["AC2"]').returncode, 0)
        hand_over(work, self.verify, 1, 2)
        self.assertEqual(merge_evidence(self.verify, '["",2]').returncode, 0)
        _, artifacts = self.gates()
        self.assertEqual(artifacts.returncode, 1, artifacts.stdout + artifacts.stderr)
        self.assertIn("shard-2/qae/AC1-network.txt: request answered 500 outside the allowlist: [POST] http://localhost:3000/api/cart",
                      artifacts.stdout)

    def test_an_explorer_with_no_criterion_keeps_no_criterions_evidence_or_line(self):
        # One of several whose whole load is the feature re-walk is told to walk no criterion.
        work = workdir(self)
        explorer_leaves(work, "AC2")
        kept = keep_evidence(work, 2, 2, share="[]")
        self.assertEqual(kept.returncode, 0, kept.stdout + kept.stderr)
        self.assertIn("::warning::explorer 2 walked AC2 outside its share (no criterion)", kept.stdout)
        evidence = os.path.join(work, "qae-artifacts")
        self.assertEqual(Path(evidence, "verdicts", "2.md").read_text(), "")
        self.assertEqual([name for name in tree(evidence) if name.startswith("qae/")], [])

    def test_a_split_explorer_whose_share_was_not_declared_keeps_nothing(self):
        # A workflow that copied this step without moving actions/qae-inputs to a commit that declares the share.
        for template in TEMPLATES:
            work = workdir(self)
            explorer_leaves(work, "AC1")
            write(work, {"qae-artifacts/verdicts/2.md": "acceptance-check: AC1 -- PASS -- pasted (qae/AC1.md::step 1: opened /)\n"})
            kept = keep_evidence(work, 2, 2, template)
            self.assertEqual(kept.returncode, 1)
            self.assertIn("::error::explorer 2 of 2 has no declared share", kept.stdout)
            self.assertFalse(os.path.isdir(os.path.join(work, "qae-artifacts", "verdicts")))


if __name__ == "__main__":
    unittest.main()
