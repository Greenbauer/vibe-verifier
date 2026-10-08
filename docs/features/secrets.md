# Secret scanning

`gitleaks` scans the commits since the base with a pinned gitleaks and fails on a new secret, redacted.

## Surfaces

- gate: `gitleaks`
- source: `gates/gitleaks.py`
- source: `gates/_tools.py`

## Reach

Add `gitleaks` to `.vibe-verifier`.

## Verify

- `tests/test_gitleaks.py::test_a_new_secret_fails_and_is_never_printed`
- `tests/test_gitleaks.py::test_a_secret_already_in_the_base_is_not_this_prs_finding`
- `tests/test_gitleaks.py::test_an_unresolvable_base_cannot_run`
- `tests/test_tools.py::test_it_succeeds_after_one_500`
- `tests/test_tools.py::test_a_digest_mismatch_is_not_tried_again`

## Gotchas

- The pinned binary is downloaded and checked against its sha256 on first use. A 429, a 5xx or a dropped connection is tried three times in all; no network is still exit 2, a few seconds later.
