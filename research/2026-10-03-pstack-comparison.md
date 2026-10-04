# pstack and Vibe Verifier: source comparison and measured limits

Updated 2026-10-04. **Adopt the existing behavior-testing pattern; do not add a new
blocking gate from these experiments.** pstack supplies agent operating practices;
Vibe Verifier supplies executable gates and review/explorer harnesses. Neither
source code nor retrospective repair examples establish which system ships faster
or causes fewer defects.

The pstack sources below are pinned to
[`e43c7ee`](https://github.com/cursor/plugins/tree/e43c7ee26e0038c6c1fa8380dd34ce86ff94cb2a/pstack).
The catalog comparison uses the implementations linked below, including the
review harness's explicit `limited` receipt exception. Private consumer histories
and their evidence remain outside this public repository.

## Eight differences, checked against implementations

### 1. Choosing what to verify

pstack's [verification-skill generator](https://github.com/cursor/plugins/blob/e43c7ee26e0038c6c1fa8380dd34ce86ff94cb2a/pstack/skills/create-verification-skill/SKILL.md)
asks for a feature map and says to reuse existing test infrastructure. Its
[autopilot](https://github.com/cursor/plugins/blob/e43c7ee26e0038c6c1fa8380dd34ce86ff94cb2a/pstack/skills/poteto-mode/playbooks/autopilot-full.md)
says, “Run the same load-bearing scenario on current trunk.” It treats a feature
absent from trunk separately instead of pretending to have a passing baseline.

The catalog [QAE prompt](../harnesses/qae/prompt.md) starts from the PR body's
acceptance list. [acceptance-verdict](../gates/acceptance_verdict.py) explicitly
says, “The gate never judges whether the evidence covers the criterion's meaning”.
An anchored PASS is a structural floor, not proof of correct requirements or behavior.

**Adapt:** keep an independently sourced inventory of existing user behavior and
run relevant existing tests against base and head. Prefer the consumer's actual
harness over a new explorer. A base failure is an existing defect, an unsupported
base is unmeasured, and only a passing base followed by a failing head supports
an introduced regression. A hindsight-selected fixture demonstrates sensitivity
to that defect; it does not prove an agent would have discovered the requirement.

### 2. Test presence versus test sensitivity

pstack's [testing principle](https://github.com/cursor/plugins/blob/e43c7ee26e0038c6c1fa8380dd34ce86ff94cb2a/pstack/skills/principle-test-behavior-not-implementation/SKILL.md)
says to “call the subject inside the test body” and proposes an undefined-return
thought experiment. The catalog [new-source-has-test](../gates/new_source_has_test.py)
uses matching names or relative imports for added JavaScript/TypeScript files.
Neither filename matching nor a regex over assertions proves test sensitivity.
A null result or a forbidden side effect can be the exact required behavior;
blanket labels for those assertions are unsound.

**Adapt the principle, reject the lexical gate:** use the real production function
and show that a concrete defective variant fails while its repaired variant and
negative controls pass. Do not promote [test_oracles.py](test_oracles.py): its
`strong`, `weak`, and `none` fields are lexical labels retained for reproducing
this experiment, not grades. It misses multiline matchers, delegated assertions,
SQL tests, and copied-production-code tests with apparently strong assertions.

This PR adds or strengthens four executable catalog examples:

- [Artifact containment](../tests/test_acceptance.py): the outside file really
  exists and the anchor parses, so rejecting it exercises containment.
- Parenthesized evidence anchors: the closing parenthesis is not part of the title.
- [Empty screenshots](../tests/test_qae_artifacts.py): an empty file is missing evidence.
- HTTP 400 is rejected while 399 is not classified as an error.

[Mutation testing](mutation_score.py) is an optional way to find candidates for
such examples. Read survivors individually; equivalent mutants are not missing
requirements. Empty test selections and timeouts are unmeasured, and the source
and test suite come from the same committed HEAD.

### 3. Preserving repository patterns

pstack's [correct skill](https://github.com/cursor/plugins/blob/e43c7ee26e0038c6c1fa8380dd34ce86ff94cb2a/pstack/skills/correct/SKILL.md)
says, “A class counts once it has happened twice.” It prioritizes architecture,
types, lint, tests, then prose. The catalog's
[complexity](../gates/cognitive_complexity.py) and
[file-length](../gates/max_file_lines.py) ratchets instead measure generic bounds;
they cannot determine whether a domain rule is preserved.

**Use existing enforcement:** identify a repeated actual defect, extend the
consumer's current type/lint/test boundary, and reproduce the defect before
calling the rule effective. No generic rules runner or failure-message rewrite
is justified merely because the comparison can imagine one. A small synthetic
agent task does not establish pattern preservation on a large application.

### 4. One reviewer versus multiple model families

pstack's [interrogate skill](https://github.com/cursor/plugins/blob/e43c7ee26e0038c6c1fa8380dd34ce86ff94cb2a/pstack/skills/interrogate/SKILL.md)
uses the same rubric across configured model reviewers, then adjudicates their
findings. Agreement is not itself truth. The catalog
[review harness](../harnesses/review/README.md) runs one reviewer and checks a
workflow-authored receipt plus unresolved threads.

**Defer an extra review job:** this work contains no blinded, matched comparison
of unique valid findings, false alarms, elapsed time, or cost. Reviewing this
research plan with another model is a critique of the plan, not that experiment.
A future test should hide repair diffs, freeze the rubric and same PR heads,
and manually adjudicate both unique and shared findings before choosing a model
count or size threshold.

### 5. Earlier feedback versus CI feedback

pstack asks agents to prove work during their own loop. The catalog's
[runner](../bin/vibe-verifier) already runs locally as well as in CI, with
[declared inputs](../docs/GATE-CONTRACT.md). This is not an inside-agent versus
CI-only divide. A tree-only gate may run early; a gate requiring receipts or a
live preview cannot be replaced with a local empty placeholder.

**Keep the existing runner:** document and reuse a consumer's available local
checks. Do not add a second profile/hook until a replay of actual failing CI heads
shows an avoidable round trip. Reproducibility requires the same inputs, tool
versions, and supported runtime; a contract alone does not make macOS and Linux
identical. Shipping-efficiency gains remain unmeasured here.

### 6. Model judgments versus structural verdicts

pstack's [autopilot verification step](https://github.com/cursor/plugins/blob/e43c7ee26e0038c6c1fa8380dd34ce86ff94cb2a/pstack/skills/poteto-mode/playbooks/autopilot-full.md)
requires independent verification lanes. The catalog separates model exploration
from deterministic artifact checks. Those checks can require evidence files,
anchors, and receipts; they cannot prove that the scenario was complete or its
expected outcome correct.

**Keep the separation**, and audit the content and real behavior as well as its
receipt. The current review harness can accept an explicitly `limited` receipt
when the provider's limit is proven. That means no model review occurred, not
that a review passed; the workflow documents this exception and requests a later
real review. This comparison does not change that policy.

### 7. Reusing a review after rebasing

pstack's [shipping playbook](https://github.com/cursor/plugins/blob/e43c7ee26e0038c6c1fa8380dd34ce86ff94cb2a/pstack/skills/poteto-mode/playbooks/shipping.md)
allows conditional patch-id reuse. The catalog's scope step uses receipt ancestry
and changed paths to select full, delta, or nochange review; receipts still name
the exact current head. Non-ancestor receipts require full review.

**Keep current behavior:** identical patches can run against changed dependencies
or base code. No measured sample here establishes either meaningful saved review
cost or safe broader reuse. Revisit only with replayed dependency-sensitive cases
and measured unnecessary reviews.

### 8. Who merges

pstack's autopilot permits owner merges under an explicit autonomy grant and
root verdict, while operator-named items wait for the operator. Vibe Verifier
checks evidence but does not itself confer merge authority. The operator's
policy is separate from the gate implementation.

**Keep explicit authority and repository rules.** “pstack always self-merges” and
“Vibe Verifier always requires a human click” are both overstatements. Neither a
source comparison nor this historical sample measures the safety of self-merge.

## Measurement corrections

The earlier draft called title/SZZ associations escaped defects, commit counts
push counts, and violations without a later fix false alarms. Those conclusions
are withdrawn. The earlier aggregate scores, timing claims, receipt counts, and
small agent experiment are not retained as verified comparative results because
the raw cloud evidence was not preserved for independent reproduction.

The [research scripts](README.md) now distinguish:

- Mature source PRs from right-censored ones, at an explicit observation timestamp.
- Mainline merges from stacked merges when `--base-branch` is supplied.
- Later title-classified fix touches from manually established causal regressions.
- A gate violation from an inability to run, preserving raw diagnostics for both.
- Test-case lexical flags from behavior demonstrated by a defective/fixed pair.

A later fix can modify documentation, repair a pre-existing issue, or touch a
large PR's lines by chance. Pure additions can also fix a real omission that
blame cannot attribute. No title/SZZ threshold is a product-quality gate or a
valid denominator for defect precision. Size correlations remain exploratory.

## Other approaches considered

[TDAD](https://arxiv.org/html/2603.17973v1) provides code-to-test impact mapping
instead of another general instruction layer. Its 100-instance experiment uses
Qwen3-Coder 30B and Python SWE-bench tasks: test-level regression falls, while
instance-level regression does not improve and patch-generation rates differ.
That is a reason to test the mapping pattern, not to transfer its headline gain
to other models or JavaScript applications. Reuse an existing test dependency
map first; installing a new graph system is not justified here.

[Google's mutation-testing practice](https://testing.googleblog.com/2021/04/mutation-testing.html)
surfaces selected actionable surviving mutants during review, illustrating a
focused alternative to a universal score gate.
[OpenAI's harness-engineering account](https://openai.com/index/harness-engineering/)
describes enforcing boundaries with structural checks and teaching conventions
through the repository itself. This is practitioner evidence, not a controlled
comparison against either system. Both favor extending an existing mechanism
over adding another broad prompt or scoring layer.

## Minimal implementation plan

1. **Question and delete:** remove unsupported comparative numbers and the proposed
   size/lexical-oracle gates. Do not install new infrastructure just to measure it.
2. **Simplify:** reuse existing consumer tests and source-derived feature maps.
   Prioritize direct production-code assertions and a real defective/fixed pair
   with a passing control. This PR delivers research-tool corrections and four
   concrete catalog test improvements; it does not deploy a consumer regression lane.
3. **Verify before promotion:** require an independently chosen requirement set,
   supported base/head comparisons, real role and environment coverage for any
   advertised user flow, and manual inspection of both failures and passes.
   Isolated function or rendered-fragment checks must remain labeled as such.
4. **Measure acceleration separately:** a same-task, same-model comparison with
   hidden repair information is needed for speed, cost, or defect-reduction claims.
   Retrospective examples are a useful challenge set, not that benchmark.
5. **Automate only what survives:** extend existing enforcement when multiple
   real cases justify it. No additional reviewer, generic runner, pre-push hook,
   patch-id bypass, or agent-merge policy is shipped by this research.

Research artifacts are created outside this repository, read locally, regenerated
from recorded inputs, and retained deliberately until their owner removes them.
Private source, credentials, screenshots and history must not be published here.
No database or product deployment is required to run these catalog tests.
