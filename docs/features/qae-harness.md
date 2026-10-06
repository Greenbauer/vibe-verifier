# Browser acceptance verification (QAE)

The QAE harness: an explorer walks a pull request's acceptance criteria (and, opt-in, the features it touches) in a real browser, and deterministic gates judge its step logs, screenshots and verdict.

## Surfaces

- gate: `acceptance-verdict`
- gate: `qae-artifacts`
- command: `criteria`
- command: `features`
- command: `qa-review`
- command: `qae-inputs`
- command: `tool`
- source: `harnesses/qae/**`
- source: `actions/criteria/**`
- source: `actions/features/**`
- source: `actions/qa-review/**`
- source: `actions/qae-inputs/**`
- source: `actions/qae-browser/**`
- source: `actions/qae-codex/**`
- source: `actions/usage/**`
- source: `gates/acceptance_verdict.py`
- source: `gates/qae_artifacts.py`
- source: `gates/_acceptance.py`
- source: `gates/_features.py`
- source: `tools/qae-browser/**`
- source: `tools/codex/**`

## Reach

Copy `harnesses/qae/explore.yml` (or `explore-codex.yml`) to a consumer's workflows and `harnesses/qae/manifest` to its `.vibe-verifier-qae`; a pull request lists its criteria under `## Acceptance criteria`, each optionally naming a design reference (`[ref: <key>]`, an image the consumer's site step supplies) or the roles to walk it as (`[as: <role>, ...]`).

## Verify

- `tests/test_acceptance.py::test_one_anchored_pass_per_criterion_passes`
- `tests/test_qae_artifacts.py::test_a_step_without_a_screenshot_fails`
- `tests/test_criteria.py::test_a_pull_request_that_changes_only_unrendered_paths_needs_no_check`
- `tests/test_features.py::test_a_selected_feature_without_its_line_is_refused`
- `tests/test_qa_review.py::test_changes_needed_lists_each_criterion_as_the_explorer_judged_it`
- `tests/test_qae_annotations.py::test_a_reference_the_workflow_did_not_supply_is_the_operators_to_supply`
- `tests/test_qae_annotations.py::test_a_pass_missing_a_role_is_refused`
- `tests/test_qae_annotations.py::test_the_declared_digests_reach_the_gate_on_the_manifest_line`
- `tests/test_runner.py::test_the_qae_browser_toolchain_installs_from_its_lockfile`

## Gotchas

- The explore templates must stay byte-identical outside the explorer block, and the inline prompts must equal prompt.md.
- The verdict is taken only from the workflow's own identity, never from another commenter.
- A design reference's digest reaches the gate as an explore job output, never as a file the explorer could rewrite; a reference the workflow did not supply is refused, never invented.
