# Tool descriptors

A gate that only runs an outside tool is a JSON descriptor (command, file globs, output format) run by one shared runner, which owns the file filter and the exit contract. The catalog's live in `gates/descriptors/`; a repository keeps its own in `.vibe-verifier-tools/`, judged from the base.

## Surfaces

- source: `gates/_descriptors.py`

## Reach

Write `.vibe-verifier-tools/<gate>.json` in the repository and add `<gate>` to `.vibe-verifier` ([format](../GATE-CONTRACT.md#tool-descriptors)).

## Verify

- `tests/test_descriptors.py::test_a_bad_file_the_pr_changes_fails_with_the_mapped_fields`
- `tests/test_descriptors.py::test_only_changed_files_its_globs_match_are_passed`
- `tests/test_descriptors.py::test_a_crash_an_unreadable_report_or_exit_one_with_none_cannot_run`
- `tests/test_descriptors.py::test_results_count_whatever_the_exit_notes_advise_and_suppressions_drop`
- `tests/test_descriptors.py::test_a_descriptor_the_base_has_runs_as_the_base_has_it`
- `tests/test_descriptors.py::test_a_descriptor_running_a_python_gates_tool_cannot_run`
- `tests/test_descriptors.py::test_a_descriptor_named_like_a_python_gate_cannot_run`
- `tests/test_descriptors.py::test_the_runner_runs_a_repositorys_own_descriptor`
- `tests/test_descriptors.py::test_every_catalog_descriptor_is_owned_by_a_feature`

## Gotchas

- A descriptor's gate is not a `gate:` surface of the feature map: the `feature-map` line in `.vibe-verifier` reads gates from `gates/*.py` only. The feature that serves it lists its file instead, a `- source:` line naming `gates/descriptors/<gate>.json`, and `tests/test_descriptors.py::test_every_catalog_descriptor_is_owned_by_a_feature` fails on one no feature lists. A pull request is judged by main's feature-map arguments, so a descriptor surface added there could never pass on the pull request that adds it.

- Every descriptor is read on every run, for the duplicate check, so one in `.vibe-verifier-tools/` that cannot be read stops every descriptor gate in that repository, the catalog's included.
- `bin/vibe-verifier consumers` reads a consumer's manifest without its checkout, so it reports a gate only that repository's own descriptor defines as unknown.
