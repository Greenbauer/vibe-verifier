# Review harness: the model reviews, the workflow signs, a gate decides

The model reads the pull request under the repository's own CLAUDE.md and posts inline findings.
The workflow, not the model, posts the receipt that says this head was reviewed. A gate turns the
receipt and the thread state into a verdict. Its prose is never the verdict.

Replaces the per-repository `claude-review.yml` files (three drifting copies, one of which turned
a quota failure into a green check). Decided by the operator on 2026-09-21.

## The shape

Two jobs on every pull request, in the consumer's own workflow. [`review.yml`](review.yml) is the
template; the consumer owns `runs-on`, the token secret, and the two catalog pins, which
`consumers` and `apply-down` keep current like the stub's.

1. **review** checks out the head with `persist-credentials: false`, pins every `CLAUDE.md` and
   `.claude/**` to the base ref (a hostile head could add one as an injection foothold), computes
   the scope, runs `claude-code-action` with [`prompt.md`](prompt.md) when there is something new
   to review, and then posts `review-receipt: <head sha> -- <mode> -- run <id>` as a PR comment.
   The receipt step is reached only when every step before it succeeded, so a quota failure, a
   model error or a broken step leaves no receipt for this head.
2. **verify** writes the declared inputs (the head SHA, the newest receipt posted by the workflow's
   own identity, the count of unresolved review threads) and runs the
   [`review-receipt`](../../gates/review_receipt.py) gate through the composite action with the
   manifest [`manifest`](manifest) (`.vibe-verifier-review` in the consumer).

Scope has three modes, decided from the newest receipt: **full** (no completed review yet, its
commit is no longer an ancestor of HEAD, or a changed name holds a backtick or is the line `EOF`,
either of which could escape the file list: the first out of the prompt's fence, the second out of
the `DELTA_FILES` value in `$GITHUB_ENV`; git C-quotes control characters, so no other name
can), **delta** (only the PR files changed since the last
receipt, so the loop converges: each push shrinks what needs review, and earlier findings stand),
**nochange** (nothing new since the last receipt: the model is not run and the receipt is posted
for this head, which is how a re-run after resolving threads turns the gate green).

## Rules the harness obeys

- **The receipt is the workflow's.** Deterministic text from a step the model cannot reach, keyed
  to the exact head. A receipt for an older commit is not a receipt for this one: a push after
  the review is a change nobody reviewed.
- **A review that did not complete is red, never green.** No `continue-on-error`, no "skipped
  review" comment that passes. The gate says why: no receipt for this head.
- **Unresolved threads block, when wired.** `--threads` in the manifest makes every unresolved
  review thread a finding, which is the operator's merge rule enforced by a machine on a plan with
  no branch protection. Resolve each thread (reply, then resolve), then re-run the workflow; with
  no new commits it runs in `nochange` mode and costs no model session.
- **The model's tools are the read tools and the inline comment, nothing else.** No Bash. All
  pull-request content is untrusted data to it.
- **House rules live in the consumer's CLAUDE.md**, pinned to the base. The prompt is the same in
  every consumer, byte for byte (a test holds `review.yml` to `prompt.md`); what differs between
  repositories is what their CLAUDE.md says a reviewer should look for.

## Subscribing

Copy `review.yml` to `.github/workflows/review.yml` and `manifest` to `.vibe-verifier-review`,
pin the catalog action, set `CLAUDE_CODE_OAUTH_TOKEN`, remove the old `claude-review.yml`, and
put any repository-specific review focus under a heading in CLAUDE.md. The subscription PR's own
run is the first review.
