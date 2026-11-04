# Plain-English copy audit

A model on the Codex CLI grades, cold, every sentence and label the QAE explorer's pages showed against four plain-English rules (answer first, short, everyday words, no system terms), and the `copy-audit` gate holds every failing grade's quote to what the page showed. Failing copy the pull request adds blocks; failing copy the base already had is listed without blocking, for a cleanup pull request to work from. Word counts and grade-level formulas, which `ui-audit` measures, pass copy that still reads badly ("They get the portal's invite email and join Acme as User when they set a password."), so this needs a reader; the gate keeps that reader's word from being the verdict.

## Surfaces

- gate: `copy-audit`
- command: `copy-audit`
- source: `gates/copy_audit.py`
- source: `gates/_copy_audit.py`
- source: `gates/_added_copy.py`
- source: `actions/qae-browser/copy-page.js`
- source: `harnesses/copy-audit/**`
- source: `actions/copy-audit/**`
- source: `tests/fixtures/copy-audit/**`

## Reach

The Codex lane (`harnesses/qae/explore-codex.yml`) grades in its explore job with `actions/copy-audit`, after the explorer; uncomment the QAE manifest template's line after `ui-audit` (the Claude lane, which shares the template, has no grade step):

    copy-audit --artifacts qae-artifacts --site-file qae-inputs/site-url --soak

Drop `--soak` once ten pull requests in a row raised no false finding. `--allow REGEX` (repeatable) skips wording the repository chose; `--all` makes existing copy block too. Before a push, from a clone: `node actions/qae-browser/ui-audit.js --copy out/copy.jsonl URL...`, then `bin/vibe-verifier copy-audit --artifacts out --repo <project> --base-ref main` ([harnesses/copy-audit/README.md](../../harnesses/copy-audit/README.md)).

## Verify

- `tests/test_copy_audit.py::test_failing_copy_the_pull_request_adds_blocks_with_its_quote_and_rewrite`
- `tests/test_copy_audit.py::test_a_short_label_is_the_pull_requests_when_all_of_it_is_on_an_added_line`
- `tests/test_copy_audit.py::test_failing_copy_the_base_already_had_is_listed_and_does_not_block`
- `tests/test_copy_audit.py::test_a_quote_that_is_not_in_the_block_is_dropped_and_said_so`
- `tests/test_copy_audit.py::test_a_block_with_no_grade_cannot_run`
- `tests/test_copy_audit.py::test_a_shard_the_grader_could_not_run_cannot_run`
- `tests/test_copy_audit.py::test_step_logs_without_a_record_cannot_run`
- `tests/test_copy_audit.py::test_the_model_gets_each_distinct_on_site_block_once_in_an_empty_directory`
- `tests/test_copy_audit.py::test_the_record_holds_the_pages_copy_and_leaves_out_values_code_and_hidden_text`
- `tests/test_copy_audit.py::test_the_model_fails_each_bad_block_and_passes_the_same_screens_written_plainly`
- `tests/test_copy_audit_wiring.py::test_the_codex_lane_grades_after_the_explorer_and_before_the_evidence_is_checked_and_kept`
- `tests/test_copy_audit_wiring.py::test_the_codex_lane_grades_the_pages_of_the_site_its_site_step_declared`
- `tests/test_copy_audit_wiring.py::test_the_action_hands_the_declared_site_to_the_grader_as_a_site_file`
- `actions/qae-browser/ui-audit.test.js::a dialog opening is read half a second later, with its copy, and a clean start leaves an empty copy record`
- `tests/test_copy_audit.py::test_a_run_that_walked_nothing_passes_on_the_empty_site_file_a_none_declaration_leaves`

## Gotchas

- A run with no record to judge (a pull request that declares `- None:` runs no explorer) passes before the site file is read: the verify job writes that file empty then.
- The model test (`Model`) needs a Codex login, so CI's unit job skips it; run it with `COPY_AUDIT_CODEX_HOME=~/.codex` and the browser toolchain before changing the prompt ([harnesses/copy-audit/README.md](../../harnesses/copy-audit/README.md), "Changing the prompt").
- A block that is a value the application shows (a name, an amount, a date, text a person typed) passes; table cells, code and fields a person types into are never recorded.
- "The pull request adds it" is four consecutive whole words of the block on an added line of `git diff <base>...HEAD`, or, for a shorter label, the whole label as a string, an element's text or an added line of its own (`"Save"`, `>Save<`, JSX text on its own line), after decoding HTML entities and straightening quotes. A label's word inside code (`unsaved`, `row.quote_status`) is not the label. A code line that is nothing but a word equal to an existing failing label (a SQL column alone on its line) does count, so that label is reported as the pull request's. Copy assembled from fragments shorter than that, or from a translation file the diff does not touch, counts as existing.
- The grade step takes the site as `site:` from the site step's outputs, not `site-file: qae-inputs/site-url`: the verify job writes that file, after the explore job, so naming it there graded nothing ("site file not found").
- A shard the model could not grade (a usage limit, a timeout) cannot run, in soak too, as every gate's could-not-run does; the explore job's step only warns, so the walk's own check stays its own.
