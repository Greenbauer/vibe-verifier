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
- `tests/test_qae_artifacts.py::test_a_step_without_a_screenshot_fails`
- `tests/test_criteria.py::test_a_pull_request_that_changes_only_unrendered_paths_needs_no_check`
- `tests/test_features.py::test_a_selected_feature_without_its_line_is_refused`
- `tests/test_features.py::test_states_left_unwalked_are_printed_and_never_a_finding`
- `tests/test_features.py::test_a_feature_with_no_walked_step_is_refused_whatever_its_line_says`
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
- `tests/test_qae_browser.py::test_where_sudo_needs_no_password_only_the_missing_packages_are_installed`
- `tests/test_qae_browser.py::test_a_fetch_that_runs_out_of_time_is_tried_once_more_from_the_next_mirror`

## Gotchas

- The explore templates must stay byte-identical outside the explorer block, and the inline prompts must equal prompt.md.
- The verdict is taken only from the workflow's own identity, never from another commenter.
- A pull request body lists criteria only under its `Acceptance criteria` heading; the plain-list reading is for a ticket's criteria file. A Dependabot body has no such heading, so its criteria are the ones the consumer's workflow supplies.
- A design reference's digest reaches the gate as an explore job output, never as a file the explorer could rewrite; a reference the workflow did not supply is refused, never invented.
- The Claude lane's `--max-turns` is the qae-inputs step's `max-turns` output, sized to the run's walks; a consumer copy that still passes a fixed number keeps that number.
- A feature re-walk covers at most `--max-states` states of each feature. A `regression-check` FAIL means a walked step broke; a state not reached goes on a `regression-skip` line, which no gate judges. A consumer copy with the older prompt still tells the explorer to walk every state.
- The evidence artifact is `qae-artifacts-<run_attempt>`, and the verify job downloads the attempt the explore job reports as its `attempt` output. A consumer copy that still names it `qae-artifacts` in both jobs hands a re-run's verify job whichever attempt's artifact GitHub returns.
- `actions/qae-browser` reads the names of the missing system packages from the pinned Playwright's `install-deps --dry-run` report. A Playwright bump that changes that report fails the step (never reads as nothing missing), and the catalog's `qae-browser` CI job is where that shows first.
