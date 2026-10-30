# Proof audit

A model on the Codex CLI grades, cold, whether a repository's tests prove each requirement its pull request's Ticket sections define, and, given a plan store, each requirement of the whole wording of a plan its `[plan:<id>]` marker names (PROVEN with quoted assertions, EXCLUDED to an owner that took the item, or UNPROVEN; a stored plan's requirement a `Split off:` line gives to another pull request or plan is SPLIT), and the `proof-audit` gate holds every quote to the repository and fails on any UNPROVEN.

## Surfaces

- gate: `proof-audit`
- command: `proof-audit`
- source: `gates/proof_audit.py`
- source: `gates/_proof_audit.py`
- source: `gates/_proof_requirements.py`
- source: `gates/_plan_store.py`
- source: `gates/_schema_subset.py`
- source: `gates/_test_body.py`
- source: `harnesses/proof-audit/**`
- source: `actions/proof-audit/**`
- source: `tests/fixtures/proof-audit/**`

## Reach

In a QAE workflow, `actions/proof-audit` plans in the criteria job, grades one shard per job on the runner with the Codex login, and verifies in the verify job ([harnesses/proof-audit/README.md](../../harnesses/proof-audit/README.md)); a consumer whose plans live in a plan store gives the plan and verify steps `title`, `plans-endpoint` and `plans-token`, and `soak: "true"` makes the verify step report only. Before pushing, from a clone: `bin/vibe-verifier proof-audit --repo <project> --base-ref main --body <body file outside the project>` (with `--title` and `--plans-endpoint`, the token in `$VV_PLANS_TOKEN`, for a stored plan), which exits as the CI step does.

## Verify

- `tests/test_proof_audit.py::test_a_valid_run_passes_and_lists_every_id`
- `tests/test_proof_audit.py::test_a_fabricated_quote_is_unproven`
- `tests/test_proof_audit.py::test_a_missing_test_title_or_file_is_unproven`
- `tests/test_proof_audit.py::test_an_unproven_id_fails_with_what_is_missing`
- `tests/test_proof_audit.py::test_proven_needs_an_assertion_for_every_case`
- `tests/test_proof_audit.py::test_an_exclusion_needs_an_owner_that_takes_the_item_at_the_merge_base`
- `tests/test_proof_audit.py::test_a_usage_limit_or_failed_run_could_not_run_even_in_soak`
- `tests/test_proof_audit.py::test_grades_that_do_not_match_the_body_or_the_schema_could_not_run`
- `tests/test_proof_audit_unlinked.py::test_requirement_ids_claimed_without_a_ticket_line_fail`
- `tests/test_proof_audit_unlinked.py::test_a_required_path_changed_with_no_ticket_needs_a_stated_reason`
- `tests/test_proof_audit_unlinked.py::test_the_action_passes_each_glob_and_keeps_the_table_when_the_gate_fails`
- `tests/test_proof_audit.py::test_the_local_run_gives_the_model_the_requirements_and_never_the_body`
- `tests/test_proof_audit.py::test_the_local_run_exits_as_the_gate_does`
- `tests/test_proof_audit_unlinked.py::test_the_local_run_passes_require_ticket_to_the_gate`
- `tests/test_proof_audit.py::test_ci_plans_then_grades_each_shard_into_its_own_file`
- `tests/test_proof_audit.py::test_a_usage_limit_is_recorded_and_the_gate_cannot_run`
- `tests/test_proof_audit.py::test_ci_runs_without_codex_sandbox_which_cannot_start_in_its_containers`
- `tests/test_proof_audit.py::test_grades_written_inside_the_repository_are_not_the_grader_changing_it`
- `tests/test_proof_audit.py::test_the_prompt_and_schema_have_one_copy`
- `tests/test_proof_audit_planted.py::test_committed_grades_are_never_judged`
- `tests/test_proof_audit_planted.py::test_a_link_is_never_grades`
- `tests/test_proof_audit_planted.py::test_the_run_s_own_grades_in_the_checkout_are_judged`
- `tests/test_proof_audit_planted.py::test_the_recipe_keeps_the_grades_outside_the_checkout`
- `tests/test_proof_audit.py::test_the_recorded_grade_of_the_gif_case_is_unproven`
- `tests/test_proof_audit_body.py::test_a_hook_or_helper_line_cited_exactly_is_not_the_test_above_it`
- `tests/test_proof_audit_body.py::test_strings_comments_and_shared_titles_neither_end_a_test_early_nor_reopen_one`
- `tests/test_proof_audit_body.py::test_an_escaped_backtick_an_unclosed_regex_and_an_overlong_header_keep_their_stated_bounds`
- `tests/test_proof_audit_body.py::test_a_template_nested_in_a_template_expression_closes_where_it_does`
- `tests/test_proof_audit_body.py::test_a_regex_a_string_or_a_comment_holding_a_backtick_opens_no_template`
- `tests/test_proof_audit_body_shapes.py::test_each_shape_keeps_its_own_lines_and_turns_away_the_hook_after_it`
- `tests/test_proof_audit.py::test_the_recorded_grade_of_the_proven_tree_passes_and_does_not_pass_on_the_other_tree`
- `tests/test_proof_audit_plan_store.py::test_a_plans_wording_is_cut_into_requirements_with_stable_ids`
- `tests/test_proof_audit_plan_store.py::test_the_title_labels_comments_and_each_aside_word_are_read_as_written`
- `tests/test_proof_audit_plan_store.py::test_a_labels_block_and_the_title_end_where_the_markdown_says`
- `tests/test_proof_audit_plan_store.py::test_the_plan_step_reads_the_named_plan_after_the_tickets_and_leaves_split_ids_ungraded`
- `tests/test_proof_audit_plan_store.py::test_an_id_a_ticket_section_and_a_stored_plan_both_define_cannot_run`
- `tests/test_proof_audit_plan_store.py::test_the_model_is_given_the_plans_wording_and_never_the_split_lines`
- `tests/test_proof_audit_plan_store.py::test_split_off_lines_name_a_stored_requirement_and_its_owner`
- `tests/test_proof_audit_plan_store.py::test_a_plan_the_audit_cannot_read_cannot_run`
- `tests/test_proof_audit_plan_store.py::test_the_actions_plan_step_passes_the_title_and_store_by_env`
- `tests/test_proof_audit_split.py::test_a_split_off_requirement_is_listed_with_its_owner_and_never_graded`
- `tests/test_proof_audit_split.py::test_a_plan_split_off_whole_grades_nothing_and_cannot_pass`
- `tests/test_proof_audit_plan_rules.py::test_an_aside_heading_is_its_whole_phrase_and_a_label_leaves_out_one_block`
- `tests/test_proof_audit_plan_rules.py::test_a_split_off_line_has_an_arrow_and_may_be_a_list_item`
- `tests/test_proof_audit_plan_rules.py::test_no_owner_is_this_pull_request_and_something_is_graded`
- `tests/test_proof_audit_plan_rules.py::test_a_title_that_starts_with_a_dash_is_still_the_title`
- `tests/test_proof_audit_plan_rules.py::test_a_store_that_redirects_is_not_followed`
- `tests/test_proof_audit_plan_rules.py::test_the_model_is_never_given_the_stores_token`
- `tests/test_proof_audit_split.py::test_an_excluded_grade_on_a_stored_requirement_is_unproven`
- `tests/test_proof_audit_split.py::test_a_store_the_gate_cannot_read_cannot_run`
- `tests/test_proof_audit_split.py::test_the_verify_step_reports_only_under_soak_but_fails_a_gate_that_cannot_run`

## Gotchas

- Whether an assertion exercises a case is the model's judgment; the gate proves only that the quotes are real. The one-file replay (`tests/fixtures/proof-audit/c8`) is the case it was built for: run it live with `VV_PROOF_AUDIT_LIVE=1` after changing the prompt.
- A stored plan has no `[takes:]`: its requirement graded EXCLUDED is UNPROVEN, and one the pull request leaves to another goes on a `Split off: <ID> -> <owner>` line (a list item or not; without `->` it is prose). An owner that is the pull request itself (`--pr`), or every requirement split off so that nothing is graded, cannot run; a made-up `#123` is not checked, only shown. The store is never followed through a redirect, and its token never reaches the model's run. Without `plans-endpoint` a `[plan:<id>]` marker is not read (a note says so, and a `Split off:` line cannot run), so a local run grades CI's list only when given the same store.
- No `Ticket:` line and no stored plan audits nothing, and passes, unless the body claims `[req: ID]` items anyway (a finding: the line that would define them is missing) or the consumer sets `require-ticket` globs and the pull request changes a matching file without a `No ticket: <why>` line (a finding).
- The model never sees the body: keep a local `--body` file outside the repository, which the model reads.
- The grades stay in the runner's temporary directory from the grade job's write to the verify job's read. The gate judges none of them when the checkout supplied any: a path the branch tracks at, under or on the way to `--grades`, or a symbolic link in it (`planted` in `gates/_contract.py`). A consumer copy made before that keeps them in the checkout: refresh the copy, not only its pins.
- A grade is for the tree it was given. Grades recorded on another commit fail their quotes, as they should.
