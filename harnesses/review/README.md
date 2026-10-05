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
   to review, reads how the run ended from the action's execution log, and then posts
   `review-receipt: <head sha> -- <mode> -- run <id>` as a PR comment. The receipt step is reached
   only when every step before it succeeded, so an invalid or missing token, a crash, a model error
   or a broken step leaves no receipt for this head. The one exception is a run whose reviewer
   subscription proved a rate or usage limit: it passes with a warning and its receipt's mode is
   `limited` (see the rules below). Usage collection and upload run under `always()` so a failed
   review can retain partial statistics without making the review green. These steps follow the receipt:
   a capture failure does not suppress evidence of a completed review, but still fails the review job.
2. **verify** writes the declared inputs (the head SHA, the newest receipt for that head posted by
   the workflow's own identity, the count of unresolved review threads) and runs the
   [`review-receipt`](../../gates/review_receipt.py) gate through the composite action with the
   manifest [`manifest`](manifest) (`.vibe-verifier-review` in the consumer).

Scope has three modes, decided from the newest receipt: **full** (no completed review yet, its
commit is no longer an ancestor of HEAD, or a changed name holds a backtick or is the line `EOF`,
either of which could escape the file list: the first out of the prompt's fence, the second out of
the `DELTA_FILES` value in `$GITHUB_ENV`; git C-quotes control characters, so no other name
can), **delta** (only the PR files changed since the last
receipt, so the loop converges: each push shrinks what needs review, and earlier findings stand),
**nochange** (nothing new since the last receipt: the model is not run and the receipt is posted
for this head, which is how a re-run after resolving threads turns the gate green). A `limited`
receipt is never the last receipt for this purpose: nothing was reviewed under it, so the next real
review covers everything it let through. A receipt for an older head that lands late (its review
finished after a newer one's) can be the newest; it still marks a reviewed commit, so the next
delta only starts further back.

## Rules the harness obeys

- **The receipt is the workflow's.** Deterministic text from a step the model cannot reach, keyed
  to the exact head. A receipt for an older commit is not a receipt for this one: a push after
  the review is a change nobody reviewed.
- **Verify reads the newest receipt for this head, not the newest receipt.** Runs that are not
  cancelled can finish out of order, so a review of an older head can post its receipt after this
  head's; taking the newest comment then failed a reviewed head (a consumer's pull request,
  2026-10-05). The verify job searches every page of comments for the newest receipt naming the
  event head. With none, it writes the newest receipt instead, and the gate fails naming the head
  that receipt was for.
- **A review that did not complete is red, unless the reviewer subscription is rate-limited.**
  The review step runs under `continue-on-error` only so the next step, "Require a completed
  review", can read the execution log; that step decides. A completed review (the step succeeded
  and the final result object is a success that is not an error, took at least two turns and cost
  something) gets its receipt. A run that did not complete and whose log proves a rate or usage
  limit passes with a `::warning::` that the review did not run because the reviewer subscription
  is rate-limited, and posts `review-receipt: <sha> -- limited -- run <id>` (decided by the
  operator on 2026-10-03). Every other ending stays red with no receipt: an invalid or missing
  token (a 401 "Invalid bearer token", "Not logged in"), a crash, a timeout, the turn cap, a model
  error. The gate says why: no receipt for this head.
- **Limited is proven structurally, never read from prose.** The proof is an SDK
  `rate_limit_event` whose status is `rejected` or `rate_limit`, or a final result object that is an
  error carrying the CLI's limit message ("You've hit your limit", "usage limit reached", "rate
  limit") or Anthropic's `rate_limit_error` type. A completed review is never limited, so a diff
  that merely contains that wording cannot fake one. The action is pinned at v1.0.171 or later
  because older versions wrote no execution log when the SDK died on a usage limit, which left a
  limit indistinguishable from a crash.
- **A limited pass is visible and temporary.** Unlike the old copy that turned a quota failure
  into a quiet green check, the job warns, the receipt says `limited`, and the scope step never
  anchors on it. The verify gate accepts a `limited` receipt only for the exact head and still
  requires zero unresolved threads when `--threads` is wired. Re-run the workflow once the limit
  resets to get the real review.
- **Unresolved threads block, when wired.** `--threads` in the manifest makes every unresolved
  review thread a finding, which is the operator's merge rule enforced by a machine on a plan with
  no branch protection. Resolve each thread (reply, then resolve), then re-run the workflow; with
  no new commits it runs in `nochange` mode and costs no model session.
- **The turn cap is sized to the scope.** The scope step sets `--max-turns` to two turns per file
  in scope plus 30, never below 50: reading a file is a turn and commenting on it another. A fixed
  50 ran out at 51 turns on an 87-file pull request in a consumer repository (2026-09-24),
  which left no receipt, and a re-run failed identically, so a large pull request could never
  pass. Hitting the cap still means no receipt and a red gate.
- **The model's tools are the read tools and the inline comment, nothing else.** No Bash. All
  pull-request content is untrusted data to it.
- **House rules live in the consumer's CLAUDE.md**, pinned to the base. The prompt is the same in
  every consumer, byte for byte (a test holds `review.yml` to `prompt.md`); what differs between
  repositories is what their CLAUDE.md says a reviewer should look for.
- **Usage is numbers, not model output.** The review job uploads
  `vv-usage-swe-reviewer-<run_attempt>` for 7 days. It contains only `usage.json`, built from the
  pinned action's final aggregate usage. A `nochange` run records `not_run`; it never invents zero
  usage. The schema and provider counting rules are in
  [`docs/USAGE-CONTRACT.md`](../../docs/USAGE-CONTRACT.md).

## Subscribing

Copy `review.yml` to `.github/workflows/review.yml` and `manifest` to `.vibe-verifier-review`,
pin the catalog actions, set `CLAUDE_CODE_OAUTH_TOKEN`, remove the old `claude-review.yml`, and
put any repository-specific review focus under a heading in CLAUDE.md. The subscription PR's own
run is the first review.

A change to `review.yml` reaches a copy only when the copy is edited: `apply-down` moves the pins,
and `consumers` reports a copy that differs from this file outside the lines the consumer owns as
drift until it is copied again.
