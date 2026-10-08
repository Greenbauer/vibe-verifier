# Runner lanes

Self-hosted GitHub Actions runner lanes for a machine you own: one disposable Sysbox system
container per CI job. A lane is a small Go program, the scale-set listener (`listener/`), that
keeps a GitHub runner scale set per kind of job and starts one systemd slot unit per job GitHub
assigns it; the unit runs one container from the lane's runner image (`image/`), the container
takes that one job and exits, and nothing it did survives into the next job. The scripts in `bin/`
provision a machine and its lanes, check them, and remove them. A machine may also serve Vibe
Verifier CI dashboards fed by its lanes (`bin/provision-dashboards.sh`, "Dashboards" below).

This directory is the engine: it holds no machine, organization, App or repository of anyone's.
Those values live in a host file, `hosts/<host>.yml`, in a private repository of the machine's
owner. A machine therefore has two checkouts, and keeps both on their `main`
(`bin/runners-pull.sh`, "Deploy model" below):

| Checkout | Default path | What it is | Fetched |
|---|---|---|---|
| the engine | `/opt/runner-lanes` | a clone of this catalog; the kit is its `lanes/` directory | anonymously, over https |
| the values | `/opt/runner-lanes-config` | the owner's private repository, with `hosts/<host>.yml` at its root | with the machine's read-only deploy key, `/etc/runners/deploy_key` |

Paths in this manual and in the scripts' comments are relative to this directory (`lanes/`) unless
they start with `/`.

## Layout

| Path | What it is |
|---|---|
| `examples/hosts/example.yml` | an example host file, for a machine that does not exist: a user lane and an org lane (with QAE and the wait kind), a dashboard each ("Configuration" below) |
| `lib/lanes.py` | the command line the bash scripts read a host file through |
| `lib/lane_load.py`, `lib/lane_model.py`, `lib/lane_render.py` | what it is made of: parsing and validating a host file, the lanes and dashboards as values, and the files and settings rendered from them |
| `lib/provision.sh` | what the provisioners share (logging, dry-run, file convergence, the Sysbox pin) |
| `bin/provision-host.sh` | converges what a machine's lanes share, once per machine |
| `bin/provision-lane.sh` | converges one lane |
| `bin/lane-slot.sh` | a slot unit's per-job prepare and cleanup, the lane's store reaper (`reap`), and a QAE lane's Codex login keepalive |
| `bin/lane-firewall.sh` | asserts a lane's two firewall rules |
| `bin/lane-smoke.sh` | the in-container proofs `provision-lane.sh --check` runs |
| `bin/lane-token-refresh.py` | writes a lane user's GitHub App installation token from the lane's key |
| `bin/build-runner-image.sh` | builds a lane's runner image and its preloaded inner Docker store |
| `bin/listener-health.sh` | restarts a lane's listener once when it is down or silent |
| `bin/provision-dashboards.sh` | installs and converges a machine's Vibe Verifier dashboards ("Dashboards" below) |
| `bin/runners-pull.sh` | the machine's self-update: fast-forward both checkouts to `origin/main`, then converge |
| `listener/` | the scale-set listener (Go); its contract is `listener/README.md` |
| `image/` | the runner image every lane builds under its own name; `image/README.md` |
| `tests/` | hermetic tests; `tests/run-all.sh` runs them. `tests/fixtures/hosts/vps-1.yml` is a second made-up host file the tests read |

## How a lane works

**One container per job.** `<name>-listener.service` runs the listener on `/etc/<name>/listener.json`.
It long-polls the lane's scale sets, and for each job GitHub assigns it mints a just-in-time runner,
writes `/run/<name>/<kind>/<n>/jit` (0400) and `job`, and starts `<name>-<kind>@<n>.service`. When
that job starts it also writes `assignment` in the same directory (the job's repository and name).
The slot's cleanup removes `jit` and `job`; the listener removes `assignment` once the unit is
inactive. The slot unit's `ExecStartPre=+bin/lane-slot.sh prepare <kind> <n>` refuses without those files,
asserts the lane's firewall rules, re-creates the slot's sparse ext4 image and mounts it at
`/var/lib/<name>/slot-<kind>-<n>`, and makes a reflink snapshot of the preloaded inner Docker store
(a btrfs snapshot where the lane's store is btrfs, "A btrfs store" below; for a wait slot an empty
store, "Waiting jobs" below).
`ExecStart` runs one `docker run --rm --runtime=sysbox-runc` with the lane's limits, its own 4-core
`--cpuset-cpus` (so `nproc` is 4, like a hosted runner), the slot on `/home/runner`, the snapshot on
`/var/lib/docker` and the JIT file read-only at `/run/jit`. `ExecStopPost=+bin/lane-slot.sh cleanup`
removes the container, deregisters a runner that never ran its job, deletes the slot image, renames
the job's store into the store's trash for the lane's reaper ("The store trash" below), and removes
the job file last, which frees the instance; `TimeoutStopSec=300` is its budget (a store the trash
cannot take is still deleted there, which took over 60 s on a loaded machine, and a killed cleanup
left its `rm` running beside the next job's snapshot). The templates have
`Restart=no` and no `[Install]`: only the listener starts a slot. `RuntimeMaxSec` is
`runtime_max_sec`, plus `warm_max_age_sec` on a lane with a warm pool, so a warm slot that takes a
job late still gives it the whole budget. The listener's budget, memory admission, idle stop and
warm recycle are in `listener/README.md`.

**Kinds.** Every lane has a `ci` kind; a lane with `labels.qae` also has a `qae` kind, with its own
scale set, slot template, slot images and snapshots. Each QAE instance has its own Codex login
store, mounted at `/home/runner/.codex-qae`: instance 1's is `codex_store`, instance n's the sibling
`<codex_store>-<n>`, one per instance up to `qae_concurrency`. Codex rotates a login's refresh token
as a job uses it, so two concurrent jobs on one store retire each other's session (OpenAI: one
`auth.json` per runner or per serialized job stream). The qae template's mount resolves per
instance (`${KNOWN_CI_CODEX_STORE_%i}`), and `prepare` refuses an instance without a store of its
own and a store that is missing or a link. Before every QAE job `prepare` deletes everything in the
instance's store but `auth.json`, writes the tracked `/etc/<name>/codex-config.toml` and hands the
store to the job's uid 1001; `cleanup` deletes everything but `auth.json` again (a job's session
rollouts and Codex databases hold what its model read) and gives the store back to the lane user.
The listener starts QAE instance n only while store n's `auth.json` exists, so an instance whose
store is not logged in takes no job while another instance can, and at most `qae_concurrency` at
once.

**Codex logins between jobs.** Codex refreshes a ChatGPT login only when the login's access token is
within 5 minutes of expiring (Codex 0.160.1; the 8-day `last_refresh` rule in OpenAI's CI/CD auth
guide is only its fallback for a token whose expiry it cannot read), and the access tokens live 10
days from `last_refresh`. A store whose QAE jobs are rare would wait for a job to do that, so
`<name>-codex-keepalive.timer` runs `lane-slot.sh codex-keepalive` daily (up to an hour late at
random, and at boot for a missed day). For each store with a login it reads `last_refresh` from
`auth.json`, nothing else, and once that is 10 days old resets the store for the lane user, runs one
`codex exec --sandbox read-only "Reply with the single word OK."` as the lane user with that store
as `CODEX_HOME` (stdin closed, an empty working directory, 90 s), prunes the store back to
`auth.json` as `cleanup` does, and checks that `last_refresh` moved. A failed run, or one that left
`last_refresh` where it was, makes the service fail, which `--check` reports. Each store has a lock,
`/run/<name>/qae/<n>.codex.lock`, outside the store: `prepare` holds it while it resets the store,
the job's container for its whole run (the qae template's `ExecStart` is `flock -F` on it), and
`cleanup` while it gives the store back. The keepalive never waits for it and skips a store whose
lock is held or whose instance has a job file (that job's Codex refreshes the login). A slot waits
at most 120 s for the lock, longer than the keepalive can hold it, then `prepare` fails the job
(counted for the back-off) and `cleanup` leaves the store for the next `prepare`. Nothing waits for
a lock while holding another, so no wait can deadlock.

**Waiting jobs.** An org lane with a `wait` block (`{label, slots, memory}`) also has a `wait` kind,
for jobs that only poll for another job's result (a required check waiting for a preview sync that
runs on the same lane). Those jobs take `runs-on: [self-hosted, <wait.label>]` and never a `ci`
slot: the kind has its own scale set, slot template `<name>-wait@.service`, `wait.slots` instances
that never count against `slots`, and a `docker --memory` of `wait.memory` in place of
`container.memory`. Without it a queue of pollers could hold every `ci` slot while the job they
waited for queued behind them. The template is the `ci` one in every other respect (image,
network, firewall, slice, 4-core set, `RuntimeMaxSec`), and there is no warm pool. `prepare` differs
in one step: a wait slot gets no copy of the preloaded store. A job that polls an API and sleeps
runs no inner image, the slots turn over constantly, and the copy and its deletion were the
costliest steps of a slot's reset ("The store trash" below). Its `/var/lib/docker` is an empty
directory at the path the template has always mounted, `/var/lib/<name>/store/slot-wait-<n>`, made
by `prepare` and retired by `cleanup`, so the container's mounts and Sysbox's nothing-to-copy start
are as they were; the inner dockerd starts on an empty data root, as it does in the image build's
preload, and a wait job that did run an inner container would pull its image like a job on any
runner without a preload. Its containers share the lane's slice cap, so `slice.memory_max` must
cover them too: mostly page cache, which the cap reclaims first. `--check` counts them in the disk
worst case.

**The store trash.** Deleting a job's store copy was the slowest step of a slot's reset, and every
slot did it in its own stop path. Measured on a live lane, 2026-10-07 (8 `ci`, 8 wait and 2 `qae`
slots, about 120 jobs an hour): a copy holds about 225,000 inodes, and copying or deleting one
alone takes 8 and 9 s, but with many running at once a copy took 48 to 63 s and a delete 21 to 121
s, each delete holding its instance until it finished, so more slots finished no more jobs. So a
slot does not delete its copy while the trash can take it. `cleanup`, and `prepare` when it finds a
leftover, rename it to `/var/lib/<name>/store/trash/<epoch second>.slot-<kind>-<n>.<pid>`: one
rename inside the store filesystem, atomic and immediate, under a name no other entry has and that
carries the entry's age. The trash is bounded, because a delete put off is a debt and nothing else
ties the lane's pace to the disk's. Measured on a live lane, 2026-10-07, with no bound: three
deletes at a time took 77 to 133 s each, 1.5 to 2.3 a minute, while the lane retired 2.1 to 2.8
copies a minute; the trash went from 39 to 70 entries in an hour and the store reached 90% of its
inodes (XFS caps a 40 GB store at 20.9 million, and a copy holds about 225,000), where every
`prepare` was deleting for itself and four slots of five waited in it. So the trash takes a `ci` or
`qae` copy only while it holds fewer than `TRASH_MAX` of them (the unit's slot count, the most one
burst retires, or 8; `KNOWN_CI_TRASH_MAX` overrides) and the store is above its 10% floor ("Disk"
below). Past that a slot deletes its copy in its own stop path, as every slot did before there was
a trash: slower, but a slot that is deleting takes no new job, so the lane cannot retire copies
faster than the disk deletes them. The trash absorbs a burst, and sustained load falls back to that.
A wait slot's empty store costs nothing to delete: it is not counted and always goes to the trash.
`<name>-store-reaper.service` (`lane-slot.sh reap`, a root oneshot) deletes the entries oldest
first, holding the trash's lock (an `flock` on the directory itself), until the trash is empty; a
second reaper finds the lock held and exits. How many it deletes at once follows the store: one
while the store has at least half of its space and of its inodes free, up to three while it has
less, read again before every batch (`STORE_AMPLE_FREE_PCT` and `REAP_JOBS` in `lane-slot.sh`).
Deletes and copies share one disk, which did the same 12,000 to 14,000 small writes a second
whatever the split, so the count trades a job's start against the store's room. One at a time, ci
`prepare` took a median of 22 s (longest 36), but at 2.3 copies retired a minute the trash grew
from 16 to 39 entries and the store's inodes from 35% to 67% used in 45 minutes. Three at a time,
inodes fell from 64% to 51% used in 12 minutes, but a delete took 71 s on average and ci `prepare`
a median of 46 s (longest 79). Eight at once is what every slot deleting in its own stop path came
to. A reaper on a busy lane never finds the trash empty, so after 5 minutes of work it replaces
itself, between batches, with the helper then on disk: a helper the machine pulled reaches a
reaper that was already running. An apply that changes the reaper's unit restarts a running reaper
for the same reason; the delete that cuts short is safe, since what is left of the entry stays in
the trash and is deleted by the next run. It runs at `Nice=19` and outside the lane's
slice, so the lane's `CPUQuota` and `MemoryHigh` never stall the one thing that gives the store its
space back. Every `cleanup` starts it with `systemctl start --no-block`, which does not wait for the
unit, and `<name>-store-reaper.timer` starts it a minute after boot and every 5 minutes, so no entry
is left by a start that was missed or by a reboot. It deletes entries of that one directory and
nothing else: it takes no path, refuses a trash that is a link, `rm` follows no link inside an
entry, and it stays on the store's filesystem; a `golden-*` store and a live `slot-*` copy are
never in the trash. The helper makes the trash when it is missing, deletes in place a copy the
trash cannot take (as every copy was before), and treats a reaper it cannot start as a log line, so
a slot that finishes between a machine's pull of this kit and its apply is cleaned up all the same.
Three deletions stay synchronous, being rare: the smoke's and the image build's `snapshot` and
`discard` (`--check` must leave nothing behind), the image build's pruning of old `golden-*`
stores, and `--remove`.

**Scopes.** An `org` lane keeps its sets in the organisation runner group `runner_group`, with a
warm pool of `min_runners` idle registered `ci` slots. Its `ci` set is labelled `self-hosted` and
its label (so `runs-on: [self-hosted, <label>]` matches); its `qae` set carries its label alone, so
only a job naming the `qae` label lands on a Codex login store and a bare `self-hosted` job
never does. A `user` lane keeps one set per kind per private,
non-archived repository of its App installation, minus `exclude`, in each repository's `default`
group, labelled with its label alone (so `runs-on: <label>` matches and a bare `self-hosted` job
does not); public repositories are never served. Everything else is the same for both scopes.

**Identity.** Every lane runs as its own system user, named for the lane, with home
`/var/lib/<name>/home`; the state directory `/var/lib/<name>` is `root:<name>` 0710. The lane's App
key is a root-only file (`app.key_file`, 0600) the operator pipes to the machine; the listener reads
it once at start, and `<name>-token-refresh.timer` (every 10 minutes) writes the lane user's gh
`hosts.yml` from it. The slot cleanup deregisters with that token, the image build reads the
preload configs as the lane user with it, and every `gh` call of the provisioner runs as the lane
user.

**Back-off.** `cleanup` counts consecutive runs of an instance that did not end normally, and
`prepare` sleeps 10 s × 2^(n−1), capped at 10 minutes, before that instance's next job. An idle
slot's `RuntimeMaxSec` expiry and the listener's idle stop are not failures.

**Aggregate limits.** `<name>.slice` holds every container (`--cgroup-parent`) and slot unit of the
lane: `MemoryMax`, `MemoryHigh`, `CPUQuota`, `CPUWeight`. systemd nests slices on dashes, so the
provisioner also gives the parent (`greenbauer.slice` for `greenbauer-ci.slice`) the lane's
`CPUWeight`. Every container runs with `--oom-score-adj 1000`. Lanes on one machine share it
through each listener's best-effort memory admission: a lane starts a job only while
`MemAvailable` covers one container's memory plus 2 GiB. Reservations for starts in flight are
local to that listener; two lanes can observe the same free memory and both admit work. The
slice caps are per lane, not a host-wide reservation budget. Before running two lanes on one
machine at caps that together exceed its memory, exercise simultaneous real CI and QAE jobs and
verify host memory pressure and job completion; passing the isolated listener tests does not prove
that combined workload.

**Network.** The Docker network `<name>` uses the bridge `<name>0` with inter-container traffic
off. `bin/lane-firewall.sh` rejects, from that bridge, forwarded traffic to 10.0.0.0/8,
172.16.0.0/12, 192.168.0.0/16, 100.64.0.0/10 (the tailnet) and 169.254.0.0/16 (cloud metadata) in
`DOCKER-USER`, and every packet to the host itself in `INPUT`. `prepare` asserts both before every
job, which also restores them after a reboot or a Docker restart, and fails closed without them.
Each lane's assertion replaces only its own bridge's rules.

**Disk.** A job writes only its slot image, its `/tmp` tmpfs and its store snapshot. The lane's
store filesystem is a sparse XFS image with reflinks (`/var/lib/<name>/store.img`, mounted at
`/var/lib/<name>/store` with `discard`, `store_disk_gb`): the image build writes the preloaded store
there once per tag (`golden-<tag>`), and `prepare` gives each `ci` and `qae` job a
`cp -a --reflink=always` copy, which costs inodes, not a second copy of the data (a wait job gets an
empty directory). A finished job's copy waits in `trash/` there until the reaper deletes it ("The
store trash" above): a bounded backlog, a slot count of copies at most. Should the store run short
all the same, one more rule keeps it from filling unseen. Before a `ci` or `qae` copy,
when the store filesystem has less than 10% of its space or of its inodes free and the trash holds
entries, `prepare` deletes the oldest ones itself until there is room, waiting at most 120 s for a
reaper that holds the lock; still short with entries left, it refuses the job with a log line that
says why, which counts toward the back-off. A short store with an empty trash is not the backlog's
doing: the copy is tried as before, and fails closed on a full filesystem. `--check` prints
`store_trash=<dir> entries=<n> oldest_age_s=<seconds>` and fails on an entry that has waited more
than 30 minutes (six of the timer's periods, and some fifteen of the slowest deletes measured: the
reaper is failing, wedged or behind), on a reaper whose last run could not delete an entry, and
while the reaper's timer is not active. With the store mounted on `/var/lib/docker`,
Sysbox copies nothing at container start (the container's first line about 1 s after `docker run`,
against 56 to 66 s when Sysbox copied a 5.6 GB store, 2026-09-26). Sysbox's own per-container data
stays under `/var/lib/sysbox`, the machine's one sparse ext4 image (`sysbox.image`,
`var-lib-sysbox.mount` with `discard`, required by `sysbox-mgr`). `--check` prints each lane's
worst case (store plus `slots` plus `wait.slots`, times `slot_disk_gb`) and the Sysbox image against the free space:
the room under the sparse images, read with `df` on the directory that holds them, whatever
filesystem is inside them.
On the host's own Docker a lane leaves only its runner image: the image build keeps the newest three
dated tags and, after every run, drops the Docker build-cache records unused for a week
(`image/README.md`), so a lane needs no other job on the machine to prune behind it.

**A btrfs store.** With `store_fs: btrfs` the store filesystem is btrfs, and a job's store is a
snapshot, not a copied tree. Every job on an XFS store copies and later deletes a tree of about
225,000 inodes, and the disk is one budget whatever the slot count, which caps a lane near 2.3 job
starts a minute. On btrfs the image build makes each `golden-<tag>` a subvolume, `prepare` takes
`btrfs subvolume snapshot` of it, and `cleanup` (and `prepare`, for a leftover) deletes it with
`btrfs subvolume delete`, which returns at once and leaves the freeing to the kernel. A wait slot
gets an empty subvolume. So there is no copy to pace: no trash, no room rule, and nothing for the
reaper, whose unit stays installed, so that both kinds of store have one provisioner, and finds an
empty trash. A snapshot that cannot be made fails the job closed. Measured by hand on a lane's
machine, 2026-10-08, in a throwaway 14 GB btrfs image, not with this code (Sysbox 0.7.1, kernel
6.8): a snapshot took 9 to 26 ms, against 22 to 50 s for the reflink copy; a job container on a
snapshot had its inner dockerd ready in 2 s, on `overlay2` over btrfs, with all nine preloaded
images there; the kit's own smoke started six preloaded Supabase stacks in 40 to 72 s each with no
image pulled (51 to 117 s that evening on a reflink copy); a delete returned in 3 to 138 ms and the
kernel had freed five snapshots 24 s later, against 30 to 130 s for each tree delete under load.
The slot helper follows the store it finds (`stat -f` on the store), never the key: an XFS store
behaves exactly as described above whatever the host file says, until the store itself is converted
("Converting a lane's store" below). It deletes only the path it derived for the slot (or the
smoke's and the build's `<word>-<digits>` name, never a `golden-*` one), and removes a link there
without following it.

The provisioner makes a new lane's store as the key says: a sparse image formatted with
`mkfs.btrfs`'s own defaults (no option of it has been measured on a lane, so none is set), mounted with
`loop,noatime,discard=async`. `noatime`, because under the default (`relatime`) the first read of
a file in a fresh snapshot updates its access time and so copies its metadata, which btrfs(5)
names as `relatime`'s worst case (many files, older than a day, read just after a snapshot), and
that is how every job starts. `discard=async`, so that what a deleted snapshot held goes back to
the sparse image as on XFS: btrfs(5) calls it the preferred mode, gathering freed extents into
larger chunks before the TRIM, where the synchronous mode (a plain `discard`) can degrade
performance. A store that already exists is mounted as what
it is and never converted by `--apply`: when it differs from the key, `--apply` changes nothing,
prints an OPERATOR ACTION gate, and `--check` fails with a `store_fs_type=` line until they agree.
`--check` names the store's type in its `store_fs=mounted (<device> <type> <size>)` line and in the
smoke's snapshot line, and prints the trash, reaper and disk lines for either kind. Nothing in the
kit reads the room inside a btrfs store: its `df` is an estimate, and what a deleted snapshot held
comes back only once the kernel has cleaned up, after the delete has returned. `--remove` deletes a
slot's leftover subvolume with `btrfs subvolume delete`. No machine has run this code on a btrfs
store yet: the measurements are the manual experiment's, and the tests stub `btrfs`, `mkfs.btrfs`,
`mount` and `stat -f`.

**The runner image contract.** `/var/lib/<name>/image.env` holds `KNOWN_CI_IMAGE_TAG=<tag>` for
`<image>:<tag>`, and its preloaded store is `/var/lib/<name>/store/golden-<tag>`; the units read
the file at every start, so a rebuilt image reaches the next job without touching a unit. The
image's side (the entrypoint, the JIT hand-off, exit codes, the preload) is `image/README.md`.

**Host prerequisites.** jq, python3-yaml, python3-jwt and python3-cryptography (the scripts read
JSON and the host file, and the token refresh signs its App JWT), and btrfs-progs, which every
host gets so that a lane's host file can say `store_fs: btrfs` (a lane that does refuses a host
without it). Docker CE, installed only where it is absent, at the versions
`image/Dockerfile` pins, from Docker's apt repository with its key checked against the pinned
sha256. Sysbox CE 0.7.1 from its release `.deb`, sha256-pinned, installed through Sysbox's
no-Docker-restart path: `daemon.json` first gets `bip` (docker0's live address) and Docker 29's
built-in `default-address-pools`, validated with `dockerd --validate`, and the install stops loudly
if Docker's start time moved. Then `apt-mark hold` on `sysbox-ce`, `docker-ce`, `docker-ce-cli`,
`containerd.io` and the installed kernel meta packages: upgrade them by hand, then require a
passing `--check`. A Sysbox upgrade is uninstall-then-install with every Sysbox container
stopped. The listener is built with `listener/build.sh` in its pinned Go image, only when its
source hash changes.

## Configuration: `hosts/<host>.yml`

One file per machine, at `hosts/<host>.yml` in the root of the owner's private values repository;
`<host>` is the name the machine holds in `/etc/runners-host`. `examples/hosts/example.yml` is a
complete one, for a machine that does not exist. Keep `sysbox.image` outside `/var/lib/runners`:
that directory holds the pull's record, and `provision-host.sh <host> --remove --apply` deletes it.

```yaml
hostname: worker-1                     # the ssh target the gates print
sysbox: {image: /abs/path/sysbox.img, disk_gb: 20}   # one per machine, mounted at /var/lib/sysbox
lanes:
  - name: ...                          # its user, units, network, state, run, etc and lib directories
    scope: org | user
    owner: <org or user login>
    runner_group: <name>               # org only, required
    repos: [..]                        # org only, required: the runner group's expected repositories
    exclude: [..]                      # user only, required, may be empty
    labels: {ci: <label>, qae: <label>}   # qae optional
    wait: {label: <label>, slots: <int>, memory: <docker size>}   # org only, optional: jobs that only wait
    app: {id: <int>, installation_id: <int>, key_file: /abs, login: <App bot login>}
    codex_store: /abs                  # with labels.qae: QAE instance 1's Codex store; instance n's is <codex_store>-<n>
    slots: <int>
    min_runners: <0..slots>            # org only, required: the warm pool
    qae_concurrency: <1..slots>        # with labels.qae: QAE jobs at once, each on its own Codex store and login
    name_prefix: <runner registration prefix>
    image: <image name>
    preload: {<repo>: <supabase dir> | "-"}   # may be {}
    slice: {memory_max, memory_high, cpu_quota, cpu_weight}
    container: {memory, pids, tmp_size}
    runtime_max_sec: <int>
    warm_max_age_sec: <int>            # optional, 1200 when absent
    slot_disk_gb: <int>
    store_disk_gb: <int>
    store_fs: xfs | btrfs              # optional, xfs when absent: the store's filesystem ("Disk")
    check_ports: [..]                  # the host ports the smoke proves a slot cannot reach
dashboards:                            # optional: the Vibe Verifier dashboards ("Dashboards" below)
  - name: <name>                       # the instance: account vibe-dashboard-<name>, /etc/vibe-dashboard/<name>
    lane: <lane>                       # one of this machine's lanes: its App mints the read token, its listener feeds the telemetry
    port: <int>                        # the loopback port
    bridge_account: <user>             # optional: the account whose forward carries the HTTPS route to the port
    config: {version: 1, owner, repositories, bots, proxy_origin, agents}   # dashboard.json, less telemetry_file; repositories is a non-empty OWNER/NAME list, or the string all
```

`lib/lanes.py` refuses a file that breaks any of these rules, before any script changes anything:

- every key is the scope's: no unknown key, no `min_runners`, `runner_group` or `repos` on a user
  lane, no `exclude` on an org lane, no `wait` on a user lane, and `codex_store` and
  `qae_concurrency` exactly when `labels.qae` is set; a key given twice is refused;
- `name` is `[a-z][a-z0-9]*(-[a-z0-9]+)*` and its bridge `<name>0` fits the kernel's 15
  characters; labels are `[a-z0-9]+(-[a-z0-9]+)*`, outside the repository-runner routing labels
  (`ci-*`, `vps`, `vps-N`), and `ci`, `qae` and `wait.label` differ from each other;
  `wait` declares exactly `label`, `slots` (a positive integer, a budget of its own) and `memory`
  (docker's suffixes); `name_prefix` matches the listener's own
  pattern; paths are absolute, plain and free of `..`; sizes use systemd's suffixes in `slice` and
  docker's in `container`; `cpu_weight` is 1..10000; ports are 1..65535 without duplicates;
- on a lane with a warm pool, `warm_max_age_sec` is below `runtime_max_sec`;
- `store_fs` is `xfs` or `btrfs`;
- `preload` directories are plain relative paths named `supabase`; an org lane has exactly one
  `preload` entry per repository in `repos` (`"-"` for one without a Supabase project), so a
  repository added to the lane cannot silently cold-pull;
- across one machine's lanes, no two share a name, a label (compared without case), a
  `name_prefix`, an `image`, an `app.key_file`, a Codex store (any QAE instance's) or a slice unit (a lane's own and
  its dashed parent);
- a dashboard declares exactly `name`, `lane`, `port` and `config`, and may declare
  `bridge_account`; its `name` is a lane-style name of at most 17 characters (useradd caps
  `vibe-dashboard-<name>` at 32), its `lane` one of the machine's, its `port` 1..65535; its
  `config` has `version: 1`, the lane's owner (without case) and no `telemetry_file`, which is
  always `/var/lib/vibe-dashboard/<name>/telemetry.json`; no two dashboards share a name or a
  port. The dashboard's own loader checks the rest of `config` on the machine before a new
  `dashboard.json` replaces the old one.

## Deploy model

A change reaches a machine by a merge, to either repository: the catalog's `main` for the kit,
the values repository's `main` for what a machine runs. Each machine converges itself within 5
minutes of a merge that changed the kit or its values. `runners-pull.timer` (written by
`provision-host.sh`, every 5 minutes, root) runs `bin/runners-pull.sh` from the engine checkout,
which takes a lock and refuses, logged and with a non-zero exit, when either checkout has local
modifications or is not a git checkout. It then
fetches the engine with no credential and fast-forwards `/opt/runner-lanes` to `origin/main`, and
fetches the values checkout with the read-only deploy key `/etc/runners/deploy_key` and
fast-forwards `/opt/runner-lanes-config` to its `origin/main`. When the kit or the values moved, or
no successful apply is recorded for the pair, it runs `provision-host.sh <host> --apply`, then
`provision-lane.sh <host> <lane> --apply` for every lane of `hosts/<host>.yml`, then
`provision-dashboards.sh <host> --apply`, `<host>` being the one line of `/etc/runners-host`. A
failed host apply skips the rest; a failed lane stops neither the other lanes nor the dashboards.
It records the pair in `/var/lib/runners/applied` only when all of them succeeded, so a failure is
retried at the next tick. A gate is not a failure. When the engine's fast-forward changed
`bin/runners-pull.sh` itself, it runs the new copy in its place before it fetches the values
checkout, so a change to what a pull applies takes effect on the tick that pulled it rather than
one merge later.

The kit, for this, is the tree of `lanes/` in the engine checkout
(`git -C /opt/runner-lanes rev-parse HEAD:lanes`), not the engine's commit. The engine is a clone of
the whole catalog, and a merge there that touches nothing under `lanes/` fast-forwards the engine,
leaves that tree id as it was, and applies nothing. The record is
`engine=<tree id of lanes/> config=<values commit>`, and the line the pull logs when it converges
names the engine's commit range, so the journal leads to the merge. `provision-host.sh --check`
prints the same pair.

Never hand-edit a machine: change a repository and merge. Both checkouts are root-owned and the
pull refuses to move while either has local edits; the units the scripts write say they are managed.

Each script is dry-run by default. `--apply` converges, `--check` is a read-only report that exits
non-zero until converged (the lane's includes the smoke proofs), and `--remove` plans the teardown
(`--remove --apply` performs it); a lane's `--convert-store` plans the move of its store to btrfs
("Converting a lane's store" below):

```bash
bin/provision-host.sh <host> [--check | --apply | --remove [--apply]]
bin/provision-lane.sh <host> <lane> [--check | --apply | --remove [--apply] | --convert-store [--apply]]
bin/provision-dashboards.sh <host> [--check | --apply | --remove [--apply]]
flock -o /run/<lane>-runner-build.lock bin/build-runner-image.sh <host> <lane> [--refresh] [--if-provisioned]
```

Every one of them, and each timer that runs one, reads the host file from
`${RUNNERS_HOST_YML:-$RUNNERS_CONFIG/hosts/<host>.yml}`, and the units they write run the kit at
`$RUNNERS_ENGINE/lanes`. `RUNNERS_ENGINE` is `/opt/runner-lanes` and `RUNNERS_CONFIG` is
`/opt/runner-lanes-config` when unset. No unit or timer sets any of the three, so a machine keeps
its two checkouts at those defaults; the variables are there for the tests, and `RUNNERS_HOST_YML`
for reading one host file from somewhere else by hand.

`provision-lane.sh` refuses to run until the host phases have converged, naming
`provision-host.sh <host> --apply` (`--remove` still runs). `--apply` ends at OPERATOR ACTION gates it never performs:

- the App key: `ssh root@<hostname> 'umask 077; cat > <key_file>' < key.pem`;
- for a lane with QAE, one Codex login per QAE instance's store (`<codex_store>`, then
  `<codex_store>-2` up to `qae_concurrency`), each its own login:
  `sudo -u <lane> env CODEX_HOME=<store> HOME=/var/lib/<lane>/home PATH=/usr/bin:/bin codex login --device-auth`
  (the Codex CLI must be at `/usr/bin` or `/bin`);
- for an org lane, the App's organization permission "Self-hosted runners: Read and write", and
  the runner group `runner_group` with exactly the lane's `repos`;
- the lane's first image: `flock -o /run/<lane>-runner-build.lock /opt/runner-lanes/lanes/bin/build-runner-image.sh <host> <lane>`;
- for a lane whose host file says `store_fs: btrfs` while its store is XFS, the conversion:
  `/opt/runner-lanes/lanes/bin/provision-lane.sh <host> <lane> --convert-store --apply`
  ("Converting a lane's store" below);
- for the machine, the deploy key (the gate names the values repository by the origin of the
  machine's values checkout) and `/etc/runners-host`.

## Adding a lane

1. Add it to `hosts/<host>.yml` in the values repository; `python3 lib/lanes.py env <that file> <lane>`
   shows what the scripts will read. Merge.
2. The machine's pull runs `provision-lane.sh --apply` for it: the user, `/etc/<lane>`, the store
   filesystem, the network, the units, and the gates.
3. Pipe its App key; for an org lane grant the permission and create the runner group; for a lane
   with QAE log each QAE instance's Codex store in.
4. `provision-lane.sh <host> <lane> --apply` again (the token and image-build timers, the listener
   binary and config), build its first image, then `--apply` once more: the listener starts.
5. `provision-lane.sh <host> <lane> --check` until it passes, smoke included.

To remove a lane, run `provision-lane.sh <host> <lane> --remove --apply` on the machine, then merge
its deletion from the host file before anything else lands on either `main` (a pull applies every
lane the file still lists). `--remove` stops the store reaper once every slot has stopped and
empties the store's trash. It keeps the host's Docker and Sysbox, the store filesystem with its
preloaded stores, the runner image, the App key, the lane's user and its Codex stores, and the
(then inert) firewall rules.

## Converting a lane's store

A lane made before `store_fs` existed has an XFS store. To move it to btrfs ("A btrfs store" above):

1. The machine's kit must know the key: a host file that names `store_fs` is refused by a kit from
   before it. Then set `store_fs: btrfs` for the lane in `hosts/<host>.yml` and merge. The
   machine's pull applies it and changes nothing on the lane: `--apply` ends at the gate that names
   the next command, and `--check` fails with
   `store_fs_type=xfs host_file=btrfs (not converged: ...)`. The lane keeps taking jobs on its XFS
   store, as before.
2. On the machine, as root, read the plan. It changes nothing:
   `/opt/runner-lanes/lanes/bin/provision-lane.sh <host> <lane> --convert-store`. It prints the
   preloaded stores it would carry over with their sizes (`golden-<tag>` for the current tag and for
   every tag whose image is still there, which is what the image build keeps; a store no image
   names and an unfinished one stay behind), the room it needs under `/var/lib/<lane>`, and each
   command as a `DRY-RUN:` line.
3. Convert, at a time when the lane may take no job for a while:
   `/opt/runner-lanes/lanes/bin/provision-lane.sh <host> <lane> --convert-store --apply`.
4. `provision-lane.sh <host> <lane> --check`, and once the lane has run jobs on the new store,
   remove the XFS image with the `rm` command the conversion printed.

**What it does, in order,** each step a `RUN:` line:

- refuses, before it stops anything, unless the host file says `store_fs: btrfs`, the store is
  mounted, the filesystem under `/var/lib/<lane>` has room for the copies beside the XFS image
  (their sizes plus 1 GB), and no image build holds the lane's build lock. It then holds that lock
  and the machine's apply lock until it ends, so neither a build nor a pull's apply runs meanwhile;
- stops the listener's health timer, then the listener, so nothing starts a slot;
- stops every slot that is not on a job, the way the listener's own idle stop does: the
  `idle-stop` marker, the runner's removal from GitHub, which GitHub refuses for a runner on a job,
  and only then the unit's stop. It waits up to 15 minutes for the slots that are on a job. Past
  that it gives up: the store was not touched, and the listener, its health timer and the reaper's
  timer are started again;
- stops the reaper, unmounts the store, renames the XFS image to
  `/var/lib/<lane>/store.img.xfs-<UTC time>`, and never deletes it;
- makes a new sparse btrfs image at `/var/lib/<lane>/store.img`, rewrites the store's mount unit
  for btrfs, and mounts it at the same path;
- mounts the XFS image read-only at `/var/lib/<lane>/store-xfs` and copies each carried
  `golden-<tag>` into a new subvolume, under the unfinished store's name (`golden-<tag>.new`) until
  it is whole, then renames it. No image is rebuilt;
- checks that the store is btrfs and that the slot helper can make and delete a snapshot of the
  current tag's store, as a job's `prepare` and `cleanup` will;
- starts the reaper's timer, the listener and its health timer again, and prints where the XFS
  image is and the command that removes it.

**Downtime.** The lane takes no job from the listener's stop to its start: the wait for running
jobs (15 minutes at most), then the copies. A copy was estimated at about 2 minutes for a store of
225,000 inodes; no conversion has been timed, so read the plan's sizes and expect minutes per
store. Jobs queued meanwhile stay queued on GitHub and start when the listener is back. Idle slots
are stopped, so a warm pool is refilled after the conversion, on the new store.

**When it fails.** From the unmount to the last check, any failure (a format, a mount, a copy, the
check itself), and an interrupt or a dropped session, puts the XFS store back: the new image is
deleted, the XFS image renamed to its path, the mount unit rewritten for XFS and mounted, and the
listener started again. The run exits non-zero and says `Convert: <store> is the XFS store again`.
The conversion can be run again. What it cannot undo is a run killed outright (`kill -9`, the
machine going down) between the unmount and the check. Then, by hand: `systemctl stop` the
listener, `umount /var/lib/<lane>/store-xfs` if it is mounted, `systemctl stop` the store's mount
unit (`systemd-escape -p --suffix=mount /var/lib/<lane>/store` prints its name),
`mv /var/lib/<lane>/store.img.xfs-<UTC time> /var/lib/<lane>/store.img`, and
`provision-lane.sh <host> <lane> --apply`, which writes the XFS mount unit again, mounts the store
and starts the listener.

**Afterwards.** Run on a store that is already btrfs, `--convert-store` does nothing. The lane's
next `--apply` is converged. The kit converts in this direction only: a btrfs store under a host
file that says `xfs` (or nothing) is left as it is, with a gate that says to set the key. To go
back to XFS while the XFS image is still there, restore it by hand as above and take the key out
of the host file.

## Running more QAE jobs at once

A lane runs `qae_concurrency` QAE jobs at once, each on its own Codex store. To raise it:

1. Set the lane's `qae_concurrency` to N (at most `slots`) in `hosts/<host>.yml` and merge. The
   machine's pull (within 5 minutes) creates `<codex_store>-2` up to `<codex_store>-N` (0700, the
   lane user), rewrites the qae template with each instance's store, and restarts the listener with
   one required login per instance. A store without a login keeps its instance idle; QAE goes on
   running on the instances whose stores have one.
2. Log each new store in with a login of its own; the apply's gates and `--check` print each
   command, for example
   `sudo -u acme-ci env CODEX_HOME=/var/lib/acme-ci/codex-2 HOME=/var/lib/acme-ci/home PATH=/usr/bin:/bin codex login --device-auth`.
   Never copy one store's `auth.json` into another: the two would share one refresh token, and each
   job's rotation would retire the other's session.
3. `provision-lane.sh <host> <lane> --check`: one
   `codex_login=<store>/auth.json present last_refresh=<time> age_days=<days>` line per store. It
   fails on a store past 12 days (the keepalive has missed two of its daily runs), on a last
   keepalive run that failed (`codex_keepalive=<name>-codex-keepalive.service failed`; read its
   journal), and while the keepalive timer is not active.

Every QAE job is a slot like any other: it counts against `slots` and the listener's memory
admission. The listener gives a job the lowest instance whose store is logged in, so a higher
instance runs only while QAE jobs overlap. That is why the keepalive exists ("Codex logins between
jobs" above): a store whose instance rarely runs is refreshed by the timer, not left to age until
its next job. Lowering `qae_concurrency` leaves the higher stores in place, logged in and unused, as
`--remove` leaves every store; `--remove` disables the keepalive timer and lets a keepalive already
running finish, since one stopped between Codex's refresh and its write of `auth.json` would leave a
refresh token the server has already retired.

## Adding a machine

1. Ubuntu 24.04 amd64. In your private values repository add `hosts/<host>.yml` with its
   `hostname`, a `sysbox.image` path (for example `/var/lib/runner-lanes/sysbox.img`) and its lanes,
   starting from `examples/hosts/example.yml`. Merge.
2. On the machine, as root, clone the engine, which needs no credential:
   `git clone https://github.com/Greenbauer/vibe-verifier.git /opt/runner-lanes`.
3. Make the machine's deploy key:
   `install -d -m 0700 /etc/runners && ssh-keygen -t ed25519 -N '' -C runners-<host> -f /etc/runners/deploy_key`;
   then, from a login with admin on the values repository, add it read-only:
   `ssh root@<hostname> cat /etc/runners/deploy_key.pub > runners-<host>.pub && gh repo deploy-key add runners-<host>.pub --repo <owner>/<values repository> --title runners-<host>`.
4. On the machine, clone the values repository with that key and name the host:
   `GIT_SSH_COMMAND='ssh -i /etc/runners/deploy_key -o IdentitiesOnly=yes -o StrictHostKeyChecking=accept-new' git clone git@github.com:<owner>/<values repository>.git /opt/runner-lanes-config`
   and `echo <host> > /etc/runners-host`.
5. `/opt/runner-lanes/lanes/bin/provision-host.sh <host> --apply`, then each lane as in "Adding a lane".
6. `provision-host.sh <host> --check`, and each lane's `--check`.

## Moving a machine from an earlier checkout

A machine that runs lanes from an earlier single checkout of this kit (one repository holding both
the scripts and `hosts/`, at `/opt/runners`) moves to the two checkouts in place, and nothing is
torn down. The mechanism:

- **The names stay.** A lane's user, key, token, Codex logins, image tag (`image.env`), store,
  network, firewall rules, unit names and its `/var/lib`, `/run`, `/etc` and `/usr/local/lib` paths
  derive from the lane's name alone, and this kit derives the same ones ("Legacy names" below). So
  re-running `provision-lane.sh` with the same lane name adopts the lane: it makes no new user,
  store or image.
- **The paths differ on purpose.** The two checkouts are at `/opt/runner-lanes` and
  `/opt/runner-lanes-config`, never at the earlier path, so the earlier checkout and the units
  that point into it keep working until an apply from this kit rewrites them.
- **The first apply rewrites the units.** Every file this kit manages opens with a line naming the
  kit, and each unit that runs a script carries the script's path, so `--apply` rewrites them all,
  the scripts' paths to `/opt/runner-lanes/lanes/bin/...`, reloads systemd, and
  `runners-pull.service` then runs this kit's pull. It rebuilds each lane's listener
  once, when the listener's source here differs from what the installed binary was built from, and
  restarts it once; a restarted listener adopts the slots that are running (`listener/README.md`,
  "Crash safety"), and a job in a slot is not touched. A job that starts after the rewrite uses the
  new template; an apply builds no image.
- **The record is re-made.** The earlier pull recorded one HEAD in `/var/lib/runners/applied`. That
  matches nothing this pull compares, so its first tick applies once and records its own pair.
- **Order.** Clone both checkouts ("Adding a machine", steps 2 to 4; the deploy key the machine
  already has must be added to the values repository), read `provision-host.sh <host>` and each
  `provision-lane.sh <host> <lane>` as dry runs first, then `--apply` the host, each lane and the
  dashboards, and require every `--check` to pass. From then on nothing may run the earlier
  checkout's provisioners for these lanes: they would point the units back at it.

`sysbox.image` lets a machine adopt the Sysbox image its first lane made: an existing image there
is mounted as it is. A lane still on always-on slots (`<name>@.service`) is moved to its listener by
Phase L of `--apply`, which retires those units once the listener has started a slot.

## Dashboards

A machine's `dashboards` are Vibe Verifier's private CI dashboards: each a web service on a loopback
port, showing one owner's pull requests and CI bots, with telemetry from one of the machine's lanes.
Everything they run comes from the catalog's dashboard host kit, in a checkout of its own at
`/opt/vibe-verifier` (`bin/vibe-dashboard-follow`, `bin/vibe-dashboard-host` and the unit templates
in `dashboard/systemd/`; its runbook is [`docs/dashboard-host.md`](../docs/dashboard-host.md)).
`bin/provision-dashboards.sh` automates the runbook per machine. The dashboards' checkout is not the
engine's: the two are clones of the same catalog that move by different rules.

- **Dashboard code** goes live through the dashboards' own follower: the kit's
  `vibe-dashboard-follow.service` fetches `main` every minute, fast-forwards `/opt/vibe-verifier`
  to a commit only once all its GitHub check runs passed, restarts the dashboards when server code
  changed, and rolls back a commit a dashboard does not come up healthy on. The lane kit never
  resets that checkout; it clones it only when absent.
- **What a machine serves** (which dashboards, their ports, configs and lanes) and the dashboard
  kit's unit templates go live through the pull: `runners-pull` runs
  `provision-dashboards.sh --apply` after the lanes. A changed template reaches a machine at its
  next apply (the next change to the kit or to the machine's values, or a run by hand; a template
  lives outside `lanes/`, so its own merge applies nothing): the dashboard runbook leaves template
  changes to the host, and the follower does not reload units.

Per dashboard `<name>`, as the runbook lays out: the account `vibe-dashboard-<name>` (system,
nologin); `/etc/vibe-dashboard/<name>/` (root, 0755) with `dashboard.json` (the account's, 0600: the
host file's `config` plus `telemetry_file`), `host.env` (root, 0644: `PORT` and the lane's App id,
installation and login), `app.pem` (root, 0600: a copy of the lane's App key, re-copied by the next apply
after the key changes) and `collector.json` (root, 0600: the collector's local mode on the lane's
`/etc/<lane>/listener.json`, with `workspace_path` the lane's state directory `/var/lib/<lane>`,
which holds its slot and store images, and `codex_home` the lane's first Codex store, or
`/nonexistent` on a lane without QAE, whose account quota then reads unavailable);
`/var/lib/vibe-dashboard/<name>/gh` (root, 0755) and `/var/lib/vibe-dashboard-state/<name>` (root,
0700). The web unit's `CacheDirectory=` adds `/var/cache/vibe-dashboard-<name>` (the account's, 0700),
which systemd creates when the unit starts and where the dashboard keeps its last reading and
GitHub caches across a restart; the kit does not create it and removes it with the dashboard. The units are the kit's: `vibe-dashboard@<name>.service`, `vibe-dashboard-token@<name>.timer`
(every 10 minutes) and `vibe-dashboard-telemetry@<name>.timer` (every 30 seconds), and one
`vibe-dashboard-follow.service` per machine, whose drop-in `vibe-dashboard-follow.service.d/runners.conf`
gives it one `--unit vibe-dashboard@<name>.service <port>` pair per dashboard.

**Loopback guard.** A dashboard trusts direct loopback reads, so only root, its account and its
`bridge_account` may connect to its port: the nftables table `inet runners_dashboards`
(`/etc/runners-dashboard-guard.nft`, loaded by `runners-dashboard-guard.service`), which every
dashboard requires (`vibe-dashboard@.service.d/runners-guard.conf`). A `bridge_account` is the
account whose SSH forward carries the HTTPS route to the port; a dashboard whose route's proxy runs
as root on the machine itself names none.

**First start, and the legacy install.** Some machines already running carry an earlier dashboard
install on the same ports: two families of units, `greenbauer-dashboard*` and `ci-dashboard*`, each
with a cron file of its name in `/etc/cron.d`. The names are that install's and are kept as they
were; a machine that never had it has nothing to retire, and `--apply` then goes straight from
step 1 to step 3. While any of it is found, `--apply`:

1. writes everything above, loads the guard, and mints each new dashboard's token once (a failure
   stops the run here, with the legacy dashboards still serving) and samples its telemetry once (a
   failure warns: the dashboard serves without telemetry until the timer's next run succeeds);
2. removes the two cron files, waits out a deploy they started (`flock` on `/run/<name>.lock`), then
   stops and disables every legacy unit;
3. starts the dashboards, their timers and the follower, and requires each dashboard to answer
   `GET http://127.0.0.1:<port>/api/dashboard` with 200 and `"version": 1` from its own unit's
   process within 30 seconds.

It leaves the legacy unit files, accounts and data, the forward accounts, the SSH bridge and the
HTTPS routes in place. While a dashboard's lane key is absent and the legacy install still holds the
ports, nothing starts and a gate names the key. If a later apply finds the legacy install back (its
cron file or an enabled or running unit), it retires it again.

**When the health check fails**, the run fails with the unit's state and journal, and nothing is
recorded, so every `runners-pull` tick (5 minutes) applies again: the units are already enabled, the
legacy install stays retired, and the check fails the same way until a fix merges. That dashboard is
down meanwhile. To bring a legacy one back by hand: `systemctl disable --now
vibe-dashboard@<name>.service vibe-dashboard-follow.service`, then `systemctl enable --now` the legacy
units (the cron files only redeployed releases and are not needed to serve). A failed token or
telemetry run later shows in `--check` and `systemctl --failed`.

**Removing.** A dashboard deleted from the host file is removed by the next apply: its units
disabled, its three directories, its cache directory and its account deleted, the follower and the
guard rewritten without it. `--remove --apply` removes every dashboard, the follower, the guard and the templates;
it keeps `/opt/vibe-verifier`, the retired legacy install and the lanes.

## Legacy names

These are legacy names, kept for machines already running: the images and units there carry them,
so they are a contract. Renaming any of them is a migration of every such machine, not an edit here:

- the `KNOWN_CI_` prefix of the slot units' environment, of the scripts' settings and of
  `image.env` (`KNOWN_CI_IMAGE_TAG`);
- the in-image paths `/usr/local/bin/known-ci-entrypoint`, `/opt/known-ci/preload` and
  `/etc/known-ci-runner`;
- the Sysbox drop-in `sysbox-mgr.service.d/known-ci-storage.conf`;
- the firewall rules' comment `ai-fleet-known-ci`;
- the image label `ai-fleet.disk-watch=keep`: a disk-watch job that some of those machines still
  run prunes, at 85% disk, every image no container references except those carrying it. A lane
  image is unreferenced between jobs, so the build fails unless the committed image still carries
  the label. It can go once no machine runs that job.

The systemd unit names (`runners-pull.service` and `.timer`, `<lane>-*.service`), `/etc/runners`,
`/etc/runners-host`, `/var/lib/runners`, and the state, run, etc and lib paths under a lane's name
are a contract in the same way. So are a dashboard's `name` and `port`: the name is its account,
directories and units, and the port is where its HTTPS route forwards; renaming one removes the old
instance and makes a new one.

## Tests

From the catalog's root:

```bash
lanes/tests/run-all.sh                # every hermetic test; Linux, bash 5, python3 with PyYAML, jq, git, flock
lanes/listener/build.sh test          # gofmt -l, go vet ./..., go test ./... in the pinned Go image
docker run --rm --platform linux/amd64 -v "$PWD":/w -w /w ubuntu:24.04 bash -c \
  'apt-get update -qq && apt-get install -y -qq python3 python3-yaml jq git procps >/dev/null && useradd -m t && su t -c lanes/tests/run-all.sh'
```

The tests stub every system tool on PATH; nothing real is installed, mounted, started or called.
They are Linux-only, like the kit: on a Mac run them in the container above. The catalog's CI
(`.github/workflows/ci.yml`) runs both beside the catalog's own gates: the `lanes` job on
`ubuntu-24.04`, the release the machines run, and the `lanes-listener` job. The main scripts stay
shellcheck-clean:
`shellcheck -x lanes/bin/*.sh lanes/lib/provision.sh lanes/image/entrypoint.sh lanes/listener/build.sh`.
