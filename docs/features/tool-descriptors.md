# Tool descriptors

A gate that only runs an outside tool is a JSON descriptor (command, file globs, output format) run by one shared runner, which owns the file filter and the exit contract. The catalog's live in `gates/descriptors/`; a repository keeps its own in `.vibe-verifier-tools/`, judged from the base.

## Surfaces

- source: `gates/_descriptors.py`
- source: `gates/_parsers.py`
- source: `gates/_ratchet.py`

## Reach

Write `.vibe-verifier-tools/<gate>.json` in the repository and add `<gate>` to `.vibe-verifier` ([format](../GATE-CONTRACT.md#tool-descriptors)).

A repository's own ESLint, with its own config and plugins, is the usual case: the `eslint` parser reads its report, and the job that runs the gates installs the repository's packages and puts `node_modules/.bin` on PATH first. Leave out `"ratchet": "count"`: the ratchet lints the merge base's copy of each file in a folder of its own, where the repository's `eslint.config.*` is not, so ESLint exits 2 and the gate cannot run. An error blocks and a warning is only reported, so a repository with no ESLint errors on its default branch needs no ratchet.

```json
{
  "gate": "eslint",
  "description": "Fail when a file this pull request changed has an ESLint error under the repository's own config.",
  "tool": "eslint",
  "args": ["--format", "json", "--no-warn-ignored", "--"],
  "files": ["**/*.js", "**/*.jsx", "**/*.mjs", "**/*.cjs", "**/*.ts", "**/*.tsx"],
  "output": {"parser": "eslint"}
}
```

## Verify

- `tests/test_descriptors.py::test_a_bad_file_the_pr_changes_fails_with_the_mapped_fields`
- `tests/test_descriptors.py::test_only_changed_files_its_globs_match_are_passed`
- `tests/test_descriptors.py::test_a_crash_an_unreadable_report_or_exit_one_with_none_cannot_run`
- `tests/test_descriptors.py::test_results_count_whatever_the_exit_notes_advise_and_suppressions_drop`
- `tests/test_descriptors.py::test_a_descriptor_the_base_has_runs_as_the_base_has_it`
- `tests/test_descriptors.py::test_a_descriptor_running_a_python_gates_tool_cannot_run`
- `tests/test_descriptors.py::test_a_descriptor_named_like_a_python_gate_cannot_run`
- `tests/test_descriptors.py::test_the_runner_runs_a_repositorys_own_descriptor`
- `tests/test_descriptors.py::test_a_declared_option_is_passed_to_the_tool_only_when_given`
- `tests/test_descriptors.py::test_an_option_spec_maps_the_flag_with_its_default_and_choices`
- `tests/test_descriptors.py::test_a_rule_only_blocks_a_file_where_its_count_rose`
- `tests/test_descriptors.py::test_a_new_file_with_a_finding_blocks_and_a_moved_one_does_not`
- `tests/test_descriptors_eslint.py::test_eslint_errors_block_and_its_warnings_advise`
- `tests/test_descriptors_eslint.py::test_eslint_warnings_alone_pass`
- `tests/test_descriptors_eslint.py::test_a_file_eslint_could_not_parse_blocks`
- `tests/test_descriptors_eslint.py::test_a_message_eslint_gives_no_rule_is_named_for_what_it_is`

## Gotchas

- Every descriptor is read on every run, for the duplicate check, so one in `.vibe-verifier-tools/` that cannot be read stops every descriptor gate in that repository, the catalog's included.
- `bin/vibe-verifier consumers` reads a consumer's manifest without its checkout, so it reports a gate only that repository's own descriptor defines as unknown.
