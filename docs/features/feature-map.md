# Feature map drift

`feature-map` keeps a repository's feature map true against its code: every declared surface owned, every listing real, every anchor resolving.

## Surfaces

- gate: `feature-map`
- source: `gates/feature_map.py`
- source: `gates/_features.py`
- source: `gates/_acceptance.py`
- source: `docs/feature-map.md`
- source: `docs/features/**`

## Reach

Add `docs/features/` and a `feature-map --surface ...` line to `.vibe-verifier`; this repository's own line is the example.

## Verify

- `tests/test_feature_map.py::test_a_surface_no_feature_lists_is_owned_by_no_feature`
- `tests/test_feature_map.py::test_a_listed_surface_the_code_no_longer_declares_is_stale`
- `tests/test_feature_map.py::test_a_verify_anchor_must_resolve`
- `tests/test_feature_map.py::test_without_a_surface_the_pass_says_completeness_was_not_checked`

## Gotchas

- The whole map is judged on every run, so a new gate or subcommand lands with its feature file.
