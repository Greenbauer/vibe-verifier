#!/usr/bin/env bash
# provision-lane.sh: provision one disposable-container lane of a machine.
#
#   provision-lane.sh <host> <lane> [--check | --apply | --remove [--apply] | --convert-store [--apply]]
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
# runs it with --apply), --convert-store plans the move of the lane's store from XFS to btrfs (and
# makes it with --apply). runners-pull.service runs --apply for every lane after every change to
# the kit or to the machine's values.
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
#       copy at container start (README.md, "Disk"). With store_fs: btrfs in the host file a new
#       store is btrfs instead, and a job's store a snapshot of the golden-<tag> subvolume. A store
#       that exists is mounted as what it is and never converted here: one that differs from the
#       host file ends at a gate, and --check fails until --convert-store has run
#   D   <state>/store/trash, where a slot retires its store copy with a rename instead of deleting
#       it in its stop path, and <name>-store-reaper.timer: a root oneshot (bin/lane-slot.sh reap)
#       deletes the retired copies (one at a time, or a few at a time while the store is under
#       pressure), started by every slot's cleanup and by the timer every 5 minutes. A reaper that
#       is running when its unit changes is restarted. Before the templates (H), so the trash and
#       the reaper are there before a slot of a rewritten template can finish
#   F   the Docker network <name> on the bridge <name>0, inter-container traffic off
#   G   the lane firewall rules (bin/lane-firewall.sh)
#   H   <name>.slice (and its root-level ancestor, which systemd derives from the dash) and one slot
#       template per kind, <name>-<kind>@.service, with Restart=no and no [Install]: only the
#       listener starts a slot; with a qae kind, one Codex store per qae instance, the tracked
#       config.toml its jobs get, and a qae template whose job holds its store's lock; with a wait
#       kind (an org lane's `wait` block: jobs that only poll for another job's result), a template
#       that differs from the ci one only in its container memory and its instance count,
#       wait.slots, a budget of its own (the slot helper gives a wait job an empty inner Docker
#       store where a ci job gets a snapshot)
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
# --convert-store moves the lane's store from XFS to btrfs, for a lane whose host file says
# store_fs: btrfs. It takes the lane off its jobs while it runs: it stops the listener, stops the
# slots that are not on a job, waits (bounded) for the ones that are, swaps a new btrfs image in at
# the store's path, copies every preloaded store an image still names out of the XFS image, mounted
# read-only beside it, and starts the listener again once a snapshot of the current one can be made
# and deleted. The XFS image is moved aside and never deleted. Whatever fails before that last
# check, the XFS store is put back and the listener restarted (README.md, "Converting a lane's
# store").
#
# --remove disables the health timer first and the listener next, so nothing restarts what it stops.
# A Codex keepalive already running finishes. Once every slot has stopped it stops the store reaper
# and empties the store's trash. It leaves the host's Docker and Sysbox, the store filesystem with
# its preloaded stores, the runner image, the App key, the lane's user and its Codex stores, and the
# (then inert) firewall rules.
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
# How long --convert-store waits for the slots that are on a job once the listener is stopped. The
# lane takes no job all that time, so past it the conversion gives up, with nothing changed, rather
# than hold the lane down for the length of its longest job.
CONVERT_WAIT_SEC=900
APPLY=0
CHECK=0
REMOVE=0
CONVERT=0
HOST=""
LANE=""

usage() {
  cat <<EOF
usage: $0 <host> <lane> [--check | --apply | --remove [--apply] | --convert-store [--apply]]

<host> names hosts/<host>.yml in the values checkout and <lane> one of its lanes. Default is dry-run
planning. --apply converges the lane. --check is read-only (its smoke proofs start one throwaway
container and remove it) and exits non-zero until the lane is converged. --remove plans the
teardown; --remove --apply performs it. --convert-store plans the move of the lane's store from XFS
to btrfs, for a lane whose host file says store_fs: btrfs; --convert-store --apply makes it, and
the lane takes no job meanwhile.
EOF
}

while [ $# -gt 0 ]; do
  case "$1" in
    --apply) APPLY=1 ;;
    --check) CHECK=1 ;;
    --remove) REMOVE=1 ;;
    --convert-store) CONVERT=1 ;;
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
[ "$CHECK" = "1" ] && { [ "$APPLY" = "1" ] || [ "$REMOVE" = "1" ] || [ "$CONVERT" = "1" ]; } && die "--check is read-only; do not combine it with --apply, --remove or --convert-store"
[ "$REMOVE" = "1" ] && [ "$CONVERT" = "1" ] && die "--remove and --convert-store are two different runs"

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
BUILD_LOCK="${RUNNERS_LANE_BUILD_LOCK:-/run/$NAME-runner-build.lock}"
HEALTH_SERVICE="$NAME-listener-health.service"
HEALTH_TIMER="$NAME-listener-health.timer"
KEEPALIVE_SERVICE="$NAME-codex-keepalive.service"
KEEPALIVE_TIMER="$NAME-codex-keepalive.timer"
# Where the slots retire their store copies, and the reaper that deletes them. The slot helper
# derives both names the same way (bin/lane-slot.sh, TRASH_DIR and REAPER_UNIT), from the store and
# the lane name alone, so a slot started before this run reaches them too.
TRASH_DIR="$STORE_DIR/trash"
REAPER_SERVICE="$NAME-store-reaper.service"
REAPER_TIMER="$NAME-store-reaper.timer"
# How old the trash's oldest entry may be before --check calls the reaper stuck. The reaper is
# started at least every 5 minutes (its timer), and one delete took 9 s alone and at most 121 s
# when eight ran at once (2026-10-07), so an entry 30 minutes old has waited six timer periods and
# some fifteen of the slowest deletes: the reaper is failing, wedged, or slower than the lane
# retires copies.
TRASH_STALE_SEC=1800
# --convert-store keeps the XFS image under this name, and reads it at this mount point.
OLD_STORE_IMG="$STORE_IMG.xfs-$(date -u +%Y%m%dT%H%M%SZ)"
OLD_STORE_DIR="$STATE_DIR/store-xfs"
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

# The store's mount options after `loop`. XFS: discard, so a deleted snapshot gives its blocks back
# to the sparse image. btrfs: noatime, because under the default (relatime) the first read of a
# file in a fresh snapshot updates its access time and so copies its metadata, which btrfs(5) names
# as relatime's worst case (many files, older than a day, read just after a snapshot), and that is
# how every job starts; and discard=async, for the same blocks back to the sparse image: btrfs(5)
# calls it the preferred mode, gathering freed extents into larger chunks before the TRIM, where
# the synchronous mode (a plain discard) can degrade performance.
store_mount_options() { if [ "$1" = btrfs ]; then printf 'noatime,discard=async'; else printf 'discard'; fi; }

render_store_mount() {  # $1: the store's filesystem, xfs or btrfs
  local what
  if [ "$1" = btrfs ]; then
    what="# The lane's inner Docker store: golden-<tag> (the image build's preload, a subvolume) and one
# snapshot of it per running job, mounted on that job's /var/lib/docker. btrfs for the snapshots;
# the options are explained at store_mount_options in bin/provision-lane.sh."
  else
    what="# The lane's inner Docker store: golden-<tag> (the image build's preload) and one reflink snapshot
# of it per running job, mounted on that job's /var/lib/docker. XFS for the reflinks; discard so a
# deleted snapshot gives its blocks back to the sparse image."
  fi
  cat <<EOF
# Managed by the runner lanes kit (bin/provision-lane.sh); do not edit on the machine.
$what
[Unit]
Description=Preloaded inner Docker store and per-job snapshots ($NAME lane)
RequiresMountsFor=$STATE_DIR

[Mount]
What=$STORE_IMG
Where=$STORE_DIR
Type=$1
Options=loop,$(store_mount_options "$1")

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
# Also the cleanup's budget (ExecStopPost). The cleanup renames the job's store into
# $TRASH_DIR for $REAPER_SERVICE and deletes nothing of it; what can still take minutes is a
# qae store's lock (120 s) and a store the trash could not take, which is deleted here. Deleting one
# (270k files) took 20 to over 60 s on a loaded machine (2026-10-05), and at 60 s systemd killed 195
# of 332 cleanups, whose rm kept running beside the next job's snapshot on the same store.
TimeoutStopSec=300
EOF
}

render_reaper_service() {
  cat <<EOF
# Managed by the runner lanes kit (bin/provision-lane.sh); do not edit on the machine.
# The $NAME lane's store reaper (bin/lane-slot.sh reap): deletes the store copies its slots retired
# into $TRASH_DIR under one lock: one at a time, or a few at a time while the store is under
# pressure (REAP_JOBS in that script). Every slot's cleanup starts it without waiting (systemctl
# start --no-block), and $REAPER_TIMER does, so no copy is left there by a start that was
# missed or by a reboot. No start timeout (a oneshot has none): emptying a backlog is its job, and a
# killed run would only begin again where it stopped. A run that never ends replaces itself now
# and then, between deletes, with the helper on disk (REAP_MAX_SEC there), so a pulled helper
# reaches it.
[Unit]
Description=$NAME lane: delete the store copies its slots retired
RequiresMountsFor=$STORE_DIR
# Never refuse a start: a cleanup starts it after every job, several times within seconds when jobs
# end together, and a refused start would leave a copy in the trash until the timer.
StartLimitIntervalSec=0

[Service]
Type=oneshot
Environment=KNOWN_CI_NAME=$NAME
Environment=KNOWN_CI_STORE_DIR=$STORE_DIR
ExecStart=$HELPER reap
# The lowest CPU priority, so a delete yields to everything else it shares a core with. It runs
# outside $SLICE on purpose: the lane's CPUQuota and MemoryHigh throttle what is in the slice, and
# the one thing that gives the store its space back must not stall with the jobs that fill it.
Nice=19
EOF
}

render_reaper_timer() {
  cat <<EOF
# Managed by the runner lanes kit (bin/provision-lane.sh); do not edit on the machine.
# A minute after boot, then every 5 minutes, counted from the reaper's last activation: the
# backstop for a copy no cleanup's start reached, not the reaper's usual trigger.
[Unit]
Description=$NAME lane: delete retired store copies, every 5 minutes

[Timer]
OnBootSec=1min
OnUnitActiveSec=5min

[Install]
WantedBy=timers.target
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

# The store's filesystem as it is on the machine, whatever the host file says: what is mounted at
# its path, else what its image holds; empty when the lane has no store yet, "unknown" for an image
# that holds nothing blkid knows.
found_store_fs() {
  local fs
  if mountpoint -q "$STORE_DIR" 2>/dev/null; then
    fs="$(findmnt -n -o FSTYPE "$STORE_DIR" 2>/dev/null || true)"
  elif [ -e "$STORE_IMG" ]; then
    fs="$(blkid -p -o value -s TYPE "$STORE_IMG" 2>/dev/null || true)"
  else
    return 0
  fi
  printf '%s\n' "${fs:-unknown}"
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
  [ "$STORE_FS" != btrfs ] || [ -n "$(installed_version btrfs-progs)" ] || missing+="btrfs-progs, "
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

make_store_fs() {  # $1: xfs or btrfs, $2: the image
  if [ "$1" = btrfs ]; then
    # mkfs.btrfs's own defaults: no option of it has been measured on a lane, so none is set.
    run_mutation mkfs.btrfs -q "$2"
  else
    run_mutation mkfs.xfs -q -m reflink=1 "$2"
  fi
}

# A store that exists is mounted as what it is, never as what the host file says: the slot helper
# and the image build follow the filesystem they find, so a lane whose host file changed keeps
# running, and the move to the other filesystem is the operator's (the gate below, --convert-store).
STORE_FOUND=""
ensure_store_storage() {
  local changed="" fs
  STORE_FOUND="$(found_store_fs)"
  fs="${STORE_FOUND:-$STORE_FS}"
  case "$fs" in
    xfs) log "Phase C: $STORE_DIR on its own ${STORE_GB}G XFS filesystem (reflinks)" ;;
    btrfs) log "Phase C: $STORE_DIR on its own ${STORE_GB}G btrfs filesystem (snapshots)" ;;
    *) die "the lane's store ($STORE_DIR, from $STORE_IMG) is on a filesystem that is neither XFS nor btrfs ($fs); this kit made no such store. Move it aside, then re-run." ;;
  esac
  [ "$fs" = "$STORE_FS" ] || log "  the store is $fs and the host file says $STORE_FS: it stays as it is (see the gate below)"
  if ! mountpoint -q "$STORE_DIR" 2>/dev/null && [ -n "$(ls -A "$STORE_DIR" 2>/dev/null)" ]; then
    die "$STORE_DIR holds files but is not a mount; mounting over it would hide them. Move the directory aside, then re-run."
  fi
  if [ -e "$STORE_IMG" ]; then
    log "  $STORE_IMG present"
  else
    run_mutation truncate -s "${STORE_GB}G" "$STORE_IMG"
    make_store_fs "$fs" "$STORE_IMG"
  fi
  run_mutation install -d -m 0700 "$STORE_DIR"
  changed+="$(converge_file "$UNIT_DIR/$STORE_MOUNT_UNIT" "$(render_store_mount "$fs")")"
  [ -n "$changed" ] && run_mutation systemctl daemon-reload
  if mountpoint -q "$STORE_DIR" 2>/dev/null; then
    log "  $STORE_DIR mounted"
    # A changed unit does not touch the live mount, and restarting a .mount unit would unmount
    # under the running slots; a remount applies the options in place.
    [ -z "$changed" ] || run_mutation mount -o "remount,$(store_mount_options "$fs")" "$STORE_DIR"
  else
    run_mutation systemctl enable --now "$STORE_MOUNT_UNIT"
  fi
}

# The trash first, then the reaper's units, then its timer: a slot may retire a copy the moment the
# directory exists (the helper also makes it on demand), and from the timer's first run on nothing
# stays there. Nothing here touches a running slot.
ensure_store_reaper() {
  log "Phase D: $TRASH_DIR, and $REAPER_TIMER deletes the store copies the slots retire there"
  [ -x "$HELPER" ] || [ "$APPLY" != "1" ] || die "slot helper not executable at $HELPER (the machine's engine checkout)"
  ensure_dir "$TRASH_DIR" 0700
  local changed=""
  changed+="$(converge_file "$UNIT_DIR/$REAPER_SERVICE" "$(render_reaper_service)")"
  changed+="$(converge_file "$UNIT_DIR/$REAPER_TIMER" "$(render_reaper_timer)")"
  [ -z "$changed" ] || run_mutation systemctl daemon-reload
  run_mutation systemctl enable --now "$REAPER_TIMER"
  # A reaper that is deleting when its unit changes would run as the old unit until it ended, and
  # on a busy lane it does not end: restart it, without waiting for the new run. The delete this
  # cuts short is safe: what is left of the entry stays in the trash and the new run deletes it.
  if [ -n "$changed" ] && unit_running "$(systemctl is-active "$REAPER_SERVICE" 2>/dev/null || true)"; then
    run_mutation systemctl restart --no-block "$REAPER_SERVICE"
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
  if [ -n "$STORE_FOUND" ] && [ "$STORE_FOUND" != "$STORE_FS" ]; then
    if [ "$STORE_FS" = btrfs ]; then
      gate "The host file says store_fs: btrfs and the lane's store $STORE_DIR is $STORE_FOUND. --apply converts no store, so the lane keeps running on it as it is. Convert it when the lane may take no job for a while (README.md, \"Converting a lane's store\"): $CHECKOUT/bin/provision-lane.sh $HOST $NAME --convert-store --apply"
    else
      gate "The lane's store $STORE_DIR is $STORE_FOUND and the host file says store_fs: $STORE_FS (xfs when the key is absent). The kit converts a store to btrfs only: set store_fs: $STORE_FOUND for $NAME in hosts/$HOST.yml."
    fi
  fi
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
  # On a btrfs store a slot's store is a subvolume: one call deletes it. What that leaves (a plain
  # directory, a link) goes with the rest below.
  if [ "$STORE_FOUND" = btrfs ] && [ -d "$STORE_DIR/slot-$1" ] && [ ! -L "$STORE_DIR/slot-$1" ]; then
    run_mutation btrfs subvolume delete "$STORE_DIR/slot-$1" || true
  fi
  run_mutation rm -rf "$STATE_DIR/slot-$1" "$STATE_DIR/slot-$1.img" "$STORE_DIR/slot-$1"
}

remove_lane() {
  log "Remove: $NAME lane (listener, scale sets, slot units, slot images, registrations, timers, network)"
  local kind n slot rows target id name status busy
  STORE_FOUND="$(found_store_fs)"
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
  # The reaper only now: each slot stopped above retired its copy into the trash and started it. A
  # reaper stopped in the middle of a delete leaves part of a copy, which goes with the rest here.
  if [ -f "$UNIT_DIR/$REAPER_TIMER" ]; then
    run_mutation systemctl disable --now "$REAPER_TIMER"
    run_mutation systemctl stop "$REAPER_SERVICE"
  fi
  run_mutation rm -rf "$TRASH_DIR"
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
    "$UNIT_DIR/$KEEPALIVE_SERVICE" "$UNIT_DIR/$KEEPALIVE_TIMER" "$UNIT_DIR/$REAPER_SERVICE" "$UNIT_DIR/$REAPER_TIMER")
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

# ---- convert the store ------------------------------------------------------------------------------
# What an exit of --convert-store --apply has to undo: nothing (empty), the stop of the lane's units
# (stopped), or the swap of the store as well (swapped). convert_exit reads it.
CONVERT_STAGE=""
LISTENER_WAS=""
HEALTH_WAS=""
REAPER_WAS=""

# The preloaded stores the new store must hold: the current tag's, and every other whose image is
# still there, which is what the image build's prune keeps (bin/build-runner-image.sh). One that no
# image names, or an unfinished one, stays behind in the XFS image.
carried_stores() {
  local dir tag current
  current="$(image_tag)"
  for dir in "$STORE_DIR"/golden-*; do
    [ -d "$dir" ] && [ ! -L "$dir" ] || continue
    tag="${dir##*/golden-}"
    case "$tag" in *.new) continue ;; esac
    if [ "$tag" = "$current" ] || docker image inspect "$IMAGE:$tag" >/dev/null 2>&1; then printf '%s\n' "$tag"; fi
  done
}

running_slots() {
  local kind n
  for kind in $KINDS; do
    for n in $(seq 1 "$(kind_slots "$kind")"); do
      if unit_running "$(systemctl is-active "$NAME-$kind@$n.service" 2>/dev/null || true)"; then printf '%s-%s@%s.service ' "$NAME" "$kind" "$n"; fi
    done
  done
}

# Stops one slot unless its runner is on a job, the way the listener's own idle stop does
# (listener/README.md, "Idle stop"): the idle-stop marker first, so the slot's cleanup counts no
# failure; then the runner's removal, which GitHub refuses for a runner on a job; and only then
# the stop. Fails, leaving the slot as it was, when the removal was refused or the slot has no job
# file (the listener did not start it).
stop_idle_slot() {  # $1: the kind, $2: the instance
  local dir="$RUN_DIR/$1/$2" target id
  [ -r "$dir/job" ] || return 1
  read -r target _ _ id _ < "$dir/job" || true
  [ -n "$id" ] || return 1
  install -m 0600 /dev/null "$dir/idle-stop" || return 1
  if run_mutation gh_lane api -X DELETE "$(runners_endpoint "$target")/$id" --silent; then
    run_mutation systemctl stop "$NAME-$1@$2.service"
  else
    rm -f "$dir/idle-stop"
    return 1
  fi
}

# Takes the lane off its jobs: nothing may start a slot, and no slot may be running, while the store
# is swapped. The store is not touched here, so an exit only has to start these units again.
quiesce_lane() {
  local kind n unit i running
  HEALTH_WAS="$(systemctl is-active "$HEALTH_TIMER" 2>/dev/null || true)"
  LISTENER_WAS="$(systemctl is-active "$LISTENER_UNIT" 2>/dev/null || true)"
  REAPER_WAS="$(systemctl is-active "$REAPER_TIMER" 2>/dev/null || true)"
  [ "$APPLY" != "1" ] || CONVERT_STAGE=stopped
  # The health check first, so that it restarts no listener this stops (as --remove does).
  if [ -f "$UNIT_DIR/$HEALTH_TIMER" ]; then run_mutation systemctl stop "$HEALTH_TIMER" "$HEALTH_SERVICE"; fi
  if [ -f "$UNIT_DIR/$LISTENER_UNIT" ]; then run_mutation systemctl stop "$LISTENER_UNIT"; fi
  # With the listener stopped no job reaches an idle slot any more, and an idle slot would not end
  # by itself: those are stopped now, once each. A slot on a job ends with its job.
  for kind in $KINDS; do
    for n in $(seq 1 "$(kind_slots "$kind")"); do
      unit="$NAME-$kind@$n.service"
      case "$(systemctl is-active "$unit" 2>/dev/null || true)" in active|activating) ;; *) continue ;; esac
      if [ "$APPLY" != "1" ]; then
        log "DRY-RUN: stop $unit if its runner is on no job (its registration is removed first), else wait for its job"
      elif ! stop_idle_slot "$kind" "$n"; then
        log "  $unit keeps running: GitHub did not remove its runner, as for one that is on a job. The conversion waits for it."
      fi
    done
  done
  if [ "$APPLY" != "1" ]; then
    log "DRY-RUN: wait up to ${CONVERT_WAIT_SEC}s for the slots that are on a job"
  else
    for ((i = 0; ; i++)); do
      running="$(running_slots)"
      [ -n "$running" ] || break
      [ "$i" -lt $((CONVERT_WAIT_SEC / 5)) ] || die "still running after ${CONVERT_WAIT_SEC}s: ${running% }. The store was not touched; run this again when the lane is quieter."
      [ $((i % 12)) != 0 ] || log "  waiting for ${running% }"
      sleep 5
    done
  fi
  # The reaper last: each slot that stopped retired its copy and started it. What it leaves in the
  # trash stays behind in the XFS image.
  if [ -f "$UNIT_DIR/$REAPER_TIMER" ]; then run_mutation systemctl stop "$REAPER_TIMER" "$REAPER_SERVICE"; fi
}

# Starts again what quiesce_lane found running, and only that.
resume_lane() {
  ! unit_running "$REAPER_WAS" || run_mutation systemctl start "$REAPER_TIMER" || warn "could not start $REAPER_TIMER"
  ! unit_running "$LISTENER_WAS" || run_mutation systemctl start "$LISTENER_UNIT" || warn "could not start $LISTENER_UNIT: start it by hand"
  ! unit_running "$HEALTH_WAS" || run_mutation systemctl start "$HEALTH_TIMER" || warn "could not start $HEALTH_TIMER"
}

# The swap, and the copy of each preloaded store ($@: their tags) out of the XFS image. From its
# first line an exit puts the XFS store back (restore_xfs_store).
swap_store() {
  local tag src dst
  [ "$APPLY" != "1" ] || CONVERT_STAGE=swapped
  run_mutation systemctl stop "$STORE_MOUNT_UNIT" \
    || die "could not unmount $STORE_DIR: something still uses it (a shell inside it, a container, a --check under way). The store was not touched."
  run_mutation mv "$STORE_IMG" "$OLD_STORE_IMG"
  run_mutation truncate -s "${STORE_GB}G" "$STORE_IMG"
  make_store_fs btrfs "$STORE_IMG"
  [ -z "$(converge_file "$UNIT_DIR/$STORE_MOUNT_UNIT" "$(render_store_mount btrfs)")" ] || run_mutation systemctl daemon-reload
  run_mutation systemctl start "$STORE_MOUNT_UNIT"
  run_mutation install -d -m 0700 "$OLD_STORE_DIR"
  run_mutation mount -o loop,ro "$OLD_STORE_IMG" "$OLD_STORE_DIR"
  for tag in "$@"; do
    src="$OLD_STORE_DIR/golden-$tag"
    dst="$(store_dir "$tag")"
    # A subvolume, so that a job's store can be a snapshot of it; under the unfinished store's name
    # until it is whole, as the image build writes one. cp -a of <src>/. copies the directory
    # itself onto the subvolume: its files, and its own owner, mode and times.
    run_mutation btrfs subvolume create "$dst.new"
    run_mutation cp -a "$src/." "$dst.new/"
    run_mutation mv "$dst.new" "$dst"
  done
  run_mutation umount "$OLD_STORE_DIR"
  run_mutation rmdir "$OLD_STORE_DIR"
}

# The check the conversion ends on: the store is btrfs, and the current tag's preloaded store ($1,
# empty when the XFS store held none) takes a snapshot and gives it up, made and removed by the slot
# helper as a job's is.
verify_new_store() {
  [ "$(found_store_fs)" = btrfs ] || { warn "$STORE_DIR is not btrfs after the swap"; return 1; }
  [ -n "$1" ] || return 0
  smoke_snapshot snapshot "$1" && smoke_snapshot discard "$1"
}

# Puts the XFS store back at its path, from any point of swap_store. The image at the store's path
# is this run's own new one only once the XFS image has been moved aside; only then is it deleted.
restore_xfs_store() {
  log "Convert: putting the XFS store back"
  if mountpoint -q "$OLD_STORE_DIR" 2>/dev/null; then
    umount "$OLD_STORE_DIR" || { warn "could not unmount $OLD_STORE_DIR"; return 1; }
  fi
  [ ! -d "$OLD_STORE_DIR" ] || rmdir "$OLD_STORE_DIR" || true
  if [ -e "$OLD_STORE_IMG" ]; then
    if mountpoint -q "$STORE_DIR" 2>/dev/null; then
      systemctl stop "$STORE_MOUNT_UNIT" || { warn "could not unmount the new store at $STORE_DIR"; return 1; }
    fi
    rm -f "$STORE_IMG"
    mv "$OLD_STORE_IMG" "$STORE_IMG" || { warn "could not move $OLD_STORE_IMG back to $STORE_IMG"; return 1; }
  fi
  converge_file "$UNIT_DIR/$STORE_MOUNT_UNIT" "$(render_store_mount xfs)" >/dev/null
  systemctl daemon-reload || true
  mountpoint -q "$STORE_DIR" 2>/dev/null || systemctl start "$STORE_MOUNT_UNIT" || { warn "could not mount the XFS store at $STORE_DIR"; return 1; }
  log "Convert: $STORE_DIR is the XFS store again"
}

convert_exit() {
  local rc=$?
  if [ "$CONVERT_STAGE" = swapped ]; then
    if restore_xfs_store; then
      CONVERT_STAGE=stopped
    else
      warn "the XFS store is not back at $STORE_DIR, so the lane stays stopped: its image is $OLD_STORE_IMG, or still $STORE_IMG (README.md, \"Converting a lane's store\", has the steps by hand)"
    fi
  fi
  [ "$CONVERT_STAGE" != stopped ] || resume_lane
  repair_gh_config
  exit "$rc"
}

# One build at a time holds the lane's build lock, and a build writes into the store: the
# conversion holds it too (fd 7, until the script exits), so the weekly build waits for it.
take_build_lock() {
  [ "$APPLY" = "1" ] || return 0
  exec 7>"$BUILD_LOCK"
  flock -n 7 || die "an image build holds $BUILD_LOCK; convert once it has finished (systemctl status $IMAGE_BUILD_SERVICE)"
}

convert_store() {
  local tags tag size need=1 free current=""
  [ "$STORE_FS" = btrfs ] || die "the host file does not say store_fs: btrfs for $NAME (it says $STORE_FS, or nothing). Set it there first: --convert-store moves a store from XFS to btrfs and nothing else."
  STORE_FOUND="$(found_store_fs)"
  case "$STORE_FOUND" in
    btrfs) log "Convert: $STORE_DIR is btrfs already; nothing to do"; return 0 ;;
    "") log "Convert: $NAME has no store yet; --apply makes a new one btrfs"; return 0 ;;
    xfs) ;;
    *) die "the lane's store ($STORE_DIR, from $STORE_IMG) is on a filesystem that is neither XFS nor btrfs ($STORE_FOUND); nothing to convert" ;;
  esac
  mountpoint -q "$STORE_DIR" 2>/dev/null || die "$STORE_DIR is not mounted, so its preloaded stores cannot be read; run --apply first"
  log "Convert: $STORE_DIR from XFS to btrfs (a new ${STORE_GB}G image at $STORE_IMG; the XFS image is kept as $OLD_STORE_IMG)"
  tags="$(carried_stores)"
  for tag in $tags; do
    size="$(du -sx -BG "$(store_dir "$tag")" 2>/dev/null | cut -f1 | tr -dc '0-9' || true)"
    log "  carries over $(store_dir "$tag") (${size:-?}G)"
    need=$((need + ${size:-0}))
    [ "$tag" != "$(image_tag)" ] || current="$tag"
  done
  [ -n "$tags" ] || log "  no preloaded store to carry over: the lane needs an image build afterwards (the gate --apply prints)"
  # What the copies write, and 1G over: btrfs keeps two copies of its metadata on a single device.
  free="$(df -BG --output=avail "$STATE_DIR" 2>/dev/null | tail -n 1 | tr -dc '0-9' || true)"
  [ -n "$free" ] && [ "$free" -ge "$need" ] \
    || die "the conversion writes about ${need}G beside the XFS image, which stays, and the filesystem under $STATE_DIR has ${free:-?}G free. Nothing was changed."
  log "  needs about ${need}G of the ${free}G free under $STATE_DIR"
  take_build_lock
  trap convert_exit EXIT
  trap 'exit 129' HUP
  trap 'exit 130' INT
  trap 'exit 143' TERM
  quiesce_lane
  # shellcheck disable=SC2086 # the tags are a word list
  swap_store $tags
  if [ "$APPLY" = "1" ]; then
    verify_new_store "$current" || die "the new store did not verify${current:+ (a snapshot of $(store_dir "$current") could not be made and deleted)}"
    log "  verified: $STORE_DIR is btrfs${current:+, and a snapshot of $(store_dir "$current") was made and deleted}"
    # Past this line the btrfs store is the lane's: an exit starts the lane's units and undoes nothing.
    CONVERT_STAGE=stopped
  else
    log "DRY-RUN: check that $STORE_DIR is btrfs${current:+ and that a snapshot of $(store_dir "$current") can be made and deleted}; if anything above failed, put the XFS store back"
  fi
  ensure_dir "$TRASH_DIR" 0700
  resume_lane
  CONVERT_STAGE=""
  [ "$APPLY" = "1" ] || return 0
  log "Convert: done. $STORE_DIR is btrfs and the lane takes jobs again."
  log "  The XFS store is kept at $OLD_STORE_IMG. Once the lane has run jobs on the new store, remove it: rm $OLD_STORE_IMG"
  log "  Now: $CHECKOUT/bin/provision-lane.sh $HOST $NAME --check"
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
  STORE_FOUND="$(found_store_fs)"
  if mountpoint -q "$STORE_DIR" 2>/dev/null; then
    src="$(findmnt -n -o SOURCE,FSTYPE,SIZE "$STORE_DIR" 2>/dev/null | tr -s ' ')"
    printf 'store_fs=mounted (%s) mount_unit=%s\n' "${src:-?}" "$(systemctl is-enabled "$STORE_MOUNT_UNIT" 2>/dev/null || echo unknown)"
    # The store's own type, against the host file's: a lane is converged on the one it asks for.
    case "$STORE_FOUND" in
      "$STORE_FS") ;;
      xfs) printf 'store_fs_type=xfs host_file=%s (not converged: %s %s %s --convert-store converts the store)\n' "$STORE_FS" "$CHECKOUT/bin/provision-lane.sh" "$HOST" "$NAME"; bad ;;
      btrfs) printf 'store_fs_type=btrfs host_file=%s (not converged: the kit converts a store to btrfs only, so the host file must say store_fs: btrfs)\n' "$STORE_FS"; bad ;;
      *) printf 'store_fs_type=%s (a lane store is XFS, for its reflink snapshots, or btrfs)\n' "$STORE_FOUND"; bad ;;
    esac
  else
    printf 'store_fs=not-mounted\n'
    bad
  fi
  # The room under the lane's sparse images, whatever filesystem is inside them: df on the
  # directory that holds them. Nothing here reads the room inside a btrfs store (its df is an
  # estimate, and what a deleted snapshot held comes back only once the kernel has cleaned up).
  # Before --apply the state directory does not exist yet; its parent is the same filesystem.
  dir="$STATE_DIR"
  [ -d "$dir" ] || dir="$(dirname "$dir")"
  free="$(df -BG --output=avail "$dir" 2>/dev/null | tail -n 1 | tr -dc '0-9')"
  worst=$(( STORE_GB + (SLOTS + WAIT_SLOTS) * SLOT_GB ))
  printf 'disk free=%sG lane_worst_case=%sG (store %sG + %s slots x %sG, all sparse)\n' "${free:-?}" "$worst" "$STORE_GB" "$((SLOTS + WAIT_SLOTS))" "$SLOT_GB"
  if [ -n "$free" ] && [ "$free" -lt "$worst" ]; then
    warn "the lane's filesystems could grow past the free space on $STATE_DIR (${free}G < ${worst}G)"
  fi
  check_store_trash
}

# The trash's backlog and the reaper that empties it. An entry's name starts with the second it was
# retired (bin/lane-slot.sh, "The store trash"), so its age is read from the name alone.
check_store_trash() {
  local names count oldest age="none" state
  if [ ! -d "$TRASH_DIR" ] || [ -L "$TRASH_DIR" ]; then
    printf 'store_trash=%s absent (re-run --apply)\n' "$TRASH_DIR"; bad
  else
    names="$(ls -A "$TRASH_DIR" 2>/dev/null)"
    count="$(grep -c . <<< "$names" || true)"
    # min keeps the field's own text: an awk that prints a large number in %.6g would not give a second back.
    oldest="$(awk -F. '$1 ~ /^[0-9]+$/ && (min == "" || $1 + 0 < min + 0) { min = $1 } END { print min }' <<< "$names")"
    [ -z "$oldest" ] || age=$(( $(date +%s) - oldest ))
    printf 'store_trash=%s entries=%s oldest_age_s=%s\n' "$TRASH_DIR" "$count" "$age"
    if [ "$age" != none ] && [ "$age" -gt "$TRASH_STALE_SEC" ]; then
      printf 'store_trash_stale=the oldest retired store copy has waited %ss, past %ss: the reaper is stuck or behind (journalctl -u %s)\n' "$age" "$TRASH_STALE_SEC" "$REAPER_SERVICE"; bad
    fi
  fi
  check_timer store_reaper_timer "$REAPER_TIMER"
  # A oneshot is inactive after a run that succeeded (or none yet), failed after one that did not.
  state="$(systemctl is-active "$REAPER_SERVICE" 2>/dev/null || true)"
  printf 'store_reaper=%s %s\n' "$REAPER_SERVICE" "${state:-unknown}"
  if [ "$state" = failed ]; then
    printf 'store_reaper_failed=its last run could not delete a retired store copy (journalctl -u %s)\n' "$REAPER_SERVICE"; bad
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
  printf 'smoke store_snapshot_s=%s (%s snapshot of %s)\n' "$(( started - snapshot_started ))" "$([ "$STORE_FOUND" = btrfs ] && printf btrfs || printf reflink)" "$(store_dir "$tag")"
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

log "host=$HOST lane=$NAME scope=$SCOPE owner=$OWNER kinds=[$KINDS] slots=$SLOTS${WAIT_LABEL:+ wait_slots=$WAIT_SLOTS}${GROUP:+ group=$GROUP repos=[$REPOS]}${EXCLUDE:+ exclude=[$EXCLUDE]} apply=$APPLY check=$CHECK remove=$REMOVE convert_store=$CONVERT"
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
if [ "$CONVERT" = "1" ]; then
  convert_store
  log "done"
  exit 0
fi
ensure_lane_user
ensure_app_key
ensure_token_timer
ensure_image_build_timer
ensure_store_storage
ensure_store_reaper
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
