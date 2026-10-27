---
name: write-acceptance-criteria
description: Write a pull request's `## Acceptance criteria` so Vibe Verifier's QAE can check them on the preview, check them before pushing, and re-run QAE after editing them. Use when opening a pull request, editing its description, or when the QAE check (Acceptance criteria and its walk jobs, or qae-verify and qae-explore in an older copy, acceptance-verdict) fails.
---

# Write acceptance criteria QAE can check

QAE is a model driving a real browser through the pull request's preview, one criterion at a time. It
reads the criteria from the pull request description each time it runs. It has the preview, the sign-in
the repository's workflow gives it, and nothing else: no main branch, no production, no GitHub, no
other pull request.

`<catalog>` below is a clone of Vibe Verifier.

## The section

```markdown
## Acceptance criteria

- On /orders, the Export button downloads a CSV with one row per visible order.
- A read-only user sees no Export button. [as: read-only]
```

- The heading is exactly `## Acceptance criteria`. Nothing after it: a note such as "(check at 1280 px)"
  makes the section unreadable, and the check fails. Notes go in the list items. The widths QAE checks
  come from the repository's `.vibe-verifier-qae`, not from the description.
- One list item per claim a person can check on the preview: the page, the action, the visible result.
- Nothing renders (CI, docs, a backend change no page shows)? Write exactly one item,
  `- None: <why>`. A bare `- None` is read as a criterion.

## What makes a criterion checkable

- **State what the preview shows, never a comparison.** "Same rows as on main", "as in production" and
  "unchanged" can never pass: QAE opens neither. Write the result: "The Orders page lists a row for each
  open order."
- **Name visible text.** A button's label, a heading, a message. Not an aria-label, a test id or a CSS
  class.
- **End every write in something on screen.** "Clicking Save shows 'Profile updated.'" A save with no
  visible result cannot be told from a click that did nothing.
- **Say who.** When the result depends on who is signed in, add `[as: <role>]` with the roles the
  workflow provides, spelled as it names them. QAE signs in as those test accounts only, so a control only
  some other user sees cannot be a criterion; prove it with a test and say so in the description. A
  workflow that passes its roles to the criteria job fails any other role there, before a walk.
- **Expected errors are declared.** A criterion that expects the server to refuse something says so in
  its own text: `expected-refusal: 422 /api/profile`. Otherwise the refused request fails the check.
- **Keep data local to the criterion.** Explorers may run in parallel on one preview. A criterion that
  uses what another one created says so with `[after: AC2]`, so one explorer walks both in order (a
  plain `AC2` in the wording chains too, but the marker survives rewording). Otherwise each criterion
  creates its own data. A chain stays with one explorer even past its size; the criteria job warns when
  it does, so keep chains short.
- **Stay on the site.** Never ask QAE to follow a link off the preview (GitHub, a private dashboard): it
  is signed out there. Have it read the link's label and address instead.
- **Small lists.** About six walks per explorer (a walk is one criterion, as one role, at one width).
  Fewer, sharper criteria beat many vague ones.

## Before pushing

Check the description parses:

```bash
gh pr view <number> --json body --jq .body > /tmp/body.md   # or the description you are about to use
python3 <catalog>/bin/vibe-verifier criteria /tmp/body.md
```

- `criteria: <n>`: n must equal the items you wrote.
- `none: <reason>`: nothing to check, as declared.
- Exit 1 with `::error::` lines: fix each one as it says. The QAE run fails the same way, before any
  browser starts.

## After the check fails

1. Read the QA review comment and the failing criterion's steps in the run's `qae-artifacts` artifact.
2. A FAIL means the code is wrong or the criterion is. Fix that one. Never delete or soften a criterion
   to turn the check green.
3. Edited only the description? Re-run the QAE workflow; do not push an empty commit. It reads the
   description when it runs. Re-run the whole run (a re-run of failed jobs only can reuse an explorer
   that reported FAIL but exited green), and for a workflow your organization requires, use the API,
   since `gh run rerun` cannot reach it:

   ```bash
   gh api -X POST repos/<owner>/<repo>/actions/runs/<run id>/rerun
   ```
