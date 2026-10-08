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
| Checks running, or changed in the last 30 minutes | Nothing this run |
| Ready: not a draft, every check on its head passed | Updated, oldest first |
| Anything else (red, draft, no checks) | Updated once its head commit is 24 hours old, so at most once a day |

At most two pull requests may have checks running at once (`max-in-flight`), counting the ones people
pushed. A pull request with no free slot waits for a later run. An update is refused by GitHub when
the head moved since the run read it, so it never lands on top of a push it did not see.

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

- **The schedule matters as much as the push.** One run after a merge updates only what has a free
  slot and is quiet; later runs pick up the rest.
- **Start with a dry run.** Leave `act` out and read the plan in the job's summary before turning it on.
- **Several repositories from one place.** `repositories:` takes a list of `owner/name`, separated by
  spaces or new lines, for an organization that runs this in one workflow with one token. Each
  repository has its own slots, and one that cannot be read does not stop the others.

## The token

- It must be able to write contents and pull requests in the repository: an update is a merge commit
  on the pull request's branch, and the conflict label is a write to the pull request.
- It must not be the workflow's `GITHUB_TOKEN`. GitHub starts no workflow for an event that token
  causes, so the updated head would have no checks at all.
- The token's identity becomes the actor of every workflow the update starts. A workflow that refuses
  runs started by a bot (the review harness's `allowed_bots`, for one) has to allow that identity first.

## Limits

- It reads up to 100 open pull requests, and fails instead of judging a partial list.
- It writes nothing when the token has fewer than 300 API points left.
- "Ready" means the head's checks passed. It does not read review threads or approvals.
- A new head makes every per-head verdict stale. The review harness answers an update that touches
  none of the pull request's files with a receipt refresh; the QAE harness explores again.
