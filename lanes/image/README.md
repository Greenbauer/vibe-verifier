# The runner image

The image every lane's slot runs: one GitHub Actions job per container, under Sysbox
(`--runtime=sysbox-runc`), with its own inner Docker so a job's `supabase start` never touches the
host's. One definition serves every lane; each lane builds it under its own name (its `image`, for
example `greenbauer-ci-runner`). `bin/build-runner-image.sh` builds it on the lane's machine and
preloads an inner Docker store with the Supabase images the lane's repositories start, so a job
pulls nothing. The store is not in the image: it is a directory on the lane's XFS store filesystem
(`/var/lib/<lane name>/store/golden-<tag>`), and every job runs on its own reflink snapshot of it,
mounted on the container's `/var/lib/docker` (the kit's `README.md`, one directory up, "Disk").

| File | Role |
|---|---|
| `Dockerfile` | the image, linux/amd64 only; every download pinned |
| `entrypoint.sh` | PID 1 (`/usr/local/bin/known-ci-entrypoint`, a legacy in-image name): runs one job, or the build's `hold` / `preload` / `verify-preload` modes |
| `daemon.json` | the inner dockerd's config: classic overlay2, containerd snapshotter off (see "The inner Docker") |

Which repositories a lane preloads is the lane's `preload` map in `hosts/<host>.yml` (the machine's
values checkout), not a file here.

## What the image carries, and where each pin came from (2026-09-25)

| Component | Version | Pinned by | Source of the pin |
|---|---|---|---|
| Base | `ubuntu:24.04` | index digest `sha256:008173c2…3ca3` | Docker Hub registry API |
| GitHub Actions runner | 2.337.0, in `/opt/actions-runner` | sha256 `70920811…6613` | the release notes' SHA-256 list |
| Node.js | 22.23.3 (latest 22.x) | sha256 `df450af8…02de` | `SHASUMS256.txt`, whose signature checked out against nodejs/release-keys |
| pnpm | corepack shims (`corepack enable pnpm`) | Node's bundled corepack | corepack fetches the version a `packageManager` field asks for |
| bun | 1.4.2 | sha256 `36368fae…a913` | release asset digest, equal to the release's `SHASUMS256.txt` |
| Python | 3.12 (noble's `python3`), pip, venv | Ubuntu archive | |
| PostgreSQL client | 16 (noble's `postgresql-client`: `psql` on every job's PATH) | Ubuntu archive | a job's own tests may shell out to `psql`; the lane's `--check` smoke runs `psql --version` |
| uv | 0.12.19 | sha256 `23bf5552…d8c8` | release asset digest, equal to the asset's `.sha256` file |
| gh | 2.101.0 | sha256 `9bca2d1c…72b8` | release asset digest, equal to `checksums.txt` |
| Supabase CLI | 2.118.0 (`supabase` and the `supabase-go` it forwards to) | sha256 `f6089a86…c86d` | release asset digest, equal to `checksums.txt`; what `supabase/setup-cli` with `version: latest` resolved to on 2026-09-25 |
| gitleaks | 8.30.1 | sha256 `551f6fc8…70eb` | release asset digest, equal to `checksums.txt`; also Vibe Verifier's `gates/_tools.py` pin, so the gitleaks gate uses this binary instead of downloading one |
| Docker CE engine and CLI | `5:29.8.1-1~ubuntu.24.04~noble`, containerd.io `2.3.6-1~ubuntu.24.04~noble` | apt version pins; Docker's repository key by sha256 `1500c1f5…a570` (fingerprint `9DC8 5822 9FC7 DD38 854A E2D8 8D81 803C 0EBF CD88`) | download.docker.com noble `Packages` index |
| Chromium's OS packages | from `npx playwright@1.58.1 install-deps chromium` at build time | Playwright version | the `@playwright/test` version a served repository's lockfile resolved on 2026-09-25 |
| The runner's .NET libraries | libicu74, libkrb5-3, liblttng-ust1t64, libssl3t64, zlib1g | Ubuntu archive | the runner's own `bin/installdependencies.sh` list for noble |

Not in the image: `sudo` (no job step needs root; hosted-only steps are guarded by
`runner.environment`), Playwright's browsers (a job's `npx playwright install chromium` downloads
them into its home), and the compose and buildx plugins.

The full hex of every digest is in the `Dockerfile`. `bin/provision-host.sh` installs the host's
Docker CE at the same `DOCKER_VERSION`, `CONTAINERD_VERSION` and `DOCKER_GPG_SHA256`, so the host
and the inner engine run one release.

### Bumping a pin

Change the `ARG` in the `Dockerfile`, taking the new sha256 from the source named above, never
from a mirror. Any change to a file in this directory except `*.md` changes the image tag's hash,
so the build script's default mode or the next weekly `--refresh` builds it. Bump `RUNNER_VERSION`
with each runner release: a JIT runner auto-updates, and an outdated one downloads the new release
(about 230 MB) into every job's home before taking the job.

`SUPABASE_VERSION` and the workflows must move together; see "Staying pull-free" below.

## Running a job: the container contract

The slot unit (`bin/provision-lane.sh` writes it) runs, in essence:

```
docker run --rm --runtime=sysbox-runc --tmpfs /tmp:size=2g,exec --cpuset-cpus <the slot's 4 cores> \
  -v <fresh slot volume>:/home/runner -v <store snapshot>:/var/lib/docker -v <jit file>:/run/jit:ro ... <image>:${KNOWN_CI_IMAGE_TAG}
```

The tag comes from `/var/lib/<lane name>/image.env`, which the build writes. A QAE slot also
mounts its own instance's Codex login store at `/home/runner/.codex-qae`. Docker makes that mount point in
the fresh volume, so it is the one entry besides `lost+found` the entrypoint accepts in a fresh
home, and only while something is mounted there.

The rootfs is writable and discarded with the container. It is not `--read-only`: Sysbox mounts
its per-container `/var/lib/docker` read-only when the rootfs is, a remount from inside is refused,
and the inner dockerd dies with `chmod /var/lib/docker: read-only file system` (measured
2026-09-26). What holds instead: the job runs as `runner` (uid 1001) and the image has no sudo, so
`/etc`, `/usr`, `/opt`, `/var` and `/root` refuse its writes; `/home/runner` (the per-job volume)
and `/tmp` (tmpfs) take them; `/run/jit` is root-only (mode 0400), and when `/run` is read-only the
entrypoint reads it first and then mounts a 256 MB tmpfs over `/run`. The lane mounts the job's
store snapshot on `/var/lib/docker`, the only inner-Docker data root Sysbox supports. `npm install
-g` and the like fail for the job; tools go through the job's tool cache under
`/home/runner/_work/_tool`.

The entrypoint, as root: refuses a home holding anything but `lost+found` and a mounted
`.codex-qae` (a slot that was not remade must not run a second job on the first job's files),
starts the inner dockerd and waits for it, then runs the runner as `runner` (uid 1001, whose only
extra group is the inner `docker`) for exactly one job, relaying SIGTERM to it so stopping the
container cancels the job. It starts the runner with SIGINT and SIGQUIT at their defaults
(`env --default-signal`): bash starts a background command with both ignored, every job step
inherited that, and the runner's cancel, which sends a step SIGINT and then SIGTERM 7.5 s later,
reached a step only as the SIGTERM. Exit codes:

| Code | Meaning |
|---|---|
| 0 | the runner ran its job and `run.sh` exited 0 |
| non-zero from `run.sh` | passed through |
| 75 | `run.sh` exited 0 but no job ran: `run.sh` also exits 0 when the listener stops on a terminal error, so the entrypoint looks for the worker's diag log (`_diag/Worker_*.log`), which exists only when a job was dispatched |
| 64 | contract: no JIT file, or `/run` read-only and not mountable |
| 65 | the home is missing or not fresh |
| 69 | the inner dockerd did not come up (its log tail is printed) |

**Where the runner runs.** The image installs it at `/opt/actions-runner` and bakes nothing under
`/home/runner`, where the slot volume is mounted. It cannot run in place: it writes into its own
root directory (the decoded JIT files `.runner`, `.credentials` and `.credentials_rsaparams`,
`run-helper.sh`, `_diag`, any self-update) and derives that root from the real path of `bin/`
(`Runner.Listener/Runner.cs` and `Runner.Common/HostContext.cs` at v2.337.0), so a read-only
`/opt/actions-runner` or a symlinked `bin/` would fail. At each job the entrypoint copies `bin/`
(about 80 MB) and the start scripts to `/home/runner/actions-runner` and links `externals/` (about
590 MB, only read) back to `/opt/actions-runner`. The work folder is `/home/runner/_work`: the
listener mints the JIT config with that `work_folder`.

**The JIT hand-off.** The entrypoint reads `/run/jit` (0400, root) and passes it to `run.sh` in
`ACTIONS_RUNNER_INPUT_JITCONFIG`. Runner 2.337.0 reads that variable for `--jitconfig`
(`src/Runner.Listener/CommandSettings.cs`: any `ACTIONS_RUNNER_INPUT_<arg>`), masks it as a
secret, and removes it from the listener's environment. It never appears in an argv, so host `ps`
does not show it, and it is never logged. It does stay in the environment of the `run.sh` and
`run-helper.sh` shells for the job's life, readable by root and by the job's own uid, which can
read the decoded `.credentials` files in its home anyway.

## The inner Docker

`daemon.json` pins the classic `overlay2` storage driver with the containerd snapshotter off, at
the default data root `/var/lib/docker`. Docker 29 defaults new installs to the containerd image
store, and Sysbox's issue 1021 reports an inner Docker on the containerd snapshotter whose database
breaks when its Sysbox container is re-created; the classic store under `/var/lib/docker` is the
layout Sysbox supports, and the one the lane's mounted store holds. The host's own Docker is the
outer engine and is not changed here.

`SUPABASE_HOME` is `/tmp/supabase-home`: the CLI writes its trace and telemetry state there on
every command, and `/tmp` is writable in every mode (the build's preload container and the job alike).

## Preloading

`bin/build-runner-image.sh <host> <lane>` commits a container of this image after a preload, like
Sysbox's documented "docker commit" procedure (sysbox `docs/quickstart/images.md`, which leaves
the host's default runtime at runc), except that the preload's `/var/lib/docker` is a host
directory the container has mounted, so the images land on the store filesystem and not in the
committed image:

1. reads the lane's `preload` map (validated by `lib/lanes.py`: an org lane pairs every repository
   in its `repos` with an entry, so a repository added to the lane cannot silently cold-pull), and
   reads each listed repository's `config.toml` at `repos/<owner>/<repo>/contents/<dir>/config.toml`
   as the lane's user (whose App token must be able to read those repositories), plus empty
   stand-ins for any email template file the config names: the CLI rejects a config whose template
   file is missing, and template content does not change which images start pulls;
2. builds this directory as `<image>:base-<tag>`;
3. copies them to `/opt/known-ci/preload/<name>/supabase/` in a container of it run under
   sysbox-runc in `hold` mode with the empty `/var/lib/<lane name>/store/golden-<tag>.new` mounted
   on `/var/lib/docker`, and execs `known-ci-entrypoint preload`, which runs each repository's own
   `supabase start -x studio,imgproxy,mailpit,edge-runtime,logflare,vector --ignore-health-check`
   (the preload wants the images, not a healthy stack) against its own `config.toml` (so the tags
   match its CLI and Postgres major version), then `supabase stop --no-backup`, pulls
   `supabase/pg_prove:3.36` (what `supabase test db` needs and `supabase start` never pulls),
   records the images in `/etc/known-ci-runner/preloaded-images`, and stops dockerd cleanly;
4. commits the container as `<image>:<date>-<hash>` with `CMD` reset (the mounted store is not
   part of the commit: the new layer is the manifest and the project configs, under 1 MB);
5. checks `CMD`, `ENTRYPOINT` and the label `ai-fleet.disk-watch=keep`, renames the store to `golden-<tag>`, and runs `verify-preload`
   in a fresh sysbox-runc container on a reflink snapshot of that store, made and removed by
   `bin/lane-slot.sh` the way a job gets its store; it fails unless the store holds exactly the
   recorded images;
6. moves `<image>:current` to it, writes `KNOWN_CI_IMAGE_TAG=<tag>` to
   `/var/lib/<lane name>/image.env` (the lane's units read it at every start, so the next job runs
   the new image on a snapshot of the new store), and keeps the newest three dated tags (plus
   whichever one `current` names) with their stores; a dated tag without its store (no slot can
   run it) is pruned whatever its age, and so is a store whose tag has no image, or an unfinished
   one. Last it runs `docker builder prune --force --filter until=168h`: the base build's layers
   stay in Docker's build cache after their tags are removed, and the build clears what it left
   rather than rely on another job on the machine. That is the machine's whole build cache, unused
   for a week; a failure there is a warning. An image tag without its store is not converged on:
   the build untags it and builds again.

The preload and the lane's smoke run the CLI with `SUPABASE_INTERNAL_IMAGE_REGISTRY=ghcr.io`,
the value `supabase/setup-cli` exports for every later step of a job, so the store holds the tags
a job asks for (the canary's first Supabase job pulled four images from ghcr.io while the smoke,
on the CLI's default registry, reported zero, 2026-09-28).

The project configs stay in the image under `/opt/known-ci/preload`: the lane's zero-pull proof
starts those same projects. They are the repositories' checked-in `config.toml` files, so any job
on the lane can read the other repositories' local Supabase settings.

A map with no project (empty, or every entry `-`) preloads nothing: no config is read, the build
container records an empty `/etc/known-ci-runner/preloaded-images` instead of running `preload`,
the store stays empty (each job's inner dockerd starts on an empty snapshot), and `verify-preload`
is skipped; the image is still committed, checked, tagged and made current. The lane's smoke reads
the empty manifest as "nothing to pull", and fails an image with no project whose manifest is
missing or lists images.

A failure at any step exits non-zero with the reason and leaves `current` where it was.

**Tag.** `<UTC date>-<12 hex>`, the hex a sha256 over every file here except `*.md`, and the
lane's own preload map in canonical text (`<repo> <dir>` lines, sorted): the Dockerfile, the
entrypoint and `daemon.json`, because a change to any of them is a different image, and the map,
because it decides what is preloaded. Another lane's map is not part of this lane's image, so
editing it rebuilds nothing here.

**Why the store is mounted, not committed.** Sysbox CE copies an image's `/var/lib/docker` into
`/var/lib/sysbox` with one `rsync` at every container start (inner image sharing, which avoids
that, is Sysbox-EE only): 56 to 66 s and 5.6 GB per container for a five-project store, measured on
2026-09-26. With a host directory mounted on `/var/lib/docker`, sysbox-runc asks `sysbox-mgr` for
no volume there, so nothing is copied; the lane mounts a per-job reflink snapshot (`cp -a
--reflink=always`, about 8 s for 270k files; the container's first line follows `docker run` by
about 1 s), and the preload's writes go through the same kind of mount, which also keeps the inner
images' own uids (a commit of a Sysbox container had stored every file as root). The build log
prints the manifest and the store's on-disk size at every build; that number, once per kept tag
plus what jobs write, is what `store_disk_gb` bounds.

**Staying pull-free.** A job pulls nothing only while it asks for the preloaded tags:

- it must run the image's CLI. `supabase/setup-cli` with `version: latest` installs the newest CLI
  ahead of it on PATH, and a newer CLI names newer images. On a lane either drop `setup-cli` or
  pin it to `SUPABASE_VERSION`;
- it must pass the same `-x` list to `supabase start`;
- it must not `supabase link` before `supabase start`: link writes `supabase/.temp/*-version`
  files that make start use the linked project's image versions.

**The keep label.** The image carries `LABEL ai-fleet.disk-watch=keep`, and `docker commit` keeps
it. It is a legacy name, kept for machines already running: between jobs no container references
the lane image, and a disk-watch job some of those machines still run prunes at 85% disk every
unreferenced image without that label. The build fails unless the committed image still carries it.

## The lane's contract

| The lane relies on | Here |
|---|---|
| `/var/lib/<lane name>/image.env` holding `KNOWN_CI_IMAGE_TAG=<tag>` | written atomically by the build after the image is committed, verified and tagged, and restored by a converged run when missing or stale |
| no runner install under `/home/runner` | installed at `/opt/actions-runner`; the per-job copy is made at run time, on the slot |
| the entrypoint reads `/run/jit` before anything mounts over `/run` | read first; the tmpfs over a read-only `/run` comes after |
| inner dockerd at its default data root `/var/lib/docker` | `daemon.json` sets no `data-root`; the lane mounts the job's store snapshot there |
| the config through `ACTIONS_RUNNER_INPUT_JITCONFIG`, the runner as `runner` | yes, via `setpriv` |
| exit 0 after the one job, non-zero on any failure | see the exit-code table above |
| `bash`, coreutils, `getent`, `dockerd`, the Supabase CLI | all in the image |
| `/opt/known-ci/preload/<name>/supabase/config.toml` for each preloaded repository | staged there by the build and kept in the image |

## Operating

- Weekly: `<lane>-image-build.timer` runs the build with `--refresh --if-provisioned` under the
  lane's build lock `/run/<lane>-runner-build.lock`, up to an hour late at random.
- A lane's first image, or a rebuild by hand, as root on the machine:
  `flock -o /run/<lane>-runner-build.lock /opt/runner-lanes/lanes/bin/build-runner-image.sh <host> <lane>`.
  `bin/provision-lane.sh` prints that command while the lane has no image.
- Adding a repository to an org lane means adding its `preload` entry (`<repo>: <dir>`, or
  `<repo>: "-"`) in the same change; `lib/lanes.py` refuses the config otherwise. A user lane
  preloads a repository once its map has an entry for it.

Tests: `tests/build-runner-image-test.sh` (the build, tags, pruning and every failure path, with
docker, gh, runuser and timeout stubbed) and `tests/runner-entrypoint-test.sh` (the entrypoint's
modes; the JIT config never reaches an argv or output, and the runner runs as `runner`).
