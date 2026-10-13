# Python security analysis

`bandit` runs Bandit, Python's static security analyser, on the Python files a pull request changes, as a ratchet per check.

## Surfaces

- gate: `bandit`
- source: `gates/bandit.py`
- source: `tools/bandit/requirements.txt`

## Reach

Add `bandit` to `.vibe-verifier`; `--severity` and `--confidence` set what counts (default medium), and `--all` reports every result, for an audit.

## Verify

- `tests/test_bandit.py::test_a_new_file_with_a_result_fails_at_its_line`
- `tests/test_bandit.py::test_a_result_already_at_the_base_does_not_block_a_touch_but_another_one_does`
- `tests/test_bandit.py::test_a_line_marked_nosec_is_not_a_result`
- `tests/test_bandit.py::test_a_file_bandit_cannot_parse_cannot_run`

## Gotchas

- A file fails when it has more results of one check than at the merge base; every result of that check in the file is listed, because Bandit does not say which one is new.
- The repository's Bandit config files are not read. A line judged safe carries `# nosec <check id>` with the reason, where the review reads it.
- Tests are never read: Bandit flags every `assert`.
