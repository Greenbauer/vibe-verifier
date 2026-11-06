# shellcheck shell=bash
# apply-listener.sh: --apply's phases J to N: the runner image, the listener, the always-on
# template's retirement, the listener's health timer and the Codex keepalive; then the gates.
# Sourced by bin/provision-lane.sh after it has read the lane's settings and set its paths; never run.
# shellcheck disable=SC2153 # the settings are assigned by read_settings (lib/provision.sh)

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
