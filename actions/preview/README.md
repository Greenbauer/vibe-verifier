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

## Finding

```yaml
    permissions:
      checks: read
      statuses: read
    steps:
      - id: preview
        uses: Greenbauer/vibe-verifier/actions/preview@0000000000000000000000000000000000000000 # CONSUMER
        with:
          check: sync-preview-branch   # CONSUMER: the name of the check whose job records
      - run: curl -fsS "${{ steps.preview.outputs.url }}/login"
```

It waits up to `wait-minutes` (40) for the named check to pass on the head, then outputs the newest
recorded address as `url`. Of the check's runs on the commit, the newest one that was not skipped
decides, and every page of runs is read: a label event can add a newer skipped run while the real
one runs. It fails, with an error naming why:

| The check | Exit |
|---|---|
| failed, was cancelled or timed out | 1 |
| has only skipped runs for three minutes | 1 |
| did not pass within `wait-minutes` | 1 |
| passed, but its job recorded no address (a missing record step; re-running will not help) | 1 |
| could not be read from GitHub six times in a row | 2 |

From a clone: `GH_TOKEN=... python3 actions/preview/preview.py find --repo <owner/name> --sha <commit> --check <name>`.

## Limits

- The job that records is the authority. A pull request can edit that job, so it can record another
  address; a check that reads it trusts the pull request's workflow exactly as far as it already
  trusted the check it waits for.
- A repository whose preview has no setup job of its own has nothing to record from. It reads the
  host's deployment records instead (harnesses/qae/README.md, "A reachable preview instead of a site
  on the runner"), and depends on the host writing them.
