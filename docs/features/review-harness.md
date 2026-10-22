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

- `tests/test_review_harness.py::test_cursor_and_the_repositorys_pull_sync_bot_may_start_a_review`
- `tests/test_review_receipt.py::test_a_receipt_for_an_older_commit_fails_and_names_it`
- `tests/test_review_receipt.py::test_unresolved_threads_fail_when_wired`
- `tests/test_review_harness.py::test_a_full_review_sizes_the_cap_by_every_file_in_the_pull_request`
- `tests/test_review_harness.py::test_the_warning_gives_each_limited_account_its_limit_and_reset`
- `tests/test_review_receipts.py::test_a_receipt_quoted_by_another_comment_of_the_workflow_identity_is_never_read`
- `tests/test_review_receipts.py::test_the_anchor_is_read_from_the_receipt_comment_and_never_from_a_quote`
- `tests/test_comments.py::test_one_comment_edited_in_place_with_a_hidden_line_per_run`

## Gotchas

- A push after the review makes the receipt stale; a `limited` receipt counts for its head only.
- Receipts live in one comment, a hidden line per run ([docs/comments.md](../comments.md)); a receipt quoted by any other comment is never read.
