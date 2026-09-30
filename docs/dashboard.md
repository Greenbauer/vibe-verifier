# Local CI dashboard pilot

The dashboard is an optional, read-only companion. It shows open pull requests from one configured
GitHub owner, current-head checks and Actions jobs, explicit bot workflow history, and optional local
usage/capacity telemetry. It does not change the existing Vibe Verifier runner or gates.

The static view adapts the compact layout, CSS structure, and pure interaction patterns from the
operator-approved local dashboard prototype dated 2026-09-30. Its synthetic preview records were not
copied into the live dashboard. Live mode has no built-in data.

## Start it

Requirements are Python 3, `gh`, and an existing `gh` login that can read every selected repository.
Keep both local JSON files owned by the account running the server and not group- or world-writable.

```bash
chmod 600 /absolute/path/dashboard.json
bin/vibe-dashboard --config /absolute/path/dashboard.json --port 8765
```

Open the printed `http://127.0.0.1:8765` URL. The address is fixed to IPv4 loopback. There is no
public listener, browser credential, hosted authentication, reverse proxy support, or CORS access.
The server accepts `GET` only, rejects untrusted `Host` and `Origin` values, and serves a fixed path
allowlist with no remote scripts, fonts, or icons.

## Configuration contract

Configuration is read once at startup. Restart to change it. Every repository must belong to the
one configured owner. Bot types come only from these explicit workflow file and exact job names.

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
  "telemetry_file": "/absolute/path/telemetry.json"
}
```

`telemetry_file` is optional and must be absolute. Deleting it makes telemetry unavailable without
affecting GitHub data. Selected repository coverage is labeled as selected coverage, never as the
whole account or organization.

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

Lane `state` is one of `busy`, `ready`, `provisionable`, `offline`, or `unknown`. `registered` is a
separate `true`, `false`, or `null` fact because an allocated on-demand lane can have no idle runner.
A busy lane must include a same-owner job. The host panel is always labeled `SHARED HOST`; its values
are aggregate CPU sampled percent, memory bytes, and usable workspace-filesystem bytes.

Bot `state` is `working`, `idle`, `down`, or `unknown`. GitHub activity can prove `working`. A complete GitHub active scan can report `idle`; a fresh collector can report listener-backed
QAE `idle` or `down`. Missing coverage remains `unknown`. Bot history is
derived from the configured GitHub workflow and exact job names, not free-form text.

Quota windows can omit `allowance_tokens`. A used percentage alone is displayed as a provider
percentage and never converted into a token quota. The dotted pace line appears only when the source
provides an actual comparable token allowance. The charts combine observed token counts and show each bot separately. An allowance is used only
when all plotted samples belong to that same account; account-wide quota percentages remain separate.
No price is inferred.

Every token sample repeats the owner, account, bot, timestamp, input count, and output count. Any
cross-owner row rejects the whole file. Samples older than seven days are discarded. The file is
limited to 2 MiB, 1,024 lanes, 64 accounts, and 20,000 token samples. Sections older than five
minutes are visibly stale, and stale runner assignments/readiness become unknown. Invalid, missing, unsafe-permission, or mixed-owner files are unavailable,
not zero.

## GitHub behavior and limits

The server uses bounded `gh api` pages and JSON fields, never formatted table output. It reads
PR check suites and check runs for the current head, joins Actions runs by exact head SHA plus check
suite ID, selects the greatest run attempt for each run ID, and loads jobs from that exact attempt.
It re-reads the PR head after collection; a race drops the collected evidence instead of attaching
it to the new revision. Commit statuses and third-party checks remain separate evidence rows.

Direct manifest or workflow evidence reports `subscribed`. An installation subscribed only through
an organization wrapper or ruleset reports `unknown` in this pilot, not `false`; wrapper/ruleset
resolution is not duplicated here. Bot roles sharing a workflow share collection. Active runs are collected independently of history;
history is inspected until the newest five outcomes are known or coverage is explicitly partial.
A refresh is capped at 200 actual REST page requests. Active data is cached for 60
seconds and direct subscription inventory for 300 seconds. The response includes calls used and the
reported REST limit/remaining/reset values. Rate limits and source errors return partial or briefly
stale data without advancing its successful sample timestamp. Authentication or access revocation
clears derived repository data immediately; transient stale data expires after three minutes.

The dashboard shows source-proven failures, cancellations, waiting jobs, and current elapsed times.
Elapsed time alone never asserts that a job is stuck. The Actions timeline uses shared wall-clock
coordinates for parallel jobs and does not sum their durations. Unknown step totals never render as
100 percent.

## Data lifecycle and uninstall

- Configuration: create the owned local file, read it once at startup, restart to update it, and
  delete it after stopping the process to remove the installation.
- GitHub cache: created from server-side reads, replaced by scoped source identity, held only in
  memory, expired on source failure, and deleted when the process stops.
- Telemetry: created and atomically replaced by an optional collector, read only by this process,
  bounded to a current snapshot plus seven days of samples, and unavailable when deleted.
- GitHub records: never created, updated, or deleted by this dashboard. There are no retry, cancel,
  merge, pause, send, save, or publish controls.

## Verification map

`tests/test_dashboard_config_telemetry.py` covers owner isolation, two instances, mixed telemetry,
quota pace, stale sections, and 16-lane on-demand capacity. `tests/test_dashboard_github.py` covers
current-head suite/run/attempt joins, race handling, status/step categories, pagination, rate limits,
and subscription uncertainty. `test_dashboard_bot_history.py` covers bounded history and workflow
discovery; `test_dashboard_service.py` covers source timestamps and refresh caching.
`test_dashboard_live_service.py` covers independently aging quota/history and revocation during
an in-flight refresh. The collector and usage-artifact test files cover the native source contracts.
`tests/test_dashboard_server_ui.py` covers loopback HTTP, read-only methods, Host/Origin/traversal,
XSS-safe JSON and DOM construction, filters, account usage math, local assets, and the approved
palette. The repository's existing unittest command runs all of them.

Live acceptance uses private configuration outside this repository, reconciles displayed PRs and
capacity with actual source records, and walks desktop/mobile views with browser console and network
inspection. CI runs the complete suite on Ubuntu. This public repository contains no private
configuration or captured telemetry. See [collection/cache behavior](dashboard-data.md) and the
[numeric usage artifact contract](USAGE-CONTRACT.md).

The server refreshes GitHub and usage sources in the background. Numeric usage artifacts are read
every five minutes through the current GitHub credentials, verified against their run/attempt/head
and configured workflow, and parsed without extracting files or copying model content. Each scan
reads at most the first 100 artifacts per selected repository and reports partial coverage when
more exist. Captured token records expire after seven days and disappear when source artifacts
are removed. Missing captures remain unavailable. Old runs cannot be backfilled. The deterministic
QAE verifier has no model-token usage. No quota is inferred from those token counts.

Usage charts leave unobserved time buckets blank. A measured zero is drawn at zero; one captured run never fills earlier history with invented zeros.
