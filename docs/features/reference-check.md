# Second look at a design-reference FAIL

A QAE FAIL whose one anchor is a comparison with a design reference (`qae/ACn.md::step k: compared with reference <key> -> ...`) counts only when a second, fresh model sees the claimed difference too. In the explore job, `reference-check` gives that model the reference, the step's screenshot and the FAIL's claim, nothing else, and writes `qae/ACn-reference-check.json`. The `acceptance-verdict` gate judges the criterion as a PASS at that step only when the record matches the claim and both images, the reference is one the criterion names, the model did not see the difference, and the claim says nothing the images cannot settle. One model's look once failed a correct modal for a close X the reference has.

## Surfaces

- command: `reference-check`
- source: `gates/_reference_check.py`
- source: `gates/_references.py`
- source: `harnesses/reference-check/**`
- source: `actions/reference-check/**`
- source: `tests/fixtures/reference-check/**`

## Reach

The Codex lane (`harnesses/qae/explore-codex.yml`) runs `actions/reference-check` after the explorer, on the runner that holds its Codex login; the verify job's `acceptance-verdict` line reads the records with no change to the manifest. From a clone: `bin/vibe-verifier reference-check --artifacts <evidence>` ([harnesses/reference-check/README.md](../../harnesses/reference-check/README.md)).

## Verify

- `tests/test_reference_check.py::test_only_a_comparison_fail_is_asked_about_with_the_two_images_and_its_claim_in_an_empty_directory`
- `tests/test_reference_check.py::test_a_fail_the_second_look_refutes_is_judged_as_a_pass_at_its_comparison_step`
- `tests/test_reference_check.py::test_a_fail_stands_unless_its_record_refutes_exactly_this_claim_about_these_images`
- `tests/test_reference_check.py::test_a_refuted_fail_on_a_reference_the_criterion_does_not_name_still_counts`
- `tests/test_reference_check.py::test_a_fail_on_anything_but_a_comparison_is_never_put_to_a_second_look`
- `tests/test_reference_check.py::test_the_model_rejects_the_invented_close_x_and_confirms_a_missing_modal`

## Gotchas

- The model test (`Model`) needs a Codex login, so CI's unit job skips it; run it with `REFERENCE_CHECK_CODEX_HOME=~/.codex` before changing the prompt.
- The Claude lane (`explore.yml`) has no Codex login and no step, so there every comparison FAIL counts.
- A check that did not run (a usage limit, a timeout) only warns in the explore job; its FAIL stands in the verify job.
