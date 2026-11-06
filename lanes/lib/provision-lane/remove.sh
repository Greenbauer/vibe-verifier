# shellcheck shell=bash
# remove.sh: --remove: the lane's teardown.
# Sourced by bin/provision-lane.sh after it has read the lane's settings and set its paths; never run.
# shellcheck disable=SC2153 # the settings are assigned by read_settings (lib/provision.sh)

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
    # shellcheck disable=SC2034 # name, status and busy only fill the row's columns
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
