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
- `tests/test_feature_map.py::test_a_branch_that_predates_the_map_passes_and_says_it_was_not_checked`
- `tests/test_feature_map.py::test_a_pull_request_opened_before_the_map_landed_is_not_failed_for_it`
- `tests/test_feature_map.py::test_a_pull_request_that_removes_the_map_cannot_run_and_soak_does_not_hide_it`
- `tests/test_feature_map.py::test_a_map_only_one_of_two_merge_bases_has_was_removed`
- `tests/test_feature_map.py::test_a_named_base_the_checkout_lacks_is_never_replaced_by_main`
- `tests/test_feature_map.py::test_a_pull_request_that_introduces_the_map_is_judged_on_its_own_map`
- `tests/test_feature_map.py::test_with_nothing_to_tell_them_apart_a_missing_map_cannot_run`

## Gotchas

- The whole map is judged on every run, so a new gate or subcommand lands with its feature file.
- A head with no feature file is judged from the base, by the commits alone: a branch that predates the map (none at any merge base, some at the base) passes labelled `not checked`, and every other one is exit 2, a removed map included. So a consumer's checkout needs the base (`fetch-depth: 0`), or a pull request opened before its map landed stays red.
