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
- source: `gates/_verify_edits.py`
- source: `gates/_retry.py`
- source: `tools/qae-browser/**`
- source: `tools/codex/**`

## Reach

Copy `harnesses/qae/explore.yml` (or `explore-codex.yml`) to a consumer's workflows and `harnesses/qae/manifest` to its `.vibe-verifier-qae`; a pull request lists its criteria under `## Acceptance criteria`, each optionally naming a design reference (`[ref: <key>]`, an image the consumer's site step supplies) or the roles to walk it as (`[as: <role>, ...]`).

Criteria alone no longer define coverage. The manifest also subscribes to [ticket-coverage](ticket-coverage.md): `actions/criteria` traces every linked requirement ID before a browser skip or carried walk, using a full-history checkout. Requirements a browser cannot grade stay explicit in the plan's not-checked subsection and still need PR proof or an accepted deferred owner. A ticket written outside the code (a plan a requester submits) can mix in criteria no browser can check; the pull request proves each with a `ticket-proof: TCn -- <why> (<anchor>)` line, and that criterion is not walked.

## Verify

- `tests/test_acceptance.py::test_one_anchored_pass_per_criterion_passes`
- `tests/test_qae_explore.py::test_the_explorer_may_post_nothing`
- `tests/test_comments.py::test_every_state_follows_the_rules`
- `tests/test_qae_carry.py::test_a_walk_followed_only_by_unrendered_changes_is_carried`
- `tests/test_qae_carry.py::test_anything_a_browser_could_see_needs_a_new_walk`
- `tests/test_qae_carry.py::test_the_newest_walked_run_of_the_same_workflow_is_the_one`
- `tests/test_qae_codex_settings.py::test_empty_inputs_preserve_runner_selection`
- `tests/test_qae_codex_settings.py::test_explicit_settings_are_independent_arguments`
- `tests/test_qae_codex_settings.py::test_invalid_settings_fail_before_codex_without_echoing_them`
- `tests/test_qae_codex.py::test_the_locked_cli_supports_current_explicit_models_on_every_platform`
- `tests/test_qae_artifacts.py::test_a_step_without_a_screenshot_fails`
- `tests/test_qae_planted_evidence.py::test_a_committed_evidence_file_is_never_evidence`
- `tests/test_qae_planted_evidence.py::test_a_link_is_never_evidence`
- `tests/test_qae_artifacts.py::test_an_error_a_third_party_page_logged_is_not_judged`
- `tests/test_qae_feature_refusals.py::test_a_selected_features_step_declares_its_refusal_on_the_site_and_on_a_further_origin`
- `tests/test_qae_feature_refusals.py::test_a_feature_the_re_walk_did_not_select_declares_nothing`
- `tests/test_qae_console_pages.py::test_a_hosted_pages_own_error_passes_and_only_because_of_the_record`
- `tests/test_qae_secret_names.py::test_the_name_entered_through_run_code_signs_in`
- `tests/test_qae_secret_names.py::test_without_the_hook_the_same_sign_in_sends_the_name_and_fails_the_gate`
- `tests/test_criteria.py::test_a_pull_request_that_changes_only_unrendered_paths_needs_no_check`
- `tests/test_features.py::test_a_selected_feature_without_its_line_is_refused`
- `tests/test_features.py::test_states_left_unwalked_are_printed_and_never_a_finding`
- `tests/test_features.py::test_a_feature_with_no_walked_step_is_refused_whatever_its_line_says`
- `tests/test_features.py::test_a_feature_its_base_file_declares_no_browser_can_walk_is_set_aside_before_the_cap`
- `tests/test_features.py::test_only_the_bases_copy_declares_a_feature_it_has_out_of_the_re_walk`
- `tests/test_features.py::test_a_feature_its_file_declares_no_browser_can_walk_needs_no_line`
- `tests/test_features.py::test_a_branch_that_predates_the_map_is_not_re_walked_and_not_failed`
- `tests/test_features.py::test_a_pull_request_that_removes_the_map_cannot_be_re_walked`
- `tests/test_qa_review.py::test_changes_needed_lists_each_criterion_as_the_explorer_judged_it`
- `tests/test_qa_review.py::test_a_usage_limit_tells_the_driver_to_walk_by_hand`
- `tests/test_qa_review.py::test_any_other_explore_failure_keeps_the_plain_body`
- `tests/test_verify_edits.py::test_unchanged_edited_and_added_features_in_the_features_and_gate_logs`
- `tests/test_verify_edits.py::test_each_re_walked_row_says_whether_the_pull_request_changed_its_verify_section`
- `tests/test_verify_edits.py::test_a_walk_that_passes_passes_whatever_the_pull_request_did_to_the_steps`
- `tests/test_usage.py::test_a_failed_run_annotates_its_last_error_for_the_qa_review`
- `tests/test_qae_annotations.py::test_a_reference_the_workflow_did_not_supply_is_the_operators_to_supply`
- `tests/test_qae_annotations.py::test_a_pass_missing_a_role_is_refused`
- `tests/test_qae_annotations.py::test_the_declared_digests_reach_the_gate_on_the_manifest_line`
- `tests/test_qae_annotations.py::test_a_comparison_at_another_width_than_the_reference_is_refused`
- `tests/test_qae_explore.py::test_the_run_a_fixed_80_cut_off_gets_twice_that`
- `tests/test_qae_widths.py::test_a_criterion_missing_a_width_is_refused`
- `tests/test_qae_widths.py::test_a_branch_dropping_the_widths_is_still_told_the_bases`
- `tests/test_qae_widths.py::test_the_manifest_allowances_reach_the_explorer`
- `tests/test_qae_widths.py::test_a_branch_adding_an_allowance_is_told_only_the_bases`
- `tests/test_qae_ticket.py::test_a_declared_none_does_not_drop_the_tickets_criteria`
- `tests/test_qae_ticket.py::test_the_ticket_reaches_the_explorer_and_the_gate`
- `tests/test_qae_ticket.py::test_a_dependency_update_passes_on_its_supplied_criteria_alone`
- `tests/test_qae_ticket.py::test_a_ticket_criterion_proven_by_a_test_needs_its_anchor_at_the_head_and_no_walk`
- `tests/test_qa_review.py::test_a_ticket_criterion_the_pull_request_proves_with_a_test_is_listed_as_not_walked`
- `tests/test_qae_explore.py::test_dependabot_and_the_repositorys_pull_sync_bot_are_the_bots_that_reach_the_explorer`
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
- `tests/test_qae_chains.py::test_chained_criteria_are_one_explorers_however_many_walks_they_are`
- `tests/test_qae_chains.py::test_a_chain_goes_whole_to_one_explorer_which_is_told_to_walk_it_in_order`
- `tests/test_qae_chains.py::test_each_explorer_and_each_attempt_has_a_tag_of_its_own`
- `tests/test_qae_browser.py::test_where_sudo_needs_no_password_only_the_missing_packages_are_installed`
- `tests/test_qae_browser.py::test_a_fetch_that_runs_out_of_time_is_tried_once_more_from_the_next_mirror`
- `tests/test_qae_retry.py::test_a_retried_5xx_on_an_unchanged_endpoint_passes_and_says_why`
- `tests/test_qae_retry.py::test_a_400_is_not_an_environment_error`
- `tests/test_qae_retry.py::test_a_5xx_on_an_endpoint_the_pull_request_changes_is_its_own`
- `tests/test_qae_retry.py::test_a_retry_another_way_does_not_count`
- `tests/test_qae_retry.py::test_a_retry_after_walking_on_another_way_does_not_count`
- `tests/test_qae_retry.py::test_a_retry_that_fails_again_stays_a_fail`
- `tests/test_qae_retry.py::test_the_review_says_a_criterion_passed_on_a_retry_and_why`

## Gotchas

- A carried walk is found by its artifact, so a consumer whose explore job uploads no `qae-artifacts-*` artifact, or keeps artifacts for less time than its pull requests stay open, never carries. The criteria job also needs `actions: read`; without it the facts read fails, a warning says so, and the run walks as before. The verify job reads no facts of its own: it decides on the criteria job's `carry` output.
- The Codex action accepts optional `model` and `reasoning-effort` inputs; empty preserves the runner defaults. The action validates identifiers and effort values before invoking Codex, passes settings as separate arguments, and logs only the configured values. Selection does not guarantee a complete walk or a truthful verdict.
- The Codex CLI is locked to `0.162.0`, including its platform binaries. Model availability can be client-version filtered; verify the chosen model and effort with the pinned client and the actual runner account, not another client's catalog.

- The explore templates must stay byte-identical outside the explorer block, and the inline prompts must equal prompt.md.
- A console line names the resource an error is about, never the page that logged it. The page comes from `console-pages.jsonl`, which `actions/qae-browser/console-pages.js` writes through playwright-mcp's `--init-page`. The same record holds every request a page got an answer below 400 for, static ones included, which is the answer a request the page cancelled is matched against: the explorer's own network record leaves out a successful image, font, script or stylesheet. A consumer copy of the Claude lane without that argument writes no record, so every console error is judged as before and a cancelled static request is excused only by a network record. The hook depends on the pinned build, and the catalog's `QA browser tests` CI job runs it in the real browser.
- A credential reaches the explorer as a NAME (`secrets-file` on the Codex lane). playwright-mcp types its value for the name in `browser_fill_form` and `browser_type` only; `actions/qae-browser/secret-names.js`, a second `--init-page` hook `actions/qae-codex` loads whenever it writes secrets, does the same in every Playwright text-entry call, so a sign-in through `browser_run_code_unsafe` no longer sends the name and fails the run on the site's 400. It wraps classes of the pinned build, and the catalog's `QA browser tests` CI job runs it in the real browser. A failed call's error names the secret it was typing, never the value. A field set by a script inside the page, a name pressed key by key, or text dropped onto the page is still the name.
- The verdict is the file each explorer left in the run's own evidence (`verdicts/<n>.md`, copied there by a workflow step after the explorer). No pull request comment is read, and the explorer posts none: the verify job's QA review is the one comment ([docs/comments.md](../comments.md)). A consumer copy made before that still reads the newest comment from the workflow's identity.
- The evidence sits in `qae-artifacts/` inside the checkout of the pull request being judged, so both gates judge none of it when the branch tracks anything at, under or on the way to it, or anything in it is a symbolic link (`planted` in `gates/_contract.py`). The verify job downloads to the runner's temporary directory and stops its merge when the checkout already has a `qae-artifacts/`. A consumer copy made before that downloads into the checkout, where a committed `qae-evidence/qae-artifacts-<n>/` wins as the newest attempt: refresh the copy, not only its pins.
- The workflow's own inputs sit in `qae-inputs/` in that checkout too, so the first step of each job that writes them (criteria, explore, verify) stops when the checkout already has a `qae-inputs` file, directory or link: a committed link would move the writes elsewhere on the runner. A consumer copy made before that has no such stop until those steps are copied again.
- Opt-in (`max-shards` on the criteria job's `actions/criteria` step): a pull request with more walks than one explorer is sized for (six) is split across explorers that run in parallel, each given its share of the criteria by code. Criteria are assigned largest walks first, with stable document-order ties and original numbers; each final share stays in document order. A criterion is never split, and the first explorer keeps the job name `Acceptance criteria: walk` (`qae-explore` in a copy made before [check names](../check-names.md)) because its matrix value is blank (the others are `Acceptance criteria: walk (2)`, ...). Only the first re-walks features, and its share of the criteria starts at the re-walk's walks, so every explore job must run the features step before `actions/qae-inputs` to count them alike. Explorers on a shared preview share its data: criteria that name each other by number (`item 2`, `AC3`, or the explicit `[after: AC3]`) are a chain that one explorer walks whole and in order, and each of several explorers is told a tag (`qae-<run id>.<attempt>-e<n>`) to put on every record it creates and to open or judge only records that carry it or existed before the run. A dependency that names no number is not chained. The browser holds each explorer to its tag: `actions/qae-inputs` writes `qae-inputs/isolation.json` (the tag and the record ids criteria outside the share name), and `console-pages.js` drops objects carrying another explorer's tag from every JSON answer, answers 409 itself to a request on one or to a DELETE of one of those fixtures, and records what each explorer created and opened; the qae-artifacts gate fails the run on each refusal and on any record one explorer opened that another created. A site step's throwaway login needs the explorer's number in its name.
- One of several explorers renames what it left so every explorer's evidence fits in one directory (`session-<n>-*/`, `console-<n>-*.log`, `shard-<n>/`), and the verify job refuses two explorers' different files at one path. Nothing is overwritten. The same step holds the explorer to its share (`actions/qae-inputs`' `share-ids` output, never a file the explorer can rewrite): the files of a criterion outside it move under `shard-<n>/qae/`, where the network gate still reads them, and its `acceptance-check` line is left out of `verdicts/<n>.md`, so an explorer that walked another's criterion neither fails the merge nor has its verdict counted. A copy of the step without the declared share fails before keeping anything.
- A pull request body lists criteria only under its `Acceptance criteria` heading; the plain-list reading is for a ticket's criteria file. A Dependabot body has no such heading, so its criteria are the ones the consumer's workflow supplies.
- A design reference's digest reaches the gate as an explore job output, never as a file the explorer could rewrite; a reference the workflow did not supply is refused, never invented.
- The Claude lane's `--max-turns` is the qae-inputs step's `max-turns` output, sized to the run's walks; a consumer copy that still passes a fixed number keeps that number.
- A feature re-walk covers at most `--max-states` states of each feature. A `regression-check` FAIL means a walked step broke; a state not reached goes on a `regression-skip` line, which no gate judges. A consumer copy with the older prompt still tells the explorer to walk every state. A feature no browser can walk is taken out by `re-walk: none -- <why>` in its own file (read from the base), never by a skip line. A FAIL is a warning, not a finding, only when the explorer's `regression-known` line is anchored to a step of its walk of the base branch's site (`qae/features/base/<id>.md`) whose screenshot is there; the explorer walks that site only when the workflow wrote `qae-inputs/base-site.md`, and the gate checks the evidence, not that file. The network check still judges every request to the pull request's own declared origins, so a base-branch site that shares one (an API or database) can still fail the run through `qae-artifacts`.
- A pull request opened before the feature map landed on the base has no feature file to re-walk: `features` prints `features: 0` and why, and `acceptance-verdict` passes labelled `re-walk not run: the branch predates the feature map`. Any other head with no feature file, a removed map included, is exit 2 in both.
- The evidence artifact is `qae-artifacts-<run_attempt>` (`-<n>` appended for an explorer past the first), and the verify job downloads every attempt's and takes each explorer's newest, so one failed explorer can be run again alone. It fails when the explore job's `attempt` output is newer than any evidence. A consumer copy that still names it `qae-artifacts` in both jobs hands a re-run's verify job whichever attempt's artifact GitHub returns.
- A step the explorer retried after an environment error counts only on the evidence `gates/_retry.py` names: it is step k+1, makes step k's page-changing calls again (per the session log), no later step repeats it, and step k's own network or console record, saved by its tool between step k's screenshot and the retry's, showing a 5xx, a failed connection or websocket on a URL no changed path names, or Vercel's deployment protection, and nothing of that kind on a URL a changed path does name. Which URL a changed path names is a word match, so it errs toward no retry, and a retry that does not count fails `qae-artifacts`. Whether the retried step then held is still the explorer's verdict: a counted retry excuses a repeat of the same error, by status and URL anywhere in the run, so a FAIL after it rests on acceptance-verdict alone. A consumer manifest without `--changed-files` on its `qae-artifacts` line counts only the protection class.
- `actions/qae-browser` reads the names of the missing system packages from the pinned Playwright's `install-deps --dry-run` report. A Playwright bump that changes that report fails the step (never reads as nothing missing), and the catalog's `QA browser tests` CI job is where that shows first.
