# Suite scope

Decides, before a slow suite's job queues, whether a pull request's diff can change what the suite verifies, from one list of inert paths the repository keeps on its default branch; posts the suite's statuses as not applicable, with the reason, when it cannot; and checks that list against the paths the suite reads.

## Surfaces

- source: `actions/suite-scope/suite_scope.py`
- source: `actions/suite-scope/action.yml`

## Reach

From a workflow, `uses: Greenbauer/vibe-verifier/actions/suite-scope@<sha>` with `list:` in a small job whose `not-applicable` output the suite's job reads in its `if:`, with `post:` in a job that runs when that output is `true`, and with `reads:` in the repository's own test job ([docs/suite-scope.md](../suite-scope.md)). From a clone, `GH_TOKEN=... python3 actions/suite-scope/suite_scope.py decide --repo <owner>/<name> --list <path> --pr <n> --sha <commit>` prints the verdict, and `check --list <file> --reads <file>` checks a list.

## Verify

- `tests/test_suite_scope.py::test_a_diff_that_is_inert_throughout_is_not_applicable_and_says_why`
- `tests/test_suite_scope.py::test_any_path_off_the_list_runs_the_suite_and_names_it`
- `tests/test_suite_scope.py::test_both_names_of_a_rename_count`
- `tests/test_suite_scope.py::test_a_path_under_a_directory_named_e2e_is_never_inert_whatever_the_list_says`
- `tests/test_suite_scope.py::test_a_file_list_github_cut_short_runs_the_suite`
- `tests/test_suite_scope.py::test_a_file_list_github_does_not_return_runs_the_suite`
- `tests/test_suite_scope.py::test_no_pull_request_runs_the_suite_and_reads_nothing`
- `tests/test_suite_scope.py::test_a_pull_request_into_another_branch_runs_the_suite`
- `tests/test_suite_scope.py::test_a_head_that_moved_runs_the_suite`
- `tests/test_suite_scope.py::test_the_list_is_read_on_the_default_branch_so_a_pull_request_cannot_widen_its_own`
- `tests/test_suite_scope_list.py::test_a_list_that_cannot_be_read_runs_the_suite_with_a_warning`
- `tests/test_suite_scope_list.py::test_a_hash_inside_a_pattern_is_malformed_not_a_comment`
- `tests/test_suite_scope_list.py::test_a_pattern_that_would_quietly_match_less_is_malformed`
- `tests/test_suite_scope_list.py::test_a_never_line_covers_everything_under_the_path_it_names`
- `tests/test_suite_scope_list.py::test_a_byte_order_mark_or_a_trailing_line_break_cannot_make_a_path_inert`
- `tests/test_suite_scope.py::test_a_gh_that_cannot_run_or_answers_with_no_json_runs_the_suite_with_a_warning`
- `tests/test_suite_scope.py::test_a_reason_of_the_callers_own_runs_the_suite_and_reads_nothing`
- `tests/test_suite_scope.py::test_a_path_with_a_line_break_cannot_write_an_output_of_its_own`
- `tests/test_suite_scope.py::test_post_writes_each_context_as_a_success_status_with_the_reason`
- `tests/test_suite_scope_post.py::test_a_block_of_one_line_posts_a_name_with_spaces_as_one_context`
- `tests/test_suite_scope_list.py::test_check_fails_when_the_list_calls_a_path_the_suite_reads_inert`
- `tests/test_suite_scope.py::test_each_output_is_the_steps_output_under_its_own_name`
- `tests/test_suite_scope.py::test_the_action_decides_for_the_events_pull_request`

## Gotchas

- Every doubt runs the suite, and the step never fails on what it cannot read. A new fail-safe gets a test that makes the decision say run; a change that makes any existing one say not applicable is the regression this feature exists to prevent.
- The list is read from GitHub on the default branch, never from the workspace, so the pull request that adds or widens a list is not skipped by it. Only `check` reads the workspace, because it judges the list itself.
- A changed path is the pull request's to name. The reason is flattened to one line before it is written as a step output, or a line break in a file name would write an output of its own.
- A `!` line wins wherever it is written, and covers everything under the path it names. A leading or trailing `/`, a `[`, `]`, `{`, `}` or backslash, or whitespace other than a quoted space, makes a line malformed. `*` stays inside one path segment, so `*.md` is only the Markdown at the root.
- A path with a segment named `e2e`, at any depth, is never inert whatever the list says (Playwright matches `testMatch` at any depth), and so is a changed path named `e2e` itself, which may be a submodule or a link. The rule is the engine's, not a line a list must remember.
- When to post is the consumer's: a status on the suite's context can release whatever waits for the suite.
- A `post` value with a new line in it is one context per line, so a status name may hold spaces (`Preview / E2E tests`); only a value on one line is split on spaces. A `|` block that lists several names on one line therefore now posts them as one context: put each on its own line.
