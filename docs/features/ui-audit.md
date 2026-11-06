# Layout and copy audit

`ui-audit` fails a QAE run whose browser measured a layout or copy defect on the site under test: controls in one row that do not line up, bordered controls touching each other or their container's edge, a spinner off its button's middle, a count, initials or check mark off its circle's middle, a wall of text or a very long sentence, a raw value such as `in_review` or `undefined`, text or a value its box cuts off, and two buttons in one dialog or form whose names a person cannot tell apart ("Send invite" beside "Send invite"), and a short label in a pill, badge, button or tab that takes two lines (a margin pill with its arrow above "12.5%"), and a table that scrolls sideways while the layout around it has room for all of it. A finding blocks only when the pull request adds the text of the element it names or of its neighbour; one on a screen the base already had is listed without blocking, for a cleanup pull request to work from. The QAE explorer never judges these: the harness's layout hook measures every page the explorer opens, at every width it uses, from rendered rectangles and computed styles, and writes `ui-audit.jsonl` beside the run's other evidence. A screenshot diff cannot catch them, because a baseline that already has the defect passes forever.

The same audit runs from a shell before a push: `node actions/qae-browser/ui-audit.js [--width N]... [--storage-state FILE] [--channel chrome] URL...` prints one line per finding at 1280 and 375 pixels by default and exits 1 when there is one.

## Surfaces

- gate: `ui-audit`
- source: `gates/ui_audit.py`
- source: `actions/qae-browser/ui-audit.js`
- source: `actions/qae-browser/ui-audit-page.js`
- source: `actions/qae-browser/glyph-page.js`
- source: `actions/qae-browser/name-page.js`
- source: `actions/qae-browser/wrap-page.js`
- source: `actions/qae-browser/scroll-page.js`
- source: `gates/_added_copy.py`

## Reach

Add a line to the QAE manifest (`.vibe-verifier-qae`), after `qae-artifacts`:

    ui-audit --artifacts qae-artifacts --site-file qae-inputs/site-url --soak

`--soak` reports without failing (in the job log and under Not yet blocking in the QA review); drop it once ten pull requests in a row raised no false finding. `--all` makes a finding on the base's screens block too. A second line with `--rules R` and no `--soak` blocks on one rule while the rest soak. `--rules R[,R...]` judges only some rules, and `--allow REGEX` (repeatable) skips a finding whose element path or text matches, for a design the repository chose. Both QAE lanes load the hook: `harnesses/qae/explore.yml` passes `actions/qae-browser`'s `ui-audit` output as a second `--init-page`, and `actions/qae-codex` passes the file itself. A repository with its own copy of `explore.yml` needs that second `--init-page` in its copy before it adds the manifest line: without the hook there is no record, and the gate cannot run.

## Verify

- `tests/test_qae_ui_audit.py::test_every_rule_finds_its_defect_and_the_same_screens_done_right_are_clean`
- `tests/test_qae_ui_audit.py::test_a_measured_defect_fails_and_names_the_page_width_rule_and_element`
- `tests/test_qae_ui_audit.py::test_rows_of_one_table_are_one_finding_with_a_count`
- `tests/test_qae_ui_audit.py::test_a_defect_on_a_screen_the_base_already_had_is_listed_and_does_not_block`
- `tests/test_qae_ui_audit.py::test_a_defect_on_an_element_that_shows_no_text_does_not_block`
- `tests/test_qae_ui_audit.py::test_two_buttons_of_one_name_the_pull_request_adds_block`
- `tests/test_qae_ui_audit.py::test_a_page_off_the_declared_site_is_not_judged`
- `tests/test_qae_ui_audit.py::test_rules_and_allow_narrow_what_is_judged`
- `tests/test_qae_ui_audit.py::test_step_logs_without_a_record_cannot_run`
- `tests/test_qae_ui_audit.py::test_each_explorers_record_is_read_from_its_shard`
- `tests/test_qae_ui_audit.py::test_both_lanes_load_the_hook`
- `tests/test_qae_ui_audit.py::test_the_hooks_node_tests_pass`
- `actions/qae-browser/ui-audit.test.js::a clean tab still leaves a record, empty, so the gate can tell clean pages from a hook never loaded`
- `tests/test_qae_ui_audit.py::test_a_run_that_walked_nothing_passes_on_the_empty_site_file_a_none_declaration_leaves`

## Gotchas

- A run with no record to judge (a pull request that declares `- None:` runs no explorer) passes before the site file is read: the verify job writes that file empty then.
- The rules and their thresholds are in the header of `actions/qae-browser/ui-audit-page.js`. Each was checked against two subscribed apps' real screens (a quote builder, lists, dashboards, a people page, an add-user dialog, 2026-11-03) and narrowed where it flagged a design: a pill track with even padding, a tab toggle whose idle tab has no border, a map's zoom bar, a header button beside a search field in the page below, a list row with only a divider under it.
- A badge's mark is measured by its ink, not its line box: a digit has no descender, so text centered by its line box (`line-height` equal to the height, or flex centering) sits high when the font's ascent and descent are uneven. A CSS-drawn tick (a `::before` or `::after` with two borders, rotated) is measured through its transform. Checked on a subscribed app's notification count and request tab badge, with its own font (each 1.1 to 1.4px high), and its circle checks (2026-11-04).
- `duplicate-name` compares the buttons a person can press (visible, not disabled) inside one dialog, else one form: equal names, or one name the other with words added ("Create account", "Create account now"). A choice that a submit button acts on is a radio, which it does not judge. Buttons in different rows of one list or table (each row's Remove) are told apart by their row. It came from a subscribed app's add-someone dialog (2026-11-04), which showed a pressed "Send invite" choice beside the "Send invite" submit, and whose own test picked them apart with `.first()` and `.last()`.
- "The pull request adds it" is copy-audit's test (four consecutive words, or a whole short label, on an added line of `git diff <base>...HEAD`), applied to the element's text and its neighbour's. A defect a pull request causes on an existing label (a padding change that makes two old buttons touch) and one on an element with no text are listed, not blocked: nothing in the diff names them.
- A spinner shows only while something loads, so the hook also measures the moment a button gains an element whose class names a spinner or loader, or a `role=progressbar`.
- A dialog opens with no load or navigation, so the hook also measures half a second after a dialog, alert or status message appears. At each measure it also writes the page's copy record, `copy.jsonl`, for the [copy audit](copy-audit.md).
- The record holds what the page shows, as the screenshots do, never a password field's value.
- A run with step logs and no record cannot run: the hook was not loaded, and a clean soak and a broken one would otherwise look the same.
- `wrapped-label` judges labels of four words and 24 characters or fewer: a sentence in a pill may wrap. In a pill or badge (boxed, every corner rounded to half its height) an icon counts as part of the label, so an arrow above its percentage is two lines; in a button or tab it does not, so an icon placed above the label by design is not flagged, but the label's own text wrapping is. It came from a subscribed app's quote panel (2026-11-06), whose margin pill showed its arrow above "12.5%" in a card with room for both. Run on that screen, the rule found it at 1280 and 375 pixels. Text only a screen reader reads (a clipped `.sr-only` span) is no line, and an icon font's ligature ("add") above a button's label counts as its icon.
- `table-scroll` measures room, not width: a table scrolls sideways (by more than 8px) while some box around it, up to its dialog, else the page's main region (`main` or `role=main`), else the body, has unused width beside it at least as wide as what the table hides. A visible box beside it at the same height (a summary card, also through a `display:contents` wrapper) is not room, nor is the navigation beside the main region or the page behind a dialog's overlay, and a box that scrolls for something else beside a table that fits is not judged, so a wide table on a phone and a table beside a card that fills the row are not flagged. Room often appears only at wide windows: a subscribed app's quote page capped its two-column grid at 1180px, so at 1920 and 2560 pixels its lane table scrolled beside 460 and 1100px of empty page, while at 1280 the summary card filled the row (2026-11-06). The audit judges only the widths the explorer walks, so a repository that wants this caught names a wide one too (`--widths 1920,1280,375` on its `qae-artifacts` line).
