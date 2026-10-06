# Revision-bound AI review

The review harness: a model reviews the pull request, the workflow posts a receipt for the exact head, and `review-receipt` passes only on that head's receipt with no unresolved thread.

## Surfaces

- gate: `review-receipt`
- source: `harnesses/review/**`
- source: `gates/review_receipt.py`
- source: `actions/usage/**`
- source: `.github/workflows/review.yml`

## Reach

Copy `harnesses/review/review.yml` to a consumer's workflows and `harnesses/review/manifest` to its `.vibe-verifier-review`.

## Verify

- `tests/test_review_receipt.py::test_a_receipt_for_an_older_commit_fails_and_names_it`
- `tests/test_review_receipt.py::test_unresolved_threads_fail_when_wired`
- `tests/test_review_harness.py::test_a_full_review_sizes_the_cap_by_every_file_in_the_pull_request`

## Gotchas

- A push after the review makes the receipt stale; a `limited` receipt counts for its head only.
