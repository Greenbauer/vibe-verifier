# Scale-set listener

The listener (`lane-listener`, installed as `/usr/local/lib/<lane>/listener`) starts a lane's
disposable CI slots on demand. It is one static Go binary, run as root by one systemd unit on the
lane's machine, serving one lane from one JSON config. It replaces
always-on slots: GitHub tells it how many jobs each of its runner scale sets has, and it starts a
slot unit per job, so an idle lane runs no containers (beyond an optional warm pool).

It uses GitHub's [`actions/scaleset`](https://github.com/actions/scaleset) client (pinned in
`go.mod` and `go.sum`; public preview) and the standard library, nothing else.

## What it does

- **Scale sets.** For an `org` lane: one set per kind at the organisation, in the configured
  runner group. For a `user` lane: one set per kind per repository of the App installation that
  is private, not archived and not excluded, in the repository's `default` group (id 1). Each set
  is created when absent and updated when its labels differ; GitHub assigns a job to a set when
  every label the job asks for is a label of the set. A user lane lists the installation's
  repositories every `refresh_sec`: a new repository gets its sets, and a repository that is
  archived, excluded, made public or gone has its sets deleted (their sessions closed first). Every
  listing also deletes this lane's sets found on repositories it does not serve, which covers a
  repository that changed while the listener was down and retries a deletion GitHub failed.
  Public repositories are never served: a fork's pull request must not reach a self-hosted runner.
- **Sessions.** Each set gets a message session (owner: the host name) and GitHub's `listener`
  package, which long-polls it (about 50 s), acquires every available job and reports each
  message. A session GitHub refuses (typically a stale one from a listener that died) is retried
  with a doubling wait for up to 5 minutes, each wait logged; after that the process exits 1 so
  systemd restarts it. A listener that stops on an error is reopened after 10 s.
- **Starts.** Every message and every empty long poll, and a 5 s tick, re-plan the whole lane
  (below). A start picks the lowest free instance `n` of the kind, mints a JIT runner on the set
  (`<name_prefix>-<kind>-<n>-<unix time>`, `work_folder`), writes the slot's `jit` and `job`
  files, then runs `systemctl start <name>-<kind>@<n>.service`. If the mint, the files or the
  start fail, the runner is removed and the files deleted, so neither outlives the attempt.
- **Jobs.** JobStarted marks the slot busy and writes `assignment` (the job's repository and
  display name, plus the run id and job id when both can link the job). JobCompleted marks it
  done. The slot unit exits by itself after its one job; the listener never stops a slot that took
  a job, and forgets it once the unit is inactive and its job file is gone. A slot whose unit ends
  without taking a job (a crash, or its `RuntimeMaxSec`) has its runner removed from GitHub.
- **Idle stop.** A slot whose runner never took a job, older than `idle_stop_sec`, is stopped
  when its set has no assigned job and more idle slots than its `min_runners`, oldest first. The
  listener writes the `idle-stop` marker, removes the runner from GitHub, and only then runs
  `systemctl stop`. If GitHub refuses the removal (the runner has just taken a job) the marker is
  deleted and the slot is left alone for another `idle_stop_sec`. A slot whose set is no longer
  served (its repository was dropped, or it was adopted at start for a set not served now) is
  idle-stopped the same way, as if its set had no job.
- **Warm recycle.** A warm slot, idle in a set with a warm pool, is recycled once its unit has been
  active longer than `warm_max_age_sec`: it is stopped the way an idle stop stops a slot (marker,
  runner removed, then `systemctl stop`; a refused removal leaves it), and the next plan starts a
  fresh one. Otherwise a warm slot would live until its unit's `RuntimeMaxSec`, and a job it took
  late in that time would be killed with it. The age is the unit's `ActiveEnterTimestamp` (`systemctl
  show --timestamp=unix`, asked only of an active unit), the moment systemd counts `RuntimeMaxSec`
  from; never the slot's start as the listener recorded it, which a refused idle stop resets. A set
  recycles at most one slot per plan, and only while it holds every slot it wants with none starting
  or stopping and every assigned job on a busy slot, so its pool is renewed one slot at a time (with
  `min_runners` 1 the pool is empty while the fresh slot starts). A slot whose recycle GitHub
  refused (its runner had just taken a job) is not tried again until `idle_stop_sec` after the
  refusal. The slot unit's `RuntimeMaxSec` is `warm_max_age_sec` plus the job budget (the lane
  provisioner writes it so), so a job taken by a slot about to be recycled still gets the whole
  budget.
- **Crash safety.** At start it rebuilds its state from the run dir: a job file whose unit is
  running is adopted, and its `assignment` file stays so the dashboard still knows the job; one
  whose unit is not running is a leftover, whose runner is removed and whose files are deleted. An
  `assignment` left behind after the job file is already gone is removed at that start. It never
  deletes a scale set when it stops. On SIGTERM or SIGINT it
  closes every session, waits up to 20 s for starts and stops already under way, and exits 0,
  leaving running slots alone.
- **Deleting the lane's sets.** `lane-listener -delete-sets <config>` starts no session and no
  slot: it deletes every kind's scale set on every target the config names (the organisation's
  sets in its runner group; for a user lane, the sets on every repository of the installation that
  the owner holds, served or not) and exits 0, or 1 when a deletion failed, each failure logged. A
  set already gone is fine. The lane provisioner's `--remove` runs it after stopping the listener,
  so no scale set outlives its lane.

## Config

The config file's path is the one argument (after `-delete-sets`, for that mode). Unknown keys are
refused. Example (placeholders):

```json
{
  "name": "example-ci",
  "scope": "org",
  "github_url": "https://github.com/example",
  "runner_group": "example-ci",
  "app": {"client_id": "123456", "installation_id": 1234567, "key_file": "/etc/example-ci/app.pem"},
  "kinds": {
    "ci":  {"set_name": "example-ci", "labels": ["self-hosted", "example-ci"]},
    "qae": {"set_name": "example-qae", "labels": ["example-qae"],
            "requires_files": ["/var/lib/example-ci/codex/auth.json", "/var/lib/example-ci/codex-2/auth.json"]},
    "wait": {"set_name": "example-wait", "labels": ["self-hosted", "example-wait"], "slots": 8, "container_memory_bytes": 1073741824}
  },
  "budget": {"slots": 5, "min_runners": 2, "qae_concurrency": 2},
  "admission": {"container_memory_bytes": 4294967296, "reserve_bytes": 2147483648},
  "runner": {"name_prefix": "box-ci", "work_folder": "/home/runner/_work"},
  "idle_stop_sec": 300,
  "warm_max_age_sec": 1200
}
```

A `user` lane sets `"scope": "user"`, `"runner_group": "default"`, `"budget.min_runners": 0`, and
adds `"repos": {"exclude": ["some-repo"], "refresh_sec": 600}`.

| Key | Meaning |
|---|---|
| `name` | The lane name (`[a-z][a-z0-9-]*`). Slot units are `<name>-<kind>@<n>.service`; the run dir is `/run/<name>`. |
| `scope` | `org`: sets at the organisation. `user`: sets per repository of the App installation. |
| `github_url` | `https://github.com/<owner>`: the organisation, or the user whose repositories a user lane serves. |
| `runner_group` | The runner group the sets live in. An org lane names one (looked up by name; `default` is id 1). A user lane must say `default`. |
| `app.client_id` | The GitHub App's client id (the App id works too): the JWT issuer. |
| `app.installation_id` | The App's installation on the owner. |
| `app.key_file` | Absolute path of the App's private key (PEM, PKCS#1 or PKCS#8). Read once at start; never logged. |
| `kinds` | `ci` (required), `qae` and `wait` (optional). |
| `kinds.<kind>.set_name` | The scale set's name, unique within its runner group (per repository for a user lane). |
| `kinds.<kind>.labels` | The set's labels, exactly. A job reaches the set when all its `runs-on` labels are among them. |
| `kinds.<kind>.requires_files` | Optional list of absolute paths, one per instance: instance `n` of the kind starts only while the `n`-th path exists, and the kind uses no instance beyond the list. For `qae` it lists exactly `qae_concurrency` paths, each instance's own Codex login (`<store>/auth.json`), so an instance whose login is not seeded yet never takes a job, and another instance whose login is takes it instead. The `wait` kind lists none. |
| `kinds.wait.slots` | Required for `wait`, refused for every other kind: the wait kind's own budget. At most this many of its slots at once, on instances `1..slots`, and its set's `MaxRunners`; none of them counts against `budget.slots`. Its jobs only wait for another job's result (a required check polling for the preview sync), so a queue of them can never hold the shared slots the awaited job needs. |
| `kinds.wait.container_memory_bytes` | Required for `wait`, refused for every other kind: one wait container's memory limit, used by admission in place of `admission.container_memory_bytes`. |
| `repos.exclude` | User lane only: repository names never served (compared without case). |
| `repos.refresh_sec` | User lane only: how often the installation's repositories are listed. |
| `budget.slots` | The most slots (units holding an instance) at once, across every set of every kind but `wait`; also those sets' `MaxRunners`, and the instance range `1..slots` of each of those kinds without `requires_files`. |
| `budget.min_runners` | The warm pool: idle slots the org lane's `ci` set keeps registered. Must be 0 for a user lane. |
| `budget.qae_concurrency` | The most `qae` slots at once, lane-wide. At least 1 when the `qae` kind is configured, and at most `slots`. |
| `admission.container_memory_bytes` | One slot container's memory limit. |
| `admission.reserve_bytes` | Memory the host keeps beyond the lane's containers. |
| `runner.name_prefix` | JIT runners are named `<name_prefix>-<kind>-<n>-<unix time>`. See the runner-name contract below. |
| `runner.work_folder` | The runner's work folder inside the container. |
| `idle_stop_sec` | How long a slot may sit idle before an idle stop may pick it. |
| `warm_max_age_sec` | Optional, 1200 when absent. How long a warm slot's unit may be active before the slot is recycled. With a warm pool the slot unit's `RuntimeMaxSec` is this plus the lane block's `runtime_max_sec` (the job budget); without one the key has no effect. Keep it below `runtime_max_sec` minus the longest job the lane expects: a job then still fits when a recycle comes up to one more `warm_max_age_sec` late (the listener was down, or a job was waiting on the set). |

## Runner name contract

A JIT runner's name is `<name_prefix>-<kind>-<n>-<unix time>`. `name_prefix` matches
`[A-Za-z0-9][A-Za-z0-9-]*` and may contain hyphens, `kind` is `ci`, `qae`, or `wait`, `n` starts at
1, and the last field is unix seconds. The dashboard reads this shape off the job's `runner_name`
to tell QAE instances apart. A name that does not match is not an instance.

## The run-dir contract

For each slot it starts, the listener writes, under `/run/<name>/<kind>/<n>/` (directories 0700,
files root-owned, each written to a temporary name and renamed into place):

| File | Mode | Content |
|---|---|---|
| `jit` | 0400 | The encoded JIT runner config. The slot's prepare mounts it into the container read-only. |
| `job` | 0600 | One line: `<scope-target> <kind> <set-id> <runner-id> <runner-name>`; the scope target is the organisation or `owner/repo`. Written after `jit`. Its five fields are the contract `lane-slot.sh` and the dashboard sampler parse; nothing else is added to the line. |
| `assignment` | 0600 | Written when the job starts, not when the slot starts. One JSON object: `repository` (`owner/repo`), `name` (the job display name), and `run_id` plus `job_id` when both can form a GitHub job link. Absent on a listener that predates it. |
| `idle-stop` | 0600 | Empty; written before an idle stop or a warm recycle removes the runner. |

An instance is **free** when it has no `job` file, its unit is inactive (`systemctl is-active`
says `inactive` or `failed`), and no start or idle stop of the listener's is in flight on it (so a
stop can never land on the next slot's unit). The slot unit's side of the contract, which the lane
provisioner wires:

- `Restart=no`: one job per start; the listener starts the next.
- prepare reads `jit` and `job` and never deletes them (no longer mints its own runner). It does
  not read `assignment`.
- cleanup counts an `idle-stop` marker like an idle timeout (no failure, no back-off), removes
  `jit` and `idle-stop`, and removes `job` **last**, since its absence frees the instance. It does
  not remove `assignment` (its `rmdir` of the slot directory then fails and is ignored). The
  listener removes `assignment` with the slot's other files, and again once the unit is inactive
  and the job file is already gone, so a finished slot leaves the directory empty and removed.

A unit running, or a job file present, on an instance the listener did not start counts against
the budget and is logged once. A job file left behind by a unit that is no longer running is
cleared by the listener (runner removed, files deleted).

## Budget and admission

Per set, **desired = min(slots, min_runners + TotalAssignedJobs)**, where `slots` is
`kinds.wait.slots` for the wait kind and `budget.slots` for every other, and `min_runners` is the
configured warm pool for the org lane's `ci` set and 0 for every other set. Its demand is desired
minus its active slots (starting, idle or busy). Demand for assigned jobs, min(slots,
TotalAssignedJobs) minus the active slots, is met across all sets before any warm-pool demand (the
rest), and within each the next start goes to the set with the fewest active slots, ties by set key
(`<target> <kind>`). So a warm pool never takes a freed slot from a set with an assigned job, and
the set whose key sorts first does not take every freed slot from the others. A set refused by a
rule gets no more starts that round. Every start must keep all of:

1. a free instance of the kind whose `requires_files` entry is present (a kind without the list
   requires nothing);
2. all occupied instances of every kind but `wait` within `budget.slots`, and a wait start's
   within `kinds.wait.slots` (a completed slot holds its instance until its unit is gone);
3. `qae` slots within `qae_concurrency`;
4. a free instance of the kind in its range (`1..len(requires_files)`, else `1..slots`, else
   `1..kinds.wait.slots` for `wait`); the start takes the lowest one rule 1 allows, so a `qae` job
   goes to an instance whose own Codex login is seeded, never to one whose login is not;
5. `MemAvailable` (from `/proc/meminfo`), less one container of its kind for each slot still
   starting (its memory is not in use yet) and each start already planned this round, at least
   one container of the kind (`kinds.wait.container_memory_bytes` for a wait start,
   `admission.container_memory_bytes` otherwise) plus `reserve_bytes`. An unreadable meminfo
   refuses every start.

Demand a round cannot meet is logged with the rule that refused it (and `MemAvailable` for the
admission rule), at most once a minute per set and rule, and met by a later round.

## Logging

Plain `<UTC stamp> <message>` lines on stdout: each start, failed start and removal, idle stop,
warm recycle, job started and completed, finished slot, set created, updated or deleted, session
opened, refused or closed, refusal, one line per message with the set's statistics, and a heartbeat every
minute (`heartbeat: <n> scale sets served, <n> of <slots> slots in use`, then `, <n> of <n> wait
slots` when the wait kind is configured), because an idle lane logs
nothing else for hours and the lane provisioner's `--check` fails a journal silent for three
minutes. The App key, tokens and JIT configs never reach a log line.

The lane's `<name>-listener-health.timer` reads the same journal every 5 minutes
(`bin/listener-health.sh`): it restarts a listener that is not active or has been
silent for three minutes, at most once per 30 minutes, and reads a GitHub 401 or 403 logged in the
last 10 minutes as a credentials problem it does not restart for. Its verdict, `ok <utc>` or
`unhealthy <utc> <reason>`, is the state file `/run/<name>-listener-health` (0644, beside the run
dir, which only root reads), and `bin/provision-lane.sh --check` prints it and fails on an
unhealthy one.

## Build and test

```bash
listener/build.sh        # out/lane-listener; prints binary=<path> and sha256=<hex>
listener/build.sh test   # gofmt -l, go vet ./..., go test ./...
```

Both run in the pinned `golang:1.26.3-bookworm` image (by digest, in `build.sh` only) with
`GOTOOLCHAIN=local` and `CGO_ENABLED=0`, so the host needs Docker and nothing else, and needs
network access to the Go module proxy. The `lanes-listener` job in the catalog's
`.github/workflows/ci.yml` runs `build.sh test`. With a local Go toolchain of 1.26.3 or newer (or
`GOTOOLCHAIN=auto`), `go test ./...` and `go vet ./...` in this directory run the same checks.

The tests cover the plan's rules (`plan_test.go`), the run dir (`state_test.go`), the App token and
repository listing against a test server (`github_test.go`), set creation, update, deletion,
session retries, the user scope's repository loop and `-delete-sets` against a fake client (`sets_test.go`), the lane's starts, failed starts, idle
stops, warm recycles, adoption and a session driven through the real `listener` package
(`scaler_test.go`), and the `systemctl` adapter (`is-active`, and `show` for the units' start times) against a stand-in
binary (`systemd_test.go`).
