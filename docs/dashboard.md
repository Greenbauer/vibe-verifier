# Local CI dashboard pilot

The dashboard is an optional, read-only companion. It shows open pull requests from one configured
GitHub owner, current-head checks and Actions jobs, explicit bot workflow history, and optional local
usage/capacity telemetry. It does not change the existing Vibe Verifier runner or gates.

The static view adapts the compact layout, CSS structure, and pure interaction patterns from the
operator-approved local dashboard prototype dated 2026-09-30. Its synthetic preview records were not
copied into the live dashboard. Live mode has no built-in data.

## Navigation, back/forward, and refresh

The current view lives in the URL hash: `#/prs`, `#/usage`, `#/capacity`, or
`#/pr/<owner>/<repo>/<number>` for one pull request. Every view change adds a browser history
entry, so the back and forward buttons move between views, and refreshing or opening a copied link
lands on the same view. A selected PR stays selected while GitHub data loads; an unavailable PR
shows an explanation and a way back instead of silently switching views. An empty or unknown hash
shows pull requests and is rewritten to `#/prs` in place. The hash never reaches the server.

The search and repository filters, subscription/attention filters, and selected bot are kept in
the current tab's session storage, scoped to the configured owner, and restored on refresh. They
contain no API data or credentials, are read only by that dashboard origin, and disappear when the
browser ends the tab session. If storage is unavailable or a saved value is invalid, the dashboard
remains usable with defaults. No server write is added. `tests/test_dashboard_view_state.py`
covers the routes, history, and storage.

## Tab icon

The browser tab shows the configured owner's GitHub avatar with the dashboard's green check badge in
the corner, so the tab names both the owner and this dashboard. The server reads the public avatar
from `https://github.com/<owner>.png` on the first request for `/favicon.svg` and inlines it in the
SVG it serves, so the browser loads nothing from GitHub and the CSP is unchanged. If the avatar
cannot be read, the icon is the owner's first letter on a color derived from a SHA-256 hash of the
lowercased owner name, with the same badge; the server tries the avatar again after five minutes.
A successful avatar is kept until the process restarts, so a changed GitHub avatar appears after a
restart. `tests/test_dashboard_favicon.py` covers the avatar, the fallback, and the retry window.

## Start it

Requirements are Python 3, `gh`, and an existing `gh` login that can read every selected repository.
Keep both local JSON files owned by the account running the server and not group- or world-writable.

```bash
chmod 600 /absolute/path/dashboard.json
bin/vibe-dashboard --config /absolute/path/dashboard.json --port 8765
```

Open the printed `http://127.0.0.1:8765` URL. The address is fixed to IPv4 loopback. There is no
public listener, browser credential, built-in authentication, or CORS access. Without
`proxy_origin`, forwarded headers are rejected. The server accepts `GET` only, rejects untrusted or
duplicate routing headers, and serves a fixed path allowlist with no remote scripts, fonts, or icons.

## Keep it running on the newest main

`bin/vibe-dashboard-follow` runs one or more dashboards and keeps them on merged code. Every
minute it fetches `main` from origin and fast-forwards its own checkout. A merged change to the
server's Python (`dashboard/` outside `dashboard/static/`, or `bin/vibe-dashboard`) restarts the
dashboards; the first GitHub sample after a restart takes a few minutes to load. A change under
`dashboard/static/` needs no restart, because the server reads those files on every request, so a
browser reload shows it within a minute. A change to the follower re-runs it. A dashboard that exits
is started again. If origin is unreachable or the checkout has local edits, it logs that and keeps
serving the code it has.

Give it a clone nobody edits, so a fast-forward always applies:

```bash
git clone https://github.com/Greenbauer/vibe-verifier.git ~/.vibe-verifier-dashboard/checkout
~/.vibe-verifier-dashboard/checkout/bin/vibe-dashboard-follow \
  --serve /absolute/path/octocat.json 8765 --serve /absolute/path/other-owner.json 8766
```

On macOS, a LaunchAgent starts it at login and again if it exits. Save this as
`~/Library/LaunchAgents/<label>.plist`, with absolute paths and the Python and `gh` that work in
your shell (launchd's default `PATH` has neither Homebrew directory), then run
`launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/<label>.plist`:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>LABEL</string>
  <key>ProgramArguments</key>
  <array>
    <string>/opt/homebrew/bin/python3</string>
    <string>/Users/YOU/.vibe-verifier-dashboard/checkout/bin/vibe-dashboard-follow</string>
    <string>--serve</string><string>/absolute/path/octocat.json</string><string>8765</string>
  </array>
  <key>EnvironmentVariables</key>
  <dict><key>PATH</key><string>/opt/homebrew/bin:/usr/bin:/bin</string></dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>/Users/YOU/.vibe-verifier-dashboard/follow.log</string>
  <key>StandardErrorPath</key><string>/Users/YOU/.vibe-verifier-dashboard/follow.log</string>
</dict>
</plist>
```

The log gets one line per update or restart plus anything a dashboard prints. To remove it, run
`launchctl bootout gui/$(id -u)/LABEL`, then delete the plist and `~/.vibe-verifier-dashboard`.
`tests/test_dashboard_follow.py` covers the fast-forward, the restart rule, and restarting a
dashboard that exited.

## Configuration contract

Configuration is read once at startup. Restart to change it. Every repository must belong to the
one configured owner. `bots` maps CI workflow stages; `agents` declares the SWE/QAE identities
shown in the UI. Names retain configured numbers. A workflow mapping describes aggregate CI activity
and is allowed only for unnumbered SWE or QAE. Omit `workflow_role` for a persistent agent whose
ID is supplied by the runtime collector. An omitted roster shows no invented agents. See
[agent identities](dashboard-agents.md) for the runtime contract.

```json
{
  "version": 1,
  "owner": "octocat",
  "repositories": ["octocat/example"],
  "bots": {
    "reviewer": {"workflow": "review.yml", "jobs": ["review"]},
    "explorer": {"workflow": "explore.yml", "jobs": ["explore"]},
    "verifier": {"workflow": "verify.yml", "jobs": ["verify"]}
  },
  "agents": [
    {"id": "ci-swe", "name": "SWE", "role": "swe", "workflow_role": "reviewer"},
    {"id": "ci-qae", "name": "QAE", "role": "qae", "workflow_role": "explorer"}
  ],
  "telemetry_file": "/absolute/path/telemetry.json",
  "proxy_origin": "https://dashboard.example.test"
}
```

`telemetry_file` is optional and must be absolute. Deleting it makes telemetry unavailable without
affecting GitHub data. `proxy_origin` is optional; omit it for local-only mode. It must be one exact
HTTPS origin with a hostname and optional valid port, with no trailing slash, credentials, path,
query, fragment, wildcard, or control character. Configuration is immutable after startup. Selected
repository coverage is labeled as selected coverage, never as the whole account or organization.

## Optional private HTTPS proxy

Hosted access requires a trusted private upstream that authenticates every user and blocks public
access. Headers do not authenticate a request. The machine must also prevent untrusted local
processes from reaching the dashboard's loopback port because direct loopback reads remain allowed.

Configure the upstream at the origin root, not a subpath. It must remove client-supplied forwarded
headers, connect only to this loopback backend, and send exactly one `X-Forwarded-Host` containing
the configured origin authority plus exactly one `X-Forwarded-Proto: https`. Its upstream `Host`
must be `localhost` for a Unix-socket proxy, `localhost:<actual-port>` or
`127.0.0.1:<actual-port>` for TCP, or the configured origin authority itself for a proxy that
preserves the client's `Host` (Tailscale serve does). Any other `Host` is refused with 421, and the
origin authority is accepted as `Host` only with those forwarded headers present. Any browser
`Origin` must equal `proxy_origin` exactly. Missing,
mismatched, or duplicate routing headers are denied.

This option does not add a public listener, authentication, CORS, or write methods. The backend
still binds only to `127.0.0.1`; the existing assets, CSP, root paths, and read-only API are
unchanged. Direct local reads use no forwarded headers and retain the local HTTP origin.

## Optional telemetry contract

The [optional Linux collector](dashboard-collector.md) atomically replaces the telemetry file.
Its native host/account format is validated and adapted by `dashboard/collector_view.py`, retaining
source timestamps, combined CI/QAE slot limits, and separate lane caps. It reads only the configured
owner’s assignments and aggregate host resources. The dashboard never writes telemetry.
Other collectors can use this version 1 shape:

```json
{
  "version": 1,
  "owner": "octocat",
  "capacity": {
    "sampled_at": "2026-09-30T15:00:00Z",
    "host": {
      "cpu_percent": 42.5,
      "memory_used_bytes": 4000000000,
      "memory_total_bytes": 8000000000,
      "workspace_disk_free_bytes": 60000000000,
      "workspace_disk_total_bytes": 100000000000
    },
    "lanes": [
      {
        "id": "lane-01",
        "state": "busy",
        "registered": true,
        "labels": ["linux"],
        "job": {
          "repository": "octocat/example",
          "name": "test",
          "url": "https://github.com/octocat/example/actions/runs/1"
        }
      },
      {"id": "lane-02", "state": "provisionable", "registered": false, "labels": ["linux"]}
    ]
  },
  "usage": {
    "sampled_at": "2026-09-30T15:00:00Z",
    "accounts": [
      {
        "id": "account-a",
        "label": "Primary",
        "provider": "Example provider",
        "quota_windows": [
          {
            "name": "7 days",
            "used_percent": 25,
            "resets_at": "2026-10-02T15:00:00Z",
            "window_minutes": 10080,
            "allowance_tokens": 1000000
          }
        ]
      }
    ],
    "samples": [
      {
        "owner": "octocat",
        "account": "account-a",
        "bot": "reviewer",
        "timestamp": "2026-09-30T14:00:00Z",
        "input_tokens": 100,
        "output_tokens": 50
      }
    ],
    "completeness": "Describe the source, freshness, and which calls are included."
  },
  "bots": {
    "sampled_at": "2026-09-30T15:00:00Z",
    "states": [
      {"owner": "octocat", "bot": "reviewer", "state": "idle", "detail": "Source reports available"}
    ]
  }
}
```

Lane `state` is one of `busy`, `allocated`, `ready`, `provisionable`, `offline`, or `unknown`. `registered` is a
separate `true`, `false`, or `null` fact because an allocated on-demand lane can have no idle runner.
A busy lane must include a same-owner job or a positive GitHub `runner_id` with confirmed
registration. The latter shows busy with unmatched job details until a current owner-scoped
PR job matches the runner ID. Registration alone never implies ready or busy. An allocated lane without a matched job shows its repository
and pending-match message without a job link. The host panel is always labeled `SHARED HOST`; its values
are aggregate CPU sampled percent, memory bytes, and workspace-filesystem bytes. Every host meter
fills with how much is in use, so a fuller bar means less headroom; disk used is total minus free.

Displayed agent state comes from an explicit identity mapping. Aggregate CI mappings use GitHub
activity: active jobs prove working, and a complete active scan permits idle. Runner listener
health never establishes agent health. Persistent SWE/QAE identities use their own runtime state
and history, including paused; missing or stale runtime state is unknown. Numbered identities cannot
be mapped to aggregate CI job roles. Recent history uses structural run outcomes and timestamps.
Unavailable or incomplete history is labeled separately from a complete history with no failures.

Quota windows can omit `allowance_tokens` and `window_minutes` (the window's length, at most 31
days). The charts show each bot separately and then all bots combined. The All bots card draws a
flat pace line: the tokens per hour that spend the rest of the plan's window exactly at its reset,
which is (100% minus used%) of the window divided by the hours left. The server computes it once
(`dashboard/pace.py`) and the page draws that number. The window's size in tokens comes from one of
two places:

- **Reported.** A window with `allowance_tokens` uses it, but only when every plotted sample belongs
  to that same account.
- **Measured.** Otherwise a window with `window_minutes` is sized from the plotted bots' tokens since
  it began (reset minus length) divided by its used percentage. A plan's percentage is account-wide, so when other use shares the plan this
  treats the bots' tokens as standing for all of it. The line is then right while the bots keep
  their share, and the page's hover says so.

When the plan has several windows (say 5-hour and 7-day), the longest one is paced. No
line is drawn, and the page says why, when two plans could each be billed for the same tokens, the
plan shows 0% used, no bot tokens fall inside the window, the window began more than the seven days
of kept samples ago, or token history is stale or partial.
The All bots header also states the plan's fill against an even burn of its window, in points:
used percent minus the share of the window elapsed, as `12% under pace`, `21% ahead of pace` or
`on pace` (within one point). It needs only the window's length and reset, so it shows even when the
line cannot be sized. No price is inferred.

Every token sample repeats the owner, account, bot, timestamp, input count, and output count. Any
cross-owner row rejects the whole file. Samples older than seven days are discarded. The file is
limited to 2 MiB, 1,024 lanes, 64 accounts, and 20,000 token samples. Sections older than five
minutes are visibly stale, and stale runner assignments/readiness become unknown. Invalid, missing, unsafe-permission, or mixed-owner files are unavailable,
not zero.

## GitHub behavior and limits

The server uses bounded `gh api` pages and JSON fields, never formatted table output. It reads
PR check suites and check runs for the current head, joins Actions runs by exact head SHA plus check
suite ID, selects the greatest run attempt for each run ID, and loads jobs from that exact attempt.
When several runs of one workflow exist on the head (each triggering event starts its own run), only
the newest copy of each job and check counts toward step totals and attention.
It re-reads the PR head after collection; a race drops the collected evidence instead of attaching
it to the new revision. Commit statuses and third-party checks remain separate evidence rows.

GitHub lists a check the base branch's rulesets require as "Expected" without creating a check run
for it, so the dashboard reads the rules for each pull request's base branch and adds an expected
row for every required status check that has not reported and every required workflow that has not
run on the head. A required workflow runs at the SHA its ruleset pinned when the run was triggered,
so after the pin moves GitHub waits for a new run. The dashboard reads the ruleset's version history
once per pin to learn when the current pin took effect, and a run created before then does not
count. An expected row keeps the pull request pending, flags it for attention, and keeps its step
total unknown. GitHub serves an organization ruleset's history only to a token with organization
administration write, which a read-only dashboard should not hold. When the history is refused,
any run of the required workflow on the head counts, so the pull request keeps its evidence; right
after a pin moves it can show as passed while GitHub waits for a rerun, and GitHub's merge box
still enforces the rule. A required workflow that never ran on the head is still expected.
Classic branch protection is not read.

Direct manifest or workflow evidence reports `subscribed`. An installation subscribed only through
an organization wrapper or ruleset reports `unknown` in this pilot, not `false`; wrapper/ruleset
resolution is not duplicated here. Bot roles sharing a workflow share collection. Active runs are collected independently of history;
history is inspected until the newest five outcomes are known or coverage is explicitly partial.
Jobs are read for at most a workflow's 50 newest completed runs, so a job name that never runs in
that workflow reports partial history instead of reading every run of the past week.
A refresh is capped at 200 REST page requests that cost rate limit; an unchanged answer (HTTP 304) is free
and not counted. Active data is cached for 60
seconds and direct subscription inventory for 300 seconds. The response includes calls used, the
reported REST limit/remaining/reset values, and the lowest remaining count seen on any response
(`lowest_remaining`; GitHub meters some endpoint families against a separate counter that the reported
values omit). A spent counter pauses only its endpoint family until its reset. Rate limits and source errors return partial or briefly
stale data without advancing its successful sample timestamp. Authentication or access revocation
clears derived repository data immediately; transient stale data expires after three minutes.

The dashboard shows source-proven failures, cancellations, waiting jobs, and current elapsed times.
The pull request list groups pull requests by repository. A repository with no open pull request
that matches the filters gets no group. Groups are ordered by their most recently updated pull
request, newest first; within a group the newest pull request is first and the oldest is last.
A pull request's badge shows its worst current-head check. A skipped check never outranks a passed
one, so the badge reads Skipped only when every check was skipped.
The step meter is one line for the whole step total. Each outcome takes a share of that line
equal to its count, with a gap between shares: green for passed, yellow for pending, red for
failed, and gray for skipped, cancelled, or unknown. Finished steps over the total stay in the
text above the line. The badge beside it, not the line, is the worst current-head check.
Elapsed time alone never asserts that a job is stuck. The Actions timeline uses shared wall-clock
coordinates for parallel jobs and does not sum their durations. Unknown step totals never render as
100 percent. A completed job with no steps, which is how GitHub reports a skipped job, counts as
zero steps; a job that has not started yet keeps its run's step total unknown.

## Data lifecycle and uninstall

- Configuration: create the owned local file, read it once at startup, restart to update its owner,
  repository selection, telemetry path, or proxy origin, and delete it after stopping the process
  to remove the installation.
- Tab icon: the owner's public avatar, read once and held only in memory until the process stops.
- GitHub cache: created from server-side reads, replaced by scoped source identity, held only in
  memory, expired on source failure, and deleted when the process stops.
- Telemetry: created and atomically replaced by an optional collector, read only by this process,
  bounded to a current snapshot plus seven days of samples, and unavailable when deleted.
- GitHub records: never created, updated, or deleted by this dashboard. There are no retry, cancel,
  merge, pause, send, save, or publish controls.

## Verification map

`tests/test_dashboard_config_telemetry.py` covers owner isolation, two instances, mixed telemetry,
quota pace, stale sections, and 16-lane on-demand capacity. `tests/test_dashboard_github.py` covers
current-head suite/run/attempt joins, race handling, expected required checks and stale workflow pins,
status/step categories, pagination, rate limits,
and subscription uncertainty. `test_dashboard_bot_history.py` covers bounded history and workflow
discovery; `test_dashboard_service.py` covers source timestamps and refresh caching.
`test_dashboard_live_service.py` covers independently aging quota/history and revocation during
an in-flight refresh. The collector and usage-artifact test files cover the native source contracts.
`tests/test_dashboard_server_ui.py` covers loopback HTTP, proxy and direct routing headers,
read-only methods, Host/Origin/traversal, XSS-safe JSON and DOM construction, filters, account usage
math, local assets, the tab icon route, and the approved palette. `tests/test_dashboard_usage_charts.py` covers the usage
charts' clock-hour mapping, observed and unobserved hours, usual-day averages, scales, and pace. The repository's existing unittest command runs all
of them. Configuration tests cover the strict optional proxy origin and its immutable default.

Live acceptance uses private configuration outside this repository, reconciles displayed PRs and
capacity with actual source records, and walks desktop/mobile views with browser console and network
inspection. CI runs the complete suite on Ubuntu. This public repository contains no private
configuration or captured telemetry. See [collection/cache behavior](dashboard-data.md) and the
[numeric usage artifact contract](USAGE-CONTRACT.md).

The server refreshes GitHub and usage sources in the background; the page retains search focus
and expanded job details during refresh. The page asks for new data every 30 seconds while it is visible; a hidden
tab skips those requests, so it spends no GitHub calls, and loads once when shown again. Numeric usage artifacts are read
every five minutes through the current GitHub credentials, verified against their run/attempt/head
and configured workflow, and parsed without extracting files or copying model content. Each scan
pages through a repository's artifacts, newest first, until a page reaches past seven days, at most
five pages (500 artifacts) per repository; a longer week reports partial coverage. Each scan has a
budget of 80 GitHub calls. A scan that spends it keeps the records earlier scans read, reports
partial coverage, and reads the rest on the next scan; only lost access clears them. Token history
counts as stale after two missed scans (ten minutes), not during an ordinary refresh. Captured token
records expire after seven days and disappear when source artifacts are removed. Missing captures
remain unavailable. Old runs cannot be backfilled. The deterministic QAE verification gate has no
model-token usage and is shown only in PR progress. The only use of these counts beyond the charts
is sizing the plan's window for the pace line, described above; no price is inferred.

## Bot usage charts

The Bot usage view draws one card per configured bot, then an All bots card: one column on a
phone, two from 700 pixels wide, four from 1340. The x-axis is the viewer's local clock hour, with
faint gridlines at 12a, 6a, 12p and 6p and a white marker at now. The solid line is the last 24
hours and runs on to the now marker; the stretch after the marker is yesterday's tail and is faded.
Hours follow the local calendar, so a daylight-saving change does not shift them. The dashed line is a usual
day: each clock hour averaged over the prior six days that were observed, and its hover names the
fewest days any hour averages. Bot cards share one y-scale so they compare at a glance; All bots
adds the bots' observed hours and scales to itself. Only All bots carries the flat, wider-dash pace
line, and only when an allowance is reported as described above; otherwise a note says why it is
absent. Each card's header gives the most tokens it used in one clock hour of the last 24 hours, and
its totals line gives the last 24 hours and, once every hour has an observed prior day, a usual day. There is no period selector: the dashed line already carries the multi-day view,
and a tab saved with the old selector restores without it.

An hour counts as observed from the hour of a bot's first retained sample through the source's last
observation (its `sampled_at`, and the artifact scan time when present) or any later sample. Inside
that span an hour with no sample is a measured zero and is drawn at zero. Before it, and after a
stale source's last observation, nothing is drawn, so one captured run or a stopped collector never
invents zeros. A bot with no sample in the seven retained days, or no usage source at all, shows
"Not yet observed". A partial capture keeps its completeness note under the charts, because hours
drawn as observed can then be missing some runs' tokens.
