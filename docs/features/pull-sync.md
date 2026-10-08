# Pull request sync

Updates a repository's open pull requests with its default branch after it moves, a few at a time, and labels the ones that conflict so their owner can find them.

## Surfaces

- source: `actions/pull-sync/pull_sync.py`
- source: `actions/pull-sync/action.yml`

## Reach

From a workflow, `uses: Greenbauer/vibe-verifier/actions/pull-sync@<sha>` with a GitHub App or bot token, on a push to the default branch and on a schedule ([actions/pull-sync/README.md](../../actions/pull-sync/README.md)). From a clone, `GH_TOKEN=... python3 actions/pull-sync/pull_sync.py --repo <owner>/<name>` prints the plan and writes nothing; `--act` writes it.

## Verify

- `tests/test_pull_sync.py::test_a_dry_run_prints_the_plan_and_writes_nothing`
- `tests/test_pull_sync.py::test_acting_updates_a_ready_pull_request_at_the_head_it_judged`
- `tests/test_pull_sync.py::test_work_in_flight_is_not_interrupted`
- `tests/test_pull_sync.py::test_slots_go_to_ready_pull_requests_first_and_count_checks_already_running`
- `tests/test_pull_sync.py::test_a_conflict_is_labelled_once_and_never_updated`
- `tests/test_pull_sync.py::test_a_repository_that_cannot_be_read_has_no_plan`
- `tests/test_pull_sync.py::test_the_run_reads_again_while_github_has_not_said_what_merges`
- `tests/test_pull_sync.py::test_the_action_is_a_dry_run_unless_act_is_true`

## Gotchas

- An update made with a workflow's `GITHUB_TOKEN` starts no workflow on the new head. The token must be an App's or a bot's.
- Every update is a new head, so every per-head verdict is redone. The slot cap and the once-a-day rule for pull requests that are not ready exist to bound that cost; raising either spends runner time.
- A conflict is never resolved here. `sync-conflict` is the hand-off.
- A head's rollup state says FAILURE at its first failed check while the others still run. "Checks running" is read from the per-state counts, never from the rollup.
- Right after a merge GitHub answers `mergeable: UNKNOWN` for every pull request until asked again.
