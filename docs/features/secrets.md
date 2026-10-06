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

## Gotchas

- The pinned binary is downloaded and checked against its sha256 on first use; no network is exit 2.
