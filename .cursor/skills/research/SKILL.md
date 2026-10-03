---
name: research
description: "Evidence-graded research on a technical question, ending in measured experiments against this repository. Use for /research, \"what are others doing\", \"compare these approaches\", \"how would we measure this\", or before proposing a new gate, harness, or rule."
---

# Research

The deliverable is a decision, backed by sources you opened and numbers you produced. A survey
that changes nothing is not done.

## 1. Frame the decision

Write down, before searching:

- **The decision** this research informs, in one sentence ("should the catalog add a test-quality gate").
- **Three to six sub-questions.** For each, the answer that would change what we do. A sub-question
  with no such answer is trivia: drop it.
- **The success metric** each candidate idea will be judged by, and where its baseline comes from
  (this repository's history, a consumer's history, a public dataset). Pick the metric before the
  data, so the data cannot pick it for you.

## 2. Gather, primary sources first

Climb this ladder and stop when a sub-question is answered:

1. The thing itself: the repository, the docs, the code, the paper's method section.
2. Engineering write-ups that report numbers and say how they got them.
3. Practitioner posts and talks.
4. Social posts. These are leads to a source, rarely the source.

Rules:

- **Open what you cite.** A search snippet is not a source. Quote at most two lines.
- **Date every claim.** Tooling claims older than a year need a current check.
- **Mark self-reports.** A vendor's number about its own product, or an author's count of their own
  output, is grade B at best.
- **One author is one source,** however many posts they wrote.

Fan out when there are more than two sub-questions: one read-only subagent per sub-question, all in
one message. Each brief stands alone (goal, scope, what to return) and asks for a claims table:
`claim | URL | date | quote | grade`. Subagent claims are leads. Open every load-bearing URL
yourself before it reaches the report; drop any you cannot open.

## 3. Grade the evidence

| Grade | Means |
|---|---|
| A | Measured, with a stated method and sample: a paper, a controlled study, telemetry with n. |
| B | Numbers without a method, or self-reported by the party that benefits. |
| C | Opinion, demo, anecdote, or a single run. |

A recommendation that rests only on grade C is an untested idea, and the report says so.

## 4. Map each idea to the code

For every idea that survives, name where it would land (a gate, a harness, a manifest line, a
consumer's rule), what it replaces or deletes, and which contract it would bend. Here that means
[`docs/GATE-CONTRACT.md`](../../../docs/GATE-CONTRACT.md): a gate reads the tree and git history
only, uses the Python standard library, and enters the catalog when three live repositories need
it. An idea that needs network or a model belongs in a harness, not a gate.

## 5. Test the cheapest falsifiable version

Prefer, in this order:

1. **Backtest on history.** Would the check have flagged the commits that were later fixed or
   reverted, and stayed quiet on the rest? Report the hits, misses, and false alarms by commit.
2. **Prototype on a sample.** Run it over real files and hand-label what it flags. Report precision
   with the sample size.
3. **Agent eval** for claims about agent behavior: the same task, arms that differ in one variable,
   a checker the agents never see, n per arm stated up front.

Every experiment is a script that reruns, and its output is an artifact. Record the baseline, the
result, n, and what would make it inconclusive. With a small n, say "directional", not "proven".
An experiment that could not run is reported as not run, never as a pass.

## 6. Watch the cost

Free sources first. Estimate any paid call (search APIs, model runs) before making it, and ask
before anything over the budget the user set.

## 7. Report

1. **TLDR:** the decision and the one finding that drives it.
2. **Findings:** a table of who does what, with grade and link.
3. **Experiments:** what ran, the numbers, n, and the limits.
4. **Recommendations:** ranked by evidence times impact over cost, each with its landing spot.
5. **What not to do,** and why.
6. **Open questions** and the experiment that would close each.

Every factual sentence carries a link or is marked as a guess.

## Anti-patterns

- Counting output (PRs, lines, agents) as success. Count outcomes: reverts, follow-up fixes,
  defects found later, time to merge.
- Reading success stories without the failures they leave out.
- Trusting a subagent's summary, or your own memory, for a URL, a flag, or a number.
- Recommending a mechanism nobody measured because it sounds rigorous.
