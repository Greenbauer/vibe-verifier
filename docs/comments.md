# Comments the catalog leaves on a pull request

Every comment a catalog workflow leaves on a pull request is written for a person. Other jobs never
read its words: what a job reads is a file in the run's evidence, or a marker hidden in an HTML comment
(the table below). So the visible text can follow these rules without breaking anything that
coordinates.

## The rules

- **One comment per job, edited in place.** The QA review, the proof audit and the code review
  receipt each keep one comment per pull request and rewrite it on every run. The explorer posts
  nothing; the reviewer's model posts only its inline findings.
- **Lead with the outcome.** The heading or first sentence says what happened (passed, changes
  needed, could not run, skipped) and what the reader does next.
- **Link, don't paste.** A commit is `[this commit](<commit URL>)` and a run is
  `[The run and its evidence](<run URL>)`. No commit hashes, run IDs, runner paths, tailnet
  addresses or bare URLs in the text.
- **Plain words.** No em dashes, no @-mention of an account or a bot, and every item written out:
  never "and 3 more" or "not shown".

## What other jobs read

| Text | Written by | Read by | If it changed |
|---|---|---|---|
| `<!-- vibe-verifier:qa-review -->`, line one | `actions/qa-review` | the next run, to find the comment it edits | a new comment every run |
| `<!-- vibe-verifier:proof-audit -->`, line one | `actions/proof-audit` | the next run, the same way | a new comment every run |
| `<!-- vibe-verifier:review-receipt -->`, line one, then one `<!-- review-receipt: <sha> -- <mode> -- run <id> -->` line per run | the review workflow's "Post the receipt" step | its scope step (the last reviewed commit, for a delta review) and its verify job (the receipt for this head, judged by the `review-receipt` gate) | no receipt is found: every head reviews in full and the verify job fails |

Only comments by `github-actions[bot]` that start with their marker are read. A receipt is also read
from a comment that is one receipt line, which a copy of the review workflow made before the receipt
comment still posts. Any other comment of that identity can quote text a pull request wrote (the QA
review lists the criteria), so a receipt inside one is never read.

## Enforcement

`tests/test_comments.py` renders every comment the catalog writes (each QA review state, the proof
audit, each receipt mode) and fails on any rule above that a deterministic check can see. The
reviewer's inline findings are the one text a model writes; the review prompt carries the rules for
them.
