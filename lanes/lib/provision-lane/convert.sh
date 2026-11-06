# shellcheck shell=bash
# convert.sh: --convert-store: the move of the lane's store from XFS to btrfs.
# Sourced by bin/provision-lane.sh after it has read the lane's settings and set its paths; never run.

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
  if [ -f "$UNIT_DIR/$REAPER_TIMER" ]; then
    run_mutation systemctl stop "$REAPER_TIMER" "$REAPER_SERVICE"
    # A reaper stopped mid-run ends "failed (signal)", which --check reports as a reaper that could
    # not delete a retired copy until its next run (seen on a live conversion, 2026-10-08). The stop
    # is this run's, not a failure of the reaper's, so its state is cleared here.
    run_mutation systemctl reset-failed "$REAPER_SERVICE" || true
  fi
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
