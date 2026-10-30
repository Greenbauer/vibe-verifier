# Ticket requirement coverage

`ticket-coverage` traces plan requirements to criteria and linked ticket requirements to PR proof or an accepted owner. Criteria alone no longer define coverage: a subset implemented by a PR cannot become the whole spec by being copied into Acceptance criteria.

## Surfaces

- gate: `ticket-coverage`
- source: `gates/ticket_coverage.py`
- source: `gates/_ticket.py`
- source: `actions/ticket/**`
- source: `gates/_plan_store.py`

## Reach

Subscribe with both lines where those checks belong, or just the one needed:

```text
# .vibe-verifier: plan changes checked at HEAD
ticket-coverage --plans 'docs/plans/*.md'
# or, for a plan with a ledger table whose owner column is "Family"
ticket-coverage --plans 'docs/plans/*.md' --keyed-column Family
# .vibe-verifier-qae: the workflow's PR body file
ticket-coverage --body qae-inputs/pr-body.md
```

`--plans` is repeatable and uses fnmatch over tracked HEAD paths; `*` also crosses `/`. `--body` reads the declared file. Either omitted skips its check; both can run in one invocation. The gate uses only Python's standard library and local Git history, with the common `--repo`, `--base-ref`, `--format text|github|json` and `--soak` flags. Soak never masks exit 2.

A ticket section has a direct `Acceptance criteria` subsection. Its requirements are the list items in its own body before the first subheading. Every item needs `[req: ID]` or `[req: ID, ID]`. An ID starts with a letter, then letters/digits, a hyphen, digits and an optional lowercase letter: `P1-4`, `B-12`, `G2-3a`. Each ID is defined once across all matched plans, and cited in a list item of its own criteria anywhere in its own direct subsection whose heading starts with `Not checked in a browser` (case-insensitive), or in a paragraph of its own body that opens with those words. Unknown criteria/not-checked citations and `[takes: ID, ...]` tags fail.

A plan that also keeps a ledger table (row IDs with an owner column) adds `--keyed-column <header>` (repeatable): every row of a table whose header has that column must carry a `[req: ID]` that a requirement item defines. Without it, a ledger row that no bullet carries drops out of every check, as a profile row did on 2026-10-14: the ledger assigned it to a phase, no bullet named it, and nothing graded it. A `--keyed-column` that no table in the matched plans has (a renamed header, or a table written without its outer pipes) is exit 2, so the ledger check cannot switch itself off.

A PR links each section it implements with `Ticket: docs/plans/plan.md#heading-anchor`. The grammar, fenced-code handling, GitHub heading slugs (including numbered duplicate headings) and joined list items are shared with `actions/ticket`. Requirements are read at the merge base of the contract's base ref and HEAD. Each ID needs a body list item with `[req: ID]` and one of:

- `[proof: tests/trips.py]`: that repository path exists at HEAD.
- `[proof: tests/trips.py::load_more]`: that literal text occurs in the file at HEAD.
- `[deferred: docs/plans/plan.md#p2]`: that section carries `[takes: ID]` at the merge base, anywhere outside a code fence. Land its ownership before deferring to it.

The body cannot claim an ID no linked section defines. No Ticket line is a qualified pass, `no ticket linked`. A linked section without IDs is a qualified pass, `ticket section has no requirement IDs; nothing traced`. A malformed link, a file absent at the merge base or an unknown anchor is exit 2, even under soak.

`actions/ticket` also reads a plan kept in a plan store, outside the repository, when the consumer passes `plans-endpoint` and `plans-token`: a `[plan:<id>]` marker in the title or body names it, and its Acceptance sections' items, then each amendment's, become ticket criteria (harnesses/qae/README.md, "The ticket's criteria"). Such a plan carries no requirement IDs, so `ticket-coverage` does not trace it: the [proof audit](proof-audit.md) grades its whole wording instead, cut into IDs `PL-1`, `AM1-1`, ... when its steps are given the same store, and a criterion of it no browser can check is proven by a `ticket-proof:` line. A marker on a `Split off:` line names the plan that takes some of its requirements, not one this pull request implements, so neither step reads it.

The QAE template includes the body subscription. Its full-history criteria checkout and both `actions/criteria` manifest inputs run coverage before any browser skip or carried walk. A consumer with an older template copies those changes too; a wrapper supplies the same line through `entries:`. The regular QA gate run also reads the body when a browser check is needed. No consumer repository is changed by adding this catalog gate.

## Verify

- `tests/test_qae_plan_store.py::test_the_plans_acceptance_sections_and_each_amendment_are_the_ticket`
- `tests/test_qae_plan_store.py::test_a_plan_the_step_cannot_read_fails`
- `tests/test_qae_plan_store.py::test_a_split_off_lines_plan_is_another_owners_and_not_read`

- `tests/test_ticket_coverage.py::test_tagged_requirements_with_own_criteria_pass`
- `tests/test_ticket_coverage.py::test_untagged_requirement_names_its_file_line_and_text`
- `tests/test_ticket_coverage.py::test_each_id_is_defined_once_across_files_and_sections`
- `tests/test_ticket_coverage.py::test_unknown_criterion_not_checked_and_owner_ids_fail`
- `tests/test_ticket_coverage.py::test_proof_path_and_literal_text_at_head_pass`
- `tests/test_ticket_coverage.py::test_deferral_passes_only_with_owner_at_merge_base`
- `tests/test_ticket_coverage.py::test_an_inline_not_checked_paragraph_covers_an_id`
- `tests/test_ticket_coverage.py::test_keyed_ledger_rows_must_name_a_defined_requirement`
- `tests/test_ticket_coverage.py::test_load_more_incident_is_caught_in_the_plan_and_the_pr`
- `tests/test_ticket_coverage.py::test_bad_ticket_links_files_anchors_and_history_cannot_run_even_in_soak`
- `tests/test_ticket_coverage.py::test_criteria_runs_subscribed_coverage_before_browser_skip`

## Gotchas

- Proof checks existence and literal text, never whether a test ran or demonstrates the requirement's meaning. The [proof audit](proof-audit.md) grades that, from the code alone, for every ID a linked section defines.
- Tags trace explicitly named clauses. A compound bullet with an untagged clause still needs its author to assign that clause an ID; the gate cannot infer requirements from prose.
- Tagged tickets enforce traceability only when the consumer subscribes. Untagged legacy sections remain qualified passes; plan checks are how a consumer requires IDs before implementation.
- A not-checked entry closes plan-to-criteria coverage only. The linked PR still owes proof or an accepted deferred owner for that ID.
