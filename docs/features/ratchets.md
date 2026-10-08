# Complexity and file-length ratchets

`cognitive-complexity` and `max-file-lines` judge the files a pull request changed against the same files at the base.

## Surfaces

- gate: `cognitive-complexity`
- gate: `max-file-lines`
- source: `gates/cognitive_complexity.py`
- source: `gates/max_file_lines.py`
- source: `tools/cognitive-complexity/**`
- source: `tools/cognitive-complexity-python/**`

## Reach

Add `cognitive-complexity` and `max-file-lines` to `.vibe-verifier`.

## Verify

- `tests/test_complexity.py::test_a_new_file_with_an_over_limit_function_fails_at_its_line`
- `tests/test_complexity.py::test_an_over_limit_function_already_at_the_base_does_not_block_a_touch`
- `tests/test_complexity.py::test_an_over_limit_function_that_gets_worse_fails_though_the_count_is_the_same`
- `tests/test_complexity.py::test_a_lower_ranked_function_that_gets_worse_fails`
- `tests/test_complexity.py::test_the_base_is_the_merge_base_not_the_base_branchs_tip`
- `tests/test_complexity.py::test_a_long_file_that_shrank_or_was_untouched_passes_and_one_that_grew_fails`

## Gotchas

- A moved file is compared with itself at its old path, so a move never blocks.
- A file whose over-limit costs got worse is a finding even when its count of them is unchanged. Costs are compared by rank (highest first), not by function name, so the finding lists every over-limit function in the file and the message carries both cost lists.
- The comparison is with the merge base, so an improvement that landed on the base branch after the pull request started is not held against it.
- Splitting one over-limit function into two that are both still over the limit raises the count and is a finding: finish the split so each part is under the limit.
