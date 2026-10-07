# Universal checks

`universal-checks` grades a repository's standing rules, `ci/universal-checks.md`: every member of each rule's population (a regex over the code in a path glob) must conform to the rule's conforms regex. The rules are read at the merge base, so a pull request cannot weaken its own.

## Surfaces

- gate: `universal-checks`
- source: `gates/universal_checks.py`

## Reach

Write `ci/universal-checks.md` with a `Universal checks:` heading and one `` - `<population>` in `<glob>` conforms to `<conforms>` `` bullet per rule, then subscribe with a `universal-checks` line in `.vibe-verifier`.

## Verify

- `tests/test_universal_checks.py::test_every_member_conforming_passes_and_a_comment_is_not_a_member`
- `tests/test_universal_checks.py::test_a_member_that_does_not_conform_fails_at_its_line`
- `tests/test_universal_checks.py::test_a_pull_request_that_weakens_the_rules_is_still_judged_by_the_merge_base`
- `tests/test_universal_checks.py::test_an_empty_population_fails_closed_even_under_soak`

## Gotchas

- An empty population fails closed: retire a rule by removing its line first, merge, then delete the code it governed.
- The glob is `fnmatch`, so `*` crosses `/`; comments are recognized only in the `//` and `/* */` forms.
