# pstack and vibe-verifier: the differences, the evidence, and a plan to test them

pstack is Lauren Tan's Cursor skills plugin ([repo](https://github.com/cursor/plugins/tree/main/pstack)).
It puts verification inside the agent's own loop: a per-app verification skill with a feature map,
swarms of independent verifier agents, multi-model review, and principles such as "encode lessons
in structure" and "test behavior, not implementation". vibe-verifier puts verification outside the
agent: deterministic CI gates and two harnesses whose model output is never the verdict.

This document takes each difference in turn and gives:
- what the outside evidence says;
- what was measured on this repository;
- a verdict;
- how to test the change before shipping it;
- where it would land.

Evidence grades:
- **A:** measured, with a stated method and sample.
- **B:** numbers without a method, or self-reported by the party that benefits.
- **C:** opinion or anecdote.

Every external number links to a page that was opened.

## TLDR

The two systems agree on the core: don't trust the author's word, prove the change against the
real artifact, and turn a repeated mistake into a mechanism. They differ mostly in **what** gets
proven, and there pstack is ahead.

- **vibe-verifier proves the new behavior a PR's own criteria describe.** pstack also re-proves
  existing features against trunk. The evidence says regressions in existing behavior are the main
  way agents fail at feature work.
- **The deterministic gates catch hygiene, not rework.** Replaying 59 merged PRs, the security,
  workflow and ratchet gates fired on none of the 17 PRs a later fix rewrote.

The plan therefore starts with:
1. A regression lane in the QAE harness.
2. A test-quality signal.
3. A rework metric to judge both.

vibe-verifier should keep its own approach on three differences: deterministic verdicts, human
merge, and exact-head receipts.

## The differences

| # | Difference | pstack | vibe-verifier | Verdict |
|---|---|---|---|---|
| 1 | What gets checked | App-wide feature map; a regression run on trunk | Criteria in each PR body | Adopt, adapted |
| 2 | Test quality | "Would it pass if every import returned undefined?" | A test file exists | Adopt, adapted |
| 3 | Keeping patterns intact | Per-repo rules encoded as lints and types; judgment principles | Generic complexity and file-length ratchets | Adapt |
| 4 | Review | Several model families, consensus map | One Claude review against CLAUDE.md | Test first |
| 5 | Where checks run | Inside the agent's loop | In CI, after the push | Add a pre-push profile |
| 6 | Who decides | Independent agents' verdicts | Deterministic gates over artifacts | Keep vibe-verifier's |
| 7 | Rebases | Verdict survives a rebase with the same patch-id | Exact-head receipt; a rebase means a full review | Keep vibe-verifier's for now |
| 8 | Merging | The owning agent merges after a clean verdict | A human merges | Keep vibe-verifier's |

### 1. What gets checked: per-PR criteria vs a feature map with a regression run

**pstack:**
- `/create-verification-skill` writes a project skill with Launch, Doctor, Drive, Evidence and
  Cleanup sections.
- It adds a feature map: one file per user-facing feature, saying how to reach it and what end
  state proves it works.
- `/maintain-verification-skill` keeps the map current.
- Its autopilot runs the same scenario on trunk as a regression lane.

**vibe-verifier:** the QAE explorer walks only the `## Acceptance criteria` the PR author wrote,
which is the "new behavior works" half.

**Evidence:**
- **Agent feature work fails mostly by breaking what was there.** 73.6% of analyzed failures were
  "regressive implementation" ([FeatBench](https://arxiv.org/abs/2509.22237), A).
- **Knowing which tests a change touches helps; generic TDD instructions don't.** A code-to-test
  map cut the previously passing tests an agent broke from 6.08% to 1.82%. TDD instructions alone
  raised it to 9.94% ([TDAD](https://arxiv.org/html/2603.17973v1), A; small model, 100 instances).
- **Passing the tests is not the same as being mergeable.** Maintainers would not merge roughly
  half of the agent PRs that pass SWE-bench's tests. The automated grade ran 24.2 points above the
  merge decision ([METR](https://metr.org/notes/2026-03-10-many-swe-bench-passing-prs-would-not-be-merged-into-main/), A).
- **Agents game checks they can see.** GPT-5 cheated on 54.0% of impossible tasks, and hiding the
  tests cut that to near zero ([ImpossibleBench](https://arxiv.org/abs/2510.20270), A).
- **The QAE harness has this gap built in.** The author writes the criteria, so an author can pick
  easy ones. A feature map the author doesn't write closes that gap.

**Verdict: adopt, adapted to the gate contract.**
- The consumer keeps a feature map as a declared input.
- The explorer replays the features a diff touches on both the PR head and the base.
- The existing deterministic gates judge both runs.
- A feature that passes on the base and fails on the head is a regression finding.

**Test before shipping:** run an offline replay on one consumer.
1. Write a feature map for its top five features, each with the path globs it depends on.
2. Take its last 20 merged PRs.
3. Run the explorer on head and base for every feature whose globs the diff touches.
4. Compare each regression it reports with the follow-up fixes `pr_outcomes.py` finds for the same PRs.

| Outcome | Bar |
|---|---|
| Promote | At least one regression that a later fix PR actually repaired, and that the PR's own criteria never named, with no more than one false alarm per five PRs. |
| Kill | Zero real catches across the 20 PRs. |

**Implement:**
- `harnesses/qae/` gets a second site start at the base SHA, plus a prompt section for map-driven
  replay.
- A small grammar for feature files goes next to `gates/_acceptance.py`.
- A regression check goes in `qae-artifacts` or a sibling gate.
- **Cost:** each touched feature costs a second explorer pass, and the review account just hit its
  usage limit, so this needs capacity first.

### 2. Test quality: "a test file exists" vs "the test can fail"

**pstack:** keep a test only if it would fail when every imported function returned `undefined`.
Five shapes fail that check: weak or no assertion, mock-only, self-referential, constant pin, and
fixture-asserts-fixture.

**vibe-verifier:** `new-source-has-test` passes once a matching test filename or relative import
exists.

**Evidence:**
- **Most agent-written tests barely check anything.** Of 86,156 agent-written test-file patches,
  80.2% carried weak or no oracle signals ([2026](https://arxiv.org/abs/2606.18168), A).
- **The proven approach is diff-only mutation testing, shown as review comments.** Google mutates
  only changed lines, shows at most one mutant per line and seven per file, and never gates on a
  score. Its "not useful" rate fell from about 80% to about 15%
  ([paper](https://arxiv.org/abs/2102.11378), A; [blog](https://testing.googleblog.com/2021/04/mutation-testing.html)).

**Measured here** (`mutation_score.py` on the gates' own tests):
- **Scores:** 59% to 88% of single-operator mutants were killed, depending on the gate.
- **Most survivors were equivalent.** They changed only a message, or ended in the same exit 2.
- **Four were real gaps on documented rules**, each with a test added in this PR:
  - The artifact-escape test anchored to `../../etc/passwd:1`, which never parses as an anchor, so
    it passed without reaching the containment check. Flipping that check from `and` to `or` let a
    path outside `--artifacts` resolve, and every test still passed.
  - An anchor inside parentheses, the form the QAE prompt asks explorers to write, was untested.
  - An empty screenshot was untested.
  - A response of exactly 400 was untested.
- **After the new tests:** `_acceptance.py` rose from 32 to 37 of 43 mutants killed,
  `acceptance_verdict.py` from 29 to 30 of 38, and `qae_artifacts.py` from 53 to 55 of 60.
- **Open gaps** (not yet fixed): plain-list criteria, line counting without a trailing newline, an
  anchor to line 1, and the push-event branch name in `branch-name-length`.

**Verdict: adopt, in two pieces.**

**Piece 1: a tree-only check for test cases with no value assertion.** It fits the gate contract.
- **Test:** prototype it as a research script and run it over every subscribed repository's
  history. Hand-label 50 of its flags, and backtest it against the follow-up fixes.

| Outcome | Bar |
|---|---|
| Promote to a soak gate | Precision of at least 80%. |
| Kill | Precision under 60%. |

**Piece 2: diff-only mutation testing.** This runs the consumer's own tests, so it belongs in a
harness.
- Survivors are posted as findings, never as a score.
- **Test:** run `mutation_score.py`'s approach on the lines each PR changed, for the last 20 PRs of
  one consumer, and label the survivors.

| Outcome | Bar |
|---|---|
| Promote | At least 30% of surfaced survivors are real gaps (Google's mature rate is about 85% useful). |

### 3. Keeping patterns intact: generic ratchets vs rules encoded per repository

**pstack:**
- `/correct` fixes a repeated mistake at the highest level that works: architecture first, then
  types, then a lint whose error names the fix, then tests, then docs.
- Its principles cover the judgment calls: keep the reader's load low, prefer deep modules, and
  check inputs at the boundaries.
- It assumes agents copy the nearest example.

**vibe-verifier:** generic `cognitive-complexity` and `max-file-lines` ratchets that fail only when
a change adds more problems.

**Evidence:**
- **Written instructions fade within a session.** Compliance with a config-file rule fell about
  5.6% per function generated, over 1,650 Claude Code sessions
  ([McMillan](https://arxiv.org/abs/2605.10039), A).
- **Context files don't make agents more successful.** They did not raise task success and added
  over 20% to cost ([Gloaguen et al.](https://arxiv.org/abs/2602.11988), A).
- **Executable checks hold agents to rules better than prompts.** Compiling AGENTS.md rules into
  executable checks reached 88.3% constraint compliance, against 67.0% with the prompt alone
  ([ContextCov](https://arxiv.org/html/2603.00822v2), A; single author).
- **Agents copy what's already there, and lint messages can carry the fix.** OpenAI writes lint
  messages "to inject remediation instructions into agent context". It reports that Codex
  "replicates patterns that already exist in the repository, even uneven or suboptimal ones"
  ([OpenAI](https://openai.com/index/harness-engineering/), C).

**Measured here:**
- **The ratchets never fired on a PR that later needed a fix.**
- **PR size did predict it.** PRs over 100 changed lines needed a later fix 55% of the time, against
  a 29% base rate (12 of 17 caught, n=59 across two repositories, directional).
- **The agent eval** used 24 agents and four setups: no guidance, rules in AGENTS.md, a structural
  test, or cleaned-up examples.
  - All 24 followed the codebase's pattern on a repository small enough to read whole, whatever
    their setup.
  - They copied the nearest code in everything no rule covered. Next to three clean exports, 0 of 3
    quoted CSV values. In the structural-test arms, 6 of 6 did.

**Verdict: adapt.** Real boundary rules differ by codebase, so they live in each consumer, as
structural tests or lints whose failure message names the fix and whose baseline only shrinks. The
catalog's job:
- **Rewrite the ratchet messages** to state the location, the value, the limit and the fix.
  This is cheap.
- **Track PR size against rework**, using `pr_outcomes.py`, which already records size.
- **Ship a generic rules runner** (allowed import edges, path rules, each with a fix message,
  judged against the base) once three repositories carry a rules file, per the catalog's rule.

**Test before shipping:**
- **For PR size:** track it prospectively for a month on every subscribed repository.

| Outcome | Bar |
|---|---|
| Propose a soak gate | Flagged PRs keep needing fixes at twice the base rate or more. |
| Count as a failure | Splitting raises total rework per feature. |

- **For the rules runner:** write rules for two or three consumers, then count violations that
  historical PRs introduced.
- **For pattern drift at scale:** rerun the agent eval on a repository too large to read whole,
  using the fleet's own models. A setup separates the mechanisms only when its "no guidance" arm
  breaks the pattern.

### 4. Review: one model vs a panel

**pstack:**
- `/interrogate` sends the same rubric to Claude, GPT and Grok reviewers, then sorts findings into
  act on, consider, noted and dismissed, with an agreement map.

**vibe-verifier:**
- One Claude review against the consumer's CLAUDE.md.
- A receipt keyed to the head; unresolved threads block.

**Evidence:**
- **A second independent reviewer raises defect coverage**, from about 47% to about 72%, with
  diminishing returns after that. The author recommends "a small panel of two to three"
  ([Stone](https://zenodo.org/records/21328807), A; one organization, preprint).
- **Large PRs are where reviewers miss most.** AI reviewers' recall falls to 30% on PRs over 1,000
  lines ([Martian](https://withmartian.com/post/measuring-the-software-factorys-inspection-line), A).

**Constraint found today:** both review runs on this PR failed with "You've hit your limit · resets
Oct 6, 9am (UTC)". Review capacity, not model choice, is the current bottleneck.

**Verdict: test first.** Add a second reviewer from a different model family only on PRs above a
size threshold. The Codex lane the QAE harness already runs on a self-hosted runner shows how.

**Test before shipping:**
1. Replay the last 20 PRs over the threshold through both reviewers offline.
2. Hand-label which second-reviewer findings are valid and unique.

| Outcome | Bar |
|---|---|
| Promote | At least one valid, unique finding per five large PRs. |
| Track after launch | How often each reviewer's findings lead to a code change before merge. |

**Implement:** a second review job in `harnesses/review/`. The receipt would then name both
reviewers.

### 5. Where checks run: inside the agent's loop vs in CI

**pstack:** the agent runs its checks before it reports or pushes.

**vibe-verifier:** gates run in CI after the push. They also run on a laptop, but nothing wires
that up.

**Evidence:** Stripe enforces "any lint step that would fail in CI" on push, in under five seconds.
It allows at most two CI rounds before a human takes over
([Stripe](https://stripe.dev/blog/minions-stripes-one-shot-end-to-end-coding-agents-part-2), B).

**Verdict:** add a documented pre-push profile that runs the same manifest locally. The gate
contract already guarantees the same result on a laptop as in CI.

**Test before shipping:**
- Measure pushes per PR and failed CI rounds per PR on one fleet repository, for two weeks before
  and two weeks after the hook.

| Outcome | Bar |
|---|---|
| Keep | Fewer CI rounds with no rise in rework. |

**Implement:** a short section in `docs/GATE-CONTRACT.md` and a hook script under `bin/`.

### 6. Who decides: independent agents vs deterministic gates

**pstack:** "safe means a verdict from an agent that did not write the code". A swarm returns
PASS, ISSUES or BLOCKED with evidence.

**vibe-verifier:** "its prose is never the verdict". Gates refuse on structural facts.

**Evidence:**
- **Model verdicts aren't repeatable.** On this harness's pilot, the explorer passed a console 404
  on one run and failed it on the next (`harnesses/qae/README.md`).
- **The best-known LLM judge in production is unevaluated.** Spotify's judge vetoes about a quarter
  of sessions, and Spotify has "yet to invest in evals for our judge"
  ([Spotify](https://engineering.atspotify.com/2025/12/feedback-loops-background-coding-agents-part-3), B).

**Verdict: keep vibe-verifier's approach.** Its independence rule already holds: the explorer runs
under the workflow's own identity, not the author's. The part worth borrowing, a checked set the
author doesn't choose, comes through difference 1.

### 7. Rebases: patch-id carry-over vs exact-head receipts

**pstack:** records the verdict's head, base and `git patch-id`, so a pure rebase keeps the verdict
while CI reruns.

**vibe-verifier:** `review.yml` runs a full review whenever the last receipt is no longer an
ancestor of the head.

**Evidence:**
- **There is precedent, but no outcome data.** Gerrit can copy an approval to a new patch set on a
  trivial rebase ([Gerrit docs](https://gerrit-review.googlesource.com/Documentation/config-labels.html)).
- **Measured here:** 39 review receipts on this repository: 30 full, 8 delta, 1 nochange. No PR
  needed a second full review, so carry-over would have saved nothing.

**Verdict: keep exact-head receipts.**
- **When to revisit:** if a consumer shows rebase-forced full reviews in its receipts.
- **The fix:** compare the patch-id of the old receipt's diff with the current one in the scope step.

### 8. Merging: the agent merges vs a human merges

**pstack:** each PR's owning agent merges after the root's clean swarm verdict.
**OpenAI:** reports minimal blocking gates and agents merging their own PRs
([OpenAI](https://openai.com/index/harness-engineering/), C).

**Evidence:** across 182 repositories, each 10-point rise in a project's no-review rate went with
about 6% more maintenance burden on agent code. Agent lines were changed by bug fixes more often:
4.0% against 2.7% by 180 days ([2026](https://arxiv.org/abs/2607.09902), A).

**Verdict: keep human merge,** and keep the merge guard on stubs and manifests. No published study
measures agent self-merge outcomes.

## The plan

Each step ships as its own PR, with its experiment run before the change it would justify.

| Step | What | Depends on | Catalog change | Ships when |
|---|---|---|---|---|
| 1 | Run `pr_outcomes.py` monthly on every subscribed repository. Add a `Fixes: #N` line to fix PR bodies so links can be verified. | nothing | none | now |
| 2 | Rewrite the ratchet gates' failure messages to name the fix. | nothing | message text only | now |
| 3 | Prototype the test-oracle check as a research script; hand-label its flags. | step 1 for the backtest | none | precision of at least 80% |
| 4 | Offline regression-lane replay on one consumer (difference 1). | a feature map written for that consumer; review capacity | none | at least one real catch in 20 PRs |
| 5 | Regression lane in the QAE harness. | step 4 passing | `harnesses/qae/`, a new declared input, a regression check | after step 4 |
| 6 | Pre-push profile, with pushes per PR measured before and after. | nothing | `bin/`, docs | fewer CI rounds |
| 7 | Second reviewer on large PRs, replayed offline first. | a second model account; review capacity | `harnesses/review/` | one valid, unique finding per five large PRs |
| 8 | Promote PR size and test-oracle to soak gates, and add a rules runner. | three repositories that want each one | new gates | the catalog rule |

### Choices for the operator

- **Which consumer pilots step 4.** zack.land is public and already runs the QAE harness, but it has
  had almost no product PRs since it subscribed. The busiest private consumer gives a real signal,
  but its history has to be read from the operator's machine, because this agent's GitHub access
  reaches public repositories only.
- **Model capacity.** The review account's limit blocks steps 4 and 7 until there is more of it.

## How the numbers here were produced

All four experiments use only public history and rerun with the scripts in this directory (see
[README.md](README.md)).

**1. Outcome baseline** (`pr_outcomes.py`, 14-day window):

| Repository | Merged PRs | Fix PRs | PRs a later fix rewrote |
|---|---|---|---|
| vibe-verifier | 30 | 15 | 11 of 24 |
| zack.land | 43 | 11 | 6 of 35 |

- **Speed:** median time from open to merge was about 7 minutes in both.
- **Size:** PRs a later fix rewrote had a median of 297 changed lines in vibe-verifier against 53
  for the rest, and 126 against 39 in zack.land.

**2. Gate backtest** (`gate_backtest.py`, 59 PRs, 17 of them later fixed). Today's gates:

| Gate | Later-fixed PRs flagged | Other PRs flagged |
|---|---|---|
| gitleaks, actionlint, zizmor, cognitive-complexity, max-file-lines | 0 | 0 |
| new-source-has-test | 3 | 4 |

Size thresholds:

| Threshold | Later-fixed PRs flagged | Precision |
|---|---|---|
| Over 100 lines | 12 of 17 | 55% |
| Over 200 lines | 9 of 17 | 56% |
| Over 5 files | 10 of 17 | 59% |

**3. Mutation scores** (`mutation_score.py`): see difference 2.

**4. Agent eval:** the same task in four setups, two rounds, three agents each, scored by a checker
the agents never saw. See difference 3.

**Limits:**
- **SZZ labels are noisy.** (SZZ links a fix to the earlier change it repairs by blaming the lines
  the fix touched.) On Apache projects only about half of the commits SZZ labels as fixes were
  fixes ([Herbold et al.](https://link.springer.com/article/10.1007/s10664-021-10092-4), A).
- **Bigger PRs get blamed more by chance**, because they own more lines.
- **The sample is small:** two histories, one author.
- **The eval had a ceiling effect:** it used the parent model on a repository small enough to read
  whole.
- **pstack's own figures are self-reported.** Its PR counts (2,500 a month, median +177/-29) come
  from the author's posts on X.
