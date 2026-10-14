# Pull sync

After a merge, every other open pull request is behind the default branch. This action updates them
with GitHub's own "update branch", a few at a time, and labels the ones that conflict. The rules are
in [`pull_sync.py`](pull_sync.py), which is the whole engine; this page is how to run it.

## What it does on each run

| Pull request | What happens |
|---|---|
| Has the `no-auto-sync` label, comes from a fork, or targets another branch than the default | Nothing |
| Up to date | Nothing |
| Conflicts with the default branch | Gets the `sync-conflict` label. Never updated: its owner resolves it. The label comes off once it merges cleanly |
| Checks running on a head that is behind, where the default branch merges only up-to-date heads; not a draft, nothing failed yet, pushed more than 30 minutes ago | Updated now, without waiting for a slot (see below) |
| Any other with a check on its head unfinished, or changed in the last 30 minutes | Nothing this run |
| Ready: not a draft, every check on its head passed | Updated, oldest first |
| With `needs-update`: its only failed check is that status (below) | The same as ready, or as on its way to green while other checks run |
| Anything else (red, draft, no checks) | Updated once its head commit is 24 hours old. An update is a head commit, so that is at most once a day unless the author pushes an older commit back |

At most two updates may be in flight at once (`max-in-flight`). A pull request holds a slot when its
head is an update, a merge commit GitHub made (what "update branch" makes, from this action or the
button), and any check on that head is unfinished, or it has no checks yet and is newer than 30
minutes, because they may not have registered yet. A head someone pushed holds no slot, however busy:
the cap bounds the runner time this action adds, and a pull request whose author keeps pushing never
keeps a green one behind (on a consumer whose heads run checks for two hours, counting those starved
every green pull request, 2026-10-13). A pull request with no free slot waits for a later run. An update is refused by GitHub when the head moved
since the run read it, so it never lands on top of a push it did not see.

Where the default branch requires branches to be up to date before they merge (a classic protection
rule's "require branches to be up to date", or a ruleset's), checks that started on a head that is now
behind can never be the ones it merges on. Waiting for them, as the run does elsewhere, spends their
whole length (two hours on one consumer, 2026-10-14) before the checks that count can start. So there
a pull request on its way to green is updated at once, and its checks start again on the new head:

- It replaces checks instead of adding some, so it needs no free slot.
- A workflow whose concurrency cancels a superseded run (`cancel-in-progress` on a pull request's new
  push) frees the runner the stale run held. One that queues instead runs the stale head to the end
  first, so give long workflows `cancel-in-progress` for pull request pushes.
- Not a draft, and nothing failed on that head yet: a red head's remaining checks are still feedback
  for its author.
- Its head was pushed more than 30 minutes ago, measured from the push rather than from the pull
  request's last change, because the running checks' own comments keep changing it.

The run reads the setting each time and prints it in its first line. A token that can read neither
the protection rule nor the rulesets counts as "off", so a running check is never interrupted there.

GitHub works out whether a pull request merges cleanly only when asked, so right after a merge it
answers "unknown" for every one. The run asks again, up to three more times ten seconds apart, and
leaves whatever is still unknown to the next run.

## A check in place of "up to date"

A repository can drop "require branches to be up to date" and require instead a check that fails a
pull request only when it is behind and the default branch's newer commits touch the same files, or
files every change depends on (lockfiles, migrations, CI). That check's failure means exactly "this
needs an update", which is this action's job. Without help, though, a failure is a failure here: the
pull request reads as red, and a red one is updated only once its head is a day old.

Name the check's commit status in `needs-update` (`needs-update: merge-safety`). A pull request whose
only failed check is that status is then treated as ready (or, with checks still running, as not yet
red), and is updated like any green one. A pull request with any other failure is still red. The
status is read per head (`status.context(name:)`), so it must be a commit status, not a check run.

## Running it

```yaml
name: Pull sync

on:
  push:
    branches: [main]
  schedule:
    - cron: '*/10 * * * *'
  workflow_dispatch:

permissions:
  contents: read

concurrency:
  group: pull-sync
  cancel-in-progress: false

jobs:
  pull-sync:
    runs-on: ubuntu-latest
    timeout-minutes: 5
    steps:
      - uses: Greenbauer/vibe-verifier/actions/pull-sync@0000000000000000000000000000000000000000 # CONSUMER: pin the commit you subscribe to
        with:
          token: ${{ secrets.PULL_SYNC_TOKEN }}   # CONSUMER: a GitHub App or bot token, see below
          act: "true"
```

- **The schedule matters more than the push.** The run a merge starts updates only what has a free
  slot, is quiet, and GitHub has already judged; the scheduled runs pick up the rest.
- **Start with a dry run.** Leave `act` out and read the plan in the job's summary before turning it on.
- **Several repositories from one place.** `repositories:` takes a list of `owner/name`, separated by
  spaces or new lines, for an organization that runs this in one workflow with one token. Each
  repository has its own slots, and one that cannot be read does not stop the others.

## The token

- It must be able to write contents and pull requests in the repository: an update is a merge commit
  on the pull request's branch, and the conflict label is a write to the pull request. It must also
  read checks: the plan reads whether each head's checks are still running, and without it GitHub
  answers "Resource not accessible by integration" (seen on a consumer's first run, 2026-10-12).
- Measured on 2026-10-08: an update that brings in changed files under `.github/workflows/` goes
  through with contents and pull requests write; no workflows permission was needed.
- It must not be the workflow's `GITHUB_TOKEN`. GitHub starts no workflow for an event that token
  causes, so the updated head would have no checks at all.
- The token's identity becomes the actor of every workflow the update starts, and `claude-code-action`
  refuses a run a bot started unless `allowed_bots` names it. The catalog's review harness and Claude
  QAE explorer allow the bot a repository names in its `PULL_SYNC_BOT` variable (`<app>[bot]`), so set
  that variable, and copy those templates at or after the commit that added it, before turning the
  sync on. Without it every update turns the pull request's review red.

## Limits

- It is built for a repository with tens of open pull requests. It fails, instead of judging a partial
  list, on more than 100, and on a repository with hundreds GitHub can time out the read itself.
- "Ready" means the head's checks passed. It does not read review threads or approvals.
- Replacing running checks on a head that is behind restarts them at every merge. With merges more
  often than a check suite takes, a pull request finishes its checks only in a gap between merges;
  that is the cost of a branch rule that merges only up-to-date heads, and the alternative was
  finishing checks that could not be merged on.
- A new head makes every per-head verdict stale. The review harness answers an update that touches
  none of the pull request's files with a receipt refresh; the QAE harness carries the last passing walk when
  the update brought only files no browser can see, and explores again otherwise.
