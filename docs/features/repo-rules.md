# Repository rules and the generated rules block

`repo-rules` runs a repository's own ast-grep rules and catalog packs as a per-file ratchet; `rules-doc` writes the generated block that lists them, which `--doc` keeps current.

## Surfaces

- gate: `repo-rules`
- command: `rules-doc`
- source: `gates/repo_rules.py`
- source: `gates/_tools.py`
- source: `rules/**`
- source: `skills/**`

## Reach

Add `.vibe-verifier-rules/` with a rule and its test, subscribe with `repo-rules`, and run `bin/vibe-verifier rules-doc --write AGENTS.md`.

## Verify

- `tests/test_repo_rules.py::test_a_new_violation_fails_and_an_identical_one_already_at_the_base_does_not`
- `tests/test_repo_rules.py::test_editing_a_violation_in_place_is_not_new`
- `tests/test_rules_doc.py::test_write_appends_the_block_then_replaces_it_in_place`
- `tests/test_rules_doc.py::test_a_stale_block_is_a_finding_on_its_first_line_and_soak_reports_it_only`

## Gotchas

- A rule file the base has judges the pull request as the base has it.
