# Pin bump

Every consumer pins this catalog by commit, so a release reaches it only when its pins move. This action
moves them on a schedule: it runs `bin/vibe-verifier apply-down --auto` (`gates/_pin_bump.py` has the
rules), which opens the same pin-only pull requests a person would open with `apply-down` and
`--confirm`, and once one is ready it squash-merges it, so a release reaches every repository without anyone
clicking.

## What it does on each run

1. **Merges** each bump already open (with `act` true, as the example below passes it on a schedule, and `merge`, true by default; `gates/_pin_merge.py`
   has the rules), before planning, so a bump opened in this run waits for a later one, after its checks have
   registered. It squash-merges a bump at the head it judged, never with admin rights, only when:
   - every changed file is a workflow, and every changed line is a `uses:` of a catalog action (for a wrapper
     bump, of a workflow of the repository's own owner) that keeps its target and moves only its commit, so no
     CODEOWNERS, permission or secret line rides along;
   - each moved pin is the commit the branch is named for, and that commit is on the catalog's main (the
     wrapper's, for a wrapper bump), so a pushed branch cannot pin an older or a fork's commit;
   - it is into the default branch;
   - the gate job (`Gates`, or `vibe-verifier-ok` in an older stub) passed on the head, and every other check
     there concluded without failing;
   - no review asks for changes, the head is not behind its base, and GitHub says it merges cleanly.

   A bump behind its base gets the base merged in (a merge commit) and merges on a later run once its checks
   pass again. One that conflicts with its base, or has nothing left to change because its pins reached the
   base another way, is closed with its branch; the plan below opens a fresh one while that commit is still
   wanted. One with a check still running waits, as does one GitHub still reports `unstable` from before its
   checks finished. A failed check, a line that is not a pin, a review asking for
   changes or a block GitHub names holds it for a person, and the run exits 1.
2. Plans every stale catalog pin in the repositories listed (and, given `wrapper`, the wrapper's own),
   exactly as `apply-down` does. A drifted stub is skipped, as there.
3. **Holds** a repository whose bump would cross a commit under `gates/_tools.py` or `tools/`: a tool
   version moves, and [GATE-CONTRACT.md](../../docs/GATE-CONTRACT.md#subscribing-in-ci) asks for that gate
   to be swept with `--all` on every consumer first. It opens nothing there and the run exits 1, red,
   naming the files, until a person sweeps and runs `apply-down` by hand. It holds a repository the same
   way when a workflow the bump rewrites passes a `with:` input the action does not declare at the new
   commit, or names an action that commit does not have: that workflow would fail from the merge on.
4. Decides whether it is time, with the rule the first wrapper's ruleset pin mover uses for its own pins:
   - `now` is true; or
   - the oldest release commit any bump would deliver landed `overdue-hours` ago (default 6); or
   - the lane is quiet: across the repositories being bumped, at most 6 runs started in the last hour
     (scheduled ones left out), then no job waiting for a runner with the `lane` label and at most 2
     running there, this run left out. With no `lane`, it never waits.
5. When it is time and `act` is true, confirms its own plan's digest and opens the pull requests. An
   open bump to the same commit is kept; an open bump to an older commit is closed with a comment naming
   the one that replaces it, so a repository never has two. A bump to a commit the new one does not
   include is left open.

It never moves an organization ruleset's pin of a wrapper: that is the wrapper's own job, which
moves them once the wrapper's bump merges.

A bump branch with no open pull request (left by a run that failed part way, or by a bump closed by hand
without deleting its branch) is reset to the default branch and used again.

Every run prints each bump's outcome, the plan and the decision in the job summary. Exit 0: it did what the
plan said (merged, bumped, waiting, or nothing to do). Exit 1: a bump is held. Exit 2: something could not be read or
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
          permission-checks: read
          permission-statuses: read
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
repository listed, and actions read for `lane`. To merge, it also needs checks and statuses read (a bump merges
only once every check on its head passed), and must be allowed to merge into each default branch: a branch
that restricts who can push lists the app there, or every merge fails and the run is red naming GitHub's
refusal. A dry run with `merge` reads each default branch's checks and statuses the same way, so a pull
request that changes the calling workflow goes red when the token lacks either. Set `merge: false` to leave the bumps for a person. Not the job's `GITHUB_TOKEN`: it cannot write a workflow
file, and a pull request it opens starts no workflow, so the bump's own checks would never run. The
action's own pin in the calling workflow is a catalog pin like any other, so it bumps itself.
