# Pin bump

Every consumer pins this catalog by commit, so a release reaches it only when its pins move. This action
moves them on a schedule: it runs `bin/vibe-verifier apply-down --auto` (`gates/_pin_bump.py` has the
rules), which opens the same pin-only pull requests a person would open with `apply-down` and
`--confirm`. It never merges them; each merges like any other pull request, after its own checks.

## What it does on each run

1. Plans every stale catalog pin in the repositories listed (and, given `wrapper`, the wrapper's own),
   exactly as `apply-down` does. A drifted stub is skipped, as there.
2. **Holds** a repository whose bump would cross a commit under `gates/_tools.py` or `tools/`: a tool
   version moves, and [GATE-CONTRACT.md](../../docs/GATE-CONTRACT.md#subscribing-in-ci) asks for that gate
   to be swept with `--all` on every consumer first. It opens nothing there and the run exits 1, red,
   naming the files, until a person sweeps and runs `apply-down` by hand.
3. Decides whether it is time, with the rule the first wrapper's ruleset pin mover uses for its own pins:
   - `now` is true; or
   - the oldest release commit any bump would deliver landed `overdue-hours` ago (default 6); or
   - the lane is quiet: across the repositories being bumped, at most 6 runs started in the last hour
     (scheduled ones left out), then no job waiting for a runner with the `lane` label and at most 2
     running there, this run left out. With no `lane`, it never waits.
4. When it is time and `act` is true, confirms its own plan's digest and opens the pull requests. An
   open bump to the same commit is kept; an open bump to an older commit is closed with a comment naming
   the one that replaces it, so a repository never has two. A bump to a commit the new one does not
   include is left open.

It never moves an organization ruleset's pin of a wrapper: that is the wrapper's own job, which
moves them once the wrapper's bump merges.

A bump branch left behind by a bump pull request closed by hand (without deleting its branch) makes the
next bump to that commit fail, exit 2, until the branch is deleted.

Every run prints the plan and the decision in the job summary. Exit 0: it did what the plan said
(bumped, waiting, or nothing to bump). Exit 1: a bump is held. Exit 2: something could not be read or
written.

## Running it

From a workflow in a CI repository of the owner whose repositories it bumps, on that owner's runner lane,
with that owner's own GitHub App. One owner per workflow: a token of one owner never reaches another's
repositories.

```yaml
on:
  schedule:
    - cron: "23,53 * * * *"
  workflow_dispatch:
    inputs:
      act: {type: boolean, default: true, description: Open the pull requests when it is time}
      now: {type: boolean, default: false, description: Do not wait for a quiet lane}
permissions:
  contents: read
concurrency:
  group: pin-bump
  cancel-in-progress: false
jobs:
  pin-bump:
    runs-on: [self-hosted, example-ci]
    steps:
      - id: app
        uses: actions/create-github-app-token@bcd2ba49218906704ab6c1aa796996da409d3eb1 # v3.2.0
        with:
          app-id: ${{ vars.EXAMPLE_APP_ID }}
          private-key: ${{ secrets.EXAMPLE_APP_PRIVATE_KEY }}
          repositories: app,site
          permission-contents: write
          permission-pull-requests: write
          permission-workflows: write
          permission-actions: read
      - uses: Greenbauer/vibe-verifier/actions/pin-bump@<sha>
        with:
          token: ${{ steps.app.outputs.token }}
          repositories: example/app example/site
          lane: example-ci
          act: ${{ github.event_name == 'schedule' || inputs.act }}
          now: ${{ github.event_name == 'workflow_dispatch' && inputs.now }}
```

For a wrapper, check the wrapper out with its whole history (`fetch-depth: 0`) and pass `wrapper: .`
instead of `repositories`.

The token needs contents, pull requests and workflows write (a pin lives in a workflow file) in every
repository listed, and actions read for `lane`. Not the job's `GITHUB_TOKEN`: it cannot write a workflow
file, and a pull request it opens starts no workflow, so the bump's own checks would never run. The
action's own pin in the calling workflow is a catalog pin like any other, so it bumps itself.
