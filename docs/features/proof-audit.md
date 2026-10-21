# Proof audit

A model on the Codex CLI grades, cold, whether a repository's tests prove each requirement its pull request's Ticket sections define (PROVEN with quoted assertions, EXCLUDED to an owner that took the item, or UNPROVEN), and the `proof-audit` gate holds every quote to the repository and fails on any UNPROVEN.

## Surfaces

- gate: `proof-audit`
- command: `proof-audit`
- source: `gates/proof_audit.py`
- source: `gates/_proof_audit.py`
- source: `gates/_schema_subset.py`
- source: `harnesses/proof-audit/**`
- source: `actions/proof-audit/**`
- source: `tests/fixtures/proof-audit/**`

## Reach

In a QAE workflow, `actions/proof-audit` plans in the criteria job, grades one shard per job on the runner with the Codex login, and verifies in the verify job ([harnesses/proof-audit/README.md](../../harnesses/proof-audit/README.md)). Before pushing, from a clone: `bin/vibe-verifier proof-audit --repo <project> --base-ref main --body <body file outside the project>`, which exits as the CI step does.

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
- `tests/test_proof_audit.py::test_the_recorded_grade_of_the_proven_tree_passes_and_does_not_pass_on_the_other_tree`

## Gotchas

- Whether an assertion exercises a case is the model's judgment; the gate proves only that the quotes are real. The one-file replay (`tests/fixtures/proof-audit/c8`) is the case it was built for: run it live with `VV_PROOF_AUDIT_LIVE=1` after changing the prompt.
- No `Ticket:` line audits nothing, and passes, unless the body claims `[req: ID]` items anyway (a finding: the line that would define them is missing) or the consumer sets `require-ticket` globs and the pull request changes a matching file without a `No ticket: <why>` line (a finding).
- The model never sees the body: keep a local `--body` file outside the repository, which the model reads.
- The grades stay in the runner's temporary directory from the grade job's write to the verify job's read. The gate judges none of them when the checkout supplied any: a path the branch tracks at, under or on the way to `--grades`, or a symbolic link in it (`planted` in `gates/_contract.py`). A consumer copy made before that keeps them in the checkout: refresh the copy, not only its pins.
- A grade is for the tree it was given. Grades recorded on another commit fail their quotes, as they should.
