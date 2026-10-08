# Dashboard GitHub data

The dashboard reads GitHub through the locally authenticated `gh api` command. It makes no GitHub writes. Repository and workflow identifiers come from the validated startup configuration, and links in the response are limited to the configured GitHub owner.

## Collection flow

Each refresh checks the REST core rate limit, then lists open pull requests for every repository before it reads any repository's runs or jobs. A configured list is one pulls request each. `repositories: all` is one repository list and one owner-wide search, then a pulls request only for a repository the search says has an open pull request. Before any per-pull-request detail, one GraphQL search reads every open pull request's head sha, merge state, and the head ref's check rollup (`statusCheckRollup.state`). GitHub priced that query at 1 point for a page of 100 on 2026-10-08. A second page is read only when more than 100 are open. The search uses `user:` or `org:` once the owner's kind is known. This read does not consume the 200-call detail budget, so it still runs when that budget is already spent. A pull request whose head sha or rollup no longer matches its stored detail keeps that detail on screen and is detailed before any pull request that still matches. The row says the checks are from the previous push, or from before the check state changed, and that newer checks are loading. It is not merge-ready. Detail is spent next, changed heads first, then least-recently sampled (never sampled before that), so a busy head of the list cannot take the whole 200-call budget on every pass. A pull request this process has already read keeps that reading when a later pass does not reach it: the checks, bar, counts, current work and last push stay, the row shows the age, and the title is not green. `Checks are not loaded yet` is only for a pull request that has never been read. That row still shows the title, author, head and the cheap pass's overall check state (pending, failing or passing). A budget stop or a transient error does not replace a stored reading with an empty one. A 403 or 404 still leaves that pull request unavailable. Bot workflow names are read after that, with whatever budget remains. A repository scan, once its detail is reached:

1. Refreshes direct subscription evidence when the five-minute inventory cache expires.
2. Lists open pull requests.
3. Joins each pull request to check suites and check runs for its exact head SHA. The check runs come from one listing for the head (`commits/{sha}/check-runs?filter=all`), not one request per check suite, and a run counts only when its suite belongs to that head. `filter=all` is required: `filter=latest` keeps the newest `completed_at` and drops a queued or in-progress run, so a finished pass hides the pending check GitHub is showing. Within one suite the newest check run id is the current attempt, because a queued rerun often has no timestamps.
4. Joins Actions runs by head SHA and check-suite ID, selecting the latest run attempt and its matching jobs.
5. Reads commit statuses and then re-reads the pull request head. Evidence is discarded if the head changed during collection. The same read supplies merge state and the comment totals.
6. One GraphQL read per repository that has an open pull request (further pages when it has more than 50) loads each open pull request's review decision, review threads and the newest commits' messages and push times. GitHub prices a GraphQL query by the page sizes it asks for, not by what comes back, so the read asks for exactly the number of pull requests the REST list found open: about 1.2 points each, where a fixed page of 50 cost 61 points for one pull request. The read counts as one budget unit and is not reused with `If-None-Match`, because GraphQL is a POST. If it fails, the pull request's checks stay and its title stays white: a missing comment count is not shown as zero. At most four pages (200 pull requests) are read; threads past the first 100 on a pull request are a lower bound, shown with a plus, and that pull request is not treated as merge-ready.
7. Lists checks the base branch requires that have not reported on the head as `expected` rows (category `pending`). Rulesets are read once per base branch per scan. GitHub refuses that read with 403 on a private repository whose plan has no rulesets (a free personal account); classic branch protection is still read, and a 404 there means the branch has none. That one classic protection read per base branch returns the required status checks and whether the branch restricts who may push; it needs Administration read, so a token without it leaves both out and keeps the pull request's other evidence. On a branch that restricts who may push, GitHub reports every pull request's merge state as `blocked` to this token, so the title's merge-ready test reads `blocked` as clean there when GitHub reports no conflict and no outstanding review. A required workflow counts only for a run created after its current pin took effect, learned from the ruleset's version history and cached until the pin moves. GitHub serves an organization ruleset's history only with organization administration write; when it refuses, any run of the required workflow counts.

Check reruns are resolved by check suite, GitHub App identity, and check name. A newer rerun replaces an older attempt from the same provider. Providers that use the same check name remain separate.

Each event that starts a workflow on the same head (a push, a review, a review comment, a label) creates a separate run in its own check suite, so one job can appear several times. Only the newest copy counts: a job, and the check run it reports, is keyed by workflow file and job name, and the run created last wins. Non-Actions checks are keyed by provider and check name. Check totals, attention, and the current-work lines use only these current copies, and a run whose jobs were all superseded is dropped. Jobs with the same name in different workflows remain separate.

If a listed pull request's detail requests fail, its identity remains in the response with `evidence_available: false`. The dashboard may claim that a readable repository has no open pull requests only when the open-pull list itself completed successfully.

## Request and history bounds

The default refresh budget is 200 GitHub requests that cost rate limit, counting each REST page and each GraphQL read. Paginated endpoints use explicit 100-item pages. Every `gh api` process requests one page and increments the budget once. A hidden `--paginate` call is not used.

GraphQL is metered separately, in points, and the token's points are shared with everything else that uses the same App. Once a GraphQL response shows less than half of them left, the dashboard stops its GraphQL reads until the counter resets. The REST reads go on, and the latest push, merge-ready title and comment count are blank until then.

Every response that carries an ETag is kept in memory and requested again with `If-None-Match`. GitHub answers an unchanged resource with `304 Not Modified`, which does not count against its hourly rate limit, so the dashboard reuses the kept response and returns that request's budget unit. The budget therefore counts only requests that cost rate limit. Only responses used during the previous refresh are kept into the next one, and the whole cache is dropped when authentication or access is revoked. With a state directory the kept responses are also written there after each pass, so the first pass after a restart is as cheap as any other ([kept across a restart](#kept-across-a-restart)). GitHub does not meter every endpoint against one hourly counter. On 2026-10-05 the check-suite, single-run, run-attempt jobs, workflow list, and workflow runs endpoints were refused with `X-Ratelimit-Remaining: 0` and their own reset time, while pull requests, commit check runs, commit status, and the run list of the same account still had about 3,370 requests left, and `GET /rate_limit` reported neither counter as used. So when GitHub refuses a request because an hourly counter is spent, only that endpoint family (its path with owner, repository, IDs, and SHAs ignored) waits until the reset GitHub reported on the refusal; the other families keep reading. A rate-limit refusal without a spent counter, such as a secondary limit, pauses every request for 60 seconds. `github.api.lowest_remaining` is the lowest `X-Ratelimit-Remaining` seen on any response during the refresh, including refusals, and is the honest headroom; `github.api.remaining` is what `/rate_limit` reports.

Bot collection first lists the workflows on each readable repository and caches successful listings for five minutes. A configured filename absent from that successful listing needs no run calls; an API failure remains unknown. Roles sharing a present workflow share its scan. Active runs are fetched before history. Seven days of lightweight run metadata are collected across pages and sorted by update time before fetching jobs, so a recently rerun workflow on a later page is not skipped. Job reads stop once every role has five completed results and the next run was updated more than two hours ago and no later than each role’s fifth result. This bounds expensive job reads without assuming pages are ordered by completion time. Jobs are read for at most the 50 newest completed runs of a workflow, so a configured job name that never runs cannot read every run of the last seven days. When that limit ends a scan, a role's history stays `complete` only if its five newest results across all repositories are newer than the runs the scan skipped; otherwise its history coverage is `partial`, and a role with no result in the scanned runs has no history timestamp. The history query covers runs created in the last seven days; a rerun of an older run is outside this query. A budget or API failure marks history coverage `partial`.

The output keeps the latest five completed jobs per role within seven days and the latest five within two hours. `latest_failure` is set only when the most recent completed outcome failed. A newer success clears it.

Only a job whose GitHub status is `in_progress` makes a bot `working`. A queued job is not working. A complete active scan with no in-progress job yields `idle`. Missing active coverage yields `unknown`. Only telemetry can supply `down`.

## Cache and timestamp contracts

- `github.sampled_at` is the collection start time. `github.heads_sampled_at` is the last cheap head reading, including the background beat. `github.head_reading` carries that reading's call count, GraphQL points, the 180 second beat, and the calls and points those make in an hour while nobody is watching. Each repository retains its own observation timestamp after its current-head reads finish, so later bot scans do not prematurely age freshly read PR evidence. A failed refresh does not advance source timestamps.
- Repository `sampled_at` records when that repository was read successfully.
- Bot `sampled_at` records an active observation or a complete active scan. `history_sampled_at` records observed history; separate coverage fields identify partial results.
- Successful GitHub snapshots are reused for 60 seconds.
- Subscription inventory is reused for five minutes. Reusing it does not reset its age.
- Fully completed job lists are cached by repository, run ID, and run attempt until the run is seven days old. Active or partly completed jobs are never placed in that cache.
- Any failure other than lost access keeps the last repository rows and bot history and marks them stale: a rate limit, a timeout, a spent budget, an unreadable answer, and a failure code this version does not name. Age does the same after 180 seconds: the rows stay, `stale` becomes true, and the two-hour and seven-day history windows still drop runs by their own timestamps. A pull request is merge-ready only when its checks were read on this pass and are still within that 180 seconds. An older check reading keeps the row, including its checks, clears the green title, sets the pull's `stale`, and sets `checks_age_seconds`. A head or rollup that has since changed keeps those same checks and says newer ones are loading. A failed page refresh keeps the last snapshot on screen, with those same titles cleared. Opening the page paints that cache at once. It does not wait for the head reading, because that reading cannot turn a title green.
- While nobody is refreshing, a background beat reads heads only. It wakes every 15 seconds and reads when the last head sample is 150 seconds old, so the sample on screen stays under 180 seconds. It does not reset the 60 second full-refresh timer. For an owner with 22 open pull requests that is one GraphQL call and 1 point a beat: 24 calls and 24 points an hour, under 0.5% of the 5,000 point hour. Adding the pull request's title and author to that query still cost 1 point on 2026-10-08. It does not spend the REST detail budget. The beat does not run during a full pass, and it does not run again until that pass's head sample is 150 seconds old, so a viewer adds the same one call and one point once per full pass (at most 60 an hour, 1.2% of the point hour, and less when a pass takes longer than a minute). A finished head reading replaces the open list: a pull request that has since opened is shown from that reading, checks not loaded yet, with the cheap overall state, and a pull request that has closed leaves the list. A reading that stopped early keeps every row it already had. The 200-call detail cap is unchanged.
- Authentication, permission, and missing-resource failures do not reuse private cached rows. Completed-job and inventory caches are cleared when those failures are observed, and every file in the state directory is deleted. Usage readers are replaced on the same errors, including nested repository or bot-source errors; an in-flight reader from before revocation cannot publish or keep its result. A usage read that fails any other way keeps the last token history and the records already read.
- Numeric usage history retains its own `history_sampled_at` and `history_stale` fields alongside independently sampled quota. It becomes stale after ten minutes (two missed scans) on every snapshot read, including while a refresh is running. Fresh quota does not refresh the history timestamp, and a stale-history notice remains visible in source coverage.

The response sets repository, bot, and top-level partial or unavailable fields instead of turning missing evidence into healthy status.

## Kept across a restart

Started with a state directory (`--state-dir`, or `VIBE_DASHBOARD_STATE_DIR`, which the Linux unit
sets), the dashboard writes what a restart would otherwise lose, and reads it back when it starts.
Without one it keeps nothing, as before. `dashboard/state_store.py` owns the files.

| Document | Holds | Written |
|---|---|---|
| `reading` | the last GitHub reading the page was served: repositories, pull requests, checks, bot history | after every pass that did not meet lost access |
| `jobs` | completed job lists, by repository, run and attempt, each until its run is seven days old | with `reading` |
| `responses` | every GitHub answer that carries an ETag and was used in the last two passes | with `reading` |
| `usage` | the last token history and every usage record read | after every usage scan that returned and did not meet lost access |
| `usage-responses` | the usage reader's ETag answers (artifact listings) | with `usage` |

Each document is one file, `<directory>/<owner>/<document>.json.z`: zlib-compressed JSON with the
format version, a hash of the owner, repository selection and bot definitions, and the time it was
written. Dashboards of different owners may share a directory. Telemetry, the tab icon, the
subscription inventory and the rate-limit pauses are not kept.

- **Only this account reads it.** The owner's directory is created 0700 and every file 0600. A file
  is written to a draft beside it and renamed over the old one, so a reader never sees half of one.
  A file is used only when it is a regular file this account owns that nobody else can read or
  write. A symbolic link is never followed, for reading or writing.
- **Bounded.** A document is at most 16 MiB on disk and 64 MiB decompressed, which is the most a
  start loads into memory at once. A cache is compressed as it is encoded, one answer at a time,
  so writing it never holds the whole text in memory. A document over either limit, or one the
  disk refuses, is not written, and one line on standard error says so; the previous file stays
  until a later write replaces it or it expires. There is one file and at most one draft per
  document.
- **It expires.** A file written more than 24 hours ago is not used. Neither is one written for
  another owner, repository selection, bot definition or format version, one dated more than five
  minutes in the future, or one that does not decode. Each is deleted when it is read, and the
  dashboard starts empty. A reading nested too deep to copy, which no reading the dashboard
  writes is, is not served either.
- **Lost access deletes it.** A pass that meets an authentication, permission or missing-resource
  failure, on any source, deletes every file and writes none. So does the head beat, which is the
  only thing reading GitHub while the page is closed: a token refused to it empties the reading in
  memory as well. The same failure on a usage scan deletes the two usage documents, whether the
  scan failed on it or returned what it could still read. A usage write that is under way when a
  pass loses access is deleted when it lands.
- **A kept answer is not trusted on its own.** A response is served from the kept copy only when
  GitHub answers that request with 304, which it does only for a token that may still read it. A
  kept job list is used without a request, as it is in memory, and only for a run that GitHub has
  just listed to this token.

On a start with a kept `reading`, the page is served that reading at once, with `github.restored`
true, while the first pass runs. Every repository row and every bot in it is `stale`, the reading
keeps its own `sampled_at`, and no pull request in it is merge-ready: a title turns green only on
checks this process read. A pass that fails without losing access keeps showing it. A row carried
from it into a later reading, because that repository could not be read, keeps `restored` true.
Token history is different: it is a record of finished runs, not a current state, so a kept one
is served as it was and goes stale by its own age, ten minutes after its scan.

State on disk is written by whichever version ran before the restart. The first failure this
version cannot explain after a start that was handed any (a snapshot, a pass or a usage scan that
raises) is blamed on it, once. The kept reading, every row still carried from it, the token
history and the files are dropped; a pass or a head beat that was in flight publishes and writes
nothing kept; and the collector's caches are emptied by the next pass. A reading this process made itself
stays. The dashboard then carries on as it does on a first start.
`tests/test_dashboard_state_store.py` covers the files and every refusal,
`tests/test_dashboard_restart.py` what a restart, a failed read, lost access and the head beat
serve, and `tests/test_dashboard_restart_usage.py` the same for token history.

## Blocking and nonblocking snapshots

`snapshot()` remains blocking and is used by tests and direct measurements. `snapshot(nonblocking=True)` returns the current cached snapshot, the reading kept from before a restart, or an initial loading response, while one daemon refresh runs. Concurrent callers share that refresh. Collection does not hold the cache read lock, and a failed background refresh releases the refresh slot so a later request can retry.
