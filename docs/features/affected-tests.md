# Affected-test selection

`bin/vibe-verifier affected-tests` prints the test files a pull request's changes can affect (the import graph, plus every test that reads files, starts a process or uses a database), or `ALL`, for the repository's own test job to run; with `--report` it names each test a full run failed that the selection left out. It is a tool, not a gate.

## Surfaces

- command: `affected-tests`
- source: `gates/_affected_tests.py`
- source: `gates/_affected_graph.py`
- source: `actions/affected-tests/**`

## Reach

From a workflow, `uses: Greenbauer/vibe-verifier/actions/affected-tests@<sha>` after a `fetch-depth: 0` checkout, then the test step runs the `list` output, or everything when `all` is `true` ([actions/affected-tests/README.md](../../actions/affected-tests/README.md)). A repository starts in report mode: the full suite still runs, and a second step with `report:` names the misses. From a clone, `bin/vibe-verifier affected-tests --repo <project> --base-ref main`.

## Verify

- `tests/test_affected_tests.py::test_a_test_importing_a_changed_file_is_selected_and_one_that_does_not_is_not`
- `tests/test_affected_tests.py::test_a_test_reaching_a_changed_file_three_imports_away_is_selected`
- `tests/test_affected_tests.py::test_a_tsconfig_paths_alias_reached_through_extends_is_resolved`
- `tests/test_affected_tests.py::test_a_workspace_package_name_resolves_to_its_entry_and_its_subpaths`
- `tests/test_affected_tests.py::test_python_imports_absolute_and_relative_are_followed`
- `tests/test_affected_tests.py::test_a_node_test_file_counts_as_a_test_whatever_its_name`
- `tests/test_affected_tests.py::test_tests_that_read_files_or_a_database_always_run`
- `tests/test_affected_tests.py::test_a_test_whose_helper_starts_a_process_always_runs`
- `tests/test_affected_tests.py::test_a_dependency_change_in_package_json_selects_all`
- `tests/test_affected_tests.py::test_a_scripts_only_change_in_package_json_does_not`
- `tests/test_affected_tests.py::test_a_lockfile_change_selects_all`
- `tests/test_affected_tests.py::test_no_base_or_an_unreadable_base_selects_all`
- `tests/test_affected_tests.py::test_a_failure_in_a_test_the_selection_left_out_is_a_miss`
- `tests/test_affected_tests.py::test_the_node_test_reporter_writes_the_failed_files_the_report_reads`
- `tests/test_affected_tests.py::test_the_node_test_reporter_writes_each_failed_file_once`
- `tests/test_affected_tests.py::test_the_action_writes_the_list_and_says_whether_every_test_runs`

## Gotchas

- Imports are read by pattern, not by the project's bundler: an alias only a Vite or Jest config defines (not a tsconfig `paths`), a `require` of a computed name, or a package that resolves through `node_modules` is not followed. A test that imports by a computed name counts as reading files, so it always runs.
- A test always runs when it, or anything it imports, reads files, which costs most in code that reads files by design (an ETL service, a test helper that runs scripts by path): measure the share in report mode before relying on the saving.
- A Jest config kept in `package.json` (its `jest` field) is not read: only the dependency fields of a `package.json` select `ALL`. Move such a config to `jest.config.*`, or name `package.json` in `--all-on`.
- A report names a file relative to wherever its runner ran (Vitest in a package names `lib/x.test.ts`), so it is matched by path suffix: when two packages hold the same relative path, a failure in one counts for both, and a miss can be reported that was not one.
- Uncommitted edits are not seen: the diff is the merge base to `HEAD`.
- An empty list means no test can be affected. A runner handed no files usually runs all of them (`vitest run`, `pytest`), so the step must skip the run itself, as the README's example does.
