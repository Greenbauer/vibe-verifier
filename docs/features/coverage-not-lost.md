# Coverage not lost

`coverage-not-lost` runs the project's whole unit suite with line coverage at the merge base and at the head, twice when needed, and fails on lines the pull request did not change that ran before and no longer run.

## Surfaces

- gate: `coverage-not-lost`
- source: `gates/coverage_not_lost.py`
- source: `gates/_coverage_runs.py`
- source: `tools/coverage-not-lost/**`
- source: `tools/coverage-not-lost-python/**`
- source: `.github/workflows/nightly.yml`

## Reach

List `coverage-not-lost --soak` (with `--runner`, `--project`, `--args` and `--body` as the project needs) in a manifest of its own, run by a job that installs the project's dependencies first; remove `--soak` on the base manifest once its record is clean. To judge a whole suite too slow for every pull request, run it on a schedule with `--base-ref` set to the last run's commit: a loss is then reported with the commits merged since. This repository does both: per pull request on its dashboard tests (`.vibe-verifier-tests`, in `ci.yml`), and once a night on main on its whole suite (`.vibe-verifier-nightly`, in `.github/workflows/nightly.yml`, which `workflow_dispatch` also runs on demand).

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
- `tests/test_coverage_not_lost.py::test_a_line_only_a_child_process_ran_is_measured`
- `tests/test_coverage_not_lost.py::test_without_subprocess_measurement_a_line_only_a_child_ran_is_missed`
- `tests/test_coverage_not_lost.py::test_the_interpreters_own_sitecustomize_still_runs`
- `tests/test_coverage_not_lost.py::test_a_script_a_test_ran_from_a_directory_it_removed_does_not_stop_the_report`
- `tests/test_coverage_not_lost.py::test_a_venv_in_the_working_copy_is_not_taken_for_its_code`
- `tests/test_coverage_not_lost.py::test_a_loss_names_the_commits_it_came_in_with`
- `tests/test_ci_workflow.py::test_the_whole_suite_runs_nightly_and_per_pull_request_only_the_dashboard_tests`
- `tests/test_ci_workflow.py::test_no_step_that_runs_tests_holds_a_credential`
- `tests/test_ci_workflow.py::test_a_night_compares_from_the_last_successful_scheduled_nightly_on_main`
- `tests/test_ci_workflow.py::test_the_first_night_compares_from_main_of_24_hours_ago`
- `tests/test_ci_workflow.py::test_a_dispatch_with_a_base_compares_from_it`

## Gotchas

- Blind to browser and end-to-end tests: only the unit suite the runner starts is measured, so a loss only a Playwright test would show passes.
- Blind to a line that still runs but is no longer asserted on: a test that keeps calling the code and drops its `expect` passes.
- Vitest needs `@vitest/coverage-v8` of exactly its own version: the project's own, or the catalog's pin for Vitest 2.1.9, 3.2.7, 4.0.18, 4.1.11 and 5.0.3. Anything else is exit 2, because a provider of another version measured the wrong lines without an error.
- A Python suite that imports the package from the working copy instead of the checkout being measured (an editable install the runner's own path does not come before) is exit 2: the gate cannot tell which side's code ran.
- Both sides run with the dependencies installed for the head, so a pull request that removes a package the merge base's tests import is exit 2.
- Python measures every Python process a test starts, such as a script it runs with `subprocess.run([sys.executable, "app.py"])`, through coverage.py's own `[run] patch = subprocess`: each process writes a data file of its own, combined after the run. A child starts coverage.py from a `sitecustomize` the gate puts first on the suite's `PYTHONPATH` (the interpreter's own `sitecustomize`, which Homebrew's and Ubuntu's Python have, still runs after it). Only files in the checkout and the working copy (refused, as above) are reported, so a script a test writes to a temporary directory, runs and removes does not stop the report; a package installed in the project's venv is not measured, even when the venv is in the working copy. Not measured: a child given an environment of its own instead of a copy of the test's, one started with `python -I`, `-E` or `-S`, or one of a Python the pinned coverage.py cannot load in. A line only a child ran names no test function. Each child pays coverage.py's start, about 85 ms on Linux Python 3.12 against 13 ms for the process alone, so a suite of many short Python processes slows the most: in that container a test module that starts 239 of them took 5 seconds plain and 18 measured.
- A line that runs only the first time the suite runs on a machine, such as a cache the tests fill, runs at the merge base and not at the head, so the first pair finds it and the second pair (the cache full by then) dismisses it: the gate is right, at twice the cost.
- Vitest does not measure a node script a test spawns: on 2026-10-15, under Vitest 4.1.11 and its `@vitest/coverage-v8`, a script a test ran with `execFileSync("node", ["src/app.js"])` counted 0 on every statement while a module the test imported counted 1. The providers pinned for Vitest 2.1.9 to 4.1.11 have no subprocess support; Vitest 5.0.0 added `coverage.autoAttachSubprocess` (v8 only, off by default, through `NODE_V8_COVERAGE`), which the gate does not turn on.
- A line that stops running because the code before it changed (a branch the tests no longer reach) is a finding too. Say why in the pull request body, `coverage-lost: <reason>`.
