# Optional local dashboard collector

`dashboard/collector.py` writes read-only telemetry for one configured GitHub owner. It uses only
the Python standard library. The local process supports Python 3 on macOS, and its fixed remote
sampler targets Python 3 on Ubuntu 24.04 x86_64. In local mode the collector runs on that Ubuntu
host itself and runs the same sampler in-process.

This collector is optional. Keep its configuration and output outside this public repository.

## Configuration

Create a private JSON file with mode `0600`. One file represents one immutable owner and one lane:

```json
{
  "version": 1,
  "owner": "example-ci",
  "host": {
    "label": "Shared CI host",
    "ssh_argv": ["ssh", "-T"],
    "destination": "example-ci-host",
    "listener_config_path": "/etc/example-ci/listener.json",
    "lane_name": "example-ci",
    "workspace_path": "/srv/example-ci/work",
    "codex_home": "/var/lib/example-ci/codex"
  }
}
```

The collector rejects unknown configuration fields. `ssh_argv` must invoke `ssh`; `destination`
is separate so subprocess calls stay list based. The remote command is fixed to
`sudo -n python3 -` with a safely encoded JSON argument. The sampler source is sent on standard
input and is not installed remotely. No browser command or general remote command is accepted.

The configured SSH identity needs passwordless permission for the fixed Python sampler. The
sampler reads only the configured lane and its existing Codex authentication file in remote memory.

### Local mode

When the lane runs on the machine that collects, leave out `ssh_argv` and `destination`:

```json
{
  "version": 1,
  "owner": "example-ci",
  "host": {
    "label": "Shared CI host",
    "listener_config_path": "/etc/example-ci/listener.json",
    "lane_name": "example-ci",
    "workspace_path": "/srv/example-ci/work",
    "codex_home": "/var/lib/example-ci/codex"
  }
}
```

The collector then loads `remote_sampler.py` from beside itself and runs it in the same process:
no SSH, no `sudo`, no remote command. It needs the access the sampler has remotely, so run it as
root. The answer is checked exactly as a remote one: an allowlisted error code is kept, any other
becomes `remote_failed`, and no raw text is recorded. A host section with only one of the two SSH
fields is refused. On a Linux dashboard
host, `bin/vibe-dashboard-host telemetry` runs this mode on a timer and publishes the snapshot
without its `samples` ([host runbook](dashboard-host.md#4-telemetry)).

## Run it

One collection:

```sh
python3 dashboard/collector.py \
  --config /var/lib/example-ci-dashboard/config.json \
  --output /var/lib/example-ci-dashboard/latest.json
```

Local refresh loop, stopped cleanly with Control-C:

```sh
python3 dashboard/collector.py \
  --config /var/lib/example-ci-dashboard/config.json \
  --output /var/lib/example-ci-dashboard/latest.json \
  --interval 30
```

Run that command from the repository checkout whose `dashboard/remote_sampler.py` belongs with
the collector. The collector does not install a service. An operator may put the loop under their
normal local process supervisor; on a Linux host, the [host runbook](dashboard-host.md) ships one.

## What is read

Before returning telemetry, the remote sampler reads the configured listener JSON and compares
its `name` and `github_url` owner with the local configuration. Owner comparison is case
insensitive. A mismatch refuses the entire sample. Only these listener fields are projected:

- `name`, `github_url`
- `budget.slots`, the combined CI and QAE slot budget
- `budget.qae_concurrency`

App credentials, installation identifiers, key paths, labels, exclusions, and the raw listener
configuration are never returned.

The host sample contains aggregate CPU from two `/proc/stat` readings, excluding the duplicated
guest counters; `MemTotal` and `MemAvailable`; free and total space for `workspace_path`; the
configured listener unit state; and the configured lane slice's memory and CPU values. The host
label is marked `shared_host` because its CPU, memory, and disk totals may include unrelated work.

Slot reads are limited to `/run/<lane>/{ci,qae}/<number>/job` and the exact matching systemd units.
The sampler never reads `jit`, adjacent directories, logs, prompts, or other lanes. A valid job
line has exactly five tokens:

```text
target kind set-id runner-id runner-name
```

The target owner must match the configured owner. A valid job file produces an `allocated` record
with its repository, set, runner, and file timestamp. This means a slot is allocated, not that its
GitHub job is running. The dashboard can join `runner_id` to a supported GitHub jobs source.

A slot is free only when its job is absent and its unit is `inactive` or `failed`. An active unit
without a job, an unreadable job, or an unreadable unit state is `unknown` and consumes capacity.
CI index 1 and QAE index 1 are separate instances, but both consume the one combined slot budget.
The output includes occupied or unknown records and remaining on-demand capacity. It does not
invent registered runners.

## Codex quota read

At most once every five minutes, the sampler reads `codex_home/auth.json` and makes one HTTPS GET
to `https://chatgpt.com/backend-api/wham/usage`. The fixed route, bearer authorization, optional
`ChatGPT-Account-ID` header, and response mapping follow Codex `rust-v0.159.2`'s
[quota client](https://github.com/openai/codex/blob/rust-v0.159.2/codex-rs/backend-client/src/client/rate_limit_resets.rs),
[window mapping](https://github.com/openai/codex/blob/rust-v0.159.2/codex-rs/backend-client/src/client.rs), and
[authentication headers](https://github.com/openai/codex/blob/rust-v0.159.2/codex-rs/model-provider/src/bearer_auth_provider.rs).
This is an internal, version-coupled endpoint rather than a public API contract. Revalidate the
mapping when upgrading Codex; a changed or unavailable response is never treated as zero usage.

The sampler does not start Codex, refresh tokens, log in, run a model, or write the credential
store. Credentials remain in the sampling process's memory (the remote one, or the collector itself in
local mode) and are never copied to local output. It
refuses redirects, symlink authentication files, authentication files over 64 KiB, and response
bodies over 256 KiB. The HTTP operation has a ten-second socket timeout, inside the collector's
thirty-second remote process deadline; in local mode the host unit's 45-second start timeout bounds
the whole run instead. Missing, malformed, or expired authentication produces
`quota_unavailable`, with no raw error or response text. A concurrent credential reset can make a
sample unavailable but cannot be overwritten by this reader. Reads are safe while QAE is active.

`rate_limit` and `additional_rate_limits` map to their actual primary and secondary windows. Each
available window keeps `duration_minutes`, `used_percent`, and `resets_at`. Null windows are
omitted while other populated windows remain available. If no windows are populated, usage is
unknown. These percentages are account-wide and may include activity outside this lane. The
response does not provide a numeric token allowance, per-bot tokens, or earlier history, so the
collector does not synthesize them. The dashboard passes each window's `duration_minutes` on as
`window_minutes` and names the window from that length (`5 hours`, `7 days`). Each `limit_id` is
its own account under provider `OpenAI`, so a second metered plan is not folded into Codex. That
length is what sizes the window from captured bot tokens for the pace line
(see [dashboard.md](dashboard.md)). Quota unavailability remains an explicit stale status.

## Snapshot schema, version 1

The root object has exactly these fields:

```text
version       integer, currently 1
owner         configured GitHub owner
observed_at   UTC time this local refresh was attempted
hosts         one host section
accounts      one account-wide quota section
samples       bounded numeric host history
```

The host section is:

```text
label, aggregate_scope="shared_host"
observed_at, last_attempt_at, available, stale, error
cpu.busy_percent
memory.total_bytes, memory.available_bytes
workspace.total_bytes, workspace.free_bytes
listener.state, listener.active_state, listener.sub_state, listener.started_at
lane_limits.memory_current_bytes, memory_max_bytes, memory_high_bytes
lane_limits.cpu_usage_nsec, cpu_quota_cores
slots.limit, slots.qae_concurrency, slots.occupied_count
slots.remaining_on_demand, slots.occupied[]
```

Each occupied entry has `kind`, `index`, `state`, and exact unit `active_state` and `sub_state`.
An `allocated` entry also has `target_repository`, `set_id`, `runner_id`, `runner_name`, and
`allocated_at`. `target_repository` is `OWNER/REPO`, or `null` when an organization-scope scale set
assigned the job and its job file names only the owner; the dashboard then takes the repository from
GitHub's in-progress job with the same runner ID. An `unknown` entry has only an allowlisted
`reason` (`job_unreadable`, `active_unit_without_job` or `unit_state_unavailable`), with no job
identity.

The account section is:

```text
label="Configured Codex account", scope="account_wide"
observed_at, last_attempt_at, available, stale, error
rate_limits[].limit_id
rate_limits[].windows[].name, duration_minutes, used_percent, resets_at
```

`observed_at` inside a host or account advances only after that source succeeds.
`last_attempt_at` records polling cadence. A failed refresh preserves the last allowlisted data and
successful observation time, while setting `available=false`, `stale=true`, and a sanitized error.
The root `observed_at` still shows when the snapshot was rewritten.
Before a source has ever succeeded, its unavailable data objects and `observed_at` are null.

Each `samples` entry contains only its UTC observation time and numeric host values: CPU busy
percent, available memory, workspace free bytes, occupied slots, and remaining on-demand slots.
Entries older than seven days are pruned. The file also keeps at most 20,160 samples, so intervals
shorter than 30 seconds retain less than seven days. Failed observations do not add samples.

The output is an atomic replacement with mode `0600`. Remote output and prior snapshots are
allowlist-projected before writing, so extra fields cannot carry raw configuration or secrets into
the local file.

## Data lifecycle and limitations

- Create: the operator creates the private config and starts the collector. The first successful or
  failed attempt creates `latest.json` atomically.
- Read: the local dashboard reads `latest.json`. No HTTP server or browser execution surface is
  included.
- Update: edit the private config and restart the process. Changing the owner requires a separate
  config and output path; an existing output owned by another exact owner is refused.
- Delete: stop the process and remove its private config and local output directory. There is no
  remote install to remove.

This collector does not query GitHub, prove that an allocated job is running, expose another
owner's occupancy, retain raw logs, or reconstruct past usage. Listener state is `down` when
`inactive` or `failed`, and `unknown` when its state cannot be read. If quota is unavailable, capacity based on
quota is also unavailable rather than estimated.
