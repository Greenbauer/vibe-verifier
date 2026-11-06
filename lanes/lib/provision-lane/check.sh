# shellcheck shell=bash
# check.sh: --check: the read-only report, which exits non-zero until the lane is converged.
# Sourced by bin/provision-lane.sh after it has read the lane's settings and set its paths; never run.
# shellcheck disable=SC2153 # the settings are assigned by read_settings (lib/provision.sh)

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
