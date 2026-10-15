# Coverage not lost

`coverage-not-lost` runs the project's whole unit suite with line coverage at the merge base and at the head, twice when needed, and fails on lines the pull request did not change that ran before and no longer run.

## Surfaces

- gate: `coverage-not-lost`
- source: `gates/coverage_not_lost.py`
- source: `gates/_coverage_runs.py`
- source: `tools/coverage-not-lost/**`
- source: `tools/coverage-not-lost-python/**`

## Reach

List `coverage-not-lost --soak` (with `--runner`, `--project`, `--args` and `--body` as the project needs) in a manifest of its own, run by a job that installs the project's dependencies first; remove `--soak` on the base manifest once its record is clean.

## Verify

- `tests/test_coverage_not_lost.py::test_a_deleted_test_that_was_the_only_cover_flags_its_lines`
- `tests/test_coverage_not_lost.py::test_a_moved_test_does_not_flag`
- `tests/test_coverage_not_lost.py::test_a_refactor_that_moves_code_does_not_flag`
- `tests/test_coverage_not_lost.py::test_coverage_that_changes_between_runs_is_not_reported`
- `tests/test_coverage_not_lost.py::test_a_suite_that_fails_at_either_side_cannot_run_even_under_soak`
- `tests/test_coverage_not_lost.py::test_a_coverage_lost_line_in_the_body_marks_the_loss_intended`
- `tests/test_coverage_not_lost.py::test_a_vitest_with_no_provider_of_its_version_cannot_run`
- `tests/test_coverage_not_lost.py::test_pytest_names_the_test_that_ran_a_lost_line`
- `tests/test_coverage_not_lost.py::test_unittest_flags_a_deleted_test_and_a_red_head_cannot_run`

## Gotchas

- Blind to browser and end-to-end tests: only the unit suite the runner starts is measured, so a loss only a Playwright test would show passes.
- Blind to a line that still runs but is no longer asserted on: a test that keeps calling the code and drops its `expect` passes.
- Vitest needs `@vitest/coverage-v8` of exactly its own version: the project's own, or the catalog's pin for Vitest 2.1.9, 3.2.7, 4.0.18, 4.1.11 and 5.0.3. Anything else is exit 2, because a provider of another version measured the wrong lines without an error.
- A Python suite that imports the package from the working copy instead of the checkout being measured (an editable install the runner's own path does not come before) is exit 2: the gate cannot tell which side's code ran.
- Both sides run with the dependencies installed for the head, so a pull request that removes a package the merge base's tests import is exit 2.
- A line that stops running because the code before it changed (a branch the tests no longer reach) is a finding too. Say why in the pull request body, `coverage-lost: <reason>`.
