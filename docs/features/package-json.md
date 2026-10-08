# package.json hygiene

`no-duplicate-package-json-keys` and `build-tools-in-devdependencies` read every `package.json`.

## Surfaces

- gate: `no-duplicate-package-json-keys`
- gate: `build-tools-in-devdependencies`
- source: `gates/no_duplicate_package_json_keys.py`
- source: `gates/build_tools_in_devdependencies.py`

## Reach

Add either gate to `.vibe-verifier`.

## Verify

- `tests/test_gates.py::test_duplicate_top_level_key_with_line`
- `tests/test_gates.py::test_build_tool_in_dependencies`
- `tests/test_gates.py::test_test_dom_and_scoped_test_and_build_packages`
- `tests/test_gates.py::test_override_needs_a_reason`

## Gotchas

- Invalid JSON is a finding, not a crash.
- A listed package that does run in production (`jsdom` parsing HTML on a server, `playwright` driving a browser in a scraper) is allowed with a reason: `"vibeVerifier": {"allowInDependencies": {"<package>": "<why>"}}` in that `package.json`.
