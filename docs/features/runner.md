# Running the subscribed gates

`bin/vibe-verifier run` reads a manifest, judges it from the base, runs each gate and aggregates their exit codes; `list` prints the catalog. The composite action and the consumer stub call it in CI.

## Surfaces

- command: `run`
- command: `list`
- source: `bin/vibe-verifier`
- source: `actions/gates/**`
- source: `consumer/vibe-verifier.yml`
- source: `gates/_contract.py`

## Reach

Write `.vibe-verifier` with one gate per line and run `bin/vibe-verifier run --repo <project> --manifest <project>/.vibe-verifier`, or copy `consumer/vibe-verifier.yml` into a project's workflows.

## Verify

- `tests/test_runner.py::test_one_failing_gate_fails_the_run_and_every_gate_still_runs`
- `tests/test_runner.py::test_verifying_nothing_is_not_a_pass`
- `tests/test_runner.py::test_soak_appended_on_the_branch_does_not_soften_the_gate`
- `tests/test_gates_action.py::test_entries_are_never_judged_from_the_base`
- `tests/test_gates.py::test_soak_never_masks_could_not_run`

## Gotchas

- A branch that weakens a gate's line is still judged by the base's line; a test of the runner needs a base branch to show it.
- A gate that cannot run is exit 2 even under `--soak`.
