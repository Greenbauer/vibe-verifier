# Regression test fails on base

`regression-test-fails-on-base` fails a fix pull request unless one of its new or changed tests fails on the code before the fix and passes at the head. It is meant to replace `new-source-has-test` for test presence: a test that exists but cannot fail on the bug it was written for is what that gate lets through.

## Surfaces

- gate: `regression-test-fails-on-base`
- source: `gates/regression_test_fails_on_base.py`
- source: `gates/_regression_runners.py`
- source: `gates/_regression_python.py`

## Reach

A wired gate that runs the project's tests, so it goes in a manifest of its own (such as `.vibe-verifier-tests`), run by a job that installs the project's dependencies and writes the pull request's title and body first, the way the QAE harness writes the body:

```text
regression-test-fails-on-base --title pr-inputs/title.txt --body pr-inputs/body.md
```

```yaml
- name: Write the pull request's title and body
  env:
    GH_TOKEN: ${{ secrets.GITHUB_TOKEN }}   # the job needs pull-requests: read
    PR_NUMBER: ${{ github.event.pull_request.number }}
    REPO: ${{ github.repository }}
  run: |
    mkdir -p pr-inputs
    gh pr view "$PR_NUMBER" --repo "$REPO" --json title --jq .title > pr-inputs/title.txt
    gh pr view "$PR_NUMBER" --repo "$REPO" --json body --jq .body > pr-inputs/body.md
```

`--python` names the project's interpreter, `--tests GLOB` declares more test code it never runs (an end-to-end directory), `--timeout` bounds both runs (default 600 seconds). The rules, what it runs and why are in [the gate contract](../GATE-CONTRACT.md#regression-test-fails-on-base).

## Verify

- `tests/test_regression_test.py::test_a_test_that_fails_on_the_code_before_the_fix_passes`
- `tests/test_regression_test.py::test_a_test_that_passes_on_the_code_before_the_fix_does_not_reproduce_the_bug`
- `tests/test_regression_test.py::test_a_test_that_fails_there_only_because_what_it_imports_is_missing_does_not_show_the_bug`
- `tests/test_regression_test.py::test_the_reason_line_passes_a_fix_and_prints_the_reason`
- `tests/test_regression_test.py::test_a_fix_that_changes_only_tests_docs_or_comments_passes_without_running_a_test`
- `tests/test_regression_test.py::test_a_pull_request_that_is_not_a_fix_passes_without_running_a_test`
- `tests/test_regression_test.py::test_a_test_that_fails_at_head_is_a_red_suite_and_cannot_run`
- `tests/test_regression_test.py::test_pytest_a_test_that_passes_there_or_fails_only_on_a_missing_module_fails`

## Gotchas

- A fix is a title whose conventional-commit type is `fix`. A fix that cannot carry a test says why on a body line `regression-test: none -- <reason>`; the reason is printed as a warning and the pass is qualified.
- A test that fails before the fix only because a module, export, file or script it needs does not exist yet is a finding (exit 1), not exit 2: the gate ran, and the author can act on it (test through code that exists before the fix, or write the reason line). As exit 2 it would also break every soak, which never masks exit 2.
- It runs Vitest, pytest and unittest. A Playwright, node:test, shell or SQL test, and a script judged only by its exit code, is reported as not judged; a fix whose only tests are such files is exit 2.
- It swaps the files the fix changed back to the merge base in the working tree for the second run and always puts them back, so they must not differ from HEAD when it starts.
- A failure counts unless its error says something is missing, so a test whose own mock lacks a method the old code calls counts as reproducing; and a test that flakes at the head is exit 2.
