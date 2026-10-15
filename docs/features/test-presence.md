# New source has a test

`new-source-has-test` fails a new source file that no test names, imports or runs.

## Surfaces

- gate: `new-source-has-test`
- source: `gates/new_source_has_test.py`

## Reach

Add `new-source-has-test` to `.vibe-verifier`; `--source` and `--exclude` narrow it.

## Verify

- `tests/test_gates.py::test_sibling_test_satisfies`
- `tests/test_gates.py::test_an_untested_python_file_fails`
- `tests/test_gates.py::test_a_test_that_runs_it_as_a_script_counts`
- `tests/test_gates.py::test_a_hyphen_or_dot_test_named_for_it_counts`

## Gotchas

- It proves a test exists, not that it tests anything; `changed-code-mutation` measures that.
- `regression-test-fails-on-base` is meant to replace it for test presence: on a fix it asks that a new or changed test fails on the code before the fix. Unlike this gate, it asks nothing of a pull request that is not a fix.
- A Python file is a test only by name: `test_*.py`, `*_test.py`, `test-*.py`, `*-test.py` or `*.test.py`. The list lives in `gates/_contract.py`, and `cognitive-complexity` and `changed-code-mutation` read the same one.
  A helper the tests import is source, and needs a test that imports it.
