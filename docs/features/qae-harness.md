# Browser acceptance verification (QAE)

The QAE harness: an explorer walks a pull request's acceptance criteria (and, opt-in, the features it touches) in a real browser, and deterministic gates judge its step logs, screenshots and verdict.

## Surfaces

- gate: `acceptance-verdict`
- gate: `qae-artifacts`
- command: `criteria`
- command: `features`
- command: `qa-review`
- command: `qae-inputs`
- command: `tool`
- source: `harnesses/qae/**`
- source: `actions/criteria/**`
- source: `actions/features/**`
- source: `actions/qa-review/**`
- source: `actions/qae-inputs/**`
- source: `actions/qae-browser/**`
- source: `actions/qae-codex/**`
- source: `actions/usage/**`
- source: `gates/acceptance_verdict.py`
- source: `gates/qae_artifacts.py`
- source: `gates/_acceptance.py`
- source: `gates/_features.py`
- source: `tools/qae-browser/**`
- source: `tools/codex/**`

## Reach

Copy `harnesses/qae/explore.yml` (or `explore-codex.yml`) to a consumer's workflows and `harnesses/qae/manifest` to its `.vibe-verifier-qae`; a pull request lists its criteria under `## Acceptance criteria`, each optionally naming a design reference (`[ref: <key>]`, an image the consumer's site step supplies) or the roles to walk it as (`[as: <role>, ...]`).

## Verify

- `tests/test_acceptance.py::test_one_anchored_pass_per_criterion_passes`
- `tests/test_qae_codex_settings.py::test_empty_inputs_preserve_runner_selection`
- `tests/test_qae_codex_settings.py::test_explicit_settings_are_independent_arguments`
- `tests/test_qae_codex_settings.py::test_invalid_settings_fail_before_codex_without_echoing_them`
- `tests/test_qae_codex.py::test_the_locked_cli_supports_current_explicit_models_on_every_platform`
- `tests/test_qae_artifacts.py::test_a_step_without_a_screenshot_fails`
- `tests/test_qae_artifacts.py::test_an_error_a_third_party_page_logged_is_not_judged`
- `tests/test_qae_console_pages.py::test_a_hosted_pages_own_error_passes_and_only_because_of_the_record`
- `tests/test_qae_secret_names.py::test_the_name_entered_through_run_code_signs_in`
- `tests/test_qae_secret_names.py::test_without_the_hook_the_same_sign_in_sends_the_name_and_fails_the_gate`
- `tests/test_criteria.py::test_a_pull_request_that_changes_only_unrendered_paths_needs_no_check`
- `tests/test_features.py::test_a_selected_feature_without_its_line_is_refused`
- `tests/test_features.py::test_states_left_unwalked_are_printed_and_never_a_finding`
- `tests/test_features.py::test_a_feature_with_no_walked_step_is_refused_whatever_its_line_says`
- `tests/test_features.py::test_a_branch_that_predates_the_map_is_not_re_walked_and_not_failed`
- `tests/test_features.py::test_a_pull_request_that_removes_the_map_cannot_be_re_walked`
- `tests/test_qa_review.py::test_changes_needed_lists_each_criterion_as_the_explorer_judged_it`
- `tests/test_qae_annotations.py::test_a_reference_the_workflow_did_not_supply_is_the_operators_to_supply`
- `tests/test_qae_annotations.py::test_a_pass_missing_a_role_is_refused`
- `tests/test_qae_annotations.py::test_the_declared_digests_reach_the_gate_on_the_manifest_line`
- `tests/test_qae_annotations.py::test_a_comparison_at_another_width_than_the_reference_is_refused`
- `tests/test_qae_explore.py::test_the_run_a_fixed_80_cut_off_gets_twice_that`
- `tests/test_qae_widths.py::test_a_criterion_missing_a_width_is_refused`
- `tests/test_qae_widths.py::test_a_branch_dropping_the_widths_is_still_told_the_bases`
- `tests/test_qae_ticket.py::test_a_declared_none_does_not_drop_the_tickets_criteria`
- `tests/test_qae_ticket.py::test_the_ticket_reaches_the_explorer_and_the_gate`
- `tests/test_qae_ticket.py::test_a_dependency_update_passes_on_its_supplied_criteria_alone`
- `tests/test_qae_explore.py::test_dependabot_is_the_one_bot_whose_pull_requests_reach_the_explorer`
- `tests/test_runner.py::test_the_qae_browser_toolchain_installs_from_its_lockfile`
- `tests/test_qae_explore.py::test_a_re_run_never_reads_an_earlier_attempts_evidence`
- `tests/test_qae_shards.py::test_each_explorer_is_judged_on_the_newest_attempt_it_ran_in`
- `tests/test_qae_shards.py::test_the_verify_job_reads_no_pull_request_comment`
- `tests/test_qae_shards.py::test_the_pull_request_one_explorer_could_not_finish_is_shared_out_evenly`
- `tests/test_qae_shards.py::test_each_explorer_is_named_its_share_and_sized_to_it`
- `tests/test_qae_shard_balance.py::test_late_multi_role_criteria_fit_with_the_feature_re_walk_without_renumbering`
- `tests/test_qae_shards.py::test_the_first_explorer_starts_with_the_feature_re_walk_and_takes_fewer_criteria`
- `tests/test_qae_shards.py::test_two_explorers_evidence_merges_without_a_collision_and_passes_the_gates`
- `tests/test_qae_shards.py::test_a_500_in_one_explorers_saved_network_record_still_fails_the_merged_evidence`
- `tests/test_qae_shards.py::test_an_explorer_that_wrote_no_verdict_leaves_its_criteria_without_one`
- `tests/test_qae_browser.py::test_where_sudo_needs_no_password_only_the_missing_packages_are_installed`
- `tests/test_qae_browser.py::test_a_fetch_that_runs_out_of_time_is_tried_once_more_from_the_next_mirror`

## Gotchas

- The Codex action accepts optional `model` and `reasoning-effort` inputs; empty preserves the runner defaults. The action validates identifiers and effort values before invoking Codex, passes settings as separate arguments, and logs only the configured values. Selection does not guarantee a complete walk or a truthful verdict.
- The Codex CLI is locked to `0.162.0`, including its platform binaries. Model availability can be client-version filtered; verify the chosen model and effort with the pinned client and the actual runner account, not another client's catalog.

- The explore templates must stay byte-identical outside the explorer block, and the inline prompts must equal prompt.md.
- A console line names the resource an error is about, never the page that logged it. The page comes from `console-pages.jsonl`, which `actions/qae-browser/console-pages.js` writes through playwright-mcp's `--init-page`. A consumer copy of the Claude lane without that argument writes no record, and every console error is judged as before. The hook depends on the pinned build, and the catalog's `qae-browser` CI job runs it in the real browser.
- A credential reaches the explorer as a NAME (`secrets-file` on the Codex lane). playwright-mcp types its value for the name in `browser_fill_form` and `browser_type` only; `actions/qae-browser/secret-names.js`, a second `--init-page` hook `actions/qae-codex` loads whenever it writes secrets, does the same in every Playwright text-entry call, so a sign-in through `browser_run_code_unsafe` no longer sends the name and fails the run on the site's 400. It wraps classes of the pinned build, and the catalog's `qae-browser` CI job runs it in the real browser. A failed call's error names the secret it was typing, never the value. A field set by a script inside the page, a name pressed key by key, or text dropped onto the page is still the name.
- The verdict is the file each explorer left in the run's own evidence (`verdicts/<n>.md`, copied there by a workflow step after the explorer). No pull request comment is read; the comment the explorer posts is for people. A consumer copy made before that still reads the newest comment from the workflow's identity.
- Opt-in (`max-shards` on the criteria job's `actions/criteria` step): a pull request with more walks than one explorer is sized for (six) is split across explorers that run in parallel, each given its share of the criteria by code. Criteria are assigned largest walks first, with stable document-order ties and original numbers; each final share stays in document order. A criterion is never split, and the first explorer keeps the job name `qae-explore` because its matrix value is blank (the others are `qae-explore (2)`, ...). Only the first re-walks features, and its share of the criteria starts at the re-walk's walks, so every explore job must run the features step before `actions/qae-inputs` to count them alike. Explorers on a shared preview share its data, so a criterion must not depend on another's leftover state, and a site step's throwaway login needs the explorer's number in its name.
- One of several explorers renames what it left so every explorer's evidence fits in one directory (`session-<n>-*/`, `console-<n>-*.log`, `shard-<n>/`), and the verify job refuses two explorers' different files at one path. Nothing is overwritten.
- A pull request body lists criteria only under its `Acceptance criteria` heading; the plain-list reading is for a ticket's criteria file. A Dependabot body has no such heading, so its criteria are the ones the consumer's workflow supplies.
- A design reference's digest reaches the gate as an explore job output, never as a file the explorer could rewrite; a reference the workflow did not supply is refused, never invented.
- The Claude lane's `--max-turns` is the qae-inputs step's `max-turns` output, sized to the run's walks; a consumer copy that still passes a fixed number keeps that number.
- A feature re-walk covers at most `--max-states` states of each feature. A `regression-check` FAIL means a walked step broke; a state not reached goes on a `regression-skip` line, which no gate judges. A consumer copy with the older prompt still tells the explorer to walk every state.
- A pull request opened before the feature map landed on the base has no feature file to re-walk: `features` prints `features: 0` and why, and `acceptance-verdict` passes labelled `re-walk not run: the branch predates the feature map`. Any other head with no feature file, a removed map included, is exit 2 in both.
- The evidence artifact is `qae-artifacts-<run_attempt>` (`-<n>` appended for an explorer past the first), and the verify job downloads every attempt's and takes each explorer's newest, so one failed explorer can be run again alone. It fails when the explore job's `attempt` output is newer than any evidence. A consumer copy that still names it `qae-artifacts` in both jobs hands a re-run's verify job whichever attempt's artifact GitHub returns.
- `actions/qae-browser` reads the names of the missing system packages from the pinned Playwright's `install-deps --dry-run` report. A Playwright bump that changes that report fails the step (never reads as nothing missing), and the catalog's `qae-browser` CI job is where that shows first.
