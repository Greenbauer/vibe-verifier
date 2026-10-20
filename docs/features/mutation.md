# Changed-code mutation

`changed-code-mutation` mutates only the lines a pull request changed (StrykerJS, mutmut) and fails below a score.

## Surfaces

- gate: `changed-code-mutation`
- source: `gates/changed_code_mutation.py`
- source: `gates/_mutation_python.py`
- source: `gates/_local_guard.py`
- source: `gates/_local_only.py`
- source: `tools/changed-code-mutation/**`
- source: `tools/changed-code-mutation-python/**`

## Reach

List `changed-code-mutation` in a manifest of its own, run by a job that installs the project's dependencies first. StrykerJS and mutmut run guarded ([the rules](../GATE-CONTRACT.md#test-runs-reach-only-this-machine)): the gate's step holds no credential for anywhere else, and neither does a `.env` file in the repository, or the run is exit 2 before it starts (`--local-env NAME`, repeatable, declares a secret of the job's own local stack); a connection to anything but this machine is refused.

## Verify

- `tests/test_mutation.py::test_a_test_that_copies_the_logic_leaves_the_changed_lines_unguarded`
- `tests/test_mutation.py::test_only_the_lines_added_or_modified_at_head_are_mutated`
- `tests/test_mutation.py::test_without_the_projects_vitest_it_cannot_run_even_under_soak`
- `tests/test_mutation.py::test_a_project_vitest_that_does_not_load_cannot_run_and_says_why`
- `tests/test_mutation.py::test_python_tests_by_every_name_are_not_mutated_and_near_names_are`
- `tests/test_local_guard_runs.py::test_a_run_that_reaches_only_this_machine_is_judged_as_before`
- `tests/test_local_guard_runs.py::test_a_failure_in_a_run_that_reached_out_cannot_be_judged`
- `tests/test_local_guard_runs.py::test_a_credential_for_anywhere_else_stops_each_runner_before_it_starts`

## Gotchas

- A run that refused a connection and killed a mutant is exit 2, even when the test that met the refusal expected it: StrykerJS and mutmut run many mutants in shared processes, so the record cannot say whose kill it was. Stub the call in the test.
- The guard refuses what Node's `net` module and Python's `socket` module connect to, in every process that keeps `NODE_OPTIONS` and `PYTHONPATH`; not curl, psql, another program, a driver's native socket, a process with an environment of its own, UDP sent without connecting, or Python's name lookup before it connects ([the list](../GATE-CONTRACT.md#test-runs-reach-only-this-machine)).

- It runs the project's own Vitest or pytest; a red suite at the head is exit 2.
- A Vitest that is installed but does not load is exit 2 with the reason. `npm ci` exits 0 when an optional package's download fails, which leaves Vite without its native binding.
- It never mutates a test. A Python test is `conftest.py` or a file named `test_*.py`, `*_test.py`, `test-*.py`, `*-test.py` or `*.test.py`, the same list `new-source-has-test` reads (`gates/_contract.py`).
