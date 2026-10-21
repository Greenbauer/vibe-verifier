# Check names

How every repository that uses this catalog names its GitHub checks, so a pull request's check list reads as
a list of plain questions grouped by stage. This file is the one source for the names; a repository's own
mapping goes in the pull request that applies it.

## The rules

1. **A check shows as `Workflow / Job`. The workflow name is the stage, the job name is what it proves.**
   Stages, in the order they run: `Code`, `Tests`, `Security`, `Preview`, `Sign-in`, `QA`, `Review`,
   `Standards`, `Merge`. Workflows that never run on a pull request use `Deploy` or `Maintenance`.
2. **Industry terms where one exists, plain English otherwise.** No internal words (QAE, settle, probe,
   anchors, observe, guards, sanity, verdict) and no tool names (semgrep, stryker, playwright) in a name.
3. **Short nouns in sentence case, at most three words**: `Unit tests`, not `run-unit-tests`.
4. **The name never changes; the detail goes in the status line.** "6 of 6 criteria passed", "Skipped:
   docs-only change", "Report only: 3 findings". A report-only check says so in its status line, never in its
   name.
5. **A helper job is named after the check it serves**: `<Check>: <step>`, so it sorts next to it
   (`E2E tests: find preview`, `E2E tests: post result`). The job that decides pass or fail carries the plain
   name, and that plain name is the one a branch rule requires.
6. **Workflow names are unique in a repository.** Two workflow files in one stage are folded into one file
   where that is a move of independent jobs; where it is not (an organization-required workflow, a
   `pull_request_target` workflow), the second takes a narrower stage name from the table below.
7. **A commit status names its stage itself**, because it has no workflow: the context is `Stage / Name`
   (`Merge / Safe to merge`).
8. **Matrix copies keep one name.** GitHub adds ` (2)`, ` (3)` to the copies; the job that judges them all
   is the one that is required.

Checks posted by an app keep the app's name and are out of scope: `Vercel`, `Vercel Preview Comments`,
`Supabase Preview`, CodeQL default setup, Cursor.

## Names

Old names are the job name or status context as it was. A repository job that matches none of these rows is
named by the rules above and listed in the applying pull request.

| Old | New (`Workflow / Job`, or a status context) |
|---|---|
| `Vibe Verifier` / `vibe-verifier-ok` | `Standards / Gates` |
| `Review` / `review` | `Review / Code review: model run` |
| `Review` / `review-verify` | `Review / Code review` |
| `Claude Review` / `claude-review`, `Claude Code Review` | `Review / Code review` |
| `QAE explore` or `QAE` / `qae-criteria` | `QA / Acceptance criteria: read` |
| `qae-preview`, `qae-preview-readiness` | `QA / Acceptance criteria: wait for preview` |
| `qae-settle`, `qae-settle (2)` | `QA / Acceptance criteria: wait for checks` (`(2)` kept) |
| `qae-explore`, `qae-explore (N)` | `QA / Acceptance criteria: walk`, `... walk (N)` |
| `qae-proof-audit` | `QA / Acceptance criteria: proof audit` |
| `qae-base-site`, `qae-base-walk` | `QA / Acceptance criteria: main site`, `... main walk` |
| `qae-verify` | `QA / Acceptance criteria` |
| `Auth probe` / `auth-probe-preview` | `Sign-in / Every role: find preview` |
| `Auth probe` / `auth-probe` | `Sign-in / Every role` |
| `lint-and-test`, `Lint, typecheck, unit tests`, `Lint, build, verify` | `Code / Lint and unit tests` |
| `Typecheck, test, build`, `Type-check & build`, `build` / `Build` | `Code / Types and build` (add `and unit tests` when it runs them) |
| `lint`, `typecheck`, `test` (unit) | `Code / Lint`, `Code / Types`, `Code / Unit tests` |
| `tests` (the job that requires every test job) | `Code / All tests` |
| `schema-guards` | `Code / Repo rules` |
| `design-guards`, `design-token-lint` | `Code / Design system` |
| migration checks (`db / migration-check`, `validate-migrations`, `Apply migrations to ephemeral Supabase`, `migration version uniqueness`) | `Code / Migrations` |
| `ui-contract` | `UI parity / Legacy UI` |
| `mutation-diff` | `Tests / Mutation testing` |
| `test-gates` | `Tests / Regressions and coverage` |
| `coverage-not-lost` (nightly) | `Tests / Coverage kept` |
| `semgrep-observe` | `Security / Code scan` |
| `sync-preview-branch`, `Preview reads its own branch database` | `Preview / Database` |
| `preview-env-sanity`, `Preview NetSuite is the sandbox`, `Preview Supabase is not production` | `Preview / Safe settings` |
| `catalog-data-gate` | `Preview / Catalog data` |
| `preview-e2e`, `browser-suite` | `Preview / E2E tests` (helpers `E2E tests: <step>`) |
| `browser-regressions` | `Preview / E2E regressions` |
| `ui-geometry-observe` | `Preview / Layout` |
| `deploy-pr`, `teardown-pr`, `decide`, `sweep` | `Preview / Deploy`, `Deploy: tear down`, `Deploy: decide`, `Deploy: sweep` |
| status `preview` | `Preview / Address` |
| status `preview-database` | `Preview / Database address` |
| status `preview-e2e-verdict`, `preview-e2e-run`, `preview-e2e-observe` | `Preview / E2E tests`, `Preview / E2E tests: running`, `Preview / E2E tests` |
| status `ui-geometry-observe` | `Preview / Layout` |
| `proof-anchors` | `Review / PR claims` |
| `Merge safety` / `judge merge safety` | `Merge / Safe to merge: judge` |
| status `merge-safety` | `Merge / Safe to merge` |
| `Pull sync` / `pull-sync` | `Maintenance / Update open PRs` |
| `Keep the Codex login fresh` / `codex-keepalive` | `Maintenance / Codex login` |

`Sign-in` and `UI parity` are separate stages because their workflows cannot fold into `Preview` and `Code`:
the sign-in workflow is required by organization rulesets, and the UI contract runs on `pull_request_target`.

## Renaming a repository's checks without blocking a merge

Something reads almost every check by name: a branch rule, an organization ruleset, a workflow that waits on
another, the dashboard. The order:

1. **Readers first.** Every reader of a name the change renames accepts the old and the new name, and lands
   before the rename. Readers include workflow steps that wait on a check or status, the QAE and sign-in
   workflows' preview check, the `needs-update` input of `actions/pull-sync`, the `check` input of
   `actions/preview`, and the dashboard's `bots.*.jobs` lists in each lane host file.
2. **A required name in one repository's branch rule**: open the rename pull request, swap the required
   context to the new name once its checks are green, and merge it straight away. Other open pull requests
   show the new check as expected until they take main (pull sync updates them).
3. **A required name in an organization ruleset that covers several repositories**: each repository's rename
   keeps the old name as well (a one-step job that `needs` the new one, or a second status) until every
   repository has merged; then swap the ruleset to the new name and drop the old one in each repository.
4. **Readers drop the old name** once nothing posts it.
