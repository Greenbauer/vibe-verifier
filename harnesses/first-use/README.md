# First-use walk: a QAE session that gets only the task, and must come back

The QAE explorer is handed a pull request's acceptance criteria and checks each one. So a defect no criterion names
passes it: a record page with no way back to its list, two blue buttons that both say "Send invite", an error that
says only "Failed to send invite email". A subscribed app shipped all three on one pull request (2026-11-04), after
the criteria walk, the proof audit and a narrated demo had all passed it; its requester found them in two minutes,
because he was given nothing but the feature.

The first-use walk is that requester. A second Codex session is given only the task and the site:

1. **Task.** The pull request body states it in a section of its own, in the requester's words (quote the plan or
   ticket; never the acceptance criteria):

   ```markdown
   ## First-use task

   Give a person at one of our customers access to the portal.
   ```

   `bin/vibe-verifier first-use-prompt --body FILE --site URL --out FILE` ([`gates/_first_use.py`](../../gates/_first_use.py))
   writes [`prompt.md`](prompt.md) with that text and the site filled in; [`actions/first-use`](../../actions/first-use/action.yml)
   runs it in a workflow and says whether there is a task (`walk`).
2. **Walk.** [`actions/qae-codex`](../../actions/qae-codex/action.yml) runs the prompt with `artifacts: qae-first-use`,
   on a QAE runner, after the site step has written `qae-inputs/site.md` for its sign-in. The session signs in, logs
   step 1 as its `start:`, does the task using only what the pages show, then retraces its path to the start page by
   what each page shows (a breadcrumb, a back link, Cancel), using the main menu only to reach or leave a page the
   menu itself links to. It writes `walk.md` (one line and one screenshot per step) and `verdict.md`: one
   `first-use: DONE|STUCK` line, one `first-use-back: DONE|STUCK` line, and a `first-use-snag:` line for each thing a
   person new to the site would trip on.
3. **Judge.** The verify job puts the walk's artifact at `qae-artifacts/first-use/`, and the
   [`first-use-walk`](../../gates/first_use_walk.py) gate, deterministic, checks it: every step has its screenshot,
   in walk.md's order in the session log; from step 1's screenshot on, the log holds no typed address, Back or
   Forward or script, and nowhere a move between tabs; the page at the last step's screenshot, and at the log's last call, is the
   page at step 1's; every verdict line cites a step `walk.md` has.
   A STUCK line and every snag are findings. A pull request with acceptance criteria and no task is a finding too.

## In a QAE workflow

A job of its own beside the explore job, so the walk's session is never handed the explorer's prompt and runs in
parallel on another QAE slot. `actions/first-use` removes the body once it has read the task, so the session, which
has the workspace, cannot read the criteria there either. The wrapper's `Acceptance criteria: main walk` job is the
pattern: the same site step (by YAML anchor) with its own login suffix, then

```yaml
      - name: Write the first-use walk's prompt
        id: task
        uses: Greenbauer/vibe-verifier/actions/first-use@<sha>
        with:
          body: ${{ runner.temp }}/pr-body.md   # outside the workspace the session can read
          site: ${{ steps.site.outputs.url }}
      - name: Walk the task as a person new to the site
        if: steps.task.outputs.walk == 'true'
        uses: Greenbauer/vibe-verifier/actions/qae-codex@<sha>
        with:
          prompt-file: qae-inputs/first-use-prompt.md
          mcp: ${{ steps.browser.outputs.mcp }}
          artifacts: qae-first-use
          storage-state: ${{ steps.site.outputs.storage-state }}
          secrets-file: ${{ steps.site.outputs.secrets-file }}
          shard: first-use
```

then the write-scope step (with `qae-first-use/` in scope) and an upload of `qae-first-use/` as
`qae-first-use-<run_attempt>`. The verify job removes any `qae-artifacts/first-use/` the explorers left, copies the
newest attempt's artifact there, and runs the manifest line:

    first-use-walk --artifacts qae-artifacts --body qae-inputs/pr-body.md --soak

Drop `--soak` once ten pull requests in a row raised no false finding.

## Measured

On a replica of that pull request's screens (2026-11-04, the Codex model on a ChatGPT login, the pinned
playwright-mcp), the walk of the screens as first built was STUCK, with snags for the unexplained invite error, the
Create choice that changed nothing, and the record page with no way back to its list; the layout hook's
`duplicate-name` fired on the dialog in the same run. The walk of the fixed screens (a breadcrumb, a radio choice, an
error that says which addresses a test copy accepts) was DONE with no snag, after one retry with an address the
error named. The first version of the prompt also flagged list pages the main menu links to, and the test copy's
email guard itself; the menu and test-copy sentences in step 2 and 3 are what removed those. With the snag for a
second place for one job added (a placement rule a separate blind backtest graded on 41 past pull requests: 3
of the 4 placement complaints caught, 1 false alarm in the 31 nobody complained about), the walk as first built named all four of that pull request's problems,
"two buttons have the same Send invite label" among them, and the fixed walk still had none.

## Limits

- The session reads the page's accessibility snapshot as the explorer does, so it can tell apart controls a
  sighted person cannot by name alone (a pressed toggle). `ui-audit`'s `duplicate-name` covers that case
  deterministically.
- Whether a way back is the page's own or the main menu is the session's judgment, reported as a snag; the gate
  checks only that it got back, and how.
- The task is written by the pull request's author. Quote the requester; a task reworded to match what was built
  walks only that.
