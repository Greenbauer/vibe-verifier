"""Explorers of one run never share a record (harnesses/qae/README.md, "Criteria that build on each other").

A consumer's pull request listed seven criteria in which each worked on the record the one before it created
("on the profile from item 4"). Split across three explorers on one preview, the explorer given the fifth had no
record of its own, opened the one another explorer had created from a list both could see, and snapshotted it
while that explorer still had the fourth criterion's change on it: it failed a correct criterion on what it saw.
So criteria that name each other stay with one explorer, and each of several explorers is given a tag it names
its records by and keeps to."""
import os
import unittest
from pathlib import Path

import test_qae_shards as shards
from helpers import ROOT, make_repo, runner, write

# The consumer's case, described generically: each criterion builds on the record of the one before it, and
# the last reads the record the second created.
SEVEN = shards.body(
    "Open New and choose the mode",
    "On the form from item 1, name the record and save it",
    "On the record from item 2, change the cost and save",
    "On the record from item 3, add a second line and save, then delete it",
    "On the record from item 4, take a snapshot: its total is the first line's",
    "On the snapshot from item 5, download the PDF",
    "As a viewer, open the record from item 2: nothing is editable")
ISOLATION = ("Your tag is {tag}. Other explorers create records on this site at the same time and change them while you look: "
             "never open, change or judge a record another explorer made. Every record you create (a profile, a quote, an "
             "order, anything with a name or a title) carries your tag: where a criterion gives the name, write that name "
             "followed by a space and {tag}, and it is still the name the criterion means. Open, change, snapshot or judge "
             "only records that carry your tag, or records a criterion names that existed before this run (a seeded "
             "customer, a fixture). Where a criterion would have you pick any record from a list, pick one with your tag or "
             "one no explorer made; never one whose name ends in another explorer's tag (qae-<run>-e<number>). When a "
             "criterion of yours builds on a record that a criterion outside your share creates, create your own first, by "
             "that criterion's steps, with your tag.")
# Three criteria that stand alone and a chain of three, named three ways.
MIXED = shards.body("a", "b", "c", "Create a record named R", "On the record from item 4, edit it", "After AC5, delete it")


class Plan(unittest.TestCase):
    plan = shards.Plan.plan

    def test_chained_criteria_are_one_explorers_however_many_walks_they_are(self):
        # Fourteen walks at two widths were three explorers; chained, they are one, and the plan says the chain
        # is more than one explorer is sized for.
        self.assertEqual(self.plan(SEVEN, 3, manifest=shards.QAE), [
            "shards: [1]", "walks: 14", "chain: AC1, AC2, AC3, AC4, AC5, AC6, AC7 (14 walks, one explorer)",
            "::warning title=vibe-verifier criteria::14 walks (each criterion once per role and width) for 1 explorer sized "
            "for 6, and one of them walks 14: expect criteria left unfinished. List fewer criteria on one pull request, chain "
            "fewer of them, or raise max-shards (harnesses/qae/README.md)."])

    def test_a_chain_counts_as_one_toward_how_many_explorers_there_are(self):
        # Twelve walks are two explorers' worth, and four chains (three lone criteria and one of three) allow two.
        self.assertEqual(self.plan(MIXED, 3, manifest=shards.QAE),
                         ["shards: [1,2]", "walks: 12", "chain: AC4, AC5, AC6 (6 walks, one explorer)"])

    def test_the_after_marker_is_no_annotation_the_gates_refuse(self):
        work = shards.workdir(self)
        write(work, {"pr-body.md": shards.body("a", "b [after: AC1]")})
        result = runner("criteria", os.path.join(work, "pr-body.md"), "--max-shards", "2", "--repo", work)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertNotIn("malformed", result.stdout)
        self.assertIn("chain: AC1, AC2 (2 walks, one explorer)", result.stdout)

    def test_how_a_criterion_names_another(self):
        cases = [
            # An item or criterion number counts in its own list, a list of them names each, and the case of the word
            # does not matter.
            (shards.body("a", "b", "c", "Compare criteria 1 and 3"), [], ["chain: AC1, AC3, AC4 (3 walks, one explorer)"]),
            (shards.body("a", "b", "Item 1, again", "c"), [], ["chain: AC1, AC3 (2 walks, one explorer)"]),
            (shards.body("a", "b", "c", "items 2, 3 or 4"), [], ["chain: AC2, AC3, AC4 (3 walks, one explorer)"]),
            # A ticket's "item 1" is its TC1; ACn and TCn name a criterion of either list outright.
            (shards.body("a", "b"), "- t1\n- on the record from item 1\n- after AC2\n",
             ["chain: AC2, TC3 (2 walks, one explorer)", "chain: TC1, TC2 (2 walks, one explorer)"]),
            # The explicit marker: `[after: ...]` is read as the ids it names.
            (shards.body("a", "b", "c [after: AC1, AC2]", "d"), [], ["chain: AC1, AC2, AC3 (3 walks, one explorer)"]),
            # A number past the list, a word that only ends in TC or AC, and a bare number name nothing.
            (shards.body("a", "b", "item 9, AC12 and TC1", "at 14:05 UTC1 set the cost to 2"), [], []),
        ]
        for text, ticket, chains in cases:
            with self.subTest(text=text, ticket=ticket):
                lines = self.plan(text, 4, ticket=ticket or "")
                self.assertEqual([line for line in lines if line.startswith("chain")], chains)


class Share(unittest.TestCase):
    run_action = shards.Share.run_action
    shares = shards.Share.shares

    def test_a_chain_goes_whole_to_one_explorer_which_is_told_to_walk_it_in_order(self):
        found = self.shares({"qae-inputs/pr-body.md": MIXED}, 2)
        self.assertEqual(found, [("AC4, AC5, AC6", 220), ("AC1, AC2, AC3", 220)])
        result, declared, _ = self.run_action({"qae-inputs/pr-body.md": MIXED}, 1, 2)
        self.assertIn(" AC4, AC5, AC6 name each other by number, so they may build on each other: walk them in that order, and where a "
                      "later one works on a record an earlier one made, use that record. ", declared["share"])
        self.assertNotIn("name each other", self.run_action({"qae-inputs/pr-body.md": MIXED}, 2, 2)[1]["share"])

    def test_the_seven_chained_criteria_stay_together_whatever_the_matrix(self):
        # Even a matrix planned before the chain was known (three explorers) leaves the chain whole: the first
        # explorer walks it, and the others have nothing to walk, so they fail before a model runs.
        result, declared, _ = self.run_action({"qae-inputs/pr-body.md": SEVEN}, 1, 3)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("share: AC1, AC2, AC3, AC4, AC5, AC6, AC7 (explorer 1 of 3)", result.stdout)
        self.assertEqual(self.run_action({"qae-inputs/pr-body.md": SEVEN}, 2, 3)[0].returncode, 2)

    def test_each_explorer_and_each_attempt_has_a_tag_of_its_own(self):
        tags = set()
        for run in ("38600000000.1", "38600000000.2"):
            for shard in (1, 2):
                result, declared, _ = self.run_action({"qae-inputs/pr-body.md": MIXED}, shard, 2, run=run)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                tag = "qae-%s-e%d" % (run, shard)
                self.assertTrue(declared["share"].endswith(" " + ISOLATION.format(tag=tag)), declared["share"])
                tags.add(tag)
        self.assertEqual(len(tags), 4)
        # One explorer shares the site with no other in its run: it is told nothing, and gets no tag.
        self.assertEqual(self.run_action({"qae-inputs/pr-body.md": MIXED}, 1, 1)[1]["share"], "")

    def test_without_a_run_the_tag_is_the_explorers_number(self):
        repo = make_repo(self, {".vibe-verifier-qae": shards.QAE})
        write(repo, {"qae-inputs/pr-body.md": MIXED})
        share = os.path.join(repo, ".git", "share")
        result = runner("qae-inputs", "--repo", repo, "--manifest", os.path.join(repo, ".vibe-verifier-qae"),
                        "--criteria", os.path.join(repo, "qae-inputs", "pr-body.md"), "--turns", os.path.join(repo, ".git", "turns"),
                        "--references", os.path.join(repo, "qae-inputs", "references"), "--evidence", os.path.join(repo, "qae-artifacts"),
                        "--widths-file", os.path.join(repo, "qae-inputs", "widths"), "--shard", "2", "--shards", "2", "--share", share)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("Your tag is qae-e2. ", Path(share).read_text())

    def test_the_action_names_the_run_and_its_attempt(self):
        action = (ROOT / "actions" / "qae-inputs" / "action.yml").read_text()
        self.assertIn("        VV_RUN: ${{ github.run_id }}.${{ github.run_attempt }}\n", action)
        self.assertIn('--run "${VV_RUN:-}"', action)
