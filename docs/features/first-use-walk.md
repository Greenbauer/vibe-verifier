# First-use walk

`first-use-walk` fails a pull request whose site a person new to it could not use: a second QAE session, given only the task the body's `## First-use task` section states and the site, got stuck, could not get back to the page it started on, or tripped on something on the way. The criteria walk checks what the criteria say; this one checks what they never said ([harness](../../harnesses/first-use/README.md)).

## Surfaces

- gate: `first-use-walk`
- command: `first-use-prompt`
- source: `gates/first_use_walk.py`
- source: `gates/_first_use.py`
- source: `harnesses/first-use/**`
- source: `actions/first-use/**`

## Reach

A wrapper's QAE workflow runs the walk in a job of its own (harnesses/first-use/README.md, "In a QAE workflow") and its verify job places the walk's artifact at `qae-artifacts/first-use/`. Then the QAE manifest line:

    first-use-walk --artifacts qae-artifacts --body qae-inputs/pr-body.md --soak

The catalog's templates (`harnesses/qae/explore.yml`, `explore-codex.yml`) do not have the job yet.

## Verify

- `tests/test_first_use_walk.py::test_the_walk_of_the_customer_access_screens_fails_on_each_thing_a_person_new_to_it_tripped_on`
- `tests/test_first_use_walk.py::test_the_same_task_on_the_fixed_screens_passes`
- `tests/test_first_use_walk.py::test_back_or_a_typed_address_after_the_start_fails_though_the_verdict_says_done`
- `tests/test_first_use_walk.py::test_a_walk_that_ends_away_from_its_start_fails_though_the_verdict_says_done`
- `tests/test_first_use_walk.py::test_a_back_shot_taken_early_or_a_move_after_the_last_shot_fails`
- `tests/test_first_use_walk.py::test_a_walk_that_switches_tabs_anywhere_fails`
- `tests/test_first_use_walk.py::test_a_verdict_must_cite_a_step_the_walk_has_and_name_each_outcome_once`
- `tests/test_first_use_walk.py::test_a_step_without_its_screenshot_and_a_walk_without_start_or_back_fail`
- `tests/test_first_use_walk.py::test_criteria_and_no_task_is_a_finding_and_nothing_to_walk_needs_no_walk_even_with_a_task`
- `tests/test_first_use_walk.py::test_a_task_with_no_walk_or_no_session_log_cannot_run`
- `tests/test_first_use_walk.py::test_the_task_is_the_sections_text_up_to_the_next_heading_without_comments`
- `tests/test_first_use_walk.py::test_the_prompt_holds_the_task_and_the_site_and_nothing_of_the_criteria`
- `tests/test_first_use_walk.py::test_a_walk_back_by_the_cards_breadcrumb_passes`
- `tests/test_first_use_walk.py::test_a_card_left_by_the_browsers_back_fails`
- `tests/test_first_use_action.py::test_a_task_writes_the_prompt_says_walk_and_takes_the_body_away`
- `tests/test_first_use_action.py::test_no_task_writes_no_prompt_says_no_walk_and_still_takes_the_body_away`

## Gotchas

- The page a step is on is the last `Page URL` a call before its screenshot reported: playwright-mcp writes none in a screenshot's result.
- A body that declares `- None: <why>`, or lists no criteria, needs no walk and passes, with a task or without: a workflow walks nothing for it.
- A task with no walk, verdict or session log cannot run, in soak too: the walk's job failed or was skipped.
