# Dependency vulnerabilities

`osv-scanner` checks the dependency files a pull request changes (Python requirements files and lockfiles, npm, pnpm and yarn lockfiles) against the OSV database, as a ratchet per vulnerability.

## Surfaces

- gate: `osv-scanner`
- source: `gates/osv_scanner.py`

## Reach

Add `osv-scanner` to `.vibe-verifier`; `--all` reports every known vulnerability in every tracked dependency file, for an audit.

## Verify

- `tests/test_osv_scanner.py::test_adding_a_vulnerable_pin_fails_at_its_line`
- `tests/test_osv_scanner.py::test_a_pin_already_vulnerable_at_the_base_never_blocks_but_a_new_one_does`
- `tests/test_osv_scanner.py::test_any_requirements_file_name_and_an_npm_lockfile_are_read`
- `tests/test_osv_scanner.py::test_a_vulnerability_the_repository_ignores_with_a_reason_is_not_a_finding`

## Gotchas

- It needs the network (osv.dev); offline, the gate cannot run.
- Only what the file states is read (`--no-resolve`): an unpinned requirement is read at its lower bound.
- A vulnerability that does not apply is listed with its reason in an `osv-scanner.toml` beside the file. `tools/changed-code-mutation/osv-scanner.toml` is the example: the vitest stub's `0.0.0` version falls inside vitest's advisory ranges.
