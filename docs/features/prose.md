# Prose punctuation

`prose` runs a pinned [Vale](https://vale.sh/) with the catalog's own rules on the Markdown a pull request changed. An em dash, or an en dash with a space on each side, on an added line fails the gate; a curly quote is reported and never blocks. The pull request description is judged too, as advisory.

## Surfaces

- gate: `prose`
- source: `gates/prose.py`
- source: `gates/_prose_styles/**`
- source: `gates/_tools.py`

## Reach

Add `prose` to `.vibe-verifier`. `--files GLOB` (repeatable, default `**/*.md` and `**/*.mdx`) picks the files, `--exclude GLOB` leaves some out, `--all` judges every line of every matching file, and `--body FILE` names a description to judge in place of the pull request event's.

## Verify

- `tests/test_prose.py::test_a_new_em_dash_blocks_and_one_the_file_already_had_does_not`
- `tests/test_prose.py::test_an_en_dash_aside_blocks_and_a_range_passes`
- `tests/test_prose.py::test_code_tables_blockquotes_and_link_targets_are_not_judged`
- `tests/test_prose.py::test_a_curly_quote_is_reported_but_does_not_block`
- `tests/test_prose.py::test_the_generated_rules_block_is_skipped`
- `tests/test_prose.py::test_a_renamed_file_blocks_only_on_the_lines_it_gains`
- `tests/test_prose.py::test_a_document_cannot_switch_the_rules_off`
- `tests/test_prose.py::test_an_mdx_comment_is_not_judged`
- `tests/test_prose.py::test_an_odd_file_name_encoding_or_binary_file_does_not_stop_the_gate`
- `tests/test_prose.py::test_the_description_is_judged_advisory_from_body_or_the_pull_request_event`
- `tests/test_prose.py::test_a_vale_that_fails_is_could_not_run_even_under_soak`

## Gotchas

- Only lines the pull request added count, so a document full of em dashes blocks nothing until a line is added to it, and then only that line.
- The description finding is advisory because the gate runs on a push. Editing the description does not run it again, so a blocking finding there could not be cleared by the fix.
- Vale does not judge code spans, fenced code, HTML comments, link targets, table cells (where a lone dash means "not applicable") or blockquotes (quoted text is someone else's), and the generated rules block is skipped.
- A repository's own `.vale.ini` plays no part: the gate passes its own config and `--no-global`. Vale's switches inside a document (`<!-- vale off -->`, `<!-- vale VibeVerifier.EmDash = NO -->`, the MDX `{/* vale off */}`) are defused in the copy Vale reads, so a file cannot turn the rules off.
- A binary file is skipped, and bytes that are not UTF-8 are replaced, so one odd file never stops the gate.
