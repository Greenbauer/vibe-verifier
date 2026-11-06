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

# The rest of the script, one file per part (lib/provision-lane/): what the phases read and render,
# --apply's phases, --remove, --convert-store and --check.
# shellcheck source-path=SCRIPTDIR/.. source=lib/provision-lane/reads.sh
. "$REPO_ROOT/lib/provision-lane/reads.sh"
# shellcheck source-path=SCRIPTDIR/.. source=lib/provision-lane/render.sh
. "$REPO_ROOT/lib/provision-lane/render.sh"
# shellcheck source-path=SCRIPTDIR/.. source=lib/provision-lane/apply.sh
. "$REPO_ROOT/lib/provision-lane/apply.sh"
# shellcheck source-path=SCRIPTDIR/.. source=lib/provision-lane/apply-listener.sh
. "$REPO_ROOT/lib/provision-lane/apply-listener.sh"
# shellcheck source-path=SCRIPTDIR/.. source=lib/provision-lane/remove.sh
. "$REPO_ROOT/lib/provision-lane/remove.sh"
# shellcheck source-path=SCRIPTDIR/.. source=lib/provision-lane/convert.sh
. "$REPO_ROOT/lib/provision-lane/convert.sh"
# shellcheck source-path=SCRIPTDIR/.. source=lib/provision-lane/check.sh
. "$REPO_ROOT/lib/provision-lane/check.sh"

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
