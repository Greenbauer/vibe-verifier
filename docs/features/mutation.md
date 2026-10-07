# Changed-code mutation

`changed-code-mutation` mutates only the lines a pull request changed (StrykerJS, mutmut) and fails below a score.

## Surfaces

- gate: `changed-code-mutation`
- source: `gates/changed_code_mutation.py`
- source: `gates/_mutation_python.py`
- source: `tools/changed-code-mutation/**`
- source: `tools/changed-code-mutation-python/**`

## Reach

List `changed-code-mutation` in a manifest of its own, run by a job that installs the project's dependencies first.

## Verify

- `tests/test_mutation.py::test_a_test_that_copies_the_logic_leaves_the_changed_lines_unguarded`
- `tests/test_mutation.py::test_only_the_lines_added_or_modified_at_head_are_mutated`
- `tests/test_mutation.py::test_without_the_projects_vitest_it_cannot_run_even_under_soak`

## Gotchas

- It runs the project's own Vitest or pytest; a red suite at the head is exit 2.
