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
- `tests/test_complexity.py::test_a_long_file_that_shrank_or_was_untouched_passes_and_one_that_grew_fails`

## Gotchas

- A moved file is compared with itself at its old path, so a move never blocks.
