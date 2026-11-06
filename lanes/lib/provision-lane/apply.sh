# shellcheck shell=bash
# apply.sh: --apply's phases A0 to I: the lane's user, key, timers, store, network, firewall, runner
# group, slot templates and registration sweep.
# Sourced by bin/provision-lane.sh after it has read the lane's settings and set its paths; never run.
# shellcheck disable=SC2153 # the settings are assigned by read_settings (lib/provision.sh)

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
  # shellcheck disable=SC2034 # check.sh reads it
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
# running, and the move to the other filesystem is the operator's (print_gates, --convert-store).
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
    # shellcheck disable=SC2034 # apply-listener.sh reads it
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
