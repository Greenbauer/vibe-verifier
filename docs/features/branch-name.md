# Branch name length

`branch-name-length` fails a branch whose name is longer than `--max`.

## Surfaces

- gate: `branch-name-length`
- source: `gates/branch_name_length.py`

## Reach

Add `branch-name-length --max <n>` to `.vibe-verifier`.

## Verify

- `tests/test_gates.py::test_over_and_under_budget`
- `tests/test_gates.py::test_reads_the_checked_out_branch_then_the_pr_head`
- `tests/test_gates.py::test_detached_head_could_not_run`

## Gotchas

- `--max` is required; the line without it cannot run.
