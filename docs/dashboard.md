# Local CI dashboard pilot

The dashboard is an optional, read-only companion. It shows open pull requests from one configured
GitHub owner, current-head checks and Actions jobs, explicit bot workflow history, optional local
usage/capacity telemetry, and optional [work queues](#work-view). It does not change the existing Vibe Verifier runner or gates.

The static view adapts the compact layout, CSS structure, and pure interaction patterns from the
operator-approved local dashboard prototype dated 2026-09-30. Its synthetic preview records were not
copied into the live dashboard. Live mode has no built-in data.

## Navigation, back/forward, and refresh

The current view lives in the URL hash: `#/prs`, `#/work`, `#/usage`, or `#/capacity`. Every view change adds
a browser history entry, so the back and forward buttons move between views, and refreshing or
opening a copied link lands on the same view. An empty or unknown hash, including an older
`#/pr/<owner>/<repo>/<number>` link, shows pull requests and is rewritten to `#/prs` in place. The
hash never reaches the server.

The workspace buttons stay on screen while that view scrolls. A narrow window stacks them in a row
above the view, and that row stays put as well. Changing views scrolls the view back to the top.

The Pull requests view is `dashboard/static/prs.js`. A pull request row's title opens that pull request on GitHub in a new tab. The address is the pull
request's `html_url` only when it is an HTTPS `github.com` link under the configured owner. Any other
address leaves the title as text, not a link. A click anywhere else on the row, or on the arrow at
its end, opens the row's [CI timeline](#ci-timeline) under it; the same click closes it.

The search and repository filters, the CI timeline's time scale, subscription/attention filters, and selected bot are kept in
the browser's local storage, scoped to the configured owner, and restored on a refresh, in a new
tab, and after the browser reopens. They contain no API data or credentials, are read only by that
dashboard origin, and stay until the viewer changes them or clears the site's data. If storage is unavailable or a saved value is invalid, the dashboard
remains usable with defaults. No server write is added. `tests/test_dashboard_view_state.py`
covers the routes, history, and storage.

## Tab icon

The browser tab and the header logo show the configured owner's GitHub avatar with the dashboard's
green check badge in the corner, so they name both the owner and this dashboard. The server reads the public avatar
from `https://github.com/<owner>.png` on the first request for `/favicon.svg` and inlines it in the
SVG it serves, so the browser loads nothing from GitHub and the CSP is unchanged. If the avatar
cannot be read, the icon is the owner's first letter on a color derived from a SHA-256 hash of the
lowercased owner name, with the same badge; the server tries the avatar again after five minutes.
A successful avatar is kept until the process restarts, so a changed GitHub avatar appears after a
restart. `tests/test_dashboard_favicon.py` covers the avatar, the fallback, and the retry window.

## Start it

Requirements are Python 3, `gh`, and an existing `gh` login that can read every selected repository,
or every repository of the owner when `repositories` is `all`.
Keep both local JSON files owned by the account running the server and not group- or world-writable.

```bash
chmod 600 /absolute/path/dashboard.json
bin/vibe-dashboard --config /absolute/path/dashboard.json --port 8765
```

Open the printed `http://127.0.0.1:8765` URL. The address is fixed to IPv4 loopback. There is no
public listener, browser credential, built-in authentication, or CORS access. Without
`proxy_origin`, forwarded headers are rejected. The server accepts `GET` only, rejects untrusted or
duplicate routing headers, and serves a fixed path allowlist with no remote scripts, fonts, or icons.

By default nothing is kept when the process stops, so a restart shows no pull requests until the
first GitHub sample has loaded. To keep the last reading, give it a directory of your own:

```bash
bin/vibe-dashboard --config /absolute/path/dashboard.json --port 8765 --state-dir ~/.vibe-verifier-dashboard/state
```

`VIBE_DASHBOARD_STATE_DIR` sets the same thing. The dashboard then starts with the previous reading
on the page, marked old with its age, and with the GitHub answers it had already read, so the first
pass costs about what any other does. The files hold what the page shows about private
repositories, readable only by your account; [what is kept, for how long, and what deletes
it](dashboard-data.md#kept-across-a-restart).

## Keep it running on the newest main

`bin/vibe-dashboard-follow` runs one or more dashboards and keeps them on merged code. Every
minute it fetches `main` from origin and fast-forwards its own checkout. A merged change to the
server's Python (`dashboard/` outside `dashboard/static/`, or `bin/vibe-dashboard`) restarts the
dashboards. With a [state directory](#start-it) a restarted dashboard shows the previous reading,
marked old, until its first GitHub sample replaces it; without one it shows nothing until that
sample has loaded, which takes a few minutes. The follower passes its own environment to the
dashboards it starts, so `VIBE_DASHBOARD_STATE_DIR` on the follower covers all of them. A change under
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
  <dict>
    <key>PATH</key><string>/opt/homebrew/bin:/usr/bin:/bin</string>
    <key>VIBE_DASHBOARD_STATE_DIR</key><string>/Users/YOU/.vibe-verifier-dashboard/state</string>
  </dict>
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

On a Linux host the dashboards are systemd units, and the follower runs as root with
`--unit vibe-dashboard@NAME.service PORT` instead of `--serve`. In that mode it moves only to a `main`
commit whose GitHub check runs all passed, restarts the units, checks that each dashboard answers
`/api/dashboard` on its port, and resets to the previous commit when one does not. The
[Linux host runbook](dashboard-host.md) covers that mode, the units, the root timers that refresh a
narrowed read-only GitHub token and local telemetry, and the accounts and files they use.

## Configuration contract

The configuration file is read once at startup. Restart to change the file. Every repository must
belong to the one configured owner. `repositories` is either a non-empty list of `OWNER/NAME`, or
the exact string `all`. The two shapes are different JSON types. A list is that selection until
restart. `all` is every non-archived repository of the owner, discovered again on each refresh, so
a repository that appears or is archived shows up or drops out without a restart. `octocat/all` in
a list is a repository named `all`, not the whole owner.

`bots` maps CI workflow stages; `agents` declares the identities
shown in the UI. Names retain configured numbers. A workflow mapping is one unnumbered roster
entry, SWE or QAE. A QAE entry is shown once per lane instance (QAE 1, QAE 2, and so on) when its
runs used a numbered runner, or when the host reports `qae_concurrency`, including an instance that
has not run. When `qae_concurrency` is set, those instances are the whole QAE roster: no plain QAE
card appears. Tokens that belong to no instance still count in All bots, and that card says how
many of them are unattributed. When the host reports no instance count, a run or sample with no
lane instance stays on the unnumbered QAE, and that card appears only when it has a run or tokens.
An owner who lists a reviewer still sees one SWE. Omit
`workflow_role` for a persistent agent whose ID is supplied by the runtime collector. An omitted
roster shows no invented agents. See
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
    {"id": "ci-qae", "name": "QAE", "role": "qae", "workflow_role": "explorer"}
  ],
  "telemetry_file": "/absolute/path/telemetry.json",
  "proxy_origin": "https://dashboard.example.test"
}
```

A bot's `jobs` are exact job names. A QAE workflow that splits a large pull request across
explorers runs the extra ones as `<job> (2)`, `<job> (3)`, ..., so list those too
(`"jobs": ["explore", "explore (2)", "explore (3)"]`), or their runs are not shown.

`telemetry_file` is optional and must be absolute. Deleting it makes telemetry unavailable without
affecting GitHub data. `proxy_origin` is optional; omit it for local-only mode. It must be one exact
HTTPS origin with a hostname and optional valid port, with no trailing slash, credentials, path,
query, fragment, wildcard, or control character. The file is immutable after startup. A repository
with no open pull request has no group and is not in the filter. The Pull requests page says nothing
about the repository set while it reads well: a problem shows as a yellow dot and a short message
on the line under the title. The search and filters share the title's row and move to their own row
when the window is too narrow. If the owner-wide read for `all` fails and there is no earlier set, it says
`Repository list is unavailable.` and does not claim the owner has zero repositories.

With `all`, an installation token reads `GET /installation/repositories`. A person's `gh` login is
refused there, which is expected, and then an organization reads `GET /orgs/{owner}/repos` while a
user reads `GET /users/{owner}/repos?type=owner`. Which of those repositories have an open pull
request is one `GET /search/issues` (`org:` or `user:`, from the owner's type). A repository the
search leaves out is not read further. Search can lag a new pull request by a refresh. A configured
list does not use that search: each named repository is listed on its own.

## CI timeline

Each pull request row draws its current-head checks on a time axis, like a browser's network
waterfall (`dashboard/static/timeline.js`). It reads only times the dashboard already has: each
Actions job's created, started and finished times and its steps, each other app's check run, and
each commit status. It makes no extra GitHub call.

- **In the row**, the Checks column holds one line per workflow, every job of that workflow drawn on
  it. The lines share the cell's whole height, each up to 12 pixels with a 1-pixel gap, so a head with
  many workflows gets thinner lines rather than a taller row. Green ran and passed, red failed, yellow is running, and dim yellow is a job
  waiting for a runner. Failures are drawn on top so they are never hidden. A dashed line marks now
  while anything is still open. Skipped checks and commit statuses are not drawn; the counts under
  the line still name them. A head whose checks have no time yet keeps the check meter.
- **The time scale** is the same for every row by default: the longest timeline on screen spans the
  column, so a 12-minute run looks short beside a 77-minute one. The toolbar's scale menu can fit
  each row to its own width instead, which shows each run's shape at the cost of comparing rows.
- **A wait for a runner** is the time from GitHub creating a job to a runner starting it. GitHub
  creates a job when its run starts, or, for a job that needs another, when that one finishes, so
  waits start at those moments. A wait of 30 seconds or less is GitHub's normal pickup and is not
  drawn; a longer one means every runner that job can use was busy. Only Actions jobs say when a
  runner picked them up, so another app's queued check run says "queued" with its time and draws no
  wait, and a commit status never waits.
- **Opened**, the timeline lists every job under its workflow's name on an axis with round-time
  ticks, with its run time (or "so far", or how long it has waited) and a Log link to the job on
  GitHub when the job's page is under the configured owner. A job's name opens its steps, drawn on
  that job's own scale (a note says so) because a one-minute job's steps on an hour's axis would be
  dots. Bold names are the critical path: from the last job to finish, back through each job whose
  finish created it (a finish within five seconds of a job's creation). Only Actions jobs are
  created that way, so other apps' checks and statuses are listed but never on that path. A status
  is a report another system posted: it is a tick at its last update, yellow while pending, and
  never keeps the timeline open or widens a row's scale.
- Open rows and open steps last until the page reloads. The page's refresh every 30 seconds keeps
  them open, and keeps focus on the scale menu, a row's arrow or a job's step toggle when one of
  them had it.
- A job's line is empty while it is still within its first 30 seconds of waiting, and another
  app's queued check draws nothing until it starts; the counts under the line include both.

`tests/test_dashboard_timeline.py` runs `dashboard/static/timeline.test.js` and `dashboard/static/prs.test.js` and checks the row's
swap from the meter, the scale menu, and the bar colors and box.

## Work view

`#/work` is a board of the plans and requests a session could pick up, of a person's own to-dos, and
of what waits on a person.
It appears in the menu only when the configuration has `work_sources`, a list of at most eight
read-only queues:

```json
"work_sources": [
  {"id": "plans", "label": "Plans", "kind": "plans", "endpoint": "https://plans.example.test/api/v1",
   "token_file": "/etc/vibe-dashboard/example/plans.token", "repository": "octocat/example",
   "app_origin": "https://plans.example.test"},
  {"id": "calls", "label": "Calls", "kind": "calls", "endpoint": "https://calls.example.test/api/mcp",
   "token_file": "/etc/vibe-dashboard/example/calls.token"},
  {"id": "todos", "label": "Zach's to-dos", "kind": "supabase-todos",
   "url": "https://your-project-ref.supabase.co", "table": "todos",
   "token_file": "/etc/vibe-dashboard/example/todos.token"}
]
```

Each source is reached over HTTPS with `Authorization: Bearer`, the bearer read from `token_file` on
every refresh (a `supabase-todos` source sends it as `apikey` too). The file must be a regular file
of the account running the dashboard that no other account can read (mode 0600), so a replaced key is used without a restart and the key never enters
the configuration. Give each source a read-only key where the source has one. `id` is a lowercase
slug, `label` what the page shows. `kind` says how the source is read; the dashboard makes only
these reads:

| `kind` | `endpoint` is | Lists with | Reads each open item with | What an item carries |
|---|---|---|---|---|
| `plans` | a read API's base URL | `GET <endpoint>/plans?limit=100`, answering `{"plans": [...]}` | `GET <endpoint>/plans/<planId>` | the plan body (`planMarkdown`), every amendment oldest first (`amendments`), status, claimant, PR, preview, blocked reason |
| `calls` | an MCP server (streamable HTTP) | the `list_open_calls` tool twice: `{limit: 100}` for the open Calls, and `{status: ["closed", "merged"], limit: 100}` for the done ones, which that tool leaves out unless asked | the `get_call` tool, `{callId}` | the brief, every follow-up with its design references, every ask with its ruling, status, submitter, claim, PR, preview |

A `supabase-todos` source takes `url` (the Supabase project URL, an HTTPS origin) and `table` (a
lowercase table name) in place of `endpoint`, and no `repository` or `app_origin`. Its table has the
columns `id`, `title`, `notes`, `status` (`todo`, `in_progress`, `waiting` or `done`), `area`,
`source`, `source_url`, `due`, `created_at`, `updated_at` and `done_at`. Row-level security can
stay on with no anon access: the key in `token_file` is one that may read the table (a service-role or
secret key), and it is sent only to that project, server side, never to the page. One read, `GET
<url>/rest/v1/<table>?select=...&or=(status.neq.done,done_at.gte.<14 days ago>)&order=created_at.asc&limit=100`,
lists the rows not done and those done in the last 14 days (a done row with no `done_at` is not
shown). `todo` is To do, `in_progress` In progress, `waiting` Waiting on you and `done` Done; a card
shows the title, the area, the due date and, when `source_url` is HTTPS, links the title to it, with
the notes behind a toggle.

A done Call is kept only when it was updated in the last 30 days; the tool lists oldest first with
no paging, so a full page of done Calls reads as truncated (newer ones may be missing). A merged or
closed item is listed without reading its detail, and an item that is gone by the time
it is read (404) stays listed without one. `repository` (OWNER/NAME of the configured owner) turns a
PR number into a GitHub link; `app_origin` (an HTTPS origin) links the item to
`<app_origin>/plans/<id>` or `<app_origin>/calls/<id>`. A design reference shows its label only: the
links a server signs expire.

The board has four columns, each oldest first. A column is one card with its items as rows, like the pull-request groups:

- **To do**: `submitted` or `backlog`, not picked up yet.
- **In progress**: any other open status, claimed or being built.
- **Waiting on you**: an ask with no ruling, a `blocked` item (with its reason, or "no reason
  recorded"), an `await_answer` item, a `pr_ready` item (its PR waits on review and merge), or a
  to-do whose status is `waiting`. An item here is in no other column. On a phone the columns stack and this one comes first.
- **Done**: `merged` or `closed` (for Calls, those updated in the last 30 days), and to-dos done in
  the last 14 days, behind a Show toggle.

Every row shows when the item was created and last updated; the update age is yellow after three
days and red after two weeks, and the summary line counts open items not updated in over two weeks,
so a stale backlog reads as stale. A row's details hold the request text, amendments or follow-ups,
and asks, as plain text.

Sources are read in the background every five minutes, whether or not a page is open, one after another.
A source that fails keeps its last items, marked stale with the reason (`unreachable`, `unavailable`,
`redirected`, `invalid_response`, `tool_error`); a refused key (`rejected`) forgets them, as lost GitHub access
does. A source never read says why (`credential_missing`, `credential_unsafe`) instead of showing an
empty board. A source that answers with a redirect is not followed and reads as `redirected`, because
following it would send the source's key to the redirect target. Nothing is kept across a restart,
and the dashboard never writes to a source: there is no claim, status, amend, or answer control. More than 100 items in a source shows the oldest 100 and
says so. `tests/test_dashboard_work.py` covers the configuration, the key file, both transports, both
kinds, the columns, failure handling, and that a redirect never carries a key to its target; `tests/test_dashboard_work_todos.py` covers the
`supabase-todos` kind, including that its key never reaches the page.

## Capacity view

`#/capacity` shows what each of the owner's runners is doing now and how busy the lane has been
(`dashboard/static/capacity.js`). Top to bottom:

- **The limits, in words**: `Up to 8 runners at once (AI QA at most 4) · 8 more for jobs that only
  wait · 10 CPU cores · 20.0 GB memory`.
- **Five cards**: runners in use now (`5 of 8`, the runner slots only, and how many the warm pool keeps
  up on purpose); jobs waiting for a runner right now (`Unknown` when the listener's queue was not
  read); and, for the chosen range, the busy share (the share of the runner slots' time spent running
  jobs, from the samples and from each job's start and finish, so a job shorter than a sample counts),
  the wait for a runner, and idle runners (time
  runners were up with no job, not counting the warm pool, which the detail names separately). The
  wait card counts from GitHub handing a job to the lane until a runner takes it. Its value is the
  median; its detail is the 95th percentile, the job count and, when GitHub reports when it queued
  jobs, how long GitHub took on average before handing them over.
- **Runners over time**, with `1h`, `24h` and `7d` buttons (24h first; the choice lasts while the
  tab is open). Each bar is one clock-aligned period (2 minutes, 30 minutes or 3 hours) of the runner
  slots, on their limit: green is the average number running a job, pale green kept warm, yellow idle
  with no job, the dashed outline the rest (free). A red mark above a bar means jobs waited for a
  runner during it. A hatched bar had no samples (the collector was down, or the samples predate this
  history). The last bar is the period still filling. Hover a bar for its numbers.
- **Runners now**, in two pools. **Runner slots** (`5 of 8 in use`) hold two kinds: **Build and
  test** (the repositories' CI jobs) and **AI QA** (the QA explorer against a preview; each slot has
  its own Codex login, and the line under it gives the at-most count). **Waiting-job slots** hold
  jobs that only wait, such as for a preview to deploy; they are cheap and not one of the runner
  slots. Each kind is a titled group with a one-line note. A row is one runner: `Build 3`, `QA 2` or `Wait 1`
  (each kind numbers its own slots; hovering says which service it is: "Service ci@3 on the runner
  machine", the lane's `<lane>-ci@3`), its state (`Running`; `Kept warm`; or `Idle, no job` with how long it
  has been up), the job (linked when the host or GitHub gave a link), the repository and, when the job
  is on an open pull request's current head, its number, and the time. Running time counts from the
  job's start (GitHub assigning it to the runner) when the listener recorded it, else from when the
  runner was allocated. A runner shows as `Kept warm` when the listener names it as one its warm pool
  keeps (`queue.json` `warm_instances`, at most a few seconds plus one collector refresh old) and it
  has no job; a listener that predates that list marks none. Free slots are not runners yet
  (the listener starts one in any free slot when a job arrives), so each pool ends with one count:
  `3 free`. While the runner listener is not answering, the host knows no state for the slots
  without a runner, so each pool shows them as one `N unknown` row instead.
- **The shared host** meters, unchanged.

The history comes only from the [Linux collector](dashboard-collector.md#lane-history); another
collector's file, or one from before this history existed, shows the cards and rows without it.

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
source timestamps, combined CI/QAE slot limits, the wait pool when the lane has one, and separate lane caps. It reads only the configured
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
registration. The Capacity page shows an allocated slot as `Idle, no job` (or `Kept warm`, for the warm pool). When the host recorded
the job, that lane is `busy` and names the job and repository even if no open pull request carries
it, and links the job when the record has a run id and a job id. A missing record falls back to the
current-head pull-request match, and a job that match finds carries its pull request's `pull` number. `registered: null` is omitted: this source never knows registration,
so the page does not call it unknown. An allocated lane that names a repository but has no host
record shows that repository and a pending-match message without a job link. The host panel is always labeled `SHARED HOST`; its values
are aggregate CPU sampled percent, memory bytes, and workspace-filesystem bytes. Every host meter
fills with how much is in use, so a fuller bar means less headroom; disk used is total minus free.

Displayed agent state comes from an explicit identity mapping. Aggregate CI mappings use GitHub
activity: a job that had a runner and was not skipped proves working while it is in progress, and a
complete active scan permits idle. A mapped QAE is one card per lane instance, from the job's runner
name and the host's `qae_concurrency`. When that count is set there is no extra plain QAE. A role's
runs include its workflow in every repository the dashboard covers, matched by the run path's file
name, including a required workflow from another repository of the same owner. Jobs on open pull
requests come from the detail already read. A workflow an organization ruleset requires is not in
the repository's own workflow list, so its past week of runs, including merged and closed pull
requests, is read through the per-repository id GitHub puts on each of its runs; one page of a
repository's newest runs is read once to find that id when no open pull request showed it. Runner listener
health never establishes agent health. Persistent SWE/QAE identities use their own runtime state
and history, including paused. A missing runtime is unknown. A stale one keeps its last state and is marked stale. Numbered identities cannot
be mapped to aggregate CI job roles. Recent history uses structural run outcomes and timestamps.
Unavailable or incomplete history is labeled separately from a complete history with no failures.

Quota windows can omit `allowance_tokens` and `window_minutes` (the window's length, at most 31
days). A plan's pace is in the plan's own unit: 100% over the window's length, which is what the
white tick on the usage bar marks. It needs the window's length and reset and nothing about the
bots. A reported `allowance_tokens` is shown in the bar's hover and draws no line.

The All bots chart is drawn in the same unit, percent of the plan an hour, with a flat line at the
pace that spends the rest exactly at the reset: (100% minus used%) over the hours left
(`usage.plan_line`, `dashboard/pace.py`). The line is exact and needs no tokens. To put the bots on
that scale, each sample whose account has the plan's provider gets `plan_percent`: its tokens
weighted the way the plan counts them (OpenAI's Codex credit rates make a cached input token a tenth
of a fresh one, or less, and an output token five times), times the window's used percent over the
weighted tokens those samples spent since the window began. Raw counts, which are mostly cache
reads, swung too much to size a plan (107m tokens at 49% used, then 227m at 57% four hours later).
The plan's percent is account-wide, so this reads the bots' tokens as standing for all of its use,
which holds while they keep their share; the note's hover says so, and says when token history is
incomplete (the bots' share then reads high). With no billed tokens in the window, or a window
longer than the seven days of kept samples, All bots stays in tokens and the line is still drawn.
The other bot cards stay in tokens.

The All bots header states one plan's fill against an even burn of its window, in points: used
percent minus the share of the window elapsed, as `12% under pace`, `21% ahead of pace` or
`on pace` (within one point). The server works it out once (`usage.pace_points`, `dashboard/pace.py`).
When the plan has several windows (say 5-hour and 7-day), the longest one is paced. The header says
nothing when no single plan reports a window length, or when the window has already reset. No
price is inferred.

Above the charts, each provider is one block (`OpenAI usage`, `Anthropic usage`). The block
does not name the model or plan. Each window is a bar, shorter windows first, labeled from its
length (`5 hours`, `7 days`). The fill is green under half and on pace, yellow from half full or when the fill is more than a point
ahead of an even burn, and red from 90% full or 25 points ahead. A white tick, as tall as the bar, hangs under it and marks the even burn. Its top touches the bar.
The line reads `70% · resets 3d 4h · +16%` (the signed points, omitted within one point of even).
The hover keeps the clock time of the reset and a reported token allowance. There is no
primary/fallback account row.

Every token sample repeats the owner, account, bot, timestamp, input count, and output count. Any
cross-owner row rejects the whole file. Samples older than seven days are discarded. The file is
limited to 2 MiB, 1,024 lanes, 64 accounts, and 20,000 token samples. Sections older than five
minutes are visibly stale. The last lane state and job stay on screen with that stale mark. Invalid, missing, unsafe-permission, or mixed-owner files are unavailable,
not zero.

## GitHub behavior and limits

The server uses bounded `gh api` pages and JSON fields, never formatted table output. It reads
PR check suites and check runs for the current head, joins Actions runs by exact head SHA plus check
suite ID, selects the greatest run attempt for each run ID, and loads jobs from that exact attempt.
When several runs of one workflow exist on the head (each triggering event starts its own run), only
the newest copy of each job and check counts toward check totals and attention.
It re-reads the PR head after collection; a race drops the collected evidence instead of attaching
it to the new revision. Commit statuses and third-party checks remain separate evidence rows.

GitHub lists a check the base branch requires as "Expected" without creating a check run for it.
The dashboard reads the rulesets for each pull request's base branch, and the branch's classic
protection, which that rules read does not return: its required status checks, and whether it
restricts who may push. It adds an expected row
for every required status check that has not reported and every required workflow that has not run
on the head. A required workflow runs at the SHA its ruleset pinned when the run was triggered, so
after the pin moves GitHub waits for a new run. The dashboard reads the ruleset's version history
once per pin to learn when the current pin took effect, and a run created before then does not
count. An expected row keeps the pull request pending, flags it for attention, and takes one waiting
share on the check line. GitHub serves an organization ruleset's history only to a token with
organization administration write, which a read-only dashboard should not hold. When the history is
refused, any run of the required workflow on the head counts, so the pull request keeps its
evidence; right after a pin moves it can show as passed while GitHub waits for a rerun, and GitHub's
merge box still enforces the rule. A required workflow that never ran on the head is still expected.
Classic protection needs Administration read. A token without it, or a branch with no classic
protection, leaves those checks and the push restriction out and still shows the pull request's
other evidence.

Direct manifest or workflow evidence reports `subscribed`. An installation subscribed only through
an organization wrapper or ruleset reports `unknown` in this pilot, not `false`; wrapper/ruleset
resolution is not duplicated here. Bot roles sharing a workflow share collection. Active runs are collected independently of history;
history is inspected until the newest five outcomes are known or coverage is explicitly partial.
Jobs are read for at most a workflow's 50 newest completed runs, so a job name that never runs in
that workflow reports partial history instead of reading every run of the past week.
A refresh is capped at 200 GitHub requests that cost rate limit, counting each REST page and each GraphQL read; an unchanged REST answer (HTTP 304) is free
and not counted. Active data is cached for 60
seconds and direct subscription inventory for 300 seconds. The response includes calls used, the
reported REST limit/remaining/reset values, and the lowest remaining count seen on any response
(`lowest_remaining`; GitHub meters some endpoint families against a separate counter that the reported
values omit). A spent counter pauses only its endpoint family until its reset. GraphQL is metered in points, so GitHub refuses a query that costs more than what is left while the counter still reads above zero; that pauses only the GraphQL reads, and the checks keep loading. Rate limits and source errors return partial or briefly
stale data without advancing its successful sample timestamp. Authentication or access revocation
clears derived repository data immediately, in memory and in the state directory. Any other failure (a rate limit, a timeout, a spent budget, a failure this version does not name) keeps the last pull requests and bot rows, and the dot beside the Pull requests title says some GitHub data is unavailable. A restart with a state directory serves the reading from before it; one over three minutes old is served only after the fetch has waited up to 8 seconds for GitHub. When the page's own request fails it keeps the last snapshot and says the refresh failed. A page opened while the service is down tries again every five seconds while it is visible, one request at a time.

Every fetch is answered from a current reading: the server reads GitHub in the background whether
or not a page is open (see [collection](dashboard-data.md#cache-and-timestamp-contracts)), and a fetch whose
reading is more than three minutes old waits up to 8 seconds for the pass under way. So the page never changes how
it looks to say data is old: no dimmed rows, no yellow ages, no "stale" or "refreshing" flags in
the header, no gray bot states. The header says when the reading on screen was taken
(`GitHub sampled Oct 8, 2026, 3:02 PM`). An old check reading is still never green (the title rules
below).

The dashboard shows source-proven failures, cancellations, waiting jobs, and current elapsed times.
The pull request list groups pull requests by repository. A repository with no open pull request
that matches the filters gets no group. Groups are ordered by their most recently updated pull
request, newest first; within a group the newest pull request is first and the oldest is last.
A row's age is how long that pull request has been open, and it is the only figure in the
right-hand column. Under a day it shows hours and minutes.
From a day through seven days it counts whole days, with no hours. Past seven days it counts
whole weeks, past thirty days whole months, and from a year whole years. The number stays the
default text color through three days, turns yellow after that, and turns red after two weeks.
An age of exactly three days or exactly two weeks keeps the younger color.
The right of the line under the check meter shows how long CI has taken on the head, in those
same units and in the same size and weight as that line. When the counts leave too little room,
it wraps onto a line of its own, still on the right. While any check is still open it is
"CI running" and counts from the first check's start; once every check has finished it is "CI
took" and stops at the last check's finish. Before any check has started it is "CI waiting",
counted from the push. The time turns yellow after an hour and red after three hours; exactly an
hour or exactly three hours keeps the younger color. A head with no checks, and a row whose checks
are still from the previous push, show "pushed <age> ago" instead, which turns yellow after three
hours and red after a day. Its right edge lines up with the meter, not
with the comment mark beside the meter; in a row with a CI timeline the line is in the CI column,
left-aligned under the counts, and the comment mark sits above them. The kind of push follows the time on that same line, and
the tooltip adds how long ago the push was:
feature, bug fix, refactor, performance, docs, test, chore, ci,
build, style, revert, work in progress, or update when the commit message has no such prefix.
A merge of the pull request's base branch is "sync with main" (or "sync with <base>" when the
base is not main or master), whether the message is git's ("Merge branch 'main' into x") or
written by hand ("Merge main (#943 …) into x"). A merge of another branch is "picked up #N" when
that branch is the head of open pull request #N in the same repository, "merged in <branch>"
when it is not, and "merged another branch" when the message names no branch. A merge of the
pull request's own branch (a `git pull` joining two copies of it) is "update". Apart from git's
own "Merge branch '<base>'" wording, a message is read as a merge only on a commit with more than
one parent, so "Merge main navigation into the header" on an ordinary commit stays an update. When one push contains several
commits, a sync of the base is
ignored if the push also contains other work, and the label is the kind of the newest other
commit. The time is the commit's pushed time when GitHub has one, otherwise the commit time.
If that read fails, the line says the last push is unavailable. A long kind truncates instead
of widening the meter.
The title is green when the pull request is fully merge-ready: GitHub's merge state is clean
(not a draft, not behind the base, and not blocked or conflicting), every current-head check
is green (skipped checks may sit beside at least one success, and a check that is not required
still counts), and every review thread is resolved and has a reply. That is only painted from a
check reading taken on this pass. A reading older than one pass (three minutes) is not green;
the row looks the same otherwise. When the head has moved, the row keeps the previous push's
checks and says newer ones are loading. A check state that no longer matches says the same
about the checks already on screen. A pull request this process has never read is the only
row that says checks are not loaded yet, and its badge is the overall state from the cheap
pass. A thread list that did not
load completely, or a merge state GitHub has not calculated, keeps the title white. A stale
row is never green, even when an older reading had said it was merge-ready.
GitHub reports the merge state for whoever asks. A base branch that restricts who may push answers
"blocked" to the dashboard's read-only token for every pull request, including one its allowed
pushers see as clean. On such a branch "blocked" counts as clean when GitHub reports no conflict and
no review is outstanding (no review required, or approved); the checks and review threads above
must still hold, and a required check that has not reported keeps the title white. GitHub answers
"behind" before "blocked" on a branch that must be up to date, so a stale head stays white. Two
requirements cannot be told apart from the token's own block there and are not counted: signed
commits and a locked branch. GitHub's merge box still enforces them.
When the repository's default branch has commits the head does not, the line under the title
ends with a yellow "N behind main" pill (the default branch's own name when it is not main), and
its tooltip says how many commits. It comes from the cheap head read, so it follows a merge to main
within one beat, and a pull request the detail budget has not reached shows it too. It is hidden
when the head is up to date, for a pull request from a fork, for one into a branch other than the
default branch, and when GitHub returned no comparison. The title rule above is separate: a head
behind the base stays white only where the base branch requires it to be up to date.
When the pull request has unresolved review threads, the check line shows a message icon and
that number. The number is a lower bound, with a plus, when more threads exist than the read
returned. The icon is hidden when every thread is resolved, and when the comment read did not
succeed, so a missing read is not shown as zero.
A pull request's badge shows its worst current-head check. A skipped check never outranks a passed
one, so the badge reads Skipped only when every check was skipped.
The check meter counts what GitHub's pull request page counts: one share for each check run,
commit status, and expected required check on the current head. It is one line for the whole
total, taller than the host meters. Each outcome takes a share of that line equal to its count,
with a gap between shares: green for passed, yellow for running, dim yellow for waiting, red for
failed, and gray for skipped, cancelled, or unknown. A check is running when GitHub marks it in
progress. A queued job, a pending commit status, and an expected required check are all waiting.
The Current work column holds that badge and, under it, what the pull request is doing or stuck
on. Each check in progress is one line, the job and its current step, newest first, at most two
lines and then "+N more". A job with no current step is named by the job alone. When nothing is
running, failed checks are named the same way, then required checks that have not run, then a
count of queued checks. The attention reason is the line when none of those apply, and it stays
the column tooltip and the screen-reader text when a more specific line is shown. Each of those
lines stays on one line and truncates, with the full text in its title.
The text under the meter counts every outcome except passed, or says all passed. The CI time
and the latest push's kind are the right-hand side of that same line. A head with no checks at all says so and never
renders as complete, and the push line sits under that notice too.
A job that has not started has no steps yet, but it is still one waiting check, so the meter
shows as soon as anything reports on the head. Elapsed time alone never asserts that a job is stuck.
Once any check run on the head that was not skipped has a time, the row's [CI timeline](#ci-timeline)
takes the meter's place; commit statuses alone do not, since the row does not draw them. The timeline
then has the Checks column to itself, and the counts, CI time and push kind move to the CI column
beside it, the counts on one line and the CI time and push kind under them. A row with the meter keeps
them under the meter, across both columns.

## Data lifecycle and uninstall

- Configuration: create the owned local file, read it once at startup, restart to update its owner,
  repository selection, telemetry path, or proxy origin, and delete it after stopping the process
  to remove the installation.
- Tab icon: the owner's public avatar, read once and held only in memory until the process stops.
- GitHub cache: created from server-side reads, replaced by scoped source identity, kept and
  marked stale on any source failure but lost access, and cleared when access is revoked. Without
  a state directory it is held only in memory and deleted when the process stops. With one, the
  last reading and the caches are also written there after each pass, readable only by the
  account running the server, bounded in size, ignored and deleted once 24 hours old, and deleted
  at once when access is revoked. Delete the directory after stopping the process to remove them.
- Telemetry: created and atomically replaced by an optional collector, read only by this process,
  bounded to a current snapshot plus seven days of samples, and
  unavailable when deleted.
- Work queues: read from each source at most every five minutes while viewed, held only in memory,
  replaced by the next reading, forgotten when the source refuses the key, and gone when the process
  stops. The key file is the operator's: create it, replace it to rotate the key, delete it (and the
  source's entry) to stop reading.
- GitHub records and work items: never created, updated, or deleted by this dashboard. There are no retry, cancel,
  merge, pause, send, save, or publish controls.

## Verification map

`tests/test_dashboard_config_telemetry.py` covers owner isolation, two instances, mixed telemetry,
quota pace, stale sections, and 16-lane on-demand capacity. `tests/test_dashboard_github.py` covers
current-head suite/run/attempt joins, race handling, expected required checks and stale workflow pins,
status/step categories, pagination, rate limits,
and subscription uncertainty. `test_dashboard_bot_history.py` covers bounded history and workflow
discovery; `test_dashboard_service.py` covers source timestamps and refresh caching.
`test_dashboard_state_store.py` covers the files kept across a restart: their mode, the atomic
replace, the size and age limits, and every file that is refused and deleted.
`test_dashboard_restart.py` covers what a restart serves and marks old, which failures keep the
last reading and which delete it, a token refused to the head beat, and kept state this version
cannot use; `test_dashboard_restart_usage.py` covers the same for token history.
`test_dashboard_old_labels.py` covers the age on the header line and the bot strip, the title warning
after a restart, and the page's single retry while it has nothing to show.
`test_dashboard_live_service.py` covers independently aging quota/history and revocation during
an in-flight refresh. The collector and usage-artifact test files cover the native source contracts.
`tests/test_dashboard_host.py` covers the Linux host's token minting, telemetry publishing, and unit
templates, and `test_dashboard_follow.py` the follower's check-run gate and rollback.
`tests/test_dashboard_server.py` covers loopback HTTP, proxy and direct routing headers,
read-only methods, Host/Origin/traversal, XSS-safe JSON, local assets and the tab icon route.
`tests/test_dashboard_server_ui.py` covers DOM construction, filters, account usage
math, the class on a row's age number, the push line under the meter, the current-work names,
and the approved palette, and runs `dashboard/static/capacity.test.js` (the Capacity view's cards,
bars, range buttons and runner rows). `tests/test_dashboard_lane_history.py` covers the lane
history: the queue and job-wait projections, the kept samples and waits, the bars and totals, the
published history's reader, and the collector-to-page path.
`tests/test_dashboard_pull_age.py` covers pull-request age units and color.
`tests/test_dashboard_timeline.py` covers the CI timeline: which checks become lanes, statuses as
points, runner waits (jobs only, over 30 seconds), the critical path, the shared and fitted scales
and the menu that picks them, the opened rows and steps, the bar colors and box, the row's swap
from the check meter, and the row's title link to GitHub and its timeline toggle.
`tests/test_dashboard_behind.py` covers how far a head is behind the default branch and that both
the full pass and the head beat put it on the row.
`tests/test_dashboard_push_kind.py` covers the latest push's label and time, and
`tests/test_dashboard_pull_signals.py` reading it from GitHub, merge-ready titles, and the
unresolved-comment count. `tests/test_dashboard_fresh.py` covers a head or
rollup change leaving the title white and first in the detail queue, check-reading age, the
cheap pass under an exhausted detail budget, the head beat's hourly calls, and the first
paint after a long idle. `tests/test_dashboard_usage_charts.py` covers the usage
charts' clock-hour mapping, observed and unobserved hours, usual-day averages, scales, and that no
card draws a pace in tokens. `tests/test_dashboard_pace.py` covers which plan window is paced and
its points against an even burn, whatever the bots used.
`tests/test_dashboard_usage_quota.py` covers the subscription bars: window labels, pace tick, color,
reset countdown, and one block per provider. The repository's existing unittest command runs all
of them. Configuration tests cover the strict optional proxy origin and its immutable default.

Live acceptance uses private configuration outside this repository, reconciles displayed PRs and
capacity with actual source records, and walks desktop/mobile views with browser console and network
inspection. CI runs the complete suite on Ubuntu. This public repository contains no private
configuration or captured telemetry. See [collection/cache behavior](dashboard-data.md) and the
[numeric usage artifact contract](USAGE-CONTRACT.md).

The server keeps GitHub, usage and Work sources current in the background whether or not a page
is open; the page retains search focus during refresh. The page asks for new data every 30 seconds
while it is visible, and loads once when a hidden tab is shown again. A beat wakes every 15 seconds:
it runs a full GitHub pass once the last one ended a minute ago, and otherwise reads heads when the
last head sample is 150 seconds old. A pull request that opened since the last full pass appears
from a head reading, with checks not loaded yet. If a refresh
request fails, the page keeps the last snapshot and says the refresh failed. Numeric usage artifacts are read
every five minutes through the current GitHub credentials, verified against their run/attempt/head
and configured workflow, and parsed without extracting files or copying model content. Each scan
pages through a repository's artifacts, newest first, until a page reaches past seven days, at most
five pages (500 artifacts) per repository. A listing that stops short of seven days is still complete
when the oldest artifact it read is at or before the start of the paced plan window. An unreadable
artifact, a partial sample, or a budget stop stays partial. A capture that reports no counts is a
gap only when it falls inside that window. Each scan has a
budget of 80 GitHub calls. A scan that spends it, or that a rate limit or a timeout stops, keeps
the records earlier scans read, reports partial coverage, and reads the rest on the next scan; a
scan that cannot start keeps the last token history on the page. Only lost access clears them. Token history
counts as stale after two missed scans (ten minutes), not during an ordinary refresh. Captured token
records expire after seven days and disappear when source artifacts are removed. Missing captures
remain unavailable. Old runs cannot be backfilled. The deterministic QAE verification gate has no
model-token usage and is shown only in PR progress. These counts are used by the token charts and
nothing else: they do not size a plan, and no price is inferred.

## Bot usage charts

The Bot usage view draws one card per configured bot, then an All bots card: one column on a
phone, two from 700 pixels wide, four from 1340. The x-axis is the viewer's local clock hour, with
faint gridlines at 12a, 6a, 12p and 6p and a white marker at now. The solid line is the last 24
hours and runs on to the now marker; the stretch after the marker is yesterday's tail and is faded.
Hours follow the local calendar, so a daylight-saving change does not shift them. The dashed line is a usual
day: each clock hour averaged over the prior six days that were observed, and its hover names the
fewest days any hour averages. Bot cards share one y-scale so they compare at a glance; All bots
adds the bots' observed hours and scales to itself. Each scale tops out at 7/6 of the highest value
it holds, so the smoothed line can round over a peak without running into the top of the card. No card carries a pace line: tokens are what
the bots used, and the plan's pace is not in tokens. Each card's header gives the most tokens it used in one clock hour of the last 24 hours, and
its totals line gives the last 24 hours and, once every hour has an observed prior day, a usual day. There is no period selector: the dashed line already carries the multi-day view,
and a tab saved with the old selector restores without it.

An hour counts as observed from the hour of a bot's first retained sample through the source's last
observation (its `sampled_at`, and the artifact scan time when present) or any later sample. Inside
that span an hour with no sample is a measured zero and is drawn at zero. Before it, and after a
stale source's last observation, nothing is drawn, so one captured run or a stopped collector never
invents zeros. A bot with no sample in the seven retained days, or no usage source at all, shows
"Not yet observed". A partial capture keeps its completeness note under the charts, because hours
drawn as observed can then be missing some runs' tokens.
