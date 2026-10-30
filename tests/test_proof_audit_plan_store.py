"""proof-audit and a plan kept in a plan store: a `[plan:<id>]` marker makes the plan's whole wording, amendments
included, requirements PL-1, AM1-1, ... (gates/_plan_store.py), a `Split off:` line gives some of them to another
owner (gates/_proof_requirements.py), and a plan the audit cannot read cannot run. The store is
tests/test_qae_plan_store.py's, served on localhost; the repository and the fake Codex CLI are
tests/test_proof_audit.py's."""
import json
import os
import re
import subprocess
import unittest

from helpers import ROOT, clean_env, commit, git, make_repo, runner, write

import _plan_store  # noqa: E402  (tests/helpers.py puts gates/ on the path)
import _proof_requirements  # noqa: E402
import test_proof_audit as base  # as modules, so their test classes are not collected twice
import test_proof_audit_unlinked as unlinked
import test_qae_plan_store as store

PLAN_ID = store.PLAN_ID
OTHER = "a8f1bf53-0a04-4d73-8ebd-57bffa15257d"
STORED = ("# Show prices in euros\n\nShoppers abroad see prices they can read.\n\n"                    # 1-3
          "## Why\n\nShoppers abroad cannot read dollar prices.\n- not a requirement\n\n"             # 5-8
          "### Evidence\n\n- left out with its parent\n\n"                                            # 10-12
          "## Scope\n\n- Prices show in euros on the shop page.\n  - Rounded to the cent.\n"          # 14-17
          "- The checkout total matches the cart,\n  tax included.\n"                                 # 18-19
          "- Out of scope: the invoice PDF.\n- **Out of scope:** the mobile app.\n\n"                 # 20-21
          "The currency switch is remembered per shopper.\nIt survives a reload.\n"                   # 23-24
          "```js\nconst notARequirement = 1;\n```\nThe switch sits in the header.\n\n"               # 25-28
          "**Out of scope**\n- Refund emails.\n\n**Added acceptance criteria**\n1. A refund shows in euros.\n\n"  # 30-34
          "## Repo baseline\n\n- src/prices.ts formats dollars.\n\n## Non-goals\n- Other currencies.\n")  # 36-41
AMENDMENTS = [store.AMENDED, store.PLAIN_AMENDMENT, {"body": "## Amendment 3\n\n```\nGET /prices?currency=EUR\n```\n"},
              {"body": "Out of scope: the PDF."}]
CUT = [("PL-1", 3, "Shoppers abroad see prices they can read."),
       ("PL-2", 16, "Prices show in euros on the shop page. Rounded to the cent."),
       ("PL-3", 18, "The checkout total matches the cart, tax included."),
       ("PL-4", 23, "The currency switch is remembered per shopper. It survives a reload."),
       ("PL-5", 28, "The switch sits in the header."),
       ("PL-6", 34, "A refund shows in euros."),
       ("AM1-1", 3, "Round to the cent."), ("AM1-2", 6, "A price of 1.005 shows as 1.01."),
       ("AM1-3", 7, "The PDF uses the same rounding."), ("AM2-1", 1, "Also show the currency code beside every total."),
       ("AM3-1", 1, "## Amendment 3 ``` GET /prices?currency=EUR ```")]
# A title that starts with an aside word, a bold sentence, a label, an HTML comment, and the other aside words.
EDGES = ("# Background sync for uploads\n<!-- a template's hint -->\nUploads sync in the background.\n\n"  # 1-3
         "**Prices never show in dollars.**\n\n**Context**\nThe old sync ran nightly.\n\nSync starts within a minute.\n\n"  # 5-10
         "## PROBLEM\n- left out\n## Problems\n- left out\n## Background\n- left out\n## Context\n- left out\n"  # 12-19
         "## Done when\n- The queue drains.\n")  # 20-21
EDGE_AMENDMENTS = [{"body": "## Out of scope\n\n- The PDF export.\n"}, {"body": None},
                   {"body": "<!-- a hint\n  over two lines -->\n- Totals round to the cent.\n"}]
# Where a label's block ends: not at a blank line before an item or an indented line, but at a paragraph after a
# code fence, and at a heading.
LABELLED = ("# Exports\n\n**Out of scope**\n- The PDF export,\n\n  and its email.\n\n- The CSV export.\n"  # 1-8
            "```\ncode\n```\nAfter the fence.\n\n**Non-goals**\n- Other formats.\n## Done when\n- Export works.\n")  # 9-17
TICKET_IDS = ["C-15", "C-8", "C-30"]
# tests/test_proof_audit.py's fake Codex, which also grades the requirements of a plan in a store.
FAKE = base.FAKE_CODEX.replace('" (docs/plan.md:" in line', '(" (docs/plan.md:" in line or " (plan:" in line)')


def proven(rid):
    """A grade the gate accepts: tests/test_proof_audit.py's quote, which is in its test file."""
    return base.grade(rid, "PROVEN", [base.CASE_15])


class PlanStoreAudit(unittest.TestCase):
    def setUp(self):
        store.PlanStore.setUp(self)  # the store, at self.endpoint, answering self.plans with the token "secret"
        base.Grader.setUp(self)  # the repository with tests/test_proof_audit.py's plan, test and Codex login
        write(self.work, {"codex": FAKE})
        self.plans[PLAN_ID] = {"planMarkdown": STORED, "amendments": AMENDMENTS}

    def audit(self, *args, body=base.BODY, title="Euro prices [plan:%s]" % PLAN_ID, token="secret", grades=None, env=None):
        if grades is not None:
            write(self.work, {"grades.json": json.dumps({g["id"]: g for g in grades})})
        write(self.work, {"body.md": body})
        env = dict({"FAKE_LOG": str(self.work / "calls.jsonl"), "FAKE_GRADES": str(self.work / "grades.json"),
                    "VV_PLANS_TOKEN": token}, **(env or {}))
        return runner("proof-audit", "--repo", self.repo, "--base-ref", "main", "--codex", str(self.work / "codex"),
                      "--codex-home", str(self.work / "home"), "--body", str(self.work / "body.md"), "--title=" + title,
                      "--plans-endpoint", self.endpoint, *args, env=env)

    def planned(self, *args, **options):
        result = self.audit("--plan", str(self.work / "plan.json"), *args, **options)
        cuts = json.loads((self.work / "plan.json").read_text())["shards"] if result.returncode == 0 else []
        return result, [item for cut in cuts for item in cut]

    def test_a_plans_wording_is_cut_into_requirements_with_stable_ids(self):
        plan = {"planMarkdown": STORED, "amendments": AMENDMENTS}
        items = _plan_store.plan_requirements([(PLAN_ID, plan)])
        self.assertEqual([(item["id"], item["line"], item["text"]) for item in items], CUT)
        self.assertEqual({item["plan"] for item in items}, {"plan:" + PLAN_ID})
        self.assertEqual((items[0]["heading"], items[1]["heading"], items[6]["section"], items[6]["heading"]),
                         ("Show prices in euros", "Scope", "plan:%s, amendment 1" % PLAN_ID, "Amendment 1: rounding"))
        self.assertTrue(all(re.fullmatch(_proof_requirements.ticket_coverage.ID, item["id"]) for item in items))
        # An amendment added later numbers after the others and renumbers none; a second plan's IDs are its own.
        later = dict(plan, amendments=AMENDMENTS + [{"body": "- Totals show the code too."}])
        self.assertEqual(_plan_store.plan_requirements([(PLAN_ID, later)])[:len(CUT)], items)
        self.assertEqual(_plan_store.plan_requirements([(PLAN_ID, later)])[-1]["id"], "AM5-1")
        two = _plan_store.plan_requirements([(PLAN_ID, plan), (OTHER, {"planMarkdown": "- One more.\n", "amendments": []})])
        self.assertEqual((two[-1]["id"], two[-1]["plan"]), ("P2PL-1", "plan:" + OTHER))

    def test_the_title_labels_comments_and_each_aside_word_are_read_as_written(self):
        items = _plan_store.plan_requirements([(PLAN_ID, {"planMarkdown": EDGES, "amendments": EDGE_AMENDMENTS})])
        self.assertEqual([(item["id"], item["line"], item["text"]) for item in items], [
            ("PL-1", 3, "Uploads sync in the background."), ("PL-2", 5, "**Prices never show in dollars.**"),
            ("PL-3", 10, "Sync starts within a minute."), ("PL-4", 21, "The queue drains."),
            ("AM3-1", 3, "Totals round to the cent.")])
        self.assertEqual((items[0]["heading"], items[-1]["heading"]), ("Background sync for uploads", "Amendment 3"))

    def test_a_labels_block_and_the_title_end_where_the_markdown_says(self):
        cut = _plan_store.plan_requirements([(PLAN_ID, {"planMarkdown": LABELLED, "amendments": []})])
        self.assertEqual([(item["line"], item["text"]) for item in cut], [(12, "After the fence."), (17, "Export works.")])
        # Only a `# ...` heading is the title: a first heading of another level that starts with an aside word is an aside.
        plain = "## Background\n- The old system ran nightly.\n## Done when\n- Export works.\n"
        cut = _plan_store.plan_requirements([(PLAN_ID, {"planMarkdown": plain, "amendments": []})])
        self.assertEqual([(item["line"], item["text"]) for item in cut], [(4, "Export works.")])
        # An HTML comment left open is text, so a fenced example of one, with no `-->` after it, keeps the plan after it.
        fenced = "# Plan\n\n```html\n<!-- an example\n```\n\n- Totals round.\n"
        cut = _plan_store.plan_requirements([(PLAN_ID, {"planMarkdown": fenced, "amendments": []})])
        self.assertEqual([(item["line"], item["text"]) for item in cut], [(7, "Totals round.")])

    def test_the_plan_step_reads_the_named_plan_after_the_tickets_and_leaves_split_ids_ungraded(self):
        result, items = self.planned(body=base.BODY + "\nSplit off: PL-5, AM1-3 -> #123\n")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("requirements: 12", result.stdout)
        kept = [rid for rid, _, _ in CUT if rid not in ("PL-5", "AM1-3")]
        self.assertEqual([item["id"] for item in items], TICKET_IDS + kept)
        # The Ticket section's requirements read as they always have.
        self.assertEqual([(item["plan"], item["line"], item["section"]) for item in items[:3]],
                         [("docs/plan.md", 5, "docs/plan.md#c-covers"), ("docs/plan.md", 6, "docs/plan.md#c-covers"),
                          ("docs/plan.md", 7, "docs/plan.md#c-covers")])
        self.assertEqual(self.seen, [("/api/v1/plans/%s" % PLAN_ID, "Bearer secret")])

    def test_the_model_is_given_the_plans_wording_and_never_the_split_lines(self):
        body = base.BODY + "\nSplit off: PL-5 -> https://github.com/Greenbauer/vibe-verifier/pull/9\n"
        grades = base.valid() + [proven(rid) for rid, _, _ in CUT]
        result = self.audit("--grades", str(self.work / "out"), body=body, grades=grades)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        # Two shards, graded at once: every prompt between them.
        prompt = "\n".join(json.loads(line)[-1] for line in (self.work / "calls.jsonl").read_text().splitlines())
        self.assertIn("- PL-2 (plan:%s:16): Prices show in euros on the shop page. Rounded to the cent." % PLAN_ID, prompt)
        self.assertIn('- plan:%s, amendment 1 (heading "Amendment 1: rounding")' % PLAN_ID, prompt)
        self.assertIn("A requirement whose plan is `plan:<id>` comes from a plan kept in a plan store", prompt)
        self.assertNotIn("PL-5", prompt)
        self.assertNotIn("Split off", prompt)
        self.assertIn("| PL-5 | SPLIT | split off to Greenbauer/vibe-verifier#9 |", result.stdout)
        self.assertIn("13 of 14 requirements proven or excluded, 1 split off", result.stdout)

    def test_split_off_lines_name_a_stored_requirement_and_its_owner(self):
        owners = ("Split off: PL-1 -> #12\nSplit off: PL-2, PL-3 -> https://github.com/Greenbauer/vibe-verifier/pull/9\n"
                  "split off: AM2-1 -> [plan:%s]\n```\nSplit off: PL-99 -> #1\n```\n" % OTHER)
        result, items = self.planned(body=owners)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertFalse({"PL-1", "PL-2", "PL-3", "AM2-1"} & {item["id"] for item in items}, items)
        self.assertEqual(len(items), len(CUT) - 4)
        # The owner's marker names another plan: the store is never asked for it.
        self.assertEqual([path for path, _ in self.seen], ["/api/v1/plans/%s" % PLAN_ID])
        for line, words in (("Split off: PL-99 -> #12", "no plan this pull request names in the plan store defines PL-99"),
                            ("Split off: C-8 -> #12", "goes to another section that carries [takes: C-8]"),
                            ("Split off: PL-1 -> issue 12", "is not a split-off line"),
                            ("Split off: PL-1 -> [plan:%s]" % PLAN_ID, "is this pull request or the plan it implements"),
                            ("Split off: PL-1 -> #1\nSplit off: PL-1 -> #2", "PL-1 is split off twice")):
            with self.subTest(line=line):
                result, _ = self.planned(body=base.BODY + line + "\n")
                self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
                self.assertIn(words, result.stderr)

    def test_a_plan_the_audit_cannot_read_cannot_run(self):
        self.plans[OTHER] = {"planMarkdown": "# Only why\n\n## Why\n\n- because\n\n## Out of scope\n\nAll else.\n",
                             "amendments": []}
        missing = "5d2e1d27-0000-4f5f-a5be-99f64acddd0a"
        for title, token, words in (("[plan:%s]" % missing, "secret", "the plan store answered 404"),
                                    ("[plan:%s]" % PLAN_ID, "", "no token for the plan store ($VV_PLANS_TOKEN)"),
                                    ("[plan:%s]" % PLAN_ID, "wrong", "the plan store answered 404"),
                                    ("[plan:%s]" % OTHER, "secret", "has no list item or paragraph to grade")):
            with self.subTest(words=words):
                result, _ = self.planned(body="Built it.\n", title=title, token=token)
                self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
                self.assertIn(words, result.stderr)
        write(self.work, {"body.md": "[plan:%s]\n" % PLAN_ID})
        result = runner("proof-audit", "--repo", self.repo, "--body", str(self.work / "body.md"), "--plans-endpoint",
                        "file:///etc/passwd", "--plan", str(self.work / "plan.json"), env={"VV_PLANS_TOKEN": "secret"})
        self.assertEqual(result.returncode, 2)
        self.assertIn("not an http(s) URL", result.stderr)
        # With no store given a marker is not read, and the run says so rather than grade nothing in silence.
        result = runner("proof-audit", "--repo", self.repo, "--body", str(self.work / "body.md"), "--plan",
                        str(self.work / "plan.json"), env={"VV_PLANS_TOKEN": "secret"})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("requirements: 0", result.stdout)
        self.assertIn("names plan %s and no plan store is given (--plans-endpoint)" % PLAN_ID, result.stdout)
        write(self.work, {"body.md": "[plan:%s]\nSplit off: PL-1 -> #3\n" % PLAN_ID})
        result = runner("proof-audit", "--repo", self.repo, "--body", str(self.work / "body.md"), "--plan",
                        str(self.work / "plan.json"))
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn("no plan was read: none is named with [plan:<id>], or no store is given", result.stderr)

    def test_an_id_a_ticket_section_and_a_stored_plan_both_define_cannot_run(self):
        self.repo = make_repo(self, {"docs/plan.md": "# Plan\n\n## A. Prices\n\n- Prices show. [req: PL-1]\n\n"
                                                     "### Acceptance criteria\n\n- Prices show [req: PL-1]\n"})
        git(self.repo, "checkout", "-q", "-b", "feat/prices")
        commit(self.repo, {"src/prices.ts": "export {};\n"})
        result, _ = self.planned(body="Ticket: docs/plan.md#a-prices\n")
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn("PL-1 is defined by docs/plan.md#a-prices and by plan:%s" % PLAN_ID, result.stderr)
        result, items = self.planned(body="Ticket: docs/plan.md#a-prices\n", title="")
        self.assertEqual((result.returncode, [item["id"] for item in items]), (0, ["PL-1"]), result.stderr)

    def test_the_actions_plan_step_passes_the_title_and_store_by_env(self):
        write(self.work, {"body.md": "Built it.\n", "output": ""})
        env = clean_env({"GITHUB_ACTION_PATH": str(ROOT / "actions" / "proof-audit"), "RUNNER_TEMP": str(self.work),
                         "GITHUB_OUTPUT": str(self.work / "output"), "VV_BODY": str(self.work / "body.md"),
                         "VV_MAX_IDS": "4", "VV_TITLE": "Euro prices [plan:%s]" % PLAN_ID,
                         "VV_PLANS_ENDPOINT": self.endpoint, "VV_PLANS_TOKEN": "secret", "VV_PR": ""})
        result = subprocess.run(["bash", "--noprofile", "--norc", "-eo", "pipefail", "-c",
                                 unlinked.action_step("- name: Plan the proof audit")], cwd=self.repo, env=env,
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        output = (self.work / "output").read_text()
        self.assertIn("count=%d\nshards=[1,2,3]\n" % len(CUT), output)
        self.assertNotIn("secret", output + result.stdout)
        self.assertTrue(os.path.exists(self.work / "proof-audit-plan.json"))
