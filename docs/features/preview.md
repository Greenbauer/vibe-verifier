# Preview address

Records the address of a pull request's preview from the job that proved it, as a `preview` commit status on the head, and, for a preview with a branch database of its own, that database's project ref as a `preview-database` status. Finds both for every check that tests the preview, so they read one answer instead of the host's deployment records or a branch found again by name.

## Surfaces

- source: `actions/preview/preview.py`
- source: `actions/preview/action.yml`

## Reach

From a workflow, `uses: Greenbauer/vibe-verifier/actions/preview@<sha>` with `record:` in the job that sets the preview up, and with `check:` in every job that needs the address ([actions/preview/README.md](../../actions/preview/README.md)). From a clone, `GH_TOKEN=... python3 actions/preview/preview.py find --repo <owner>/<name> --sha <commit> --check <name>` (several names, one per line, for a check being renamed).

## Verify

- `tests/test_preview.py::test_record_writes_a_preview_status_on_the_commit`
- `tests/test_preview.py::test_find_prints_the_newest_recorded_address_once_the_check_passes`
- `tests/test_preview.py::test_record_writes_the_database_before_the_address`
- `tests/test_preview.py::test_find_prints_the_newest_recorded_database_on_a_second_line`
- `tests/test_preview_checks.py::test_the_action_outputs_the_database_recorded_with_the_address`
- `tests/test_preview.py::test_a_newer_skipped_run_does_not_hide_the_real_one`
- `tests/test_preview.py::test_only_running_time_counts_against_the_running_budget`
- `tests/test_preview.py::test_a_passed_check_with_no_address_fails_at_once_naming_the_recorder`
- `tests/test_preview.py::test_six_failed_reads_in_a_row_exit_2_naming_the_call`
- `tests/test_preview_checks.py::test_the_action_shows_why_a_find_failed`
- `tests/test_preview.py::test_a_wait_that_ends_with_the_check_queued_or_running_exits_3`
- `tests/test_preview_checks.py::test_a_handover_passes_with_no_address_when_the_check_is_still_running`
- `tests/test_preview_checks.py::test_a_handover_still_fails_a_check_that_failed_or_never_ran`
- `tests/test_preview_checks.py::test_the_runs_of_every_name_are_one_checks_and_the_newest_decides`

## Gotchas

- Record the immutable build address, not a branch alias: a later push moves the alias.
- A passed check with no `preview` status is a missing record step, not a slow one. The finder fails at once and says re-running will not help.
- `handover: "true"` passes only the finder's exit 3 (the wait ended with the check's run still queued or running), with an empty `url`. It is for the first job of a two-job wait; the job that uses the address must wait again without it, or a check that never passed is read as a pass.
- The finder trusts the recording job as far as it trusts the check it waits for: a pull request can edit both. A reader that writes to the recorded database should still refuse a ref that is not one of production's preview branches.
- The database status is written before the address, so a reader that finds the address after the check passed also finds the database.
- `check` may name the check several ways, one per line, while it is being renamed ([check names](../check-names.md)): the runs under every name are judged as one check's, so the newest of them decides. Each name costs one read per poll.
