# Preview

One answer to "where is the preview of this commit", written once and read by every check that needs
it. The job that sets a pull request's preview up records the address it proved; every other check
waits for that job to pass and reads the address. [`preview.py`](preview.py) is the whole engine.

## Why not read the host's deployment records

A host such as Vercel builds each commit and usually tells GitHub about it with a deployment record.
Reading those records works until the host stops writing them: on 2026-10-12 Vercel built
one repository's previews but wrote nothing back to GitHub for hours. The job that set each
preview up asked Vercel directly and passed; every check that read GitHub's records failed with "no
Preview deployment", and re-running them changed nothing. The records also leave each reader to guess
which of a commit's builds is the one to test: a preview pointed at a branch database is redeployed
after its first build, and only the redeploy reads the branch. The job that redeployed already knows.

## Recording

In the job that makes the preview ready, after it has proved the address serves:

```yaml
    permissions:
      statuses: write
    steps:
      # ... set the preview up, ending with its address in PREVIEW_URL ...
      - uses: Greenbauer/vibe-verifier/actions/preview@0000000000000000000000000000000000000000 # CONSUMER: pin the commit you subscribe to
        with:
          record: ${{ env.PREVIEW_URL }}
```

It writes a commit status on the pull request's head: context `preview`, state success, its link the
address. The pull request's checks list then shows a `preview` row that links to the preview under
test. Record the immutable address of the build (Vercel's `<project>-<hash>.vercel.app`), not a
branch alias, which a later push moves. A re-sync of the same commit records again, and the newest
record wins. A record GitHub refuses three times fails the step, so the job cannot pass without one.

The [check names](../../docs/check-names.md) call this status `Preview / Address`, and the
database's below `Preview / Database address`. Both keep their present contexts until every
consumer's readers of them (a workflow that waits on the status, a branch rule) accept the new ones.

### The database the preview reads

A preview pointed at a database of its own (a Supabase branch) passes that database's project ref
too, from the step that pointed the preview at it:

```yaml
      - uses: Greenbauer/vibe-verifier/actions/preview@0000000000000000000000000000000000000000 # CONSUMER
        with:
          record: ${{ steps.branch.outputs.url }}
          database: ${{ steps.branch.outputs.database }}   # CONSUMER: the branch's project ref
```

It writes a second status first, context `preview-database`, its link `https://<ref>.supabase.co`.
Without it, every check that needs the database has to find it again by name, and a Supabase project
can hold two branches for one pull request: a workflow's `pr-<N>` and the branch Supabase's GitHub
integration makes for the git branch when the pull request changes migrations. A lookup by name
once took the integration's branch, which the preview never read, and every call to it answered
403. The job that pointed the preview at its database is the one that knows which it is.

## Finding

```yaml
    permissions:
      checks: read
      statuses: read
    steps:
      - id: preview
        uses: Greenbauer/vibe-verifier/actions/preview@0000000000000000000000000000000000000000 # CONSUMER
        with:
          check: Database   # CONSUMER: the job name of the check that records (shown as Preview / Database)
      - run: curl -fsS "${{ steps.preview.outputs.url }}/login"
```

`check` is the job's own name, the one GitHub's check runs carry, without its workflow's name. While
the check is being renamed ([check names](../../docs/check-names.md)), give both names, one per line;
the runs under every name are judged as one check's, the newest deciding, and every message names
each of them:

```yaml
          check: |
            sync-preview-branch
            Database
```

It waits up to `wait-minutes` (40) for the named check to pass on the head, then outputs the newest
recorded address as `url`, and the newest recorded database's project ref as `database` (empty when
the job recorded none). `running-minutes` bounds only the time the check spends running: polls that
see it queued for a runner, or not yet created, do not count, because a check waiting for a free
runner is not a slow check. It polls every 30 seconds, since every job of a repository shares its
token's hourly request budget. Of the check's runs on the commit, the newest one that was not skipped
decides, and every page of runs is read: a label event can add a newer skipped run while the real
one runs. It fails, with an error naming why:

| The check | Exit |
|---|---|
| failed, was cancelled or timed out | 1 |
| has only skipped runs for three minutes | 1 |
| ran longer than `running-minutes` | 1 |
| did not pass within `wait-minutes`, and has no run that could still pass | 1 |
| did not pass within `wait-minutes`, and its run is still queued or running | 3 |
| passed, but its job recorded no address (a missing record step; re-running will not help) | 1 |
| could not be read from GitHub six times in a row | 2 |

### A wait longer than one job

A runner that stops every job after a fixed time bounds `wait-minutes`, and a check can outlast it.
Chain two jobs: the first waits with `handover: "true"`, the second `needs` the first and waits
again without it.

```yaml
  wait:
    name: "E2E tests: wait for preview"   # CONSUMER: docs/check-names.md
    steps:
      - uses: Greenbauer/vibe-verifier/actions/preview@0000000000000000000000000000000000000000 # CONSUMER
        with:
          check: Database   # CONSUMER
          handover: "true"
  test:
    name: E2E tests   # CONSUMER
    needs: wait
    steps:
      - id: preview
        uses: Greenbauer/vibe-verifier/actions/preview@0000000000000000000000000000000000000000 # CONSUMER
        with:
          check: Database   # CONSUMER
```

With `handover`, exit 3 alone passes the step, with a notice and an empty `url`: the check's run is
still queued or running, so the second job's wait can still see it pass. Every other row above
fails the first job, and the second job, which has no `handover`, fails on all of them, exit 3
included. So the chain never passes on a check that failed or never ran; it only waits twice as
long for one that is still going.

From a clone: `GH_TOKEN=... python3 actions/preview/preview.py find --repo <owner/name> --sha <commit> --check <name>`.

## Limits

- The job that records is the authority. A pull request can edit that job, so it can record another
  address or database; a check that reads it trusts the pull request's workflow exactly as far as it
  already trusted the check it waits for. A check that writes to the database should still refuse a
  ref that is not one of its production project's preview branches.
- A repository whose preview has no setup job of its own has nothing to record from. It reads the
  host's deployment records instead (harnesses/qae/README.md, "A reachable preview instead of a site
  on the runner"), and depends on the host writing them.
