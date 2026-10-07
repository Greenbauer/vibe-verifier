# Workflow lint and audit

`actionlint` and `zizmor` judge the workflows a pull request adds or changes, with pinned tools.

## Surfaces

- gate: `actionlint`
- gate: `zizmor`
- source: `gates/actionlint.py`
- source: `gates/zizmor.py`
- source: `gates/_workflows.py`
- source: `gates/_tools.py`

## Reach

Add `actionlint` and `zizmor` to `.vibe-verifier`; `--all` judges every tracked workflow.

## Verify

- `tests/test_workflow_lint.py::test_a_bad_workflow_the_pr_adds_fails`
- `tests/test_workflow_lint.py::test_a_pre_existing_problem_is_not_this_prs_finding_unless_all`
- `tests/test_workflow_lint.py::test_a_bad_workflow_the_pr_adds_fails_with_zizmors_idents`

## Gotchas

- Every finding in a touched workflow counts, including ones the base already had: subscribe clean.
