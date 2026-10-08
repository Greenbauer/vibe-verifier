#!/usr/bin/env bash
# lane-slot.sh: per-job preparation and cleanup for one slot of a disposable-container lane, the
# lane's store reaper, and the lane's Codex login keepalive between QAE jobs.
#
# Linux-only by design: Ubuntu 24.04, bash 5, GNU coreutils, util-linux, systemd. It runs as root
# from the slot units bin/provision-lane.sh writes, one template per kind of job:
#   <lane>-<kind>@.service   ExecStartPre=+lane-slot.sh prepare <kind> %i
#                            ExecStopPost=+lane-slot.sh cleanup <kind> %i
# from <lane>-store-reaper.service (lane-slot.sh reap) and, for a lane with QAE, from
# <lane>-codex-keepalive.service (lane-slot.sh codex-keepalive).
# The lane's scale-set listener (listener/README.md, "The run-dir contract") mints each slot's
# just-in-time runner, writes <run>/<kind>/<n>/jit and job, and starts the unit for that one job;
# the unit has Restart=no. Never run this by hand on a machine with live jobs; stop the unit instead.
#
# prepare <kind> <n>
#   1. refuse when the listener's jit and job files are absent: a unit started by hand has no job;
#   2. back off after consecutive failed runs of this instance (see "Back-off" below);
#   3. assert the lane firewall rules (bin/lane-firewall.sh), failing closed;
#   4. remove a leftover container and mount from an earlier run;
#   5. re-create the instance's sparse ext4 image and mount it at <state>/slot-<kind>-<n>, so
#      nothing a job wrote survives into the next job and a job's disk use is bounded;
#   6. give the job its inner Docker store at <store>/slot-<kind>-<n>, which the unit mounts on the
#      container's /var/lib/docker, after retiring a leftover there from an earlier run ("The store
#      trash" below). For ci and qae it is a snapshot of the preloaded store: a reflink copy
#      (cp --reflink=always, XFS) of <store>/golden-<image tag>, so every job gets its own writable
#      store holding the preloaded images at the cost of the copy's metadata, never a second copy
#      of the data (README.md, "Disk"); the layer directories are copied in parallel, the rest
#      serially. A store filesystem short of room gets some back from the trash first, or the job
#      is refused. For wait it is an empty directory, and no copy is made: a job that only polls
#      for another job's result runs no inner image, and the inner dockerd starts on an empty data
#      root (the image build's preload starts it on the empty store it then fills);
#   7. for the qae kind, reset the instance's own Codex login store (KNOWN_CI_CODEX_STORE_<n>; it
#      refuses an instance without one, and a store that is missing or a link), holding the store's
#      lock (below): delete every entry but auth.json, write the tracked config.toml
#      (KNOWN_CI_CODEX_CONFIG, rendered by the provisioner), and hand the store to the job's uid
#      (1001, the image's runner), so only the login outlives a job.
#   A wait slot (an org lane's jobs that only wait for another job's result) is prepared like a ci
#   slot in every step but 6; its unit's container memory and the listener's budget for it differ.
#   It never mints a runner and never deletes the listener's files.
# cleanup <kind> <n>
#   removes the container; deregisters the job file's runner at its scope's endpoint
#   (orgs/<org>/actions/runners/<id> or repos/<owner>/<repo>/actions/runners/<id>) unless the
#   listener's idle-stop marker says the listener already removed it; unmounts; retires the job's
#   store into the trash (a rename) and starts the lane's reaper without waiting for it, or, with
#   the trash at its bound or the store short of room, deletes that store here and now; deletes
#   the slot image; prunes a qae store back to auth.json and gives it back to the lane user
#   (KNOWN_CI_CODEX_OWNER), holding its lock, so an operator can log it in again; records the
#   outcome; removes jit and idle-stop, and the job file LAST, because its absence is what frees the
#   instance for the listener.
# reap
#   deletes what the slots retired into <store>/trash, oldest first, holding the trash's lock, until
#   the trash is empty: one entry at a time while the store has ample room, up to REAP_JOBS (three)
#   at a time while it is under pressure. A second reaper finds the lock held and exits 0, so a lane
#   never runs more than those few deletes at once. After REAP_MAX_SEC of work it starts itself
#   afresh, so a helper the machine pulled since reaches a reaper that never runs dry. It deletes
#   entries of that one directory and nothing else: it refuses a trash that is a link, rm
#   follows no link inside an entry, and it stays on the store's filesystem. Exits 1 when an entry
#   could not be deleted. <lane>-store-reaper.service runs it, started by every cleanup and by
#   <lane>-store-reaper.timer (every 5 minutes, and after boot). Needs only the store, as snapshot
#   and discard do.
# codex-keepalive
#   for each qae instance n whose store holds a login: when its last_refresh is at least 10 days old
#   (see KEEPALIVE_AFTER_SEC), resets the store for the lane user, runs one trivial `codex exec` on
#   it as that user, so Codex refreshes the login, and prunes it back to auth.json as cleanup does.
#   It reads only last_refresh from auth.json and never logs a token. A store whose lock is held or
#   whose instance has a job file is skipped: that job's Codex refreshes it. Exits 1 when a store it
#   ran Codex on was not refreshed. <lane>-codex-keepalive.timer runs it daily.
# snapshot <name> / discard <name>
#   the same store snapshot at <store>/<name>, for the provisioner's --check smoke container and
#   the image build's verify container (one implementation of the copy, so their timing is the
#   lane's); <name> is <word>-<digits>. These need only KNOWN_CI_STORE_DIR and KNOWN_CI_IMAGE_TAG.
#   discard deletes its copy before it returns, and never uses the trash: the two callers are rare,
#   and --check must leave nothing behind.
#
# The store trash: deleting a job's store copy was the slowest step of a slot's reset, and every
# slot did it in its own stop path. Measured on a live lane, 2026-10-07: about 225,000 inodes a
# copy, 9 s to delete one alone, 21 to 121 s with eight or more running at once, each holding its
# instance until it finished. So a slot never deletes its copy. prepare (a leftover) and cleanup
# rename it to <store>/trash/<epoch second>.slot-<kind>-<n>.<pid>: one rename inside one filesystem,
# so atomic and immediate, under a name no other entry has and that carries the entry's age. The
# reaper deletes it later, at its own pace. The trash is bounded: it takes a ci or qae copy only
# while it holds fewer than TRASH_MAX of them and the store has room (STORE_MIN_FREE_PCT). Past
# that the slot deletes its copy in place, in its own stop path, as every slot did before there was
# a trash: slower, but a slot that is deleting takes no new job, so the lane cannot retire copies
# faster than the disk deletes them. The trash absorbs a burst; sustained load falls back to that.
# A copy the trash cannot take for another reason (the directory cannot be made, it is a link, or
# the rename fails) is deleted in place too.
# Fail closed on disk: a backlog must not fill the store unseen. Before a ci or qae copy, when the
# store filesystem has less than STORE_MIN_FREE_PCT of its space or of its inodes free (a reading
# that cannot be made counts as short) and the trash holds entries, prepare deletes the oldest
# entries itself until there is room, waiting at most REAP_LOCK_WAIT for a reaper that holds the
# lock. Still short with entries left, it refuses the job, which counts toward the back-off. A
# short store with an empty trash is not the backlog's doing: the copy is tried as before, and
# fails closed on a full filesystem.
#
# Back-off: a failing prepare must not turn the lane into a tight loop of failed starts. cleanup
# counts consecutive runs of an instance that did not end normally ($SERVICE_RESULT, which systemd
# sets for ExecStopPost, is "success"; or an idle slot's RuntimeMaxSec "timeout" whose runner never
# took a job; or the listener's idle stop), and prepare sleeps BASE * 2^(failures-1) seconds,
# capped at MAX, before the next job on that instance. A run that ends normally resets the count.
# The image's entrypoint must exit 0 after its one job and non-zero on any failure
# (README.md, "The runner image contract").
#
# The Codex store lock: <run>/qae/<n>.codex.lock, outside the store so a prune never removes it, is
# held by whatever touches store n: prepare's reset, the job's container (the qae unit's ExecStart
# is `flock -F` on it), cleanup's return and the keepalive. A slot waits at most CODEX_LOCK_WAIT for
# it, then prepare fails (the unit's failure counts toward the back-off) and cleanup leaves the
# store for the next prepare's reset; the keepalive never waits. Nobody waits for the lock while
# holding another, so no wait can deadlock, and the keepalive's longest hold (its Codex timeout
# plus a prune) is shorter than a slot's wait. Between prepare, the run and cleanup the lock is
# briefly free; the job file, which the listener writes before it starts the unit and cleanup
# removes last, covers those gaps: the keepalive checks it while holding the lock.
#
# Configuration comes from the unit's Environment= lines (KNOWN_CI_*) and its EnvironmentFile
# (KNOWN_CI_IMAGE_TAG, which names the store to snapshot). The paths derive from the lane name
# (<state> is /var/lib/<name>, <run> is /run/<name>, <store> is <state>/store); the unit passes each
# one explicitly anyway, and they are overridable so tests/lane-slot-test.sh can run this
# unprivileged.
set -euo pipefail

ACTION="${1:-}"
KIND=""
SLOT=""
usage() {
  echo "usage: $0 prepare|cleanup <kind> <n> | snapshot|discard <name> | reap | codex-keepalive" >&2
  exit 2
}
case "$ACTION" in
  prepare|cleanup)
    [ "$#" -eq 3 ] || usage
    KIND="$2"; SLOT="$3"
    case "$KIND" in ci|qae|wait) ;; *) echo "lane-slot: kind must be ci, qae or wait, got '$KIND'" >&2; exit 2 ;; esac
    case "$SLOT" in ''|*[!0-9]*) echo "lane-slot: slot must be a number, got '$SLOT'" >&2; exit 2 ;; esac
    : "${KNOWN_CI_NAME:?}" "${KNOWN_CI_SCOPE:?}" "${KNOWN_CI_GH_HOSTS:?}" "${KNOWN_CI_BRIDGE:?}" ;;
  snapshot|discard)
    SLOT="${2:-}"
    [[ "$SLOT" =~ ^[a-z]+-[0-9]+$ ]] || { echo "lane-slot: snapshot name must be <word>-<digits>, got '$SLOT'" >&2; exit 2; } ;;
  reap)
    # No argument: the one directory it deletes from is the store's own trash, never a path given.
    [ "$#" -eq 1 ] || usage ;;
  codex-keepalive)
    [ "$#" -eq 1 ] || usage
    : "${KNOWN_CI_NAME:?}" "${KNOWN_CI_CODEX_CONFIG:?}" "${KNOWN_CI_CODEX_OWNER:?}" ;;
  *) usage ;;
esac

# A qae instance's own Codex login store, KNOWN_CI_CODEX_STORE_<n>, and never another instance's:
# Codex rotates a login's refresh token as a job uses it, so two concurrent jobs on one store retire
# each other's session. An instance without one (beyond the lane's qae_concurrency) runs no job; its
# cleanup still runs.
CODEX_STORE=""
if [ "$KIND" = qae ]; then
  : "${KNOWN_CI_CODEX_CONFIG:?}" "${KNOWN_CI_CODEX_OWNER:?}"
  store_var="KNOWN_CI_CODEX_STORE_$SLOT"
  CODEX_STORE="${!store_var:-}"
  if [ -z "$CODEX_STORE" ] && [ "$ACTION" = prepare ]; then
    echo "lane-slot: qae instance $SLOT has no Codex store of its own ($store_var is unset: the lane's qae_concurrency is below $SLOT)" >&2
    exit 1
  fi
fi
# snapshot, discard and reap need only the store, named outright or through the lane name.
[ -n "${KNOWN_CI_NAME:-}" ] || [ -n "${KNOWN_CI_STORE_DIR:-}" ] \
  || { echo "lane-slot: set KNOWN_CI_NAME or KNOWN_CI_STORE_DIR" >&2; exit 2; }
STATE_DIR="${KNOWN_CI_STATE_DIR:-/var/lib/${KNOWN_CI_NAME:-}}"
# The lane user's home (bin/provision-lane.sh, Phase A0), where the keepalive's Codex runs.
LANE_HOME="$STATE_DIR/home"
STORE_DIR="${KNOWN_CI_STORE_DIR:-$STATE_DIR/store}"
RUN_DIR="${KNOWN_CI_RUN_DIR:-/run/${KNOWN_CI_NAME:-}}"
API="${KNOWN_CI_API:-https://api.github.com}"
HARDEN="${KNOWN_CI_HARDEN:-$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lane-firewall.sh}"
BACKOFF_BASE="${KNOWN_CI_BACKOFF_BASE:-10}"
BACKOFF_MAX="${KNOWN_CI_BACKOFF_MAX:-600}"
# The image's runner user (image/Dockerfile): the Codex store must be its own while a QAE job runs,
# since Sysbox maps a bind mount's host ids one to one.
JOB_UID=1001
# How long a slot waits for its Codex store's lock: longer than the keepalive can hold it
# (KEEPALIVE_TIMEOUT, 10 s for the kill, and a prune), well inside the unit's TimeoutStartSec after
# the longest back-off and inside its TimeoutStopSec.
CODEX_LOCK_WAIT="${KNOWN_CI_CODEX_LOCK_WAIT:-120}"
# Codex refreshes a ChatGPT login only once its access token expires within 5 minutes; only when that
# expiry is unreadable does it fall back to a last_refresh older than 8 days (Codex 0.160.1,
# codex-rs/login/src/auth/manager.rs, should_refresh_proactively). The access tokens live 10 days
# from last_refresh (every store on one machine, measured 2026-10-06), so a run on a younger store
# refreshes nothing: the keepalive runs Codex only on a store at least that old.
KEEPALIVE_AFTER_SEC=$((10 * 86400))
KEEPALIVE_TIMEOUT=90
# /usr/bin/codex is the Codex CLI itself; /usr/local/bin/codex can be another tool's wrapper, which
# reads another user's home and fails for the lane's.
CODEX_BIN="${KNOWN_CI_CODEX_BIN:-/usr/bin/codex}"
# Where the slots retire their store copies ("The store trash" above): inside the store, so a
# rename there never leaves the filesystem.
TRASH_DIR="$STORE_DIR/trash"
# The lane's reaper, under the name bin/provision-lane.sh writes it. Derived from the lane name
# rather than passed by the unit, so a slot started from a template written before the reaper
# existed starts it too.
REAPER_UNIT="${KNOWN_CI_NAME:-}-store-reaper.service"
# Below this share of free space, or of free inodes, the store filesystem is short of room for
# another copy. The copy itself is small (inodes and extent maps; reflinks share the data), but
# what a job writes into it is not bounded, and a store nine tenths full has no room left for a
# backlog of retired copies: a tenth is early enough to delete from the trash before a copy fails
# for want of space.
STORE_MIN_FREE_PCT="${KNOWN_CI_STORE_MIN_FREE_PCT:-10}"
# How long a prepare short of room waits for the trash's lock while a reaper holds it: the reaper
# is deleting, which is what makes room, and one delete took 9 s alone and at most 121 s among
# eight (2026-10-07). It fits the unit's TimeoutStartSec (15 minutes) beside the copy and the Codex
# lock; only on top of the longest back-off (10 minutes) can the start time out, which is one more
# failed run of an instance that was already failing.
REAP_LOCK_WAIT="${KNOWN_CI_REAP_LOCK_WAIT:-120}"
# How many entries the reaper deletes at once: one while the store has ample room, REAP_JOBS while
# it is under pressure, read again before every batch. Deletes and copies share one disk, which
# does the same 12,000 to 14,000 small writes a second whatever the split, so the count trades a
# job's start against the store's room. Measured on a live lane, 12 minutes each, 2026-10-07. One
# at a time: ci prepare took a median of 22 s (p90 29, longest 36), but at about 2.3 copies retired
# a minute the trash grew from 16 to 39 entries and the store's inodes from 35% to 67% used in 45
# minutes. Three at a time: inodes fell from 64% to 51% used and space from 81% to 66% in 12
# minutes, but a delete took 71 s on average (longest 108) and ci prepare a median of 46 s (p90 68,
# longest 79). So jobs start fast while there is room, and the trash is drained only when it has to
# be. Never eight: that many ran at once when every slot deleted in its own stop path, and each
# took 21 to 121 s.
REAP_JOBS="${KNOWN_CI_REAP_JOBS:-3}"
# With at least this share of its space and of its inodes free the store has ample room; with less
# it is under pressure. Half: far above the tenth at which a prepare must delete for itself, so the
# reaper has the whole stretch between them to win the room back.
STORE_AMPLE_FREE_PCT="${KNOWN_CI_STORE_AMPLE_FREE_PCT:-50}"
# How long one reaper process works before it starts itself afresh from the helper on disk. A
# reaper ends only when the trash is empty, and on a busy lane it never is: a helper a machine
# pulled at 23:45 never reached the reaper it had started at 22:56 (2026-10-07). It starts afresh
# between batches, so no delete is cut short. One that is (the unit stopped, a reboot) is safe too:
# what is left of the entry stays in the trash under its name, and a later pass deletes the rest.
REAP_MAX_SEC="${KNOWN_CI_REAP_MAX_SEC:-300}"
# The most retired ci and qae copies the trash may hold. Without a bound nothing ties the lane's
# pace to the disk's: measured on a live lane, 2026-10-07, three deletes at a time took 77 to 133 s
# each, 1.5 to 2.3 a minute, while the lane retired 2.1 to 2.8 copies a minute. The trash went from
# 39 to 70 entries in an hour and the store reached 90% of its inodes (an XFS store of 40G caps
# them at 20.9 million, and a copy holds about 225,000), where every prepare was deleting for
# itself and four slots of five waited in it. A slot that deletes its own copy in its stop path is
# slow but cannot outrun the disk: it takes no new job until the delete is done. So the trash
# absorbs a burst and no more. The default is the unit's slot count, which is the most copies one
# burst can retire (a slot unit sets one KNOWN_CI_CPUSET_<n> for each instance), or 8.
TRASH_MAX="${KNOWN_CI_TRASH_MAX:-$(compgen -v KNOWN_CI_CPUSET_ | grep -c . || true)}"
[[ "$TRASH_MAX" =~ ^[1-9][0-9]*$ ]] || TRASH_MAX=8

INSTANCE="$KIND-$SLOT"
SLOT_RUN="$RUN_DIR/$KIND/$SLOT"
FAILURES="$RUN_DIR/$KIND/$SLOT.failures"
CONTAINER="${KNOWN_CI_NAME:-}-$INSTANCE"
IMAGE_FILE="$STATE_DIR/slot-$INSTANCE.img"
MOUNT_POINT="$STATE_DIR/slot-$INSTANCE"
case "$ACTION" in snapshot|discard) SNAPSHOT="$STORE_DIR/$SLOT" ;; *) SNAPSHOT="$STORE_DIR/slot-$INSTANCE" ;; esac
GOLDEN="$STORE_DIR/golden-${KNOWN_CI_IMAGE_TAG:-}"
JIT="$SLOT_RUN/jit"
JOB="$SLOT_RUN/job"
IDLE_STOP="$SLOT_RUN/idle-stop"

log() { printf 'lane-slot[%s%s]: %s\n' "$ACTION" "${KIND:+ $KIND}${SLOT:+ $SLOT}" "$*" >&2; }

# The lane's App installation token, from the top-level oauth_token of gh's hosts.yml.
app_token() {
  local token
  token="$(awk '/^    oauth_token: / { print $2; exit }' "$KNOWN_CI_GH_HOSTS" 2>/dev/null || true)"
  [ -n "$token" ] || { log "no installation token in $KNOWN_CI_GH_HOSTS"; return 1; }
  printf '%s' "$token"
}

# curl with the bearer header supplied as a config file on standard input (printf is a builtin,
# so the token is never in any process's arguments). Prints the HTTP status; the body goes to $1.
github() {
  local body_out="$1" token
  shift
  token="$(app_token)" || return 1
  printf 'header = "Authorization: Bearer %s"\n' "$token" \
    | curl -sS --config - -o "$body_out" -w '%{http_code}' \
        -H 'Accept: application/vnd.github+json' -H 'X-GitHub-Api-Version: 2022-11-28' "$@"
}

remove_container() {
  docker rm -f "$CONTAINER" >/dev/null 2>&1 || true
}

unmount_slot() {
  if mountpoint -q "$MOUNT_POINT" 2>/dev/null; then
    umount "$MOUNT_POINT" || umount -l "$MOUNT_POINT"
  fi
}

# The store's overlay2/ holds one directory per image layer (about 130, 270k files); those are
# reflinked in parallel and everything else serially: 8 s at four or more workers against 14.7 s
# serial (measured 2026-09-26; the loop device is the ceiling, so more workers gain nothing).
# --reflink=always fails, rather than copying the data, on a filesystem without reflinks: the
# store must be the lane's XFS image.
snapshot_store() {
  local src="$1" dst="$2"
  mkdir -m 0700 "$dst" || return 1
  (cd "$src" && find . -mindepth 1 -maxdepth 1 ! -name overlay2 -exec cp -a --reflink=always {} "$dst/" \;) || return 1
  [ -d "$src/overlay2" ] || return 0
  mkdir "$dst/overlay2" && chmod --reference="$src/overlay2" "$dst/overlay2" || return 1
  (cd "$src/overlay2" && find . -mindepth 1 -maxdepth 1 -print0 \
    | xargs -0 -r -P "${KNOWN_CI_SNAPSHOT_JOBS:-4}" -I{} cp -a --reflink=always "$src/overlay2/{}" "$dst/overlay2/{}") || return 1
  touch -r "$src/overlay2" "$dst/overlay2"
  touch -r "$src" "$dst"
}

# The trash as a directory of the store's own, made on first use: a slot may retire a copy before
# the lane provisioner has made it (the kit's first apply on a machine with running slots).
trash_ready() {
  [ -d "$TRASH_DIR" ] || mkdir -m 0700 "$TRASH_DIR" 2>/dev/null || [ -d "$TRASH_DIR" ] || return 1
  [ ! -L "$TRASH_DIR" ]
}

# Starts the lane's reaper and returns at once: --no-block queues the start without waiting for the
# unit, so a slot's stop path never waits for a delete, and a start while the reaper runs joins
# that run. Between the kit's first pull on a machine and its first apply the unit is not there
# yet; the copy then waits in the trash for the timer that apply enables.
start_reaper() {
  systemctl start --no-block "$REAPER_UNIT" 2>/dev/null \
    || log "could not start $REAPER_UNIT (bin/provision-lane.sh writes it); its timer, or the next cleanup's start, empties $TRASH_DIR"
}

# True while the trash holds fewer than TRASH_MAX retired ci and qae copies: one listing of the
# directory. A wait slot's entry is an empty store that takes no time to delete, and is not counted.
# A trash that cannot be read counts as full.
trash_has_space() {
  local entries
  entries="$(find "$TRASH_DIR" -mindepth 1 -maxdepth 1 ! -name '*.slot-wait-*' -print 2>/dev/null)" || return 1
  [ "$(grep -c . <<< "$entries" || true)" -lt "$TRASH_MAX" ]
}

# retire_store <path>: gets a store copy out of the way ("The store trash" above). A slot's
# (prepare, cleanup) is renamed into the trash for the reaper while the trash has space for it and
# the store has room; otherwise, like one the trash cannot take and like the smoke's and the image
# build's (snapshot, discard), it is deleted here before this returns. That removal is serial: a
# parallel rm -rf of one copy gained nothing on the loop device (9 s either way, 2026-09-26).
retire_store() {
  local path="$1"
  [ -e "$path" ] || [ -L "$path" ] || return 0
  case "$ACTION" in
    prepare|cleanup)
      if ! trash_ready; then
        log "could not move $path into $TRASH_DIR; deleting it in place"
      elif [ "$KIND" != wait ] && ! trash_has_space; then
        # Whatever is in a full trash needs a reaper at work on it.
        start_reaper
        log "$TRASH_DIR holds its $TRASH_MAX retired copies (or cannot be read); deleting $path in place, before this instance is free"
      elif [ "$KIND" != wait ] && ! store_has_room; then
        log "the store has under $STORE_MIN_FREE_PCT% of its space or inodes free; deleting $path in place, before this instance is free"
      # mv -T renames onto exactly that name, never into a directory that already has it.
      elif mv -T -- "$path" "$TRASH_DIR/$(date +%s).${path##*/}.$$" 2>/dev/null; then
        start_reaper
        return 0
      else
        log "could not move $path into $TRASH_DIR; deleting it in place"
      fi ;;
  esac
  rm -rf -- "$path"
}

snapshot() {
  : "${KNOWN_CI_IMAGE_TAG:?}"
  [ -d "$GOLDEN" ] || { log "no preloaded store for image tag $KNOWN_CI_IMAGE_TAG at $GOLDEN; the image build (bin/build-runner-image.sh) writes it"; return 1; }
  retire_store "$SNAPSHOT"
  snapshot_store "$GOLDEN" "$SNAPSHOT" || { log "could not snapshot $GOLDEN to $SNAPSHOT (is $STORE_DIR the lane's XFS store?)"; retire_store "$SNAPSHOT"; return 1; }
}

discard() { retire_store "$SNAPSHOT"; }

# reap_entry <entry>: one entry's delete, which reap_trash runs in the background. rm removes a
# link, never what it points to, and --one-file-system keeps it from descending into anything
# mounted inside an entry.
reap_entry() {
  local entry="$1" started="$SECONDS"
  if rm -rf --one-file-system -- "$entry"; then
    log "deleted ${entry##*/} in $((SECONDS - started))s"
  else
    log "could not delete $entry"
    return 1
  fi
}

# reap_trash <lock wait> [<enough> ...]: deletes the trash's entries oldest first (their names
# start with the second they were retired), a batch at a time: it starts a batch's deletes, waits
# for all of them, and starts the next. The reaper's batch is one entry while the store has ample
# room and REAP_JOBS while it is under pressure, read again before every batch (REAP_JOBS above);
# a prepare deleting for itself is short of room already and always takes REAP_JOBS. It holds the
# trash's lock throughout: an flock on the directory itself, so the lock needs no file of its own
# and is gone with its holder. It waits at most <lock wait> seconds for the lock (0: not at all)
# and returns 75 when it stays held. Given an <enough> command, it starts no further delete once
# that succeeds, and waits for those under way. An entry can arrive while it works, so it lists the
# trash again until a pass deletes nothing. A reaper that has deleted something and worked for
# REAP_MAX_SEC replaces itself, between batches, with the helper now on disk. Returns 1 when an
# entry could not be deleted, when REAP_JOBS is not a positive number, or when the trash is not a
# directory of the store's own.
reap_trash() {
  local wait="$1" fd entry entries pid pids progressed failed=0 enough jobs="$REAP_JOBS" batch why reaped=0
  shift
  # A count that is no number would never fill a batch, and every entry would be deleted at once.
  [[ "$REAP_JOBS" =~ ^[1-9][0-9]*$ ]] || { log "KNOWN_CI_REAP_JOBS must be a positive number, got '$REAP_JOBS'"; return 1; }
  if [ -L "$TRASH_DIR" ] || { [ -e "$TRASH_DIR" ] && [ ! -d "$TRASH_DIR" ]; }; then
    log "$TRASH_DIR is a link or not a directory; refusing to delete through it"
    return 1
  fi
  [ -d "$TRASH_DIR" ] || return 0
  exec {fd}<"$TRASH_DIR"
  if ! flock -w "$wait" "$fd"; then
    exec {fd}<&-
    return 75
  fi
  # The reaper begins at one at a time, so that its first batch says so when it takes more.
  [ "$ACTION" != reap ] || jobs=1
  while :; do
    progressed=0
    failed=0
    enough=0
    pids=()
    # Only the trash's own entries, by their full path: nothing outside it is ever named.
    mapfile -d '' -t entries < <(find "$TRASH_DIR" -mindepth 1 -maxdepth 1 -print0 | LC_ALL=C sort -z)
    # A last, empty round collects the deletes still under way after the last entry.
    for entry in "${entries[@]}" ""; do
      if [ -z "$entry" ] || [ "${#pids[@]}" -ge "$jobs" ]; then
        for pid in "${pids[@]}"; do
          if wait "$pid"; then progressed=1; reaped=1; else failed=$((failed + 1)); fi
        done
        pids=()
      fi
      [ -n "$entry" ] && [ "$enough" = 0 ] || continue
      if [ "$#" -gt 0 ] && "$@"; then enough=1; continue; fi
      # Gone since the listing (--remove, or a hand): nothing to delete.
      [ -e "$entry" ] || [ -L "$entry" ] || continue
      if [ "$ACTION" = reap ] && [ "${#pids[@]}" -eq 0 ]; then
        # The reaper begins a batch. The lock goes first: the new process takes it for itself.
        if [ "$reaped" = 1 ] && [ "$SECONDS" -ge "$REAP_MAX_SEC" ]; then
          log "has worked for ${SECONDS}s; starting afresh from the helper on disk"
          exec {fd}<&-
          exec "${BASH_SOURCE[0]}" reap
        fi
        if store_free "$STORE_AMPLE_FREE_PCT"; then
          batch=1; why="the store has ample room"
        else
          batch="$REAP_JOBS"; why="the store has under $STORE_AMPLE_FREE_PCT% of its space or inodes free"
        fi
        [ "$batch" = "$jobs" ] || log "deleting $batch at a time: $why"
        jobs="$batch"
      fi
      reap_entry "$entry" &
      pids+=("$!")
    done
    [ "$enough" = 0 ] && [ "$progressed" = 1 ] || break
  done
  exec {fd}<&-
  [ "$failed" -eq 0 ]
}

reap() {
  local rc=0
  reap_trash 0 || rc=$?
  if [ "$rc" = 75 ]; then
    log "another reaper holds the lock of $TRASH_DIR and deletes what is there; nothing to do"
    return 0
  fi
  return "$rc"
}

trash_backlog() { [ -n "$(find "$TRASH_DIR" -mindepth 1 -maxdepth 1 -print -quit 2>/dev/null)" ]; }

# store_free <percent>: true when the store filesystem has at least that share of its space and of
# its inodes free. A reading that cannot be made counts as not that free, so the trash is emptied
# before a copy and the reaper takes its larger batch.
store_free() {
  local size avail itotal iavail
  read -r size avail itotal iavail < <(df -B1 --output=size,avail,itotal,iavail "$STORE_DIR" 2>/dev/null | tail -n 1) || return 1
  [[ "$size $avail $itotal $iavail" =~ ^[0-9]+\ [0-9]+\ [0-9]+\ [0-9]+$ ]] || return 1
  [ $((avail * 100)) -ge $((size * $1)) ] && [ $((iavail * 100)) -ge $((itotal * $1)) ]
}

store_has_room() { store_free "$STORE_MIN_FREE_PCT"; }

# Before a ci or qae copy ("The store trash" above, "Fail closed on disk").
make_room() {
  store_has_room && return 0
  trash_backlog || return 0
  log "the store filesystem at $STORE_DIR has under $STORE_MIN_FREE_PCT% of its space or inodes free and $TRASH_DIR holds retired copies; deleting from it here, before this job's copy"
  local rc=0
  reap_trash "$REAP_LOCK_WAIT" store_has_room || rc=$?
  store_has_room && return 0
  trash_backlog || return 0
  if [ "$rc" = 75 ]; then
    log "a reaper has held the lock of $TRASH_DIR for ${REAP_LOCK_WAIT}s and the store is still short of room; refusing to start a job on it"
  else
    log "the store is still short of room and $TRASH_DIR holds copies that could not be deleted; refusing to start a job on it"
  fi
  return 1
}

backoff() {
  local failures=0 delay i
  [ -r "$FAILURES" ] && failures="$(tr -dc '0-9' < "$FAILURES")"
  failures="${failures:-0}"
  [ "$failures" -gt 0 ] || return 0
  delay="$BACKOFF_BASE"
  for ((i = 1; i < failures && delay < BACKOFF_MAX; i++)); do delay=$((delay * 2)); done
  [ "$delay" -gt "$BACKOFF_MAX" ] && delay="$BACKOFF_MAX"
  log "backing off ${delay}s after $failures consecutive failed run(s)"
  sleep "$delay"
}

# Steps 2-6 of prepare. prepare_job calls this in an || list, and bash ignores set -e for everything
# a function runs there, so each step that must stop the prepare says so itself: a slot whose mkfs
# or mount failed would otherwise leave the unit to start its container on the bare mount point, a
# directory of the host filesystem that is neither bounded nor fresh.
prepare_slot() {
  backoff
  "$HARDEN" >/dev/null || { log "lane firewall rules could not be asserted; refusing to start a job"; return 1; }
  remove_container
  unmount_slot
  install -d -m 0700 "$MOUNT_POINT" || { log "could not make the slot's mount point $MOUNT_POINT; refusing to start a job"; return 1; }
  rm -f "$IMAGE_FILE" || { log "could not delete the old slot image $IMAGE_FILE; refusing to start a job"; return 1; }
  truncate -s "${KNOWN_CI_SLOT_GB:?}G" "$IMAGE_FILE" || { log "could not create the slot image $IMAGE_FILE; refusing to start a job"; return 1; }
  mkfs.ext4 -q -F -m 0 "$IMAGE_FILE" || { log "mkfs.ext4 failed on the slot image $IMAGE_FILE; refusing to start a job"; return 1; }
  mount -o loop,nodev,nosuid "$IMAGE_FILE" "$MOUNT_POINT" || { log "could not mount the slot image $IMAGE_FILE at $MOUNT_POINT; refusing to start a job on the bare directory"; return 1; }
  # A leftover of an earlier run of this instance: its cleanup was killed, or never ran.
  retire_store "$SNAPSHOT"
  if [ "$KIND" = wait ]; then
    # mkdir, not install -d: it fails on a leftover that could not be retired, so a wait job never
    # starts on another job's store.
    mkdir -m 0700 "$SNAPSHOT" || { log "could not make the empty store $SNAPSHOT"; return 1; }
  else
    make_room || return 1
    snapshot
  fi
}

# Everything in the Codex store but the login: a job's session rollouts, logs_*.sqlite,
# memories_*.sqlite and state_*.sqlite hold what the model read (a QAE site step's login included),
# and Codex also obeys AGENTS.md, rules/ and skills/ in its home, so nothing else may persist.
prune_codex_store() {
  find "$1" -mindepth 1 -maxdepth 1 ! -name auth.json -exec rm -rf -- {} +
}

# reset_codex_store <store> <owner>: the store as a Codex run gets it: only auth.json survives, the
# tracked config.toml is written fresh, and the store is <owner>'s (the job's uid for a QAE job, the
# lane user for the keepalive). -h and chown -R's default of not following links keep a link a job
# left in the store from pointing a root chown anywhere else.
reset_codex_store() {
  local store="$1"
  [ -d "$store" ] && [ ! -L "$store" ] || { log "no Codex store directory at $store"; return 1; }
  [ -r "$KNOWN_CI_CODEX_CONFIG" ] || { log "no tracked Codex config at $KNOWN_CI_CODEX_CONFIG (the lane provisioner renders it)"; return 1; }
  prune_codex_store "$store"
  cp "$KNOWN_CI_CODEX_CONFIG" "$store/config.toml"
  chmod 0600 "$store/config.toml"
  chmod 0700 "$store"
  chown -hR "$2" "$store"
}

# return_codex_store <store>: after a QAE job (or the keepalive's run) the store is pruned to the
# login at once, not at the next QAE job, so what the model read does not sit on the machine in
# between, and goes back to the lane user, whose `codex login` seeds it.
return_codex_store() {
  local store="$1"
  [ -d "$store" ] && [ ! -L "$store" ] || return 0
  prune_codex_store "$store" || log "could not prune $store to its login"
  chown -hR "$KNOWN_CI_CODEX_OWNER:" "$store" || log "could not give $store back to $KNOWN_CI_CODEX_OWNER"
}

# with_codex_lock <n> <wait> <command...>: runs the command holding qae instance n's store lock
# ("The Codex store lock" above), waiting at most <wait> seconds for it (0: not at all). Returns 75
# without running the command when the lock stays held.
with_codex_lock() {
  local n="$1" wait="$2" fd rc=0
  shift 2
  [ -d "$RUN_DIR/qae" ] || install -d -m 0700 "$RUN_DIR" "$RUN_DIR/qae"
  exec {fd}>"$RUN_DIR/qae/$n.codex.lock"
  if ! flock -w "$wait" "$fd"; then
    exec {fd}>&-
    return 75
  fi
  "$@" || rc=$?
  exec {fd}>&-
  return "$rc"
}

# The login's last_refresh, and nothing else of auth.json: no token is ever read into the script.
last_refresh() { jq -er '.last_refresh | strings' "$1/auth.json" 2>/dev/null; }

# refresh_codex_store <n> <store>: the keepalive of one store, run holding its lock.
refresh_codex_store() {
  local n="$1" store="$2" before before_s age after workdir out rc=0
  if [ -e "$RUN_DIR/qae/$n/job" ]; then
    log "Codex store $n ($store): instance $n has a job; skipped (the job's Codex refreshes the login)"
    return 0
  fi
  if ! before="$(last_refresh "$store")" || ! before_s="$(date -d "$before" +%s 2>/dev/null)"; then
    log "Codex store $n ($store): auth.json has no readable last_refresh, so it is not a ChatGPT login Codex refreshes"
    return 1
  fi
  age=$(( $(date +%s) - before_s ))
  if [ "$age" -lt "$KEEPALIVE_AFTER_SEC" ]; then
    log "Codex store $n ($store): last refreshed $before, $(( age / 3600 ))h ago; fresh, skipped (Codex refreshes it at $(( KEEPALIVE_AFTER_SEC / 86400 )) days)"
    return 0
  fi
  reset_codex_store "$store" "$KNOWN_CI_CODEX_OWNER:" || return 1
  # An empty working root: nothing there for the model to read or obey.
  workdir="$(mktemp -d)"
  chown "$KNOWN_CI_CODEX_OWNER:" "$workdir"
  out="$(runuser -u "$KNOWN_CI_CODEX_OWNER" -- env CODEX_HOME="$store" HOME="$LANE_HOME" PATH=/usr/bin:/bin \
    timeout -k 10 "$KEEPALIVE_TIMEOUT" "$CODEX_BIN" exec --skip-git-repo-check --ephemeral --sandbox read-only \
    -C "$workdir" "Reply with the single word OK." < /dev/null 2>&1)" || rc=$?
  rm -rf "$workdir"
  return_codex_store "$store"
  after="$(last_refresh "$store" || true)"
  if [ "$rc" -ne 0 ]; then
    log "Codex store $n ($store): codex exec failed (exit $rc), last_refresh still $before; its output ends: $(tail -n 3 <<< "$out" | tr '\n' ' ')"
    return 1
  fi
  if [ "$after" = "$before" ]; then
    log "Codex store $n ($store): codex exec ran but last_refresh is still $before: Codex did not refresh the login"
    return 1
  fi
  log "Codex store $n ($store): refreshed, last_refresh $before -> $after"
}

codex_keepalive() {
  local n=0 var store rc failed=0
  while :; do
    n=$((n + 1))
    var="KNOWN_CI_CODEX_STORE_$n"
    store="${!var:-}"
    [ -n "$store" ] || break
    if [ ! -s "$store/auth.json" ]; then
      log "Codex store $n ($store): no login yet; skipped"
      continue
    fi
    rc=0
    with_codex_lock "$n" 0 refresh_codex_store "$n" "$store" || rc=$?
    case "$rc" in
      0) ;;
      75) log "Codex store $n ($store): its lock is held (a QAE job is using it); skipped (the job's Codex refreshes the login)" ;;
      *) failed=$((failed + 1)) ;;
    esac
  done
  [ "$n" -gt 1 ] || { log "no Codex store to keep (KNOWN_CI_CODEX_STORE_1 is unset)"; return 2; }
  [ "$failed" -eq 0 ] || { log "$failed Codex store(s) could not be refreshed"; return 1; }
}

# $1: the deregistration's HTTP status; $2: 1 when the listener idle-stopped the slot.
account_run() {
  # An idle slot reaching RuntimeMaxSec (result "timeout", runner still registered, so the DELETE
  # answered 204) and a slot the listener idle-stopped are ordinary on a quiet day, not failures:
  # counting them would leave the instance sleeping the maximum back-off when the next burst comes.
  if [ "$2" = 1 ] || [ "${SERVICE_RESULT:-}" = success ] || { [ "${SERVICE_RESULT:-}" = timeout ] && [ "$1" = 204 ]; }; then
    rm -f "$FAILURES"
  else
    local failures=0
    [ -r "$FAILURES" ] && failures="$(tr -dc '0-9' < "$FAILURES")"
    install -d -m 0700 "$(dirname "$FAILURES")"
    printf '%s\n' "$(( ${failures:-0} + 1 ))" > "$FAILURES"
    log "run ended with result '${SERVICE_RESULT:-unknown}' (exit ${EXIT_STATUS:-?}); consecutive failures: $(( ${failures:-0} + 1 ))"
  fi
}

# The runners endpoint of a job file's scope target: the organisation, or owner/repo.
runner_endpoint() {
  case "$KNOWN_CI_SCOPE:$1" in
    org:*/*) return 1 ;;
    org:?*) [[ "$1" =~ ^[A-Za-z0-9][A-Za-z0-9-]*$ ]] && printf 'orgs/%s/actions/runners' "$1" ;;
    user:?*) [[ "$1" =~ ^[A-Za-z0-9][A-Za-z0-9-]*/[A-Za-z0-9._-]+$ ]] && printf 'repos/%s/actions/runners' "$1" ;;
    *) return 1 ;;
  esac
}

prepare_job() {
  if [ ! -s "$JIT" ] || [ ! -s "$JOB" ]; then
    log "no job here: $JIT and $JOB are absent. The lane's listener starts its slots, one per job; a unit started by hand has nothing to run"
    return 1
  fi
  local target="" set_id="" runner_id="" runner_name="" store_note
  read -r target _ set_id runner_id runner_name < "$JOB" || true
  prepare_slot || return 1
  if [ "$KIND" = wait ]; then
    store_note="an empty inner Docker store made (a wait job gets no copy of the preloaded one)"
  else
    store_note="store snapshot of $KNOWN_CI_IMAGE_TAG made"
  fi
  if [ "$KIND" = qae ]; then
    local rc=0
    with_codex_lock "$SLOT" "$CODEX_LOCK_WAIT" reset_codex_store "$CODEX_STORE" "$JOB_UID:$JOB_UID" || rc=$?
    [ "$rc" != 75 ] || log "the lock of Codex store $CODEX_STORE stayed held for ${CODEX_LOCK_WAIT}s (the lane's keepalive refreshing it?); refusing to start a job on it"
    [ "$rc" = 0 ] || return 1
  fi
  log "slot ready: ${KNOWN_CI_SLOT_GB}G image mounted, $store_note, runner $runner_name (id $runner_id) of $target set $set_id"
}

cleanup_job() {
  remove_container
  local target="" set_id="" runner_id="" runner_name="" status="" idle_stopped=0 had_job=0 endpoint
  if [ -r "$JOB" ]; then
    had_job=1
    read -r target _ set_id runner_id runner_name < "$JOB" || true
  fi
  if [ -e "$IDLE_STOP" ]; then
    idle_stopped=1
    log "idle-stopped: the listener removed runner ${runner_name:-?} itself; nothing to deregister"
  elif [ -n "$runner_id" ]; then
    # 204: it never ran its job (a crash, or idle past RuntimeMaxSec), so remove it now rather than
    # leave an offline registration. 404: GitHub already removed it after its one job. 422: still
    # attached to a job GitHub has not closed; the provisioner's sweep catches it.
    if endpoint="$(runner_endpoint "$target")"; then
      status="$(github /dev/null -X DELETE "$API/$endpoint/$runner_id")" || status="000"
      log "deregister runner $runner_id ($runner_name) of $target: HTTP $status"
    else
      log "the job file names no $KNOWN_CI_SCOPE target ('$target'); runner $runner_id was not deregistered"
    fi
  fi
  unmount_slot
  retire_store "$SNAPSHOT"
  rm -f "$IMAGE_FILE"
  if [ "$KIND" = qae ] && [ -n "$CODEX_STORE" ]; then
    with_codex_lock "$SLOT" "$CODEX_LOCK_WAIT" return_codex_store "$CODEX_STORE" \
      || log "the lock of Codex store $CODEX_STORE stayed held for ${CODEX_LOCK_WAIT}s; the store stays as the job left it, for the next prepare to reset"
  fi
  # A unit with no job file ran nothing (it was started by hand and refused), so there is no
  # outcome to count.
  if [ "$had_job" = 1 ]; then account_run "$status" "$idle_stopped"; fi
  rm -f "$JIT" "$IDLE_STOP"
  rm -f "$JOB"
  rmdir "$SLOT_RUN" 2>/dev/null || true
}

case "$ACTION" in
  prepare) prepare_job ;;
  cleanup) cleanup_job ;;
  snapshot) snapshot ;;
  discard) discard ;;
  reap) reap ;;
  codex-keepalive) codex_keepalive ;;
esac
