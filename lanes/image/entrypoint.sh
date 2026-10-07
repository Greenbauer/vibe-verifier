#!/usr/bin/env bash
# known-ci-entrypoint: PID 1 of a lane's runner container (a legacy in-image name, kept for the
# images and units of machines already running).
#
# Runs only inside the image: Ubuntu 24.04 amd64 under Sysbox, bash 5, GNU coreutils and
# util-linux. tests/runner-entrypoint-test.sh drives it with the privileged commands stubbed; the
# KNOWN_CI_* overrides below exist for that test alone.
#
# Modes (first argument):
#   (none)          Run one GitHub Actions job: start the inner dockerd as root, install the runner
#                   into the fresh per-job home, and run it as `runner` with the one-time JIT config
#                   read from /run/jit. The config travels in ACTIONS_RUNNER_INPUT_JITCONFIG, which
#                   the runner reads, masks as a secret and deletes from its own environment
#                   (actions/runner src/Runner.Listener/CommandSettings.cs), so it never reaches an
#                   argv that host `ps` would show. Exit: run.sh's code when non-zero; otherwise 0
#                   only if a job ran, since run.sh also exits 0 when the listener stops on a
#                   terminal error (75 then).
#   hold            Sleep forever: the image build's preload container.
#   preload DIR...  Start dockerd; in each project DIR run the repository's own `supabase start`
#                   with the lane's exclusions, then `supabase stop --no-backup`, so the image tags
#                   that DIR's CLI and Postgres major version request land in /var/lib/docker (the
#                   build mounts the lane's store there); write them to the manifest; stop dockerd
#                   cleanly for `docker commit`.
#   verify-preload  Start dockerd and fail unless its images are exactly the manifest (the build
#                   mounts a snapshot of the store on /var/lib/docker first).
#
# Container contract (the unit that runs a job): --runtime=sysbox-runc --read-only --tmpfs /tmp,
# a freshly made volume on /home/runner (nothing in it but lost+found, and for a QAE job the mount
# point of the lane's Codex login store, /home/runner/.codex-qae), and the JIT config
# mounted read-only at /run/jit. /run may come as --tmpfs /run; when it is read-only the entrypoint reads
# /run/jit first and then mounts a tmpfs over /run itself (root in a Sysbox container may mount),
# which also hides the config file from the job. The job's reflink snapshot of the preloaded inner
# Docker store is mounted on /var/lib/docker (bin/lane-slot.sh); the image's own
# /var/lib/docker is empty, and Sysbox backs it with an empty directory when nothing is mounted.
#
# Why the runner is copied per job: the image installs it at /opt/actions-runner (nothing is baked
# under /home/runner, where the slot volume is mounted), but it writes into its own root directory
# (the decoded JIT files .runner/.credentials/.credentials_rsaparams, run-helper.sh, _diag,
# self-update staging) and derives that root from the real path of bin/, so it cannot run from the
# read-only image and a symlinked bin/ would not help. The entrypoint copies bin/ (about 80 MB) and
# the start scripts to /home/runner/actions-runner and links externals/ (about 590 MB, only ever
# read) back. The lane mints the JIT config with work_folder /home/runner/_work.
set -euo pipefail

JIT_FILE="${KNOWN_CI_JIT_FILE:-/run/jit}"
RUNNER_HOME="${KNOWN_CI_HOME:-/home/runner}"
RUNNER_ROOT="$RUNNER_HOME/actions-runner"
RUNNER_DIST="${KNOWN_CI_RUNNER_DIST:-/opt/actions-runner}"
RUN_DIR="${KNOWN_CI_RUN_DIR:-/run}"
MANIFEST="${KNOWN_CI_MANIFEST:-/etc/known-ci-runner/preloaded-images}"
DOCKER_ROOT="${KNOWN_CI_DOCKER_ROOT:-/var/lib/docker}"
DOCKERD_TIMEOUT="${KNOWN_CI_DOCKERD_TIMEOUT:-60}"
RUNNER_USER=runner
# Services no job on this lane uses; the workflows pass the same list to `supabase start`.
SUPABASE_EXCLUDE="studio,imgproxy,mailpit,edge-runtime,logflare,vector"
# `supabase test db` (the repositories' migration checks) pulls this image, which
# `supabase start` never does; the tag is the CLI's own at the pinned version
# (apps/cli-go/pkg/config/templates/Dockerfile in supabase/cli v2.118.0). Preloaded so no job pulls it.
PG_PROVE_IMAGE="${KNOWN_CI_PG_PROVE_IMAGE:-supabase/pg_prove:3.36}"

log() { printf '[runner] %s\n' "$*" >&2; }
die() {
  local code="$1"
  shift
  printf '[runner FAIL] %s\n' "$*" >&2
  exit "$code"
}

DOCKERD_PID=""
dockerd_failed() {
  printf '[runner FAIL] %s; last dockerd output:\n' "$1" >&2
  tail -n 40 "$RUN_DIR/dockerd.log" >&2 2>/dev/null || true
  exit 69
}

start_dockerd() {
  [ -w "$RUN_DIR" ] || die 64 "$RUN_DIR is not writable; run the container with --tmpfs /run"
  # Sysbox's documented caveat: pid files captured by docker commit keep dockerd from starting.
  rm -f "$RUN_DIR/docker.pid" "$RUN_DIR/docker/containerd/containerd.pid"
  dockerd >>"$RUN_DIR/dockerd.log" 2>&1 &
  DOCKERD_PID=$!
  local waited=0
  until docker version >/dev/null 2>&1; do
    kill -0 "$DOCKERD_PID" 2>/dev/null || dockerd_failed "dockerd exited during startup"
    [ "$waited" -lt "$DOCKERD_TIMEOUT" ] || dockerd_failed "dockerd not ready after ${DOCKERD_TIMEOUT}s"
    sleep 1
    waited=$((waited + 1))
  done
  log "inner dockerd ready"
}

stop_dockerd() {
  kill -TERM "$DOCKERD_PID"
  wait "$DOCKERD_PID" || true
  rm -f "$RUN_DIR/docker.pid" "$RUN_DIR/docker/containerd/containerd.pid" "$RUN_DIR/docker.sock"
  log "inner dockerd stopped"
}

require_fresh_home() {
  [ -d "$RUNNER_HOME" ] || die 65 "$RUNNER_HOME is missing; mount the slot volume there"
  local leftover
  leftover="$(find "$RUNNER_HOME" -mindepth 1 -maxdepth 1 ! -name lost+found ! -name .codex-qae -print -quit)"
  # A QAE job's Codex store is mounted at .codex-qae, so Docker made that directory in the fresh
  # volume; it is excused only while the store is mounted there.
  if [ -z "$leftover" ] && [ -e "$RUNNER_HOME/.codex-qae" ] && ! mountpoint -q "$RUNNER_HOME/.codex-qae"; then
    leftover=.codex-qae
  fi
  [ -z "$leftover" ] \
    || die 65 "$RUNNER_HOME is not a fresh slot volume (found ${leftover##*/}); refusing to run a job on another job's files"
}

install_runner() {
  chown "$RUNNER_USER:$RUNNER_USER" "$RUNNER_HOME"
  mkdir "$RUNNER_ROOT"
  local entry
  for entry in "$RUNNER_DIST"/*; do
    case "${entry##*/}" in
      externals) ln -s "$entry" "$RUNNER_ROOT/externals" ;;
      *) cp -a "$entry" "$RUNNER_ROOT/" ;;
    esac
  done
  chown "$RUNNER_USER:$RUNNER_USER" "$RUNNER_ROOT"
}

run_job() {
  [ -s "$JIT_FILE" ] || die 64 "no JIT runner config at $JIT_FILE; mount the one-time config there read-only"
  require_fresh_home
  local jit
  jit="$(<"$JIT_FILE")"
  if [ ! -w "$RUN_DIR" ]; then
    mount -t tmpfs -o mode=0755,nosuid,nodev,size=256m tmpfs "$RUN_DIR" \
      || die 64 "$RUN_DIR is read-only and a tmpfs could not be mounted over it; run the container with --tmpfs /run"
    log "mounted a tmpfs over $RUN_DIR"
  fi
  install_runner
  start_dockerd
  # run-helper.sh looks for the self-update flag file in the working directory: the runner root.
  cd "$RUNNER_ROOT"
  log "starting the runner as $RUNNER_USER for one job"
  # Only the child's environment carries the config. RUNNER_MANUALLY_TRAP_SIG makes run.sh forward
  # the SIGTERM this shell relays to the listener, so stopping the container cancels the job.
  # A background command of this non-interactive shell starts with SIGINT and SIGQUIT ignored, and
  # every process under the runner would inherit that, job steps included; the runner cancels a
  # step with SIGINT first, so env puts both back to their defaults before the runner starts.
  ACTIONS_RUNNER_INPUT_JITCONFIG="$jit" RUNNER_MANUALLY_TRAP_SIG=1 \
    HOME="$RUNNER_HOME" USER="$RUNNER_USER" LOGNAME="$RUNNER_USER" \
    env --default-signal=INT,QUIT \
    setpriv --reuid="$RUNNER_USER" --regid="$RUNNER_USER" --init-groups -- "$RUNNER_ROOT/run.sh" &
  local child=$! rc=0
  jit=""
  trap 'kill -TERM "$child" 2>/dev/null' TERM INT
  wait "$child" || rc=$?
  # A relayed signal interrupts wait before the runner is done; wait until it really exits.
  while kill -0 "$child" 2>/dev/null; do
    rc=0
    wait "$child" || rc=$?
  done
  trap - TERM INT
  [ "$rc" -eq 0 ] || die "$rc" "the runner exited $rc"
  # The worker process, and so its diag log, exists only when a job was dispatched to this runner.
  compgen -G "$RUNNER_ROOT/_diag/Worker_*.log" >/dev/null \
    || die 75 "the runner exited 0 without running a job (a terminal listener error; its log was $RUNNER_ROOT/_diag)"
  log "the runner ran its job and exited 0"
}

preload() {
  [ "$#" -gt 0 ] || die 64 "preload needs at least one project directory"
  local dir
  for dir in "$@"; do
    [ -f "$dir/supabase/config.toml" ] || die 64 "no supabase/config.toml under $dir"
  done
  start_dockerd
  # supabase/setup-cli exports this for every later step of a job, so a job's `supabase start` asks
  # for ghcr.io tags; the preload must fill the store with the same tags or the job pulls (the
  # canary's first Supabase job pulled four images while the smoke, on the CLI's default registry,
  # reported zero, 2026-09-28). Scoped to the preload: a job's environment comes from the runner
  # and must not inherit it from here.
  export SUPABASE_INTERNAL_IMAGE_REGISTRY="${SUPABASE_INTERNAL_IMAGE_REGISTRY:-ghcr.io}"
  for dir in "$@"; do
    log "supabase start in $dir"
    # The preload wants the images, not a healthy stack: a project's API schema list can name a
    # schema only its migrations create, and no migrations are staged, so
    # PostgREST answers 503 and the CLI would exit 1 without --ignore-health-check (2026-09-26).
    (cd "$dir" && supabase start -x "$SUPABASE_EXCLUDE" --ignore-health-check) || die 70 "supabase start failed in $dir"
    (cd "$dir" && supabase stop --no-backup) || die 70 "supabase stop --no-backup failed in $dir"
  done
  log "docker pull $PG_PROVE_IMAGE"
  docker pull "$PG_PROVE_IMAGE" >/dev/null || die 70 "docker pull $PG_PROVE_IMAGE failed"
  # docker commit cannot capture running inner containers, and a volume left behind would ship a
  # database into every job.
  local containers volumes
  containers="$(docker ps -aq)"
  [ -z "$containers" ] || die 70 "inner containers remain after supabase stop"
  volumes="$(docker volume ls -q)"
  [ -z "$volumes" ] || die 70 "inner volumes remain after supabase stop --no-backup"
  docker network prune -f >/dev/null
  mkdir -p "${MANIFEST%/*}"
  docker image ls --format '{{.Repository}}:{{.Tag}}' | LC_ALL=C sort >"$MANIFEST"
  [ -s "$MANIFEST" ] || die 70 "supabase start pulled no images"
  log "preloaded $(wc -l <"$MANIFEST" | tr -d ' ') images"
  stop_dockerd
  # The store the build mounted here; each job's snapshot of it shares these blocks (reflinks).
  log "inner store on disk: $(du -sh "$DOCKER_ROOT" | cut -f1)"
}

verify_preload() {
  [ -s "$MANIFEST" ] || die 1 "no preload manifest at $MANIFEST"
  start_dockerd
  local actual
  actual="$(docker image ls --format '{{.Repository}}:{{.Tag}}' | LC_ALL=C sort)"
  if [ "$actual" != "$(<"$MANIFEST")" ]; then
    log "inner images differ from $MANIFEST (< present, > recorded):"
    diff <(printf '%s\n' "$actual") "$MANIFEST" >&2 || true
    exit 1
  fi
  log "preload verified: $(wc -l <"$MANIFEST" | tr -d ' ') images present"
}

if [ "$(id -u)" != 0 ] && [ "${KNOWN_CI_ALLOW_NON_ROOT:-0}" != 1 ]; then
  die 64 "must start as root: it starts the inner dockerd"
fi

case "${1:-}" in
  "") run_job ;;
  hold) exec sleep infinity ;;
  preload) shift; preload "$@" ;;
  verify-preload) verify_preload ;;
  *) die 64 "unknown mode '$1' (none, hold, preload DIR..., verify-preload)" ;;
esac
