# Inventory and pin propagation

`consumers` reads every subscribed repository through `gh` and reports pin, stub, manifest and native enforcement drift; `apply-down` plans and, with the plan's digest, opens pin-bump pull requests.

## Surfaces

- command: `consumers`
- command: `apply-down`
- source: `bin/vibe-verifier`

## Reach

From a clone with an admin `gh` login: `bin/vibe-verifier consumers --owner <owner>`, then `bin/vibe-verifier apply-down --owner <owner>` and `--confirm <digest>`.

## Verify

- `tests/test_consumers.py::test_pin_behind_a_release_commit_is_stale`
- `tests/test_apply_down.py::test_confirm_opens_one_pr_changing_only_the_pin_line`
- `tests/test_apply_down.py::test_a_stale_digest_is_refused`
- `tests/test_wrapper.py::test_a_ruleset_pin_is_judged_by_commits_to_the_workflow_it_names`
- `tests/test_consumers.py::test_a_stub_and_a_review_copy_with_the_names_from_before_docs_check_names_are_not_drift`

## Gotchas

- Only commits under the release paths make a pin stale; a docs-only commit does not.
- `apply-down` never merges, and never touches a repository whose stub has drifted.
- A stub or review copy that still has the workflow and job names from before [check names](../check-names.md) is not drift: those name lines read as the template's new ones, so each repository renames on its own schedule.
