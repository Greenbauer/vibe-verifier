# Dashboard GitHub data

The dashboard reads GitHub through the locally authenticated `gh api` command. It makes no GitHub writes. Repository and workflow identifiers come from the validated startup configuration, and links in the response are limited to the configured GitHub owner.

## Collection flow

Each refresh checks the REST core rate limit, then scans configured repositories with at most four workers. A repository scan:

1. Refreshes direct subscription evidence when the five-minute inventory cache expires.
2. Lists open pull requests.
3. Joins each pull request to check suites and check runs for its exact head SHA. The check runs come from one listing for the head (`commits/{sha}/check-runs?filter=all`), not one request per check suite, and a run counts only when its suite belongs to that head. `filter=all` is required: `filter=latest` keeps the newest `completed_at` and drops a queued or in-progress run, so a finished pass hides the pending check GitHub is showing. Within one suite the newest check run id is the current attempt, because a queued rerun often has no timestamps.
4. Joins Actions runs by head SHA and check-suite ID, selecting the latest run attempt and its matching jobs.
5. Reads commit statuses and then re-reads the pull request head. Evidence is discarded if the head changed during collection. The same read supplies merge state and the comment totals.
6. One GraphQL read per repository that has an open pull request (further pages when it has more than 50) loads each open pull request's review threads and the newest commits' messages and push times. GitHub prices a GraphQL query by the page sizes it asks for, not by what comes back, so the read asks for exactly the number of pull requests the REST list found open: about 1.2 points each, where a fixed page of 50 cost 61 points for one pull request. The read counts as one budget unit and is not reused with `If-None-Match`, because GraphQL is a POST. If it fails, the pull request's checks stay and its title stays white: a missing comment count is not shown as zero. At most four pages (200 pull requests) are read; threads past the first 100 on a pull request are a lower bound, shown with a plus, and that pull request is not treated as merge-ready.
7. Lists checks the base branch requires that have not reported on the head as `expected` rows (category `pending`). Rulesets are read once per base branch per scan. GitHub refuses that read with 403 on a private repository whose plan has no rulesets (a free personal account); classic branch protection is still read, and a 404 there means the branch has none. The required-status-checks read needs Administration read, so a token without it leaves those checks out and keeps the pull request's other evidence. A required workflow counts only for a run created after its current pin took effect, learned from the ruleset's version history and cached until the pin moves. GitHub serves an organization ruleset's history only with organization administration write; when it refuses, any run of the required workflow counts.

Check reruns are resolved by check suite, GitHub App identity, and check name. A newer rerun replaces an older attempt from the same provider. Providers that use the same check name remain separate.

Each event that starts a workflow on the same head (a push, a review, a review comment, a label) creates a separate run in its own check suite, so one job can appear several times. Only the newest copy counts: a job, and the check run it reports, is keyed by workflow file and job name, and the run created last wins. Non-Actions checks are keyed by provider and check name. Check totals, attention, and the running-now lines use only these current copies, and a run whose jobs were all superseded is dropped. Jobs with the same name in different workflows remain separate.

If a listed pull request's detail requests fail, its identity remains in the response with `evidence_available: false`. The dashboard may claim that a readable repository has no open pull requests only when the open-pull list itself completed successfully.

## Request and history bounds

The default refresh budget is 200 GitHub requests that cost rate limit, counting each REST page and each GraphQL read. Paginated endpoints use explicit 100-item pages. Every `gh api` process requests one page and increments the budget once. A hidden `--paginate` call is not used.

Every response that carries an ETag is kept in memory and requested again with `If-None-Match`. GitHub answers an unchanged resource with `304 Not Modified`, which does not count against its hourly rate limit, so the dashboard reuses the kept response and returns that request's budget unit. The budget therefore counts only requests that cost rate limit. Only responses used during the previous refresh are kept into the next one, and the whole cache is dropped when authentication or access is revoked. GitHub does not meter every endpoint against one hourly counter. On 2026-10-05 the check-suite, single-run, run-attempt jobs, workflow list, and workflow runs endpoints were refused with `X-Ratelimit-Remaining: 0` and their own reset time, while pull requests, commit check runs, commit status, and the run list of the same account still had about 3,370 requests left, and `GET /rate_limit` reported neither counter as used. So when GitHub refuses a request because an hourly counter is spent, only that endpoint family (its path with owner, repository, IDs, and SHAs ignored) waits until the reset GitHub reported on the refusal; the other families keep reading. A rate-limit refusal without a spent counter, such as a secondary limit, pauses every request for 60 seconds. `github.api.lowest_remaining` is the lowest `X-Ratelimit-Remaining` seen on any response during the refresh, including refusals, and is the honest headroom; `github.api.remaining` is what `/rate_limit` reports.

Bot collection first lists the workflows on each readable repository and caches successful listings for five minutes. A configured filename absent from that successful listing needs no run calls; an API failure remains unknown. Roles sharing a present workflow share its scan. Active runs are fetched before history. Seven days of lightweight run metadata are collected across pages and sorted by update time before fetching jobs, so a recently rerun workflow on a later page is not skipped. Job reads stop once every role has five completed results and the next run was updated more than two hours ago and no later than each role’s fifth result. This bounds expensive job reads without assuming pages are ordered by completion time. Jobs are read for at most the 50 newest completed runs of a workflow, so a configured job name that never runs cannot read every run of the last seven days. When that limit ends a scan, a role's history stays `complete` only if its five newest results across all repositories are newer than the runs the scan skipped; otherwise its history coverage is `partial`, and a role with no result in the scanned runs has no history timestamp. The history query covers runs created in the last seven days; a rerun of an older run is outside this query. A budget or API failure marks history coverage `partial`.

The output keeps the latest five completed jobs per role within seven days and the latest five within two hours. `latest_failure` is set only when the most recent completed outcome failed. A newer success clears it.

Only a job whose GitHub status is `in_progress` makes a bot `working`. A queued job is not working. A complete active scan with no in-progress job yields `idle`. Missing active coverage yields `unknown`. Only telemetry can supply `down`.

## Cache and timestamp contracts

- `github.sampled_at` is the collection start time. Each repository retains its own observation timestamp after its current-head reads finish, so later bot scans do not prematurely age freshly read PR evidence. A failed refresh does not advance source timestamps.
- Repository `sampled_at` records when that repository was read successfully.
- Bot `sampled_at` records an active observation or a complete active scan. `history_sampled_at` records observed history; separate coverage fields identify partial results.
- Successful GitHub snapshots are reused for 60 seconds.
- Subscription inventory is reused for five minutes. Reusing it does not reset its age.
- Fully completed job lists are cached by repository, run ID, and run attempt until the run is seven days old. Active or partly completed jobs are never placed in that cache.
- Transient failures keep the last repository rows and bot history and mark them stale. Age does the same after 180 seconds: the rows stay, `stale` becomes true, and the two-hour and seven-day history windows still drop runs by their own timestamps. A failed page refresh keeps the last snapshot on screen.
- Authentication, permission, and missing-resource failures do not reuse private cached rows. Completed-job and inventory caches are cleared when those failures are observed. Usage readers are replaced on the same errors, including nested repository or bot-source errors; an in-flight reader from before revocation cannot publish its result.
- Numeric usage history retains its own `history_sampled_at` and `history_stale` fields alongside independently sampled quota. It becomes stale after five minutes on every snapshot read, including while a refresh is running. Fresh quota does not refresh the history timestamp, and a stale-history notice remains visible in source coverage.

The response sets repository, bot, and top-level partial or unavailable fields instead of turning missing evidence into healthy status.

## Blocking and nonblocking snapshots

`snapshot()` remains blocking and is used by tests and direct measurements. `snapshot(nonblocking=True)` returns the current cached snapshot, or an initial loading response, while one daemon refresh runs. Concurrent callers share that refresh. Collection does not hold the cache read lock, and a failed background refresh releases the refresh slot so a later request can retry.
