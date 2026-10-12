# Preview address

Records the address of a pull request's preview from the job that proved it, as a `preview` commit status on the head, and finds it for every check that tests the preview, so they read one answer instead of the host's deployment records.

## Surfaces

- source: `actions/preview/preview.py`
- source: `actions/preview/action.yml`

## Reach

From a workflow, `uses: Greenbauer/vibe-verifier/actions/preview@<sha>` with `record:` in the job that sets the preview up, and with `check:` in every job that needs the address ([actions/preview/README.md](../../actions/preview/README.md)). From a clone, `GH_TOKEN=... python3 actions/preview/preview.py find --repo <owner>/<name> --sha <commit> --check <name>`.

## Verify

- `tests/test_preview.py::test_record_writes_a_preview_status_on_the_commit`
- `tests/test_preview.py::test_find_prints_the_newest_recorded_address_once_the_check_passes`
- `tests/test_preview.py::test_a_newer_skipped_run_does_not_hide_the_real_one`
- `tests/test_preview.py::test_a_passed_check_with_no_address_fails_at_once_naming_the_recorder`
- `tests/test_preview.py::test_six_failed_reads_in_a_row_exit_2_naming_the_call`
- `tests/test_preview.py::test_the_action_shows_why_a_find_failed`

## Gotchas

- Record the immutable build address, not a branch alias: a later push moves the alias.
- A passed check with no `preview` status is a missing record step, not a slow one. The finder fails at once and says re-running will not help.
- The finder trusts the recording job as far as it trusts the check it waits for: a pull request can edit both.
