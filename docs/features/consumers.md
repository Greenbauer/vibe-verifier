# Inventory and pin propagation

`consumers` reads every subscribed repository through `gh` and reports pin, stub, manifest and native enforcement drift; `apply-down` plans and, with the plan's digest, opens pin-bump pull requests.

## Surfaces

- command: `consumers`
- command: `apply-down`
- source: `bin/vibe-verifier`
- source: `bin/cli/consumers.py`
- source: `bin/cli/wrapper.py`
- source: `bin/cli/apply_down.py`
- source: `gates/_pin_bump.py`
- source: `gates/_pin_merge.py`
- source: `actions/pin-bump/action.yml`

## Reach

From a clone with an admin `gh` login: `bin/vibe-verifier consumers --owner <owner>`, then `bin/vibe-verifier apply-down --owner <owner>` and `--confirm <digest>`. On a schedule: `uses: Greenbauer/vibe-verifier/actions/pin-bump@<sha>` from each owner's CI repository ([actions/pin-bump/README.md](../../actions/pin-bump/README.md)).

## Verify

- `tests/test_consumers.py::test_pin_behind_a_release_commit_is_stale`
- `tests/test_apply_down.py::test_confirm_opens_one_pr_changing_only_the_pin_line`
- `tests/test_apply_down.py::test_a_stale_digest_is_refused`
- `tests/test_pin_bump.py::test_a_busy_lane_waits_and_a_quiet_one_bumps`
- `tests/test_pin_bump.py::test_a_bump_that_moves_a_tool_version_is_held_and_the_run_says_so`
- `tests/test_pin_bump.py::test_an_older_open_bump_is_closed_as_superseded`
- `tests/test_pin_bump.py::test_an_input_the_action_no_longer_declares_holds_the_bump`
- `tests/test_pin_bump.py::test_with_merge_open_bumps_are_judged_before_the_plan_and_only_when_it_acts`
- `tests/test_pin_merge.py::test_a_ready_bump_is_squash_merged_at_the_head_it_judged_and_its_branch_deleted`
- `tests/test_pin_merge.py::test_a_line_that_is_not_a_catalog_pin_holds_the_bump`
- `tests/test_pin_merge.py::test_a_changed_file_other_than_a_workflow_holds_the_bump`
- `tests/test_pin_merge.py::test_a_check_still_running_waits_and_a_failed_one_holds`
- `tests/test_pin_merge.py::test_a_bump_behind_its_base_has_the_base_merged_in_and_is_not_merged`
- `tests/test_pin_merge.py::test_a_conflicting_bump_or_one_with_nothing_left_is_closed`
- `tests/test_wrapper.py::test_auto_bumps_the_wrapper_and_leaves_its_rulesets_to_the_wrapper`
- `tests/test_wrapper.py::test_a_ruleset_pin_is_judged_by_commits_to_the_workflow_it_names`
- `tests/test_consumers.py::test_a_stub_and_a_review_copy_with_the_names_from_before_docs_check_names_are_not_drift`

## Gotchas

- Only commits under the release paths make a pin stale; a docs-only commit does not.
- `apply-down` never touches a repository whose stub has drifted, and merges only with `--auto --act --merge` (what actions/pin-bump runs on a schedule, where `act` is true and `merge` defaults to true): a bump whose changed lines are catalog pins alone, each pin moved to the commit its branch is named for and on the catalog's main, at a head where the gate job passed, no check failed or is running, it is not behind its base and GitHub says it merges cleanly. A failed check or a line that is not a pin holds it for a person (a red run).
- `--auto` holds a bump that moves a tool version or leaves a workflow passing an undeclared input (a red run) and never moves a ruleset.
- A stub or review copy that still has the workflow and job names from before [check names](../check-names.md) is not drift: those name lines read as the template's new ones, so each repository renames on its own schedule.
