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
- `tests/test_max_file_lines.py::test_a_long_file_that_shrank_or_was_untouched_passes_and_one_that_grew_fails`
- `tests/test_max_file_lines.py::test_a_new_file_of_few_lines_over_the_token_limit_fails_and_says_how_to_split`
- `tests/test_max_file_lines.py::test_an_over_limit_file_may_shrink_but_not_grow`
- `tests/test_max_file_lines.py::test_a_staged_new_file_over_the_limit_fails_and_an_unstaged_one_is_not_judged`
- `tests/test_max_file_lines.py::test_the_staged_content_is_judged_not_the_working_tree`
- `tests/test_complexity.py::test_an_inline_eslint_disable_does_not_hide_a_function`
- `tests/test_complexity.py::test_python_tests_by_every_name_are_not_measured_and_near_names_are`

## Gotchas

- A moved file is compared with itself at its old path, so a move never blocks.
- A file whose over-limit costs got worse is a finding even when its count of them is unchanged. Costs are compared by rank (highest first), not by function name, so the finding lists every over-limit function in the file and the message carries both cost lists.
- Both gates compare with the merge base, so a change that landed on the base branch after the pull request started (a function improved, a file shortened) is not held against it.
- `cognitive-complexity` ignores inline eslint comments (`eslint-disable`, `/* eslint rule: off */`) as it ignores bulk suppressions: a suppressed function still counts.
- `cognitive-complexity` never measures a test. A Python test is `conftest.py` or a file named `test_*.py`, `*_test.py`, `test-*.py`, `*-test.py` or `*.test.py`, the same list `new-source-has-test` reads (`gates/_contract.py`). `max-file-lines` measures tests.
- `max-file-lines` measures two things: lines (`--max`, default 500) and tokens (`--max-tokens`, default 5000), where a token is a file's bytes divided by 4, about what a model reads to take the file in. A file of a few very long lines passes the line limit and not the token limit, and joining lines to pass the line limit leaves the token count where it was. `--max-tokens 0` turns the token limit off. Each finding ends with how to split the file, and says to `--exclude` a generated file or lockfile rather than split it.
- `max-file-lines --staged` judges what is staged for the next commit against `HEAD`, for a pre-commit check: the index content, not the working tree. Run from a pre-commit hook with the repository's own `max-file-lines` arguments, it tells an agent about a file it grew past a limit before the commit exists.
- Splitting one over-limit function into two that are both still over the limit raises the count and is a finding: finish the split so each part is under the limit.
