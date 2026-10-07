#!/usr/bin/env bash
# provision-lane.sh: provision one disposable-container lane of a machine.
#
#   provision-lane.sh <host> <lane> [--check | --apply | --remove [--apply]]
#
# The lane runs every job it serves in its own Sysbox system container, started on demand: the
# lane's scale-set listener (listener/) keeps a GitHub runner scale set per kind of job, mints a
# just-in-time runner for each job GitHub assigns, and starts one systemd slot unit
# (<name>-<kind>@<n>.service) for it; the unit re-creates the slot's filesystem, runs one container
# that takes that job and exits, and stays down. Nothing a job does survives into the next job or
# reaches a concurrent one, and no job reaches the host's Docker socket or its private networks.
# An org lane serves the organisation repositories its runner group lists, with a warm pool of
# idle registered slots; a user lane serves every private repository of the account its App is
# installed on. Every lane runs as its own system user (the lane's name). Design: README.md.
#
# Operator-invoked and dry-run by default: --apply mutates, --check is a read-only report that exits
# non-zero until the lane is converged and its smoke proofs pass, --remove plans the teardown (and
# runs it with --apply). runners-pull.service runs --apply for every lane after every change to the
# kit or to the machine's values.
# The host phases (bin/provision-host.sh) must have converged first; this script refuses otherwise.
#
# Linux-only by design: Ubuntu 24.04, bash 5, GNU coreutils, util-linux, apt, systemd, rootful
# Docker. Run it as root from the machine's engine checkout (/opt/runner-lanes/lanes): the units it
# writes execute bin/lane-slot.sh, bin/lane-token-refresh.py, bin/build-runner-image.sh and
# bin/listener-health.sh from there, so fixes reach the machine through its self-update, and it
# builds the listener from that checkout.
#
# --apply converges, in order, each step skipped when already converged:
#   A0  the lane's own system user (its name, home <state>/home); the lane's state directory
#       <state> (/var/lib/<name>) is root:<lane user> 0710, so that user reaches its home and the
#       Codex stores below it and nobody else enters
#   A1  the App key at app.key_file (root, 0600), piped by the operator (gate)
#   A3  <name>-token-refresh.timer, a root timer writing the lane user's gh hosts.yml from the App
#       key every 10 minutes
#   A4  <name>-image-build.timer, a root timer running the weekly --refresh image build
#   C   <state>/store.img, XFS with reflinks, mounted at <state>/store: the preloaded inner Docker
#       store (golden-<tag>, written by bin/build-runner-image.sh) and the per-job reflink snapshot
#       of it that each slot mounts on its container's /var/lib/docker, so Sysbox has no store to
#       copy at container start (README.md, "Disk")
#   F   the Docker network <name> on the bridge <name>0, inter-container traffic off
#   G   the lane firewall rules (bin/lane-firewall.sh)
#   H   <name>.slice (and its root-level ancestor, which systemd derives from the dash) and one slot
#       template per kind, <name>-<kind>@.service, with Restart=no and no [Install]: only the
#       listener starts a slot; with a qae kind, one Codex store per qae instance, the tracked
#       config.toml its jobs get, and a qae template whose job holds its store's lock; with a wait
#       kind (an org lane's `wait` block: jobs that only poll for another job's result), a template
#       that differs from the ci one only in its container memory and its instance count,
#       wait.slots, a budget of its own
#   I   deletes offline lane registrations no slot is using
#   J   checks the runner image named by <state>/image.env exists (bin/build-runner-image.sh)
#   K   the listener: its binary (built with listener/build.sh only when the module's source
#       changed), /etc/<name>/listener.json and <name>-listener.service, started once the key, the
#       image and (org lane) the runner group exist, restarted when any of them changed
#   L   retires the always-on template <name>@.service a lane ran before its listener, once the
#       listener has started a slot: its instances are disabled and do not restart, idle ones stop
#       now, ones on a job finish first, and the template goes when none runs
#   M   <name>-listener-health.timer, once the listener runs: every 5 minutes a root oneshot
#       (bin/listener-health.sh) restarts a listener that is down or silent, at most once per 30
#       minutes and never for a 401/403, and writes its verdict to /run/<name>-listener-health
#   N   with a qae kind, <name>-codex-keepalive.timer: daily, a root oneshot (bin/lane-slot.sh
#       codex-keepalive) refreshes each qae instance's Codex login that no job has refreshed for 10
#       days, skipping a store a job holds
# Steps end at OPERATOR ACTION gates this script never performs: the App key piped to the machine,
# one Codex login per qae instance's store, the first image build, and for an org lane the App's
# organization permission "Self-hosted runners: Read and write" and the runner group with the
# lane's repositories.
#
# --remove disables the health timer first and the listener next, so nothing restarts what it stops.
# A Codex keepalive already running finishes. It leaves the host's Docker and Sysbox, the store
# filesystem with its preloaded stores, the runner image, the App key, the lane's user and its Codex
# stores, and the (then inert) firewall rules.
# shellcheck disable=SC2153 # the settings are assigned by read_settings (lib/provision.sh)
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck source-path=SCRIPTDIR/.. source=lib/provision.sh
. "$REPO_ROOT/lib/provision.sh"

# Every path is overridable only so the hermetic test can run unprivileged.
UNIT_DIR="${RUNNERS_UNIT_DIR:-/etc/systemd/system}"
SYSBOX_DIR="${RUNNERS_SYSBOX_DIR:-/var/lib/sysbox}"
# The units run the kit in the machine's engine checkout (a clone of the catalog, whose lanes/
# directory is this kit), never the one this script happens to run from.
CHECKOUT="${RUNNERS_ENGINE:-/opt/runner-lanes}/lanes"
HELPER="$CHECKOUT/bin/lane-slot.sh"
TOKEN_REFRESH="$CHECKOUT/bin/lane-token-refresh.py"
LISTENER_HEALTH="$CHECKOUT/bin/listener-health.sh"
IMAGE_BUILD="$CHECKOUT/bin/build-runner-image.sh"
FIREWALL="${RUNNERS_FIREWALL:-$REPO_ROOT/bin/lane-firewall.sh}"
SMOKE="$REPO_ROOT/bin/lane-smoke.sh"
# --check's smoke snapshots the store with the slot helper of this checkout (one implementation).
SLOT_HELPER="$REPO_ROOT/bin/lane-slot.sh"
LISTENER_SRC="$REPO_ROOT/listener"
# How long Phase L waits for the listener's first slot before it retires an always-on template:
# the listener's start, its scale set and session, and one slot start (about 15 s on the smoke).
WARM_WAIT_SEC=180
# How recent the listener's newest journal line must be for --check: it logs a heartbeat a minute.
JOURNAL_MAX_AGE_SEC=180
APPLY=0
CHECK=0
REMOVE=0
HOST=""
LANE=""

usage() {
  cat <<EOF
usage: $0 <host> <lane> [--check | --apply | --remove [--apply]]

<host> names hosts/<host>.yml in the values checkout and <lane> one of its lanes. Default is dry-run
planning. --apply converges the lane. --check is read-only (its smoke proofs start one throwaway
container and remove it) and exits non-zero until the lane is converged. --remove plans the
teardown; --remove --apply performs it.
EOF
}

while [ $# -gt 0 ]; do
  case "$1" in
    --apply) APPLY=1 ;;
    --check) CHECK=1 ;;
    --remove) REMOVE=1 ;;
    -h|--help) usage; exit 0 ;;
    -*) die "unknown arg: $1" ;;
    *)
      if [ -z "$HOST" ]; then HOST="$1"
      elif [ -z "$LANE" ]; then LANE="$1"
      else die "unexpected argument: $1"; fi ;;
  esac
  shift
done
[ -n "$HOST" ] && [ -n "$LANE" ] || { usage >&2; exit 2; }
[[ "$HOST" =~ ^[a-z0-9][a-z0-9-]*$ ]] || die "invalid host '$HOST' (allowed: a-z 0-9 -)"
[ "$CHECK" = "1" ] && { [ "$APPLY" = "1" ] || [ "$REMOVE" = "1" ]; } && die "--check is read-only; do not combine it with --apply or --remove"

if [ "${RUNNERS_ALLOW_NON_ROOT:-0}" != "1" ]; then
  [ "$(id -u)" = "0" ] || die "must run as root (packages, mounts, systemd units, Docker)"
fi
# The host file lives in the machine's values checkout, the owner's private repository.
HOST_YML="${RUNNERS_HOST_YML:-${RUNNERS_CONFIG:-/opt/runner-lanes-config}/hosts/$HOST.yml}"
[ -r "$HOST_YML" ] || die "no host config at $HOST_YML"
read_settings env "$HOST_YML" "$LANE"

STATE_DIR="${RUNNERS_LANE_STATE_DIR:-/var/lib/$NAME}"
RUN_DIR="${RUNNERS_LANE_RUN_DIR:-/run/$NAME}"
ETC_DIR="${RUNNERS_LANE_ETC_DIR:-/etc/$NAME}"
LIB_DIR="${RUNNERS_LANE_LIB_DIR:-/usr/local/lib/$NAME}"
KEY_FILE="${RUNNERS_LANE_KEY_FILE:-$APP_KEY_FILE}"
CODEX_STORE_DIR="${RUNNERS_LANE_CODEX_STORE:-$CODEX_STORE}"
# CODEX_STORES is the lane's Codex login stores, the n-th for qae instance n (lib/lanes.py names
# them), as the machine's paths the printed login commands name. codex_store_dir gives the path this
# script manages for one of them: the same, except under the hermetic test, which moves the first.
codex_store_dir() { printf '%s%s' "$CODEX_STORE_DIR" "${1#"$CODEX_STORE"}"; }
LANE_HOME="$STATE_DIR/home"
IMAGE_ENV="$STATE_DIR/image.env"
STORE_DIR="${RUNNERS_LANE_STORE_DIR:-$STATE_DIR/store}"
STORE_IMG="$STATE_DIR/store.img"
STORE_MOUNT_UNIT="$(mount_unit_name "$STORE_DIR")"
GH_CONFIG="$LANE_HOME/.config/gh"
GH_HOSTS="$GH_CONFIG/hosts.yml"
LISTENER_BIN="$LIB_DIR/listener"
LISTENER_HASH_FILE="$LIB_DIR/listener.sha256"
LISTENER_CONFIG="$ETC_DIR/listener.json"
LISTENER_UNIT="$NAME-listener.service"
CODEX_CONFIG="$ETC_DIR/codex-config.toml"
TOKEN_SERVICE="$NAME-token-refresh.service"
TOKEN_TIMER="$NAME-token-refresh.timer"
IMAGE_BUILD_SERVICE="$NAME-image-build.service"
IMAGE_BUILD_TIMER="$NAME-image-build.timer"
BUILD_LOCK="/run/$NAME-runner-build.lock"
HEALTH_SERVICE="$NAME-listener-health.service"
HEALTH_TIMER="$NAME-listener-health.timer"
KEEPALIVE_SERVICE="$NAME-codex-keepalive.service"
KEEPALIVE_TIMER="$NAME-codex-keepalive.timer"
# The keepalive refreshes a store once it is 10 days old, daily (bin/lane-slot.sh,
# KEEPALIVE_AFTER_SEC), so a store older than this has missed two of its runs.
CODEX_STALE_DAYS=12
# The health check's verdict and restart cooldown (bin/listener-health.sh writes them): beside the
# lane's run dir, which is root's alone.
HEALTH_STATE="${RUNNERS_LANE_HEALTH_DIR:-/run}/$NAME-listener-health"
ALWAYS_ON_TEMPLATE="$NAME@.service"
# Registrations this lane mints, and nothing else: <prefix>-<kind>-<slot>-<epoch> from the listener,
# and <prefix>-<slot>-<epoch> from a retiring always-on slot. A jq filter, built with
# startswith/ltrimstr so no configured text is ever interpreted as a regular expression.
LANE_RUNNER_FILTER=".runners[] | select(.name | startswith(\"$NAME_PREFIX-\") and (ltrimstr(\"$NAME_PREFIX-\") | test(\"^((ci|qae|wait)-)?[0-9]+-[0-9]+\$\")))"

# The lane's App installation session, as gh reads it: the lane user's own hosts.yml (the A3
# timer). gh runs as that user, so it writes nothing root-owned into that user's config.
# GH_CONFIG_DIR is always set, and never an ambient GH_TOKEN or GITHUB_TOKEN: either would
# silently swap the identity.
gh_lane() {
  runuser -u "$LANE_USER" -- env -u GH_TOKEN -u GITHUB_TOKEN HOME="$LANE_HOME" GH_CONFIG_DIR="$GH_CONFIG" GH_PROMPT_DISABLED=1 gh "$@"
}

# gh run as root writes a missing config.yml into the session's directory as root:root 0600, and
# the directory's own user can then not read its config: gh as the lane's user failed with
# "failed to read configuration: ... permission denied" on its first bring-up (2026-09-28). Every
# root-owned entry there goes back to the directory's owner; -h never follows a link. Runs before
# the first GitHub read and at exit, and changes nothing outside --apply.
repair_gh_config() {
  local f
  [ -d "$GH_CONFIG" ] && [ "$(stat -c %u "$GH_CONFIG" 2>/dev/null)" != 0 ] || return 0
  for f in "$GH_CONFIG"/*; do
    [ "$(stat -c %u "$f" 2>/dev/null)" = 0 ] || continue
    log "  repair: $f is root's; it goes back to the owner of $GH_CONFIG"
    run_mutation chown -h --reference="$GH_CONFIG" "$f" || warn "could not hand $f back to the owner of $GH_CONFIG"
  done
}

has_qae() { [ -n "$QAE_LABEL" ]; }

# ---- renderers -------------------------------------------------------------------------------------
render_slice() {
  cat <<EOF
# Managed by the runner lanes kit (bin/provision-lane.sh); do not edit on the machine.
# Aggregate limits for every $NAME container (docker --cgroup-parent) and slot unit (Slice=):
# whatever the per-container caps, the lane together never takes more than this from the machine.
[Unit]
Description=$NAME lane: aggregate limits for its disposable CI containers
Before=slices.target

[Slice]
MemoryMax=$MEMORY_MAX
MemoryHigh=$MEMORY_HIGH
CPUQuota=$CPU_QUOTA
CPUWeight=$CPU_WEIGHT
EOF
}

render_top_slice() {
  cat <<EOF
# Managed by the runner lanes kit (bin/provision-lane.sh); do not edit on the machine.
# systemd nests slices on dashes, so $SLICE lives inside this one. A CPUWeight only competes with
# siblings, so the lane's weight sits here too, where it ranks against system.slice.
[Unit]
Description=Parent of $SLICE
Before=slices.target

[Slice]
CPUWeight=$CPU_WEIGHT
EOF
}

render_store_mount() {
  cat <<EOF
# Managed by the runner lanes kit (bin/provision-lane.sh); do not edit on the machine.
# The lane's inner Docker store: golden-<tag> (the image build's preload) and one reflink snapshot
# of it per running job, mounted on that job's /var/lib/docker. XFS for the reflinks; discard so a
# deleted snapshot gives its blocks back to the sparse image.
[Unit]
Description=Preloaded inner Docker store and per-job snapshots ($NAME lane)
RequiresMountsFor=$STATE_DIR

[Mount]
What=$STORE_IMG
Where=$STORE_DIR
Type=xfs
Options=loop,discard

[Install]
WantedBy=multi-user.target
EOF
}

# The docker run flags a slot uses; --check's smoke container reuses them, minus the slot mounts.
# No --read-only: Sysbox mounts its per-container /var/lib/docker read-only when the rootfs is,
# so the inner dockerd dies ("chmod /var/lib/docker: read-only file system", 2026-09-26). The job
# runs as uid 1001 without sudo, so /etc, /usr, /opt, /var and /root still refuse its writes, and the
# rootfs is discarded with the container (--rm). /tmp is executable like a hosted runner's: Docker's
# tmpfs default is noexec, and a job's stub scripts there failed with "permission denied"
# (2026-09-28). --oom-score-adj 1000 makes the runner's own processes the kernel's first choice
# under memory pressure; Sysbox lets a container lower its own score again, so the slice's hard cap,
# not this flag, is what protects the rest of the machine.
# The memory is the lane's container.memory unless a kind's is passed (the wait kind's wait.memory).
container_flags() {
  printf '%s ' --runtime=sysbox-runc --network "$NAME" --cgroup-parent "$SLICE" \
    --memory "${1:-$CONTAINER_MEMORY}" --pids-limit "$PIDS" --tmpfs "/tmp:size=$TMP_SIZE,exec" \
    --oom-score-adj 1000 --dns 1.1.1.1 --dns 8.8.8.8
}

# A kind's instance count and container memory: the wait kind's own (the lane's wait block, a budget
# apart from `slots`), the lane's for every other kind.
kind_slots() { if [ "$1" = wait ]; then printf '%s' "$WAIT_SLOTS"; else printf '%s' "$SLOTS"; fi; }
kind_memory() { if [ "$1" = wait ]; then printf '%s' "$WAIT_MEMORY"; else printf '%s' "$CONTAINER_MEMORY"; fi; }

# A job sees CPUS_PER_JOB cores, like a GitHub-hosted ubuntu runner's 4 vCPU: without a cpuset every
# container reports the host's cores, and a jest run started 15 workers inside a 4 GB container and
# was killed (2026-09-28). Instance n of each kind is pinned to the n-th 4-core set, wrapping round
# the host's cores; a quota alone would not change what nproc reports.
CPUS_PER_JOB=4
host_cpus() { nproc; }
slot_cpuset() {
  local slot="$1" total start
  total="$(host_cpus)"
  if [ "$total" -le "$CPUS_PER_JOB" ]; then printf '0-%s' "$((total - 1))"; return; fi
  start=$(( ((slot - 1) * CPUS_PER_JOB) % total ))
  [ $((start + CPUS_PER_JOB)) -le "$total" ] || start=$((total - CPUS_PER_JOB))
  printf '%s-%s' "$start" "$((start + CPUS_PER_JOB - 1))"
}

render_cpusets() {
  local n
  for n in $(seq 1 "$1"); do printf 'Environment=KNOWN_CI_CPUSET_%s=%s\n' "$n" "$(slot_cpuset "$n")"; done
}

template_of() { printf '%s-%s@.service' "$NAME" "$1"; }

# Each qae instance's own Codex login store: instance n mounts KNOWN_CI_CODEX_STORE_<n>.
render_codex_stores() {
  local n=0 store
  for store in $CODEX_STORES; do
    n=$((n + 1))
    printf 'Environment=KNOWN_CI_CODEX_STORE_%s=%s\n' "$n" "$(codex_store_dir "$store")"
  done
}

render_unit() {
  local kind="$1" store_env="" store_mount="" store_lock=""
  if [ "$kind" = qae ]; then
    store_env="# One Codex login store per qae instance, mounted into that instance's QAE jobs only: Codex
# rotates a login's refresh token as a job uses it, so two concurrent jobs on one store would retire
# each other's session. systemd expands %i first, then \${KNOWN_CI_CODEX_STORE_<n>}; an instance
# beyond qae_concurrency has no store, and its prepare refuses. prepare resets the store to the
# login and the tracked config before every job, and cleanup gives it back to $LANE_USER.
# The job holds the store's lock, $RUN_DIR/qae/<n>.codex.lock, for its whole run, as prepare and
# cleanup do while they touch the store, so $KEEPALIVE_SERVICE never refreshes a login a job is
# using (bin/lane-slot.sh, \"The Codex store lock\"). flock -F executes docker run in its own place,
# so the unit's main process and exit status stay docker run's; it waits at most 60 s, though only
# the keepalive checking the instance (a moment) can hold the lock then.
$(render_codex_stores)
Environment=KNOWN_CI_CODEX_CONFIG=$CODEX_CONFIG
Environment=KNOWN_CI_CODEX_OWNER=$LANE_USER
"
    store_mount="-v \${KNOWN_CI_CODEX_STORE_%i}:/home/runner/.codex-qae "
    store_lock="/usr/bin/flock -F -w 60 $RUN_DIR/qae/%i.codex.lock "
  fi
  cat <<EOF
# Managed by the runner lanes kit (bin/provision-lane.sh); do not edit on the machine.
# One $kind slot of the $NAME lane. The lane's listener ($LISTENER_UNIT) writes the slot's JIT
# config and job file under $RUN_DIR/$kind/<n>/ and starts this unit for that one job; the
# container takes the job and exits, and the unit stays down until the listener starts it again.
# No [Install]: an instance started at boot would have no job to run. See README.md.
[Unit]
Description=$NAME lane $kind slot %i (one disposable Sysbox container per job)
Requires=docker.service sysbox.service
After=docker.service sysbox.service network-online.target
Wants=network-online.target
RequiresMountsFor=$STATE_DIR $STORE_DIR
# Never refuse a start: the listener may start one instance several times a minute.
StartLimitIntervalSec=0

[Service]
Type=simple
Slice=$SLICE
Environment=KNOWN_CI_NAME=$NAME
Environment=KNOWN_CI_SCOPE=$SCOPE
Environment=KNOWN_CI_SLOT_GB=$SLOT_GB
Environment=KNOWN_CI_BRIDGE=$BRIDGE
Environment=KNOWN_CI_GH_HOSTS=$GH_HOSTS
Environment=KNOWN_CI_STATE_DIR=$STATE_DIR
Environment=KNOWN_CI_STORE_DIR=$STORE_DIR
Environment=KNOWN_CI_RUN_DIR=$RUN_DIR
${store_env}# Each instance's 4-core set (systemd expands %i first, then \${KNOWN_CI_CPUSET_<n>}), so a job
# sees nproc=$CPUS_PER_JOB like a hosted runner.
$(render_cpusets "$(kind_slots "$kind")")
# KNOWN_CI_IMAGE_TAG, written by bin/build-runner-image.sh; read at every start, so a rebuilt image
# (and its preloaded store, which prepare snapshots) is picked up by the next job without touching
# this unit.
EnvironmentFile=$IMAGE_ENV
ExecStartPre=+$HELPER prepare $kind %i
ExecStart=${store_lock}/usr/bin/docker run --rm $(container_flags "$(kind_memory "$kind")")--cpuset-cpus \${KNOWN_CI_CPUSET_%i} -v $STATE_DIR/slot-$kind-%i:/home/runner -v $STORE_DIR/slot-$kind-%i:/var/lib/docker -v $RUN_DIR/$kind/%i/jit:/run/jit:ro ${store_mount}--name $NAME-$kind-%i $IMAGE:\${KNOWN_CI_IMAGE_TAG}
ExecStopPost=+$HELPER cleanup $kind %i
# One job per start: the listener starts the next.
Restart=no
# runtime_max_sec, plus warm_max_age_sec for a lane with a warm pool: systemd counts it from the
# unit's start, and the listener recycles a warm slot once it has been up warm_max_age_sec, so a
# job the slot takes just before still has the whole runtime_max_sec.
RuntimeMaxSec=$SLOT_RUNTIME_MAX
# The slot helper's back-off sleeps up to ten minutes inside ExecStartPre.
TimeoutStartSec=15min
# Also the cleanup's budget (ExecStopPost). Deleting a store snapshot (270k files) took 20 to over
# 60 s on a loaded machine (2026-10-05); at 60 s systemd killed 195 of 332 cleanups, whose
# rm kept running beside the next job's snapshot on the same store while the listener cleared the
# job file the killed cleanup left behind.
TimeoutStopSec=300
EOF
}

render_listener_unit() {
  cat <<EOF
# Managed by the runner lanes kit (bin/provision-lane.sh); do not edit on the machine.
# The $NAME lane's scale-set listener: it keeps the lane's runner scale sets, and starts one
# $NAME-<kind>@<n>.service per job GitHub assigns them (listener/README.md).
[Unit]
Description=$NAME lane: scale-set listener (starts its slots on demand)
After=docker.service sysbox.service network-online.target
Wants=network-online.target

[Service]
Type=simple
ExecStart=$LISTENER_BIN $LISTENER_CONFIG
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
EOF
}

render_token_service() {
  cat <<EOF
# Managed by the runner lanes kit (bin/provision-lane.sh); do not edit on the machine.
# Mints the $NAME lane's GitHub App installation token (an hour's life) from the root-only App key
# and writes it to $LANE_USER's gh hosts.yml (0600) (bin/lane-token-refresh.py).
[Unit]
Description=$NAME lane: refresh $LANE_USER's GitHub App installation token
Wants=network-online.target
After=network-online.target

[Service]
Type=oneshot
ExecStart=/usr/bin/python3 $TOKEN_REFRESH --app-id $APP_ID --installation-id $APP_INSTALLATION_ID --key-file $KEY_FILE --hosts-out $GH_HOSTS --owner $LANE_USER --gh-user $APP_LOGIN
EOF
}

render_token_timer() {
  cat <<EOF
# Managed by the runner lanes kit (bin/provision-lane.sh); do not edit on the machine.
# Every 10 minutes, well inside the token's hour.
[Unit]
Description=$NAME lane: refresh $LANE_USER's GitHub App installation token every 10 minutes

[Timer]
OnBootSec=1min
OnUnitActiveSec=10min

[Install]
WantedBy=timers.target
EOF
}

render_listener_health_service() {
  cat <<EOF
# Managed by the runner lanes kit (bin/provision-lane.sh); do not edit on the machine.
# The $NAME lane's listener health check: restarts $LISTENER_UNIT once, at most every 30 minutes,
# when it is down or its journal is silent (it logs a heartbeat a minute), never for a GitHub
# 401/403, and writes the verdict to $HEALTH_STATE (bin/listener-health.sh).
[Unit]
Description=$NAME lane: check the scale-set listener, restart it once when it is wedged

[Service]
Type=oneshot
ExecStart=$LISTENER_HEALTH $NAME
EOF
}

render_listener_health_timer() {
  cat <<EOF
# Managed by the runner lanes kit (bin/provision-lane.sh); do not edit on the machine.
# Every 5 minutes, so a listener silent for 3 minutes is restarted within 8.
[Unit]
Description=$NAME lane: check the scale-set listener every 5 minutes

[Timer]
OnBootSec=1min
OnUnitActiveSec=5min

[Install]
WantedBy=timers.target
EOF
}

render_image_build_service() {
  cat <<EOF
# Managed by the runner lanes kit (bin/provision-lane.sh); do not edit on the machine.
# The $NAME lane's weekly image rebuild: --refresh builds $IMAGE:<today>-<hash> unless it exists,
# so each week re-reads the preloaded repositories' supabase/config.toml and re-preloads the images
# they start (bin/build-runner-image.sh). --if-provisioned makes it a logged no-op until Sysbox and
# the lane's store exist. It holds the lane's build lock, as every caller of the build does.
[Unit]
Description=$NAME lane: rebuild the runner image $IMAGE
Wants=network-online.target
After=docker.service network-online.target

[Service]
Type=oneshot
WorkingDirectory=$CHECKOUT
ExecStart=/usr/bin/flock -o $BUILD_LOCK $IMAGE_BUILD $HOST $NAME --refresh --if-provisioned
EOF
}

render_image_build_timer() {
  cat <<EOF
# Managed by the runner lanes kit (bin/provision-lane.sh); do not edit on the machine.
# Weekly, up to an hour late at random; Persistent= runs a week the machine was down through at its boot.
[Unit]
Description=$NAME lane: rebuild the runner image $IMAGE weekly

[Timer]
OnCalendar=weekly
Persistent=true
RandomizedDelaySec=1h

[Install]
WantedBy=timers.target
EOF
}

render_codex_config() {
  cat <<EOF
# Managed by the runner lanes kit (bin/provision-lane.sh); do not edit on the machine.
# The $NAME lane's tracked Codex config: bin/lane-slot.sh writes it into the Codex store before
# every QAE job (the QAE harness's Codex action requires a file-backed login store).
cli_auth_credentials_store = "file"
EOF
}

render_keepalive_service() {
  cat <<EOF
# Managed by the runner lanes kit (bin/provision-lane.sh); do not edit on the machine.
# The $NAME lane's Codex login keepalive (bin/lane-slot.sh codex-keepalive): each qae instance's
# store that no job has refreshed for 10 days gets one trivial codex exec as $LANE_USER, which makes
# Codex refresh the login, and is pruned back to auth.json. A store a job holds is skipped. Exits
# non-zero when a store it ran Codex on was not refreshed.
[Unit]
Description=$NAME lane: keep each QAE instance's Codex login fresh
Wants=network-online.target
After=network-online.target

[Service]
Type=oneshot
Environment=KNOWN_CI_NAME=$NAME
Environment=KNOWN_CI_STATE_DIR=$STATE_DIR
Environment=KNOWN_CI_RUN_DIR=$RUN_DIR
$(render_codex_stores)
Environment=KNOWN_CI_CODEX_CONFIG=$CODEX_CONFIG
Environment=KNOWN_CI_CODEX_OWNER=$LANE_USER
ExecStart=$HELPER codex-keepalive
# Each store's Codex run is bounded at 100 s; this bounds the whole pass.
TimeoutStartSec=30min
EOF
}

render_keepalive_timer() {
  cat <<EOF
# Managed by the runner lanes kit (bin/provision-lane.sh); do not edit on the machine.
# Daily, up to an hour late at random; Persistent= runs a day the machine was down through at its boot.
[Unit]
Description=$NAME lane: keep each QAE instance's Codex login fresh, daily

[Timer]
OnCalendar=daily
Persistent=true
RandomizedDelaySec=1h

[Install]
WantedBy=timers.target
EOF
}

render_listener_config() {
  local args=(listener-config "$HOST_YML" "$NAME" --key-file "$KEY_FILE")
  ! has_qae || args+=(--codex-store "$CODEX_STORE_DIR")
  python3 "$REPO_ROOT/lib/lanes.py" "${args[@]}"
}

# ---- reads ------------------------------------------------------------------------------------------
lookup_group() {  # prints the group id, empty when absent; returns 1 when the API is unreadable
  gh_lane api "orgs/$OWNER/actions/runner-groups" --paginate \
    --jq ".runner_groups[] | select(.name == \"$GROUP\") | .id" 2>/dev/null
}

group_repos() {
  gh_lane api "orgs/$OWNER/actions/runner-groups/$1/repositories" --paginate \
    --jq '.repositories[].name' 2>/dev/null | sort
}

# "<scope target> <id> <name> <status> <busy>" per lane registration: the organisation's runners,
# or every installation repository's for a user lane. Fails when any list is unreadable.
lane_runners() {
  local repos repo
  if [ "$SCOPE" = org ]; then
    gh_lane api "orgs/$OWNER/actions/runners" --paginate \
      --jq "$LANE_RUNNER_FILTER | \"$OWNER \(.id) \(.name) \(.status) \(.busy)\"" 2>/dev/null
    return
  fi
  repos="$(gh_lane api installation/repositories --paginate \
    --jq ".repositories[] | select((.owner.login | ascii_downcase) == (\"$OWNER\" | ascii_downcase)) | .full_name" 2>/dev/null)" || return 1
  for repo in $repos; do
    gh_lane api "repos/$repo/actions/runners" --paginate \
      --jq "$LANE_RUNNER_FILTER | \"$repo \(.id) \(.name) \(.status) \(.busy)\"" 2>/dev/null || return 1
  done
}

runners_endpoint() {  # the runners API of a scope target: the organisation, or owner/repo
  case "$1" in */*) printf 'repos/%s/actions/runners' "$1" ;; *) printf 'orgs/%s/actions/runners' "$1" ;; esac
}

# Runner names a slot holds right now: the listener's job files and a retiring always-on slot's
# name file.
current_slot_names() {
  { cat "$RUN_DIR"/*/name 2>/dev/null; awk '{ print $5 }' "$RUN_DIR"/*/*/job 2>/dev/null; } || true
}

image_tag() {  # empty until the lane's first image build writes image.env
  [ -r "$IMAGE_ENV" ] || return 0
  sed -n 's/^KNOWN_CI_IMAGE_TAG=//p' "$IMAGE_ENV" | tail -n 1
}

enabled_always_on_slots() {  # the retiring template's instance numbers with an enablement link
  local link
  for link in "$UNIT_DIR"/multi-user.target.wants/"$NAME"@*.service; do
    [ -e "$link" ] || [ -L "$link" ] || continue
    link="${link##*@}"
    printf '%s\n' "${link%.service}"
  done
}

always_on_slots() { { seq 1 "$SLOTS"; enabled_always_on_slots; } | sort -nu; }

# The listener's source as the build reads it: the module files and the build script, never the
# tests, so a test-only change rebuilds nothing.
listener_source_hash() {
  (
    LC_ALL=C
    cd "$LISTENER_SRC" || exit 1
    for f in build.sh go.mod go.sum *.go; do
      [ -f "$f" ] || continue
      case "$f" in *_test.go) continue ;; esac
      printf '%s %s\n' "$(hash_file "$f")" "$f"
    done
  ) | hash_stdin
}

# ---- apply phases -----------------------------------------------------------------------------------
# The host phases come first (bin/provision-host.sh); a lane on a host without them would mount
# nothing and start no container.
RUNTIME_OK=0
require_host() {
  local missing=""
  command -v jq >/dev/null 2>&1 || missing+="jq, "
  [ -n "$(installed_version docker-ce)" ] || missing+="Docker CE, "
  sysbox_version_pinned "$(installed_version sysbox-ce)" || missing+="sysbox-ce $SYSBOX_VERSION, "
  mountpoint -q "$SYSBOX_DIR" 2>/dev/null || missing+="the Sysbox filesystem on $SYSBOX_DIR, "
  docker info --format '{{json .Runtimes}}' 2>/dev/null | grep -q '"sysbox-runc"' || missing+="Docker's sysbox-runc runtime, "
  [ -z "$missing" ] || die "the host phases have not converged (missing: ${missing%, }); run bin/provision-host.sh $HOST --apply first"
  RUNTIME_OK=1
}

ensure_lane_user() {
  log "Phase A0: the lane's own user $LANE_USER (home $LANE_HOME)"
  run_mutation install -d -m 0710 "$STATE_DIR"
  if getent passwd "$LANE_USER" >/dev/null 2>&1; then
    log "  $LANE_USER present"
  else
    run_mutation useradd --system --user-group --home-dir "$LANE_HOME" --create-home --shell /usr/sbin/nologin "$LANE_USER"
  fi
  run_mutation chgrp "$LANE_USER" "$STATE_DIR"
}

KEY_STATE="absent"
ensure_app_key() {
  log "Phase A1: the App key $KEY_FILE (root, 0600)"
  ensure_dir "$(dirname "$KEY_FILE")" 0700
  if [ -s "$KEY_FILE" ]; then
    KEY_STATE="present"
    run_mutation chmod 0600 "$KEY_FILE"
    run_mutation chown root:root "$KEY_FILE"
  else
    log "  absent: the operator pipes it (see the gate below); the token and image-build timers and the listener wait for it"
  fi
}

ensure_token_timer() {
  log "Phase A3: $TOKEN_TIMER writes $GH_HOSTS every 10 minutes"
  if [ "$KEY_STATE" != present ]; then
    log "  not written: the App key is absent (see the gate below)"
    return 0
  fi
  local changed=""
  changed+="$(converge_file "$UNIT_DIR/$TOKEN_SERVICE" "$(render_token_service)")"
  changed+="$(converge_file "$UNIT_DIR/$TOKEN_TIMER" "$(render_token_timer)")"
  [ -z "$changed" ] || run_mutation systemctl daemon-reload
  run_mutation systemctl enable --now "$TOKEN_TIMER"
  # The image build, the GitHub reads below and the slots read the token now, not in ten minutes.
  [ -s "$GH_HOSTS" ] || run_mutation systemctl start "$TOKEN_SERVICE"
}

ensure_image_build_timer() {
  log "Phase A4: $IMAGE_BUILD_TIMER rebuilds $IMAGE weekly"
  if [ "$KEY_STATE" != present ]; then
    log "  not written: the App key is absent (the build reads the lane's repositories with its token)"
    return 0
  fi
  local changed=""
  changed+="$(converge_file "$UNIT_DIR/$IMAGE_BUILD_SERVICE" "$(render_image_build_service)")"
  changed+="$(converge_file "$UNIT_DIR/$IMAGE_BUILD_TIMER" "$(render_image_build_timer)")"
  [ -z "$changed" ] || run_mutation systemctl daemon-reload
  run_mutation systemctl enable --now "$IMAGE_BUILD_TIMER"
}

ensure_store_storage() {
  log "Phase C: $STORE_DIR on its own ${STORE_GB}G XFS filesystem (reflinks)"
  local changed=""
  if ! mountpoint -q "$STORE_DIR" 2>/dev/null && [ -n "$(ls -A "$STORE_DIR" 2>/dev/null)" ]; then
    die "$STORE_DIR holds files but is not a mount; mounting over it would hide them. Move the directory aside, then re-run."
  fi
  if [ -e "$STORE_IMG" ]; then
    log "  $STORE_IMG present"
  else
    run_mutation truncate -s "${STORE_GB}G" "$STORE_IMG"
    run_mutation mkfs.xfs -q -m reflink=1 "$STORE_IMG"
  fi
  run_mutation install -d -m 0700 "$STORE_DIR"
  changed+="$(converge_file "$UNIT_DIR/$STORE_MOUNT_UNIT" "$(render_store_mount)")"
  [ -n "$changed" ] && run_mutation systemctl daemon-reload
  if mountpoint -q "$STORE_DIR" 2>/dev/null; then
    log "  $STORE_DIR mounted"
    # A changed unit does not touch the live mount, and restarting a .mount unit would unmount
    # under the running slots; a remount applies the options in place.
    [ -z "$changed" ] || run_mutation mount -o remount,discard "$STORE_DIR"
  else
    run_mutation systemctl enable --now "$STORE_MOUNT_UNIT"
  fi
}

ensure_network() {
  log "Phase F: Docker network $NAME on bridge $BRIDGE"
  local bridge
  if bridge="$(docker network inspect "$NAME" --format '{{index .Options "com.docker.network.bridge.name"}}' 2>/dev/null)"; then
    [ "$bridge" = "$BRIDGE" ] || die "network $NAME exists on bridge '${bridge:-default}', not $BRIDGE; the firewall rules name $BRIDGE. Remove it (--remove) and re-run."
    log "  present"
  else
    run_mutation docker network create --driver bridge \
      --opt "com.docker.network.bridge.name=$BRIDGE" \
      --opt com.docker.network.bridge.enable_icc=false "$NAME"
  fi
}

ensure_firewall() {
  log "Phase G: lane firewall rules on $BRIDGE"
  [ -x "$FIREWALL" ] || die "firewall script not executable: $FIREWALL"
  run_mutation env KNOWN_CI_BRIDGE="$BRIDGE" "$FIREWALL"
}

# GitHub reads use the lane user's token, which exists only once the App key does.
GROUP_STATE="none"
resolve_group() {
  [ "$SCOPE" = org ] || return 0
  if [ "$KEY_STATE" != present ]; then GROUP_STATE="no-token"; return 0; fi
  local id
  if id="$(lookup_group)"; then
    # An absent group is a confirmed read: the listener would fail to find it.
    if [ -n "$id" ]; then GROUP_STATE="present"; else GROUP_STATE="absent"; fi
  else
    GROUP_STATE="unreadable"
  fi
}

ensure_units() {
  log "Phase H: $SLICE and the slot templates ($(for kind in $KINDS; do printf '%s ' "$(template_of "$kind")"; done | sed 's/ $//'))"
  [ -x "$HELPER" ] || [ "$APPLY" != "1" ] || die "slot helper not executable at $HELPER (the machine's engine checkout)"
  local changed="" kind store dir
  changed+="$(converge_file "$UNIT_DIR/$SLICE" "$(render_slice)")"
  if [ -n "$TOP_SLICE" ]; then
    changed+="$(converge_file "$UNIT_DIR/$TOP_SLICE" "$(render_top_slice)")"
  fi
  for kind in $KINDS; do
    changed+="$(converge_file "$UNIT_DIR/$(template_of "$kind")" "$(render_unit "$kind")")"
  done
  if [ -n "$changed" ]; then
    run_mutation systemctl daemon-reload
  else
    log "  converged"
  fi
  log "  the templates carry no [Install]: the listener starts <name>-<kind>@<n> for each job, and nothing else does"
  has_qae || return 0
  # Each qae instance's Codex store: created for the lane user, whose `codex login` seeds it. Its
  # ownership after that is the slot helper's (the job's during a QAE job, the lane user's between).
  for store in $CODEX_STORES; do
    dir="$(codex_store_dir "$store")"
    if [ ! -d "$dir" ]; then
      run_mutation install -d -m 0700 "$dir"
      run_mutation chown "$LANE_USER:" "$dir"
    fi
  done
  ensure_dir "$ETC_DIR" 0700
  converge_file "$CODEX_CONFIG" "$(render_codex_config)" >/dev/null
}

sweep_registrations() {
  log "Phase I: offline $NAME registrations no slot is using"
  local rows target id name status busy current
  if [ "$KEY_STATE" != present ]; then
    log "  skipped: no token until the App key is piped"
    return 0
  fi
  if ! rows="$(lane_runners)"; then
    warn "  runner list unreadable; sweep skipped"
    return 0
  fi
  current="$(current_slot_names)"
  while read -r target id name status busy; do
    [ -n "$id" ] || continue
    [ "$status" = offline ] && [ "$busy" = false ] || continue
    grep -qx "$name" <<< "$current" && continue
    run_mutation gh_lane api -X DELETE "$(runners_endpoint "$target")/$id" --silent
  done <<< "$rows"
}

IMAGE_STATE="absent"
# The image build writes the preloaded inner Docker store beside the image, per tag; a job needs both.
store_dir() { printf '%s/golden-%s' "$STORE_DIR" "$1"; }
check_image() {
  log "Phase J: runner image and its preloaded store"
  local tag
  tag="$(image_tag)"
  if [ -z "$tag" ]; then
    IMAGE_STATE="no-tag"
    log "  $IMAGE_ENV names no KNOWN_CI_IMAGE_TAG"
  elif ! docker image inspect "$IMAGE:$tag" >/dev/null 2>&1; then
    log "  $IMAGE:$tag absent"
  elif [ ! -d "$(store_dir "$tag")" ]; then
    IMAGE_STATE="no-store"
    log "  $IMAGE:$tag present, its store $(store_dir "$tag") absent"
  else
    IMAGE_STATE="present"
    log "  $IMAGE:$tag present, store $(store_dir "$tag") present"
  fi
}

# Builds the listener when its source changed since the installed binary; prints "changed" then.
ensure_listener_binary() {
  local want have out bin
  want="$(listener_source_hash)"
  have="$(cat "$LISTENER_HASH_FILE" 2>/dev/null || true)"
  if [ -x "$LISTENER_BIN" ] && [ "$want" = "$have" ]; then
    log "  $LISTENER_BIN current (source $want)" >&2
    return 0
  fi
  if [ "$APPLY" != "1" ]; then
    log "DRY-RUN: build the listener ($LISTENER_SRC/build.sh, source $want) and install it at $LISTENER_BIN" >&2
    echo changed
    return 0
  fi
  log "RUN: $LISTENER_SRC/build.sh (source $want)" >&2
  out="$("$LISTENER_SRC/build.sh")" || die "the listener build failed ($LISTENER_SRC/build.sh needs Docker and the Go module proxy)"
  bin="$(sed -n 's/^binary=//p' <<< "$out" | tail -n 1)"
  [ -n "$bin" ] && [ -x "$bin" ] || die "the listener build printed no binary ($out)"
  install -d -m 0755 "$LIB_DIR"
  install -m 0755 "$bin" "$LISTENER_BIN.new"
  mv -f "$LISTENER_BIN.new" "$LISTENER_BIN"
  printf '%s\n' "$want" > "$LISTENER_HASH_FILE"
  log "  installed $LISTENER_BIN (source $want)" >&2
  echo changed
}

LISTENER_STATE="stopped"
ensure_listener() {
  log "Phase K: the listener $LISTENER_UNIT"
  if [ "$KEY_STATE" != present ]; then
    log "  skipped: the App key $KEY_FILE is absent (see the gate below)"
    return 0
  fi
  local changed="" config state built
  built="$(ensure_listener_binary)" || exit 2
  changed+="$built"
  config="$(render_listener_config)" || die "the listener config could not be rendered from $HOST_YML"
  ensure_dir "$ETC_DIR" 0700
  changed+="$(converge_file "$LISTENER_CONFIG" "$config" 0600)"
  if [ -n "$(converge_file "$UNIT_DIR/$LISTENER_UNIT" "$(render_listener_unit)")" ]; then
    changed+="changed"
    run_mutation systemctl daemon-reload
  fi
  local blocked=""
  [ "$IMAGE_STATE" = present ] || blocked+="image $IMAGE_STATE "
  [ "$SCOPE" != org ] || [ "$GROUP_STATE" = present ] || blocked+="runner group $GROUP_STATE "
  state="$(systemctl is-active "$LISTENER_UNIT" 2>/dev/null || true)"
  if [ -n "$blocked" ]; then
    log "  not started: ${blocked% } (see the gates below)${state:+; it is $state}"
    return 0
  fi
  run_mutation systemctl enable "$LISTENER_UNIT"
  if [ "$state" != active ]; then
    run_mutation systemctl start "$LISTENER_UNIT"
  elif [ -n "$changed" ]; then
    run_mutation systemctl restart "$LISTENER_UNIT"
  else
    log "  running, unchanged"
  fi
  LISTENER_STATE="running"
}

# True once the listener has started a slot: a job file whose unit is active, so the listener's mint
# and the slot's prepare both worked. A lane without a warm pool starts nothing until a job comes,
# so there the listener running is all a wait could show.
listener_serving() {
  [ "$APPLY" = 1 ] || return 0
  [ "$MIN_RUNNERS" -gt 0 ] || return 0
  local i job kind n
  for ((i = 0; i < WARM_WAIT_SEC / 2; i++)); do
    for job in "$RUN_DIR"/*/*/job; do
      [ -e "$job" ] || continue
      n="$(basename "$(dirname "$job")")"
      kind="$(basename "$(dirname "$(dirname "$job")")")"
      [ "$(systemctl is-active "$NAME-$kind@$n.service" 2>/dev/null || true)" = active ] && return 0
    done
    sleep 2
  done
  return 1
}

retire_always_on() {
  log "Phase L: retire the always-on template $ALWAYS_ON_TEMPLATE"
  if [ ! -f "$UNIT_DIR/$ALWAYS_ON_TEMPLATE" ] && [ -z "$(enabled_always_on_slots)" ]; then
    log "  none"
    return 0
  fi
  if [ "$LISTENER_STATE" != running ]; then
    log "  kept: the listener is not running, so the always-on slots keep serving the lane"
    return 0
  fi
  if ! listener_serving; then
    warn "  kept: the listener started no slot within ${WARM_WAIT_SEC}s, so the always-on slots keep serving the lane; read journalctl -u $LISTENER_UNIT, then re-run --apply"
    return 0
  fi
  local slot state name busy="" readable=1 remaining="" rewritten
  # 1. No instance comes back at boot, and none restarts after its current job.
  for slot in $(enabled_always_on_slots); do
    run_mutation systemctl disable "$NAME@$slot.service"
  done
  if [ -f "$UNIT_DIR/$ALWAYS_ON_TEMPLATE" ]; then
    rewritten="$(sed 's/^Restart=always$/Restart=no/' "$UNIT_DIR/$ALWAYS_ON_TEMPLATE")"
    [ -z "$(converge_file "$UNIT_DIR/$ALWAYS_ON_TEMPLATE" "$rewritten")" ] || run_mutation systemctl daemon-reload
  fi
  # 2. Stop the instances whose runner is not on a job; one that is finishes it (RuntimeMaxSec
  #    bounds it) and its cleanup still runs from the template, so the file stays until then.
  if ! busy="$(lane_runners | awk '$5 == "true" { print $3 }')"; then readable=0; fi
  for slot in $(always_on_slots); do
    state="$(systemctl is-active "$NAME@$slot.service" 2>/dev/null || true)"
    unit_running "$state" || continue
    name="$(cat "$RUN_DIR/$slot/name" 2>/dev/null || true)"
    if [ "$readable" = 0 ]; then
      log "  $NAME@$slot keeps running: the runner list is unreadable, so it may be on a job"
      remaining+="$slot "
    elif [ -n "$name" ] && grep -qx "$name" <<< "$busy"; then
      log "  $NAME@$slot is on a job (runner $name): it finishes and does not restart"
      remaining+="$slot "
    else
      run_mutation systemctl stop "$NAME@$slot.service"
    fi
  done
  # 3. The template goes once no instance runs.
  if [ -n "$remaining" ]; then
    log "  $ALWAYS_ON_TEMPLATE stays until $(for slot in $remaining; do printf '%s@%s ' "$NAME" "$slot"; done)finish; re-run --apply to remove it"
  elif [ -f "$UNIT_DIR/$ALWAYS_ON_TEMPLATE" ]; then
    run_mutation rm -f "$UNIT_DIR/$ALWAYS_ON_TEMPLATE"
    run_mutation systemctl daemon-reload
    log "  retired: the listener starts every slot now"
  fi
}

# Only once the listener runs: the health check restarts a listener that is not active, which would
# start one this run held back at a gate above.
ensure_listener_health_timer() {
  log "Phase M: $HEALTH_TIMER checks $LISTENER_UNIT every 5 minutes"
  if [ "$LISTENER_STATE" != running ]; then
    log "  not written: the listener is not running"
    return 0
  fi
  [ -x "$LISTENER_HEALTH" ] || [ "$APPLY" != "1" ] || die "listener health check not executable at $LISTENER_HEALTH (the machine's engine checkout)"
  local changed=""
  changed+="$(converge_file "$UNIT_DIR/$HEALTH_SERVICE" "$(render_listener_health_service)")"
  changed+="$(converge_file "$UNIT_DIR/$HEALTH_TIMER" "$(render_listener_health_timer)")"
  [ -z "$changed" ] || run_mutation systemctl daemon-reload
  run_mutation systemctl enable --now "$HEALTH_TIMER"
}

ensure_codex_keepalive() {
  has_qae || return 0
  log "Phase N: $KEEPALIVE_TIMER keeps each qae instance's Codex login fresh, daily"
  [ -x "$HELPER" ] || [ "$APPLY" != "1" ] || die "slot helper not executable at $HELPER (the machine's engine checkout)"
  local changed=""
  changed+="$(converge_file "$UNIT_DIR/$KEEPALIVE_SERVICE" "$(render_keepalive_service)")"
  changed+="$(converge_file "$UNIT_DIR/$KEEPALIVE_TIMER" "$(render_keepalive_timer)")"
  [ -z "$changed" ] || run_mutation systemctl daemon-reload
  run_mutation systemctl enable --now "$KEEPALIVE_TIMER"
}

# The one-time login of one of the lane's Codex stores ($1, the machine's path), as the operator runs
# it. PATH is pinned to the system directories: /usr/bin/codex is the Codex CLI itself, while
# /usr/local/bin/codex can be another tool's wrapper, which reads another user's home and fails for
# this one.
codex_login_cmd() {
  printf 'sudo -u %s env CODEX_HOME=%s HOME=%s PATH=/usr/bin:/bin codex login --device-auth' "$LANE_USER" "$1" "$LANE_HOME"
}

print_gates() {
  if [ "$KEY_STATE" != present ]; then
    gate "Pipe the lane's App private key to the machine, never through chat: ssh root@$HOST_NAME 'umask 077; cat > $APP_KEY_FILE' < key.pem  Then re-run --apply."
  fi
  if [ "$SCOPE" = org ]; then
    case "$GROUP_STATE" in
      present)
        if [ -z "$(lane_runners 2>/dev/null)" ]; then
          gate "Grant the App $APP_LOGIN the organization permission 'Self-hosted runners: Read and write' in $OWNER if it is not granted yet: the listener's scale sets and their JIT runners need write. Until then the listener fails with HTTP 403 in its journal (journalctl -u $LISTENER_UNIT)."
        fi ;;
      absent)
        gate "Create the organization runner group '$GROUP' in $OWNER (Settings > Actions > Runner groups), visible to selected repositories only: $REPOS. Then re-run --apply." ;;
      unreadable)
        gate "The App $APP_LOGIN cannot read $OWNER's runner groups. Grant it the organization permission 'Self-hosted runners: Read and write' (minting a JIT runner needs write), then re-run --apply."
        gate "If the group '$GROUP' does not exist yet, create it with selected repositories: $REPOS." ;;
    esac
  fi
  local store n=0
  for store in $CODEX_STORES; do
    n=$((n + 1))
    [ -s "$(codex_store_dir "$store")/auth.json" ] && continue
    gate "Log qae instance $n's Codex store $store in once, with a login of its own (a device code opens in your browser; that instance takes no QAE job until then): $(codex_login_cmd "$store")"
  done
  case "$IMAGE_STATE" in
    present) ;;
    *) gate "Build the runner image first: flock -o $BUILD_LOCK $IMAGE_BUILD $HOST $NAME writes $IMAGE_ENV (KNOWN_CI_IMAGE_TAG=<tag>), the $IMAGE:<tag> image and its preloaded store $STORE_DIR/golden-<tag>. Then re-run --apply." ;;
  esac
}

# ---- remove -----------------------------------------------------------------------------------------
remove_slot_files() {  # $1: the instance (<kind>-<n>, or <n> for an always-on slot)
  run_mutation docker rm -f "$NAME-$1"
  if mountpoint -q "$STATE_DIR/slot-$1" 2>/dev/null; then
    run_mutation umount "$STATE_DIR/slot-$1"
  fi
  run_mutation rm -rf "$STATE_DIR/slot-$1" "$STATE_DIR/slot-$1.img" "$STORE_DIR/slot-$1"
}

remove_lane() {
  log "Remove: $NAME lane (listener, scale sets, slot units, slot images, registrations, timers, network)"
  local kind n slot rows target id name status busy
  # The health check first, so it restarts nothing this stops; then the listener, so it starts
  # nothing more and recreates no scale set. A lane whose listener never ran has no health units, and
  # systemctl refuses to disable a unit file that does not exist.
  if [ -f "$UNIT_DIR/$HEALTH_TIMER" ]; then
    run_mutation systemctl disable --now "$HEALTH_TIMER"
    run_mutation systemctl stop "$HEALTH_SERVICE"
  fi
  run_mutation systemctl disable --now "$LISTENER_UNIT"
  if [ -f "$UNIT_DIR/$TOKEN_TIMER" ]; then
    run_mutation systemctl disable --now "$TOKEN_TIMER"
    run_mutation systemctl stop "$TOKEN_SERVICE"
  fi
  # A build already running finishes: its image and store are what --remove leaves in place, and
  # stopping it midway could strand its build container.
  if [ -f "$UNIT_DIR/$IMAGE_BUILD_TIMER" ]; then
    run_mutation systemctl disable --now "$IMAGE_BUILD_TIMER"
  fi
  # A keepalive already running finishes too: stopped between Codex's refresh and its write of
  # auth.json, the store would keep a refresh token the server has already retired.
  if [ -f "$UNIT_DIR/$KEEPALIVE_TIMER" ]; then
    run_mutation systemctl disable --now "$KEEPALIVE_TIMER"
  fi
  for kind in $KINDS; do
    for n in $(seq 1 "$(kind_slots "$kind")"); do
      run_mutation systemctl stop "$NAME-$kind@$n.service"
      remove_slot_files "$kind-$n"
    done
  done
  for slot in $(always_on_slots); do
    run_mutation systemctl disable --now "$NAME@$slot.service"
    remove_slot_files "$slot"
  done
  if [ -x "$LISTENER_BIN" ] && [ -r "$LISTENER_CONFIG" ]; then
    run_mutation "$LISTENER_BIN" -delete-sets "$LISTENER_CONFIG" || warn "some of the lane's scale sets could not be deleted (the listener logged which); delete them on GitHub"
  else
    warn "no listener binary or config ($LISTENER_BIN, $LISTENER_CONFIG): any scale set of this lane stays on GitHub"
  fi
  if rows="$(lane_runners)"; then
    while read -r target id name status busy; do
      [ -n "$id" ] || continue
      run_mutation gh_lane api -X DELETE "$(runners_endpoint "$target")/$id" --silent
    done <<< "$rows"
  else
    warn "runner list unreadable; lane registrations were not deleted (GitHub removes offline ephemeral runners after a day)"
  fi
  local units=("$UNIT_DIR/$LISTENER_UNIT" "$UNIT_DIR/$HEALTH_SERVICE" "$UNIT_DIR/$HEALTH_TIMER" "$UNIT_DIR/$ALWAYS_ON_TEMPLATE" "$UNIT_DIR/$SLICE"
    "$UNIT_DIR/$TOKEN_SERVICE" "$UNIT_DIR/$TOKEN_TIMER" "$UNIT_DIR/$IMAGE_BUILD_SERVICE" "$UNIT_DIR/$IMAGE_BUILD_TIMER"
    "$UNIT_DIR/$KEEPALIVE_SERVICE" "$UNIT_DIR/$KEEPALIVE_TIMER")
  [ -z "$TOP_SLICE" ] || units+=("$UNIT_DIR/$TOP_SLICE")
  for kind in $KINDS; do units+=("$UNIT_DIR/$(template_of "$kind")"); done
  run_mutation rm -f "${units[@]}"
  ! has_qae || run_mutation rm -f "$CODEX_CONFIG"
  run_mutation systemctl daemon-reload
  run_mutation rm -rf "$RUN_DIR"
  run_mutation rm -f "$HEALTH_STATE" "$HEALTH_STATE.restarted"
  run_mutation rm -f "$LISTENER_BIN" "$LISTENER_HASH_FILE" "$LISTENER_CONFIG"
  if docker network inspect "$NAME" >/dev/null 2>&1; then
    run_mutation docker network rm "$NAME"
  fi
  local store kept_stores=""
  for store in $CODEX_STORES; do kept_stores+=" $(codex_store_dir "$store")"; done
  log "Left in place: the host's Docker and Sysbox, $STORE_MOUNT_UNIT ($STORE_IMG) with its preloaded stores, the runner image, the App key ($KEY_FILE), the lane user $LANE_USER$(has_qae && printf ' and its Codex stores%s' "$kept_stores"), and the lane firewall rules (inert without $BRIDGE)."
}

# ---- check ------------------------------------------------------------------------------------------
fail=0
bad() { fail=1; }

check_network() {
  local info icc subnet gateway bridge
  if info="$(docker network inspect "$NAME" --format '{{index .Options "com.docker.network.bridge.name"}} {{index .Options "com.docker.network.bridge.enable_icc"}} {{(index .IPAM.Config 0).Subnet}} {{(index .IPAM.Config 0).Gateway}}' 2>/dev/null)"; then
    read -r bridge icc subnet gateway <<< "$info"
    printf 'network=%s bridge=%s icc=%s subnet=%s gateway=%s\n' "$NAME" "${bridge:-}" "${icc:-}" "${subnet:-}" "${gateway:-}"
    { [ "${bridge:-}" = "$BRIDGE" ] && [ "${icc:-}" = false ]; } || bad
    NETWORK_GATEWAY="${gateway:-}"
  else
    printf 'network=%s absent\n' "$NAME"
    bad
  fi
  local fwd="absent" input="absent"
  nft list chain ip filter DOCKER-USER 2>/dev/null | grep -q "iifname \"$BRIDGE\".*reject.*comment \"ai-fleet-known-ci\"" && fwd="present"
  iptables -C INPUT -i "$BRIDGE" -m comment --comment ai-fleet-known-ci -j REJECT >/dev/null 2>&1 && input="present"
  printf 'firewall docker_user_reject=%s input_reject=%s\n' "$fwd" "$input"
  if [ "$fwd" = present ] && [ "$input" = present ]; then FIREWALL_OK=1; else bad; fi
}

check_storage() {
  local src free worst dir
  if mountpoint -q "$STORE_DIR" 2>/dev/null; then
    src="$(findmnt -n -o SOURCE,FSTYPE,SIZE "$STORE_DIR" 2>/dev/null | tr -s ' ')"
    printf 'store_fs=mounted (%s) mount_unit=%s\n' "${src:-?}" "$(systemctl is-enabled "$STORE_MOUNT_UNIT" 2>/dev/null || echo unknown)"
    case "$src" in *" xfs "*) ;; *) printf 'store_fs_type=not-xfs (reflink snapshots need XFS)\n'; bad ;; esac
  else
    printf 'store_fs=not-mounted\n'
    bad
  fi
  # Before --apply the state directory does not exist yet; its parent is the same filesystem.
  dir="$STATE_DIR"
  [ -d "$dir" ] || dir="$(dirname "$dir")"
  free="$(df -BG --output=avail "$dir" 2>/dev/null | tail -n 1 | tr -dc '0-9')"
  worst=$(( STORE_GB + (SLOTS + WAIT_SLOTS) * SLOT_GB ))
  printf 'disk free=%sG lane_worst_case=%sG (store %sG + %s slots x %sG, all sparse)\n' "${free:-?}" "$worst" "$STORE_GB" "$((SLOTS + WAIT_SLOTS))" "$SLOT_GB"
  if [ -n "$free" ] && [ "$free" -lt "$worst" ]; then
    warn "the lane's filesystems could grow past the free space on $STATE_DIR (${free}G < ${worst}G)"
  fi
}

# systemd renders MemoryMax in bytes; the host config uses its suffix form.
unit_bytes() {
  local n="${1%[KMGT]}" m=1
  case "$1" in *K) m=1024 ;; *M) m=$((1024 ** 2)) ;; *G) m=$((1024 ** 3)) ;; *T) m=$((1024 ** 4)) ;; esac
  echo $((n * m))
}

check_units() {
  local slice_props top_weight kind n running template
  slice_props="$(systemctl show "$SLICE" -p MemoryMax -p MemoryHigh -p CPUQuotaPerSecUSec -p CPUWeight 2>/dev/null | tr '\n' ' ')"
  printf 'slice=%s %s\n' "$SLICE" "${slice_props% }"
  if ! grep -q "CPUWeight=$CPU_WEIGHT " <<< "$slice_props " || ! grep -q "MemoryMax=$(unit_bytes "$MEMORY_MAX") " <<< "$slice_props " \
    || ! grep -q "MemoryHigh=$(unit_bytes "$MEMORY_HIGH") " <<< "$slice_props " || grep -q 'CPUQuotaPerSecUSec=infinity' <<< "$slice_props"; then
    bad
  fi
  if [ -n "$TOP_SLICE" ]; then
    top_weight="$(systemctl show "$TOP_SLICE" -p CPUWeight --value 2>/dev/null || true)"
    printf 'parent_slice=%s CPUWeight=%s\n' "$TOP_SLICE" "${top_weight:-unknown}"
    [ "$top_weight" = "$CPU_WEIGHT" ] || bad
  fi
  for kind in $KINDS; do
    template="$(template_of "$kind")"
    if [ ! -f "$UNIT_DIR/$template" ]; then
      printf 'template=%s absent\n' "$template"; bad
    elif [ "$(cat "$UNIT_DIR/$template")" = "$(render_unit "$kind")" ]; then
      running=""
      for n in $(seq 1 "$(kind_slots "$kind")"); do
        if unit_running "$(systemctl is-active "$NAME-$kind@$n.service" 2>/dev/null || true)"; then running+="$n,"; fi
      done
      running="${running%,}"
      printf 'template=%s current running=%s\n' "$template" "${running:-none}"
    else
      printf 'template=%s DRIFTED from this checkout (re-run --apply)\n' "$template"; bad
    fi
  done
  if [ -f "$UNIT_DIR/$ALWAYS_ON_TEMPLATE" ] || [ -n "$(enabled_always_on_slots)" ]; then
    printf 'always_on_template=%s present (retiring: re-run --apply once the listener serves and its slots finish)\n' "$ALWAYS_ON_TEMPLATE"; bad
  else
    printf 'always_on_template=absent\n'
  fi
  if [ -x "$HELPER" ]; then printf 'helper=%s\n' "$HELPER"; else printf 'helper=%s missing\n' "$HELPER"; bad; fi
}

check_timer() {  # $1: the key, $2: the timer unit
  local state
  state="$(systemctl is-active "$2" 2>/dev/null || true)"
  printf '%s=%s %s\n' "$1" "$2" "${state:-unknown}"
  [ "$state" = active ] || bad
}

check_listener() {
  local state stamp age installed warm=0 job verdict
  if [ -s "$KEY_FILE" ]; then
    if [ -n "$(find "$KEY_FILE" -maxdepth 0 -perm 0600 2>/dev/null)" ]; then
      printf 'app_key=%s present mode=0600\n' "$KEY_FILE"
    else
      printf 'app_key=%s present mode=not-0600 (chmod 0600 it)\n' "$KEY_FILE"; bad
    fi
  else
    printf 'app_key=%s absent\n' "$KEY_FILE"; bad
  fi
  installed="$(cat "$LISTENER_HASH_FILE" 2>/dev/null || true)"
  if [ ! -x "$LISTENER_BIN" ]; then
    printf 'listener_binary=%s absent\n' "$LISTENER_BIN"; bad
  elif [ "$installed" = "$(listener_source_hash)" ]; then
    printf 'listener_binary=%s current (source %s)\n' "$LISTENER_BIN" "$installed"
  else
    printf 'listener_binary=%s stale: built from %s, this checkout is %s (re-run --apply)\n' "$LISTENER_BIN" "${installed:-unknown}" "$(listener_source_hash)"; bad
  fi
  if [ -f "$LISTENER_CONFIG" ] && jq -e . "$LISTENER_CONFIG" >/dev/null 2>&1; then
    printf 'listener_config=%s parseable\n' "$LISTENER_CONFIG"
  else
    printf 'listener_config=%s absent or not JSON\n' "$LISTENER_CONFIG"; bad
  fi
  if [ ! -f "$UNIT_DIR/$LISTENER_UNIT" ]; then
    printf 'listener_unit=%s absent\n' "$LISTENER_UNIT"; bad
  elif [ "$(cat "$UNIT_DIR/$LISTENER_UNIT")" != "$(render_listener_unit)" ]; then
    printf 'listener_unit=%s DRIFTED from this checkout (re-run --apply)\n' "$LISTENER_UNIT"; bad
  fi
  state="$(systemctl is-active "$LISTENER_UNIT" 2>/dev/null || true)"
  stamp="$(journalctl -u "$LISTENER_UNIT" -n 1 -o short-unix --no-pager -q 2>/dev/null | awk 'NR == 1 { print $1 }')"
  stamp="${stamp%%.*}"
  if [[ "$stamp" =~ ^[0-9]+$ ]]; then age=$(( $(date +%s) - stamp )); else age=""; fi
  printf 'listener=%s journal_newest_line_age_s=%s\n' "${state:-unknown}" "${age:-none}"
  [ "$state" = active ] || bad
  { [ -n "$age" ] && [ "$age" -le "$JOURNAL_MAX_AGE_SEC" ]; } || { printf 'listener_journal=silent for more than %ss (it logs a heartbeat every minute)\n' "$JOURNAL_MAX_AGE_SEC"; bad; }
  check_timer listener_health_timer "$HEALTH_TIMER"
  verdict="$(head -n 1 "$HEALTH_STATE" 2>/dev/null || true)"
  printf 'listener_health=%s\n' "${verdict:-absent (the timer has not run yet)}"
  case "$verdict" in unhealthy*) bad ;; esac
  if [ "$SCOPE" = org ] && [ "$MIN_RUNNERS" -gt 0 ]; then
    for job in "$RUN_DIR"/ci/*/job; do [ -e "$job" ] && warm=$((warm + 1)); done
    if [ "$warm" -ge "$MIN_RUNNERS" ]; then
      printf 'warm_pool=%s slots hold a job file (min_runners %s)\n' "$warm" "$MIN_RUNNERS"
    else
      printf 'warm_pool=%s of %s: filling (the listener starts them as the budget and memory admit)\n' "$warm" "$MIN_RUNNERS"
    fi
  fi
  check_timer token_timer "$TOKEN_TIMER"
  check_timer image_build_timer "$IMAGE_BUILD_TIMER"
  if [ -n "$(find "$GH_HOSTS" -maxdepth 0 -mmin -15 2>/dev/null)" ]; then
    printf 'token_file=%s younger than 15 minutes\n' "$GH_HOSTS"
  else
    printf 'token_file=%s absent or older than 15 minutes\n' "$GH_HOSTS"; bad
  fi
  has_qae || return 0
  if [ "$(cat "$CODEX_CONFIG" 2>/dev/null)" = "$(render_codex_config)" ]; then
    printf 'codex_config=%s current\n' "$CODEX_CONFIG"
  else
    printf 'codex_config=%s absent or DRIFTED (re-run --apply)\n' "$CODEX_CONFIG"; bad
  fi
  check_timer codex_keepalive_timer "$KEEPALIVE_TIMER"
  local store dir state stamp epoch age days
  # A oneshot is inactive after a run that succeeded (or none yet), failed after one that did not.
  state="$(systemctl is-active "$KEEPALIVE_SERVICE" 2>/dev/null || true)"
  printf 'codex_keepalive=%s %s\n' "$KEEPALIVE_SERVICE" "${state:-unknown}"
  if [ "$state" = failed ]; then
    printf 'codex_keepalive_failed=its last run did not refresh every store it ran Codex on (journalctl -u %s)\n' "$KEEPALIVE_SERVICE"; bad
  fi
  for store in $CODEX_STORES; do
    dir="$(codex_store_dir "$store")"
    if [ ! -s "$dir/auth.json" ]; then
      printf 'codex_login=absent: %s\n' "$(codex_login_cmd "$store")"; bad
      continue
    fi
    # Only last_refresh is read from the login, never a token.
    if ! stamp="$(jq -er '.last_refresh | strings' "$dir/auth.json" 2>/dev/null)" || ! epoch="$(date -d "$stamp" +%s 2>/dev/null)"; then
      printf 'codex_login=%s/auth.json present last_refresh=unreadable (not a ChatGPT login Codex refreshes)\n' "$dir"; bad
      continue
    fi
    age=$(( $(date +%s) - epoch ))
    days="$(awk -v s="$age" 'BEGIN { printf "%.1f", s / 86400 }')"
    printf 'codex_login=%s/auth.json present last_refresh=%s age_days=%s\n' "$dir" "$stamp" "$days"
    if [ "$age" -gt $(( CODEX_STALE_DAYS * 86400 )) ]; then
      printf 'codex_login_stale=%s last refreshed %s days ago, past %s: the keepalive refreshes a store at 10 days (journalctl -u %s)\n' "$dir" "$days" "$CODEX_STALE_DAYS" "$KEEPALIVE_SERVICE"; bad
    fi
  done
}

check_image_state() {
  local tag created
  tag="$(image_tag)"
  if [ -z "$tag" ]; then
    printf 'image=%s tag=unset (%s)\n' "$IMAGE" "$IMAGE_ENV"; bad; return
  fi
  if created="$(docker image inspect "$IMAGE:$tag" --format '{{.Created}}' 2>/dev/null)"; then
    printf 'image=%s:%s present created=%s\n' "$IMAGE" "$tag" "$created"
    IMAGE_STATE="present"
  else
    printf 'image=%s:%s absent\n' "$IMAGE" "$tag"; bad
  fi
  if [ -d "$(store_dir "$tag")" ]; then
    printf 'store=%s present size=%s\n' "$(store_dir "$tag")" "$(du -sh "$(store_dir "$tag")" 2>/dev/null | cut -f1)"
    STORE_STATE="present"
  else
    printf 'store=%s absent (bin/build-runner-image.sh writes it)\n' "$(store_dir "$tag")"; bad
  fi
}

check_github() {
  local id repos want rows online=0 busy=0 offline=0 leftovers="" current r_id r_name r_status r_busy
  if [ "$SCOPE" = org ]; then
    if ! id="$(lookup_group)"; then
      printf 'runner_group=%s unreadable (grant the App "Self-hosted runners: Read and write")\n' "$GROUP"; bad
      return
    fi
    if [ -z "$id" ]; then
      printf 'runner_group=%s absent\n' "$GROUP"; bad; return
    fi
    repos="$(group_repos "$id" | tr '\n' ' ')"
    want="$(tr ' ' '\n' <<< "$REPOS" | sort | tr '\n' ' ')"
    printf 'runner_group=%s id=%s repositories=%s\n' "$GROUP" "$id" "${repos% }"
    [ "$repos" = "$want" ] || { printf 'runner_group_repositories_expected=%s\n' "${want% }"; bad; }
  fi
  if rows="$(lane_runners)"; then
    current="$(current_slot_names)"
    while read -r _ r_id r_name r_status r_busy; do
      [ -n "$r_id" ] || continue
      if [ "$r_busy" = true ]; then busy=$((busy + 1));
      elif [ "$r_status" = online ]; then online=$((online + 1));
      else
        offline=$((offline + 1))
        grep -qx "$r_name" <<< "$current" || leftovers+="$r_name "
      fi
    done <<< "$rows"
    printf 'registrations online=%s busy=%s offline=%s offline_leftovers=%s\n' "$online" "$busy" "$offline" "${leftovers:-none}"
  else
    printf 'registrations=unreadable\n'; bad
  fi
}

host_addresses() {
  PUBLIC_IP="$(ip -4 -o route get 1.1.1.1 2>/dev/null | awk '{for (i = 1; i < NF; i++) if ($i == "src") { print $(i+1); exit }}')"
  TAILNET_IP="$(ip -4 -o addr show dev tailscale0 2>/dev/null | awk '{ split($4, a, "/"); print a[1]; exit }')"
}

# The smoke's store snapshot, made and removed by the slot helper as a slot's is; the helper reads
# the golden store's tag from KNOWN_CI_IMAGE_TAG.
smoke_snapshot() {
  env KNOWN_CI_NAME="$NAME" KNOWN_CI_STATE_DIR="$STATE_DIR" KNOWN_CI_STORE_DIR="$STORE_DIR" \
    KNOWN_CI_RUN_DIR="$RUN_DIR" KNOWN_CI_IMAGE_TAG="$2" "$SLOT_HELPER" "$1" "smoke-$$"
}

check_smoke() {
  if [ "$RUNTIME_OK" != 1 ] || [ "${FIREWALL_OK:-0}" != 1 ] || [ "$IMAGE_STATE" != present ] || [ "${STORE_STATE:-}" != present ] || [ -z "${NETWORK_GATEWAY:-}" ]; then
    printf 'smoke=skipped (needs the sysbox runtime, the %s network, its firewall rules, the image and its store)\n' "$NAME"
    bad
    return
  fi
  host_addresses
  local tag out started ready line name result detail expected missing="" failed="" snapshot_started port
  tag="$(image_tag)"
  snapshot_started="$(date +%s)"
  if ! smoke_snapshot snapshot "$tag" 2>&1 | sed 's/^/smoke /'; then
    printf 'smoke=FAIL store snapshot failed\n'
    bad
    return
  fi
  started="$(date +%s)"
  printf 'smoke store_snapshot_s=%s (reflink snapshot of %s)\n' "$(( started - snapshot_started ))" "$(store_dir "$tag")"
  # shellcheck disable=SC2046 # container_flags is a word list by design
  out="$(timeout 1800 docker run --rm -i $(container_flags)--cpuset-cpus "$(slot_cpuset 1)" -e "KNOWN_CI_CPUS=$CPUS_PER_JOB" --tmpfs /home/runner:size=1g \
    -v "$STORE_DIR/smoke-$$:/var/lib/docker" \
    --name "$NAME-smoke-$$" --entrypoint /bin/bash "$IMAGE:$tag" -s -- \
    "$NETWORK_GATEWAY" "${PUBLIC_IP:--}" "${TAILNET_IP:--}" "$CHECK_PORTS" < "$SMOKE" 2>&1)" || true
  smoke_snapshot discard "$tag" || warn "could not remove the smoke's store snapshot $STORE_DIR/smoke-$$"
  while read -r line name result detail; do
    [ "$line" = probe ] || continue
    printf 'smoke %s %s %s\n' "$name" "$result" "$detail"
    [ "$result" = fail ] && failed+="$name "
    if [ "$name" = container-ready ] && [ "$result" = ok ]; then ready="$detail"; fi
  done <<< "$out"
  [ -n "${ready:-}" ] && printf 'smoke container_ready_s=%s (docker run to first line; the store is mounted, so Sysbox copies nothing)\n' "$(( ready - started ))"
  expected="container-ready net-metadata net-github write-etc write-usr write-home/runner write-tmp cpus tmp-exec psql inner-docker inner-pulls"
  for port in $CHECK_PORTS; do expected+=" net-gateway-$port"; done
  [ -n "${PUBLIC_IP:-}" ] && for port in $CHECK_PORTS; do expected+=" net-public-$port"; done
  [ -n "${TAILNET_IP:-}" ] && for port in $CHECK_PORTS; do expected+=" net-tailnet-$port"; done
  for name in $expected; do
    grep -q "^probe $name " <<< "$out" || missing+="$name "
  done
  if [ -n "$failed" ] || [ -n "$missing" ]; then
    printf 'smoke=FAIL failed=%s missing=%s\n' "${failed:-none}" "${missing:-none}"
    [ -n "$missing" ] && printf 'smoke_output_tail=%s\n' "$(tail -n 5 <<< "$out" | tr '\n' ' ' | cut -c1-400)"
    bad
  else
    printf 'smoke=PASS\n'
  fi
}

check_report() {
  log "Check mode: host=$HOST lane=$NAME scope=$SCOPE owner=$OWNER kinds=[$KINDS] slots=$SLOTS${WAIT_LABEL:+ wait_slots=$WAIT_SLOTS} image=$IMAGE"
  check_network
  check_storage
  check_units
  check_listener
  check_image_state
  check_github
  check_smoke
  [ "$fail" = 0 ]
}

log "host=$HOST lane=$NAME scope=$SCOPE owner=$OWNER kinds=[$KINDS] slots=$SLOTS${WAIT_LABEL:+ wait_slots=$WAIT_SLOTS}${GROUP:+ group=$GROUP repos=[$REPOS]}${EXCLUDE:+ exclude=[$EXCLUDE]} apply=$APPLY check=$CHECK remove=$REMOVE"
take_apply_lock
repair_gh_config
trap repair_gh_config EXIT
if [ "$REMOVE" = "1" ]; then
  remove_lane
  log "done"
  exit 0
fi
require_host
if [ "$CHECK" = "1" ]; then
  check_report || exit 1
  exit 0
fi
ensure_lane_user
ensure_app_key
ensure_token_timer
ensure_image_build_timer
ensure_store_storage
ensure_network
ensure_firewall
resolve_group
ensure_units
sweep_registrations
check_image
ensure_listener
retire_always_on
ensure_listener_health_timer
ensure_codex_keepalive
print_gates
log "done"
