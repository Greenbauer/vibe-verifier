#!/usr/bin/env bash
# provision-dashboards.sh: install and own a machine's Vibe Verifier CI dashboards.
#
#   provision-dashboards.sh <host> [--check | --apply | --remove [--apply]]
#
# <host> names hosts/<host>.yml in the machine's values checkout, whose `dashboards` declare each
# dashboard: a loopback web service
# with a read token from one of the machine's lanes' App and telemetry from that lane, all run from
# Vibe Verifier's Linux host kit in /opt/vibe-verifier. This automates the kit's runbook (Vibe Verifier
# docs/dashboard-host.md) per machine; the kit's follower then keeps that checkout on Vibe Verifier's
# green main. Design: README.md, "Dashboards". Dry-run by default; --check is read-only and exits
# non-zero until every dashboard is converged and healthy. runners-pull.service runs --apply after
# the lanes. Linux-only by design: Ubuntu 24.04, bash 5, systemd, nftables, ss, curl, jq, git,
# openssl, flock. Run it as root.
#
# --apply converges, in order, each step skipped when already converged:
#   A  /opt/vibe-verifier, cloned only when absent; the follower owns it afterwards (never reset here)
#   B  per dashboard: its account vibe-dashboard-<name> (system, nologin), the runbook's directories,
#      and /etc/vibe-dashboard/<name>/: dashboard.json (the account's, 0600; the dashboard's own loader
#      must accept a new one first), host.env (0644), app.pem (0600, the lane key's copy), collector.json
#   C  the kit's unit templates from /opt/vibe-verifier/dashboard/systemd/; a drop-in giving the
#      follower one --unit pair per dashboard; a drop-in making every dashboard require the guard
#   D  the loopback guard: table inet runners_dashboards lets only root, a dashboard's account and its
#      bridge account connect to its port; runners-dashboard-guard.service loads it at boot
#   E  a dashboard not yet running mints its token once (a failure stops the run before the legacy
#      install is touched) and samples its telemetry once
#   F  the legacy install (an earlier dashboard install on the same ports, named below), while any of
#      it is found: its cron files removed (a run they started waited out), then its dashboard units
#      and timers stopped and disabled
#   G  the dashboards, their timers and the follower started (a dashboard restarted when its files or
#      template changed); each must answer GET /api/dashboard with 200 and "version": 1 from its own
#      unit within 30 seconds, or the run fails with that unit's state
# A dashboard the host file no longer declares is removed (units, files, cache, account). A lane whose App key
# is absent stops its dashboard at an OPERATOR ACTION gate. --remove removes every dashboard, the
# follower, the guard and the templates, and leaves /opt/vibe-verifier, the retired legacy install
# (unit files, accounts, data, SSH bridge, HTTPS routes) and the lanes.
# shellcheck disable=SC2153 # the settings are assigned by read_settings (lib/provision.sh)
set -euo pipefail
umask 022

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck source-path=SCRIPTDIR/.. source=lib/provision.sh
. "$REPO_ROOT/lib/provision.sh"

# Every path is overridable only so the hermetic test can run unprivileged. The unit templates read
# /etc/vibe-dashboard/<name>, /var/lib/vibe-dashboard/<name> and /var/lib/vibe-dashboard-state/<name>,
# and the web unit's CacheDirectory= makes systemd create /var/cache/vibe-dashboard-<name>, where a
# dashboard keeps its last reading across a restart. Nothing here creates it; removal deletes it.
UNIT_DIR="${RUNNERS_UNIT_DIR:-/etc/systemd/system}"
VV="${RUNNERS_VV_CHECKOUT:-/opt/vibe-verifier}"
VV_URL="https://github.com/Greenbauer/vibe-verifier.git"
ETC_BASE="${RUNNERS_DASHBOARD_ETC:-/etc/vibe-dashboard}"
DATA_BASE="${RUNNERS_DASHBOARD_DATA:-/var/lib/vibe-dashboard}"
STATE_BASE="${RUNNERS_DASHBOARD_STATE:-/var/lib/vibe-dashboard-state}"
CACHE_BASE="${RUNNERS_DASHBOARD_CACHE:-/var/cache}"
GUARD_FILE="${RUNNERS_DASHBOARD_GUARD:-/etc/runners-dashboard-guard.nft}"
CRON_DIR="${RUNNERS_CRON_DIR:-/etc/cron.d}"
LEGACY_LOCK_DIR="${RUNNERS_LEGACY_LOCK_DIR:-/run}"
GUARD_UNIT="runners-dashboard-guard.service"
GUARD_TABLE="runners_dashboards"
FOLLOWER="vibe-dashboard-follow.service"
FOLLOW_DROPIN="$UNIT_DIR/$FOLLOWER.d/runners.conf"
WEB_DROPIN="$UNIT_DIR/vibe-dashboard@.service.d/runners-guard.conf"
# The legacy install: two earlier dashboard installs some machines already running still carry, each
# a cron file (its deploy, holding /run/<name>.lock while it runs) and a family of units, all serving
# the loopback ports the dashboards here take over. The names are theirs and are kept as they were.
LEGACY=(greenbauer-dashboard ci-dashboard)
LEGACY_UNITS=(
  greenbauer-dashboard.service greenbauer-dashboard-boundary.service
  greenbauer-dashboard-telemetry.timer greenbauer-dashboard-telemetry.service
  greenbauer-dashboard-token-refresh.timer greenbauer-dashboard-token-refresh.service
  ci-dashboard.service ci-dashboard-boundary.service ci-dashboard-telemetry.timer
  ci-dashboard-telemetry.service ci-dashboard-token.timer ci-dashboard-token.service
)
# The kit's follower allows a restarted dashboard the same 30 seconds.
HEALTH_SEC=30
APPLY=0
CHECK=0
REMOVE=0
HOST=""

usage() {
  cat <<EOF
usage: $0 <host> [--check | --apply | --remove [--apply]]

<host> names hosts/<host>.yml in the values checkout. Default is dry-run planning. --apply converges
the machine's dashboards. --check is read-only and exits non-zero until they are converged and
healthy. --remove plans the teardown; --remove --apply performs it.
EOF
}

while [ $# -gt 0 ]; do
  case "$1" in
    --apply) APPLY=1 ;;
    --check) CHECK=1 ;;
    --remove) REMOVE=1 ;;
    -h|--help) usage; exit 0 ;;
    -*) die "unknown arg: $1" ;;
    *) [ -z "$HOST" ] || die "one host only, got '$HOST' and '$1'"; HOST="$1" ;;
  esac
  shift
done
[ -n "$HOST" ] || { usage >&2; exit 2; }
[[ "$HOST" =~ ^[a-z0-9][a-z0-9-]*$ ]] || die "invalid host '$HOST' (allowed: a-z 0-9 -)"
[ "$CHECK" = "1" ] && { [ "$APPLY" = "1" ] || [ "$REMOVE" = "1" ]; } && die "--check is read-only; do not combine it with --apply or --remove"
if [ "${RUNNERS_ALLOW_NON_ROOT:-0}" != "1" ]; then
  [ "$(id -u)" = "0" ] || die "must run as root (accounts, systemd units, nftables)"
fi
# The host file lives in the machine's values checkout, the owner's private repository.
HOST_YML="${RUNNERS_HOST_YML:-${RUNNERS_CONFIG:-/opt/runner-lanes-config}/hosts/$HOST.yml}"
[ -r "$HOST_YML" ] || die "no host config at $HOST_YML"
DASHBOARDS="$(python3 "$REPO_ROOT/lib/lanes.py" dashboards "$HOST_YML")" || die "invalid host config: python3 lib/lanes.py dashboards $HOST_YML"

# ---- helpers ---------------------------------------------------------------------------------------
load() { read_settings dashboard-env "$HOST_YML" "$1"; }
uid_of() { getent passwd "$1" | cut -d: -f3; }
web() { printf 'vibe-dashboard@%s.service' "$1"; }
installed_names() { local d; for d in "$ETC_BASE"/*/; do [ -d "$d" ] && basename "$d"; done; return 0; }
declared() { grep -qx -- "$1" <<< "$DASHBOARDS"; }
key_present() { [ -s "$APP_KEY_FILE" ]; }
unit_state() { systemctl show -p ActiveState -p SubState -p Result -p NRestarts "$1" 2>/dev/null | tr '\n' ' '; }

# Write $2 to $1 when its content differs, as owner $3 (user:group) with mode $4, through a draft
# beside it that $5 (a function), when given, must accept first. Prints "changed" when it wrote (or
# would write).
converge_owned() {
  local dest="$1" content="$2" owner="$3" mode="$4" accept="${5:-}" tmp
  if [ -f "$dest" ] && [ "$(cat "$dest")" = "$content" ]; then return 0; fi
  if [ "$APPLY" != 1 ]; then log "DRY-RUN: write $dest ($owner $mode)" >&2; echo changed; return 0; fi
  tmp="$(mktemp "$dest.XXXXXX")"
  printf '%s\n' "$content" > "$tmp"
  chmod "$mode" "$tmp"
  chown "$owner" "$tmp"
  if [ -n "$accept" ] && ! "$accept" "$tmp"; then
    rm -f "$tmp"
    die "the dashboard's own loader refused the new $dest; the old file stays (fix the config in hosts/$HOST.yml)"
  fi
  mv -f "$tmp" "$dest"
  log "wrote $dest" >&2
  echo changed
}

# The dashboard's own configuration loader, from the checkout it runs, as its account will read the file.
vv_accepts() {
  python3 -B -s -c 'import sys; sys.path.insert(0, sys.argv[1]); from dashboard.config import load_config; load_config(sys.argv[2], uid=int(sys.argv[3]))' \
    "$VV" "$1" "$(uid_of "$ACCOUNT")"
}

# 200 and "version": 1 on the port, served by the unit's own process (not a legacy one still bound).
healthy() {  # $1: port, $2: unit, $3: seconds
  local i main listener
  for ((i = 0; i < $3; i++)); do
    main="$(systemctl show -p MainPID --value "$2" 2>/dev/null || true)"
    listener="$(ss -Hltnp "sport = :$1" 2>/dev/null | grep -o 'pid=[0-9]*' | head -n 1 || true)"
    if [ -n "$main" ] && [ "$main" != 0 ] && [ "$listener" = "pid=$main" ] \
      && curl -fsS --max-time 5 "http://127.0.0.1:$1/api/dashboard" 2>/dev/null | jq -e '.version == 1' >/dev/null 2>&1; then
      return 0
    fi
    sleep 1
  done
  return 1
}

# ---- renderers -------------------------------------------------------------------------------------
render_host_env() {
  cat <<EOF
# Managed by the runner lanes kit (bin/provision-dashboards.sh); do not edit on the machine.
# The loopback port, and the App, installation and login of the lane $LANE.
PORT=$PORT
APP=$APP_ID
INSTALLATION=$APP_INSTALLATION_ID
LOGIN=$APP_LOGIN
EOF
}

# The template's own command with one --unit pair per running dashboard; empty when the template's
# ExecStart no longer has the shape this reads.
render_follow_dropin() {
  local command name pairs=""
  command="$(sed -n 's#^ExecStart=\(.*/bin/vibe-dashboard-follow\) --unit .*#\1#p' "$VV/dashboard/systemd/$FOLLOWER" 2>/dev/null)"
  [ -n "$command" ] || return 0
  for name in "${LIVE[@]}"; do load "$name"; pairs+=" --unit $(web "$name") $PORT"; done
  cat <<EOF
# Managed by the runner lanes kit (bin/provision-dashboards.sh); do not edit on the machine.
# One --unit pair per dashboard of hosts/$HOST.yml; the rest is the template's own command.
[Service]
ExecStart=
ExecStart=$command$pairs
EOF
}

render_web_dropin() {
  cat <<EOF
# Managed by the runner lanes kit (bin/provision-dashboards.sh); do not edit on the machine.
# A dashboard starts only behind the loopback guard on its port.
[Unit]
Requires=$GUARD_UNIT
After=$GUARD_UNIT
EOF
}

render_guard() {
  local name uids
  printf '%s\n' "# Managed by the runner lanes kit (bin/provision-dashboards.sh); do not edit on the machine." \
    "# The dashboards trust direct loopback reads: only root, a dashboard's account and its bridge" \
    "# account may connect to its port." \
    "add table inet $GUARD_TABLE" "flush table inet $GUARD_TABLE" "table inet $GUARD_TABLE {" " chain output {" \
    "  type filter hook output priority -25; policy accept;"
  for name in $DASHBOARDS; do
    load "$name"
    uids="0, $(uid_of "$ACCOUNT" || true)${BRIDGE_ACCOUNT:+, $(uid_of "$BRIDGE_ACCOUNT" || true)}"
    printf '  %s tcp dport %s meta skuid != { %s } reject\n' "ip daddr 127.0.0.0/8" "$PORT" "$uids" "ip6 daddr ::1" "$PORT" "$uids"
  done
  printf '%s\n' " }" "}"
}

render_guard_unit() {
  cat <<EOF
# Managed by the runner lanes kit (bin/provision-dashboards.sh); do not edit on the machine.
# Loads the dashboards' loopback guard ($GUARD_FILE); every dashboard requires it.
[Unit]
Description=runners: only root and each dashboard's own and bridge accounts reach its loopback port

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=/usr/sbin/nft --file $GUARD_FILE
ExecStop=-/usr/sbin/nft delete table inet $GUARD_TABLE

[Install]
WantedBy=multi-user.target
EOF
}

# ---- apply phases ----------------------------------------------------------------------------------
require_tools() {
  local tool name missing=""
  for tool in git curl jq nft ss openssl flock; do command -v "$tool" >/dev/null 2>&1 || missing+="$tool "; done
  [ -z "$missing" ] || die "missing on this machine: ${missing% }"
  for name in $DASHBOARDS; do
    load "$name"
    [ -z "$BRIDGE_ACCOUNT" ] || getent passwd "$BRIDGE_ACCOUNT" >/dev/null \
      || die "dashboard $name names the bridge account $BRIDGE_ACCOUNT, which this machine lacks; the guard would lock its route out"
  done
}

ensure_checkout() {
  log "Phase A: $VV, the Vibe Verifier checkout the dashboards run"
  if [ -d "$VV/.git" ]; then log "  present at $(git -C "$VV" rev-parse --short HEAD); its follower moves it"; return 0; fi
  [ ! -e "$VV" ] || die "$VV exists but is not a git checkout; move it aside first"
  run_mutation git clone --quiet "$VV_URL" "$VV"
}

declare -A CHANGED=()
LIVE=()
ensure_dashboard() {  # $1: name
  local name="$1" etc="$ETC_BASE/$1" changed=""
  load "$name"
  log "Phase B: dashboard $name (port $PORT, lane $LANE, account $ACCOUNT${BRIDGE_ACCOUNT:+, bridge $BRIDGE_ACCOUNT})"
  getent passwd "$ACCOUNT" >/dev/null \
    || run_mutation useradd --system --user-group --no-create-home --home-dir "$DATA_BASE/$name" --shell /usr/sbin/nologin "$ACCOUNT"
  ensure_dir "$ETC_BASE" 0755; ensure_dir "$etc" 0755
  ensure_dir "$DATA_BASE" 0755; ensure_dir "$DATA_BASE/$name" 0755; ensure_dir "$DATA_BASE/$name/gh" 0755
  ensure_dir "$STATE_BASE" 0755; ensure_dir "$STATE_BASE/$name" 0700
  changed+="$(converge_owned "$etc/dashboard.json" "$(python3 "$REPO_ROOT/lib/lanes.py" dashboard-config "$HOST_YML" "$name")" "$ACCOUNT:$ACCOUNT" 0600 vv_accepts)"
  changed+="$(converge_owned "$etc/host.env" "$(render_host_env)" root:root 0644)"
  converge_owned "$etc/collector.json" "$(python3 "$REPO_ROOT/lib/lanes.py" dashboard-collector "$HOST_YML" "$name")" root:root 0600 >/dev/null
  CHANGED[$name]="$changed"
  if ! key_present; then
    log "  the lane's App key $APP_KEY_FILE is absent; this dashboard waits for it (see the gate below)"
    return 0
  fi
  cmp -s "$APP_KEY_FILE" "$etc/app.pem" || run_mutation install -m 0600 "$APP_KEY_FILE" "$etc/app.pem"
  LIVE+=("$name")
}

# The units whose files this run changed, by the unit a change restarts: a template is its own unit,
# the follower's drop-in the follower, the guard drop-in every dashboard.
UNITS_CHANGED=""
converge_unit() {  # $1: path, $2: content, $3: the unit it changes
  local changed
  changed="$(converge_file "$1" "$2")"
  [ -z "$changed" ] || UNITS_CHANGED+="$3 "
}

ensure_units() {
  local file follow
  log "Phase C: the host kit's unit templates, the follower's dashboards and the guard requirement"
  if [ ! -d "$VV/dashboard/systemd" ]; then
    log "DRY-RUN: install the unit templates from $VV/dashboard/systemd/ and the follower's drop-in once $VV is cloned"
  else
    for file in "$VV"/dashboard/systemd/*; do converge_unit "$UNIT_DIR/$(basename "$file")" "$(cat "$file")" "$(basename "$file")"; done
    if [ "${#LIVE[@]}" -gt 0 ]; then
      follow="$(render_follow_dropin)"
      [ -n "$follow" ] || die "cannot read the follower's command from $VV/dashboard/systemd/$FOLLOWER (ExecStart=... /bin/vibe-dashboard-follow --unit ...)"
      converge_unit "$FOLLOW_DROPIN" "$follow" "$FOLLOWER"
    fi
  fi
  converge_unit "$WEB_DROPIN" "$(render_web_dropin)" vibe-dashboard@.service
  converge_unit "$UNIT_DIR/$GUARD_UNIT" "$(render_guard_unit)" "$GUARD_UNIT"
  [ -z "$UNITS_CHANGED" ] || run_mutation systemctl daemon-reload
}

ensure_guard() {
  local changed
  log "Phase D: the loopback guard (nftables table inet $GUARD_TABLE, $GUARD_FILE)"
  changed="$(converge_file "$GUARD_FILE" "$(render_guard)" 0600)"
  if ! unit_running "$(systemctl is-active "$GUARD_UNIT" 2>/dev/null || true)"; then
    run_mutation systemctl enable --now "$GUARD_UNIT"
  elif [ -n "$changed" ] || ! nft list table inet "$GUARD_TABLE" >/dev/null 2>&1; then
    run_mutation nft --file "$GUARD_FILE"
  fi
}

legacy_present() {
  local name unit
  for name in "${LEGACY[@]}"; do [ ! -e "$CRON_DIR/$name" ] || return 0; done
  for unit in "${LEGACY_UNITS[@]}"; do
    [ -f "$UNIT_DIR/$unit" ] || continue
    [ "$(systemctl is-enabled "$unit" 2>/dev/null || true)" != enabled ] || return 0
    ! unit_running "$(systemctl is-active "$unit" 2>/dev/null || true)" || return 0
  done
  return 1
}

retire_legacy() {
  local name unit present=() enabled=()
  log "Phase F: the legacy dashboard install (${LEGACY[*]})"
  if ! legacy_present; then log "  retired"; return 0; fi
  for name in "${LEGACY[@]}"; do
    [ ! -e "$CRON_DIR/$name" ] || run_mutation rm -f "$CRON_DIR/$name"
    # A deploy that cron started before the file went may still be converging the old units.
    [ ! -e "$LEGACY_LOCK_DIR/$name.lock" ] || run_mutation flock -w 600 "$LEGACY_LOCK_DIR/$name.lock" true
  done
  for unit in "${LEGACY_UNITS[@]}"; do
    [ -f "$UNIT_DIR/$unit" ] || continue
    present+=("$unit")
    [ "$(systemctl is-enabled "$unit" 2>/dev/null || true)" != enabled ] || enabled+=("$unit")
  done
  [ "${#enabled[@]}" -eq 0 ] || run_mutation systemctl disable "${enabled[@]}"
  [ "${#present[@]}" -eq 0 ] || run_mutation systemctl stop "${present[@]}"
}

start_dashboards() {
  local name unit first=() restart_all=""
  # The legacy install holds the ports, and it goes only once every dashboard here can take its port.
  if [ "${#LIVE[@]}" -lt "$(wc -w <<< "$DASHBOARDS")" ] && legacy_present; then
    log "Phases E to G: nothing starts while a dashboard waits for its lane's key and the legacy install holds the ports"
    return 0
  fi
  log "Phase E: a dashboard not yet running mints its token and samples its telemetry once"
  for name in "${LIVE[@]}"; do
    ! unit_running "$(systemctl is-active "$(web "$name")" 2>/dev/null || true)" || continue
    first+=("$name")
    unit="vibe-dashboard-token@$name.service"
    run_mutation systemctl start "$unit" \
      || die "dashboard $name: $unit failed ($(unit_state "$unit")); nothing of the legacy install was touched. journalctl -u $unit"
    unit="vibe-dashboard-telemetry@$name.service"
    run_mutation systemctl start "$unit" || warn "dashboard $name: $unit failed ($(unit_state "$unit")); it serves without telemetry until its timer's next run succeeds"
  done
  retire_legacy
  log "Phase G: the dashboards, their timers and the follower"
  [[ " $UNITS_CHANGED " != *" vibe-dashboard@.service "* ]] || restart_all=1
  for name in "${LIVE[@]}"; do
    run_mutation systemctl enable --now "$(web "$name")" "vibe-dashboard-token@$name.timer" "vibe-dashboard-telemetry@$name.timer"
    if [[ " ${first[*]} " != *" $name "* ]] && [ -n "${CHANGED[$name]}$restart_all" ]; then
      run_mutation systemctl restart "$(web "$name")"
    fi
  done
  for name in "${LIVE[@]}"; do
    load "$name"
    if [ "$APPLY" != 1 ]; then log "DRY-RUN: then $(web "$name") must answer http://127.0.0.1:$PORT/api/dashboard with 200 and version 1 within ${HEALTH_SEC}s"; continue; fi
    if ! healthy "$PORT" "$(web "$name")" "$HEALTH_SEC"; then
      journalctl -u "$(web "$name")" -n 20 --no-pager >&2 || true
      die "dashboard $name did not answer http://127.0.0.1:$PORT/api/dashboard with 200 and version 1 from $(web "$name") within ${HEALTH_SEC}s ($(unit_state "$(web "$name")")); its journal is above. The legacy install stays retired, so this dashboard is down until a fix merges or the operator rolls back (README.md, Dashboards); every runners-pull tick retries"
    fi
    log "  $name answers on 127.0.0.1:$PORT"
  done
  [ "${#LIVE[@]}" -gt 0 ] || return 0
  if [[ " $UNITS_CHANGED " == *" $FOLLOWER "* ]] && unit_running "$(systemctl is-active "$FOLLOWER" 2>/dev/null || true)"; then
    run_mutation systemctl restart "$FOLLOWER"
  fi
  run_mutation systemctl enable --now "$FOLLOWER"
}

print_gates() {
  local name
  for name in $DASHBOARDS; do
    load "$name"
    key_present || gate "Dashboard $name mints its token from its lane's App key $APP_KEY_FILE, which is absent: pipe it as bin/provision-lane.sh $HOST $LANE --apply says, then re-run --apply."
  done
}

# ---- remove ----------------------------------------------------------------------------------------
remove_dashboard() {  # $1: name
  local name="$1"
  log "Remove: dashboard $name (its units, $ETC_BASE/$name, $DATA_BASE/$name, $STATE_BASE/$name, $CACHE_BASE/vibe-dashboard-$name and vibe-dashboard-$name)"
  if [ -f "$UNIT_DIR/vibe-dashboard@.service" ]; then
    run_mutation systemctl disable --now "$(web "$name")" "vibe-dashboard-token@$name.timer" "vibe-dashboard-telemetry@$name.timer"
    run_mutation systemctl stop "vibe-dashboard-token@$name.service" "vibe-dashboard-telemetry@$name.service"
  fi
  # The cache holds what the dashboard last read of private repositories, owned by the account
  # deleted below: left behind, the next account to get that uid could read it.
  run_mutation rm -rf "$ETC_BASE/$name" "$DATA_BASE/$name" "$STATE_BASE/$name" "$CACHE_BASE/vibe-dashboard-$name"
  ! getent passwd "vibe-dashboard-$name" >/dev/null || run_mutation userdel "vibe-dashboard-$name"
}

remove_shared() {
  local unit units=()
  log "Remove: the follower, the loopback guard and the unit templates"
  for unit in "$FOLLOWER" "$GUARD_UNIT"; do [ ! -f "$UNIT_DIR/$unit" ] || units+=("$unit"); done
  [ "${#units[@]}" -eq 0 ] || run_mutation systemctl disable --now "${units[@]}"
  run_mutation rm -rf "$UNIT_DIR/$GUARD_UNIT" "$GUARD_FILE" "$UNIT_DIR/vibe-dashboard@.service" "$UNIT_DIR/vibe-dashboard@.service.d" \
    "$UNIT_DIR"/vibe-dashboard-*
  run_mutation systemctl daemon-reload
}

# ---- check -----------------------------------------------------------------------------------------
fail=0
bad() { fail=1; }
check_file() {  # $1: key, $2: path, $3: owner, $4: mode, $5: expected content (optional)
  local got
  got="$(stat -c '%U %a' "$2" 2>/dev/null || echo absent)"
  if [ "$got" != "$3 $4" ]; then printf '%s=%s %s (want %s %s)\n' "$1" "$2" "$got" "$3" "$4"; bad; return 0; fi
  if [ "$#" -ge 5 ] && [ "$(cat "$2")" != "$5" ]; then printf '%s=%s DRIFTED from hosts/%s.yml (re-run --apply)\n' "$1" "$2" "$HOST"; bad; return 0; fi
  printf '%s=%s ok\n' "$1" "$2"
}
check_active() {  # $1: unit
  local state
  state="$(systemctl is-active "$1" 2>/dev/null || true)"
  printf '%s=%s\n' "$1" "${state:-unknown}"
  [ "$state" = active ] || bad
}

check_dashboard() {  # $1: name
  local name="$1" etc="$ETC_BASE/$1" unit
  load "$name"
  printf 'dashboard=%s port=%s lane=%s account=%s\n' "$name" "$PORT" "$LANE" "$(getent passwd "$ACCOUNT" >/dev/null && echo "$ACCOUNT" || echo "$ACCOUNT absent")"
  getent passwd "$ACCOUNT" >/dev/null || bad
  check_file dir "$etc" root 755
  check_file dir "$DATA_BASE/$name/gh" root 755
  check_file dir "$STATE_BASE/$name" root 700
  check_file config "$etc/dashboard.json" "$ACCOUNT" 600 "$(python3 "$REPO_ROOT/lib/lanes.py" dashboard-config "$HOST_YML" "$name")"
  check_file host_env "$etc/host.env" root 644 "$(render_host_env)"
  check_file collector "$etc/collector.json" root 600 "$(python3 "$REPO_ROOT/lib/lanes.py" dashboard-collector "$HOST_YML" "$name")"
  check_file app_key "$etc/app.pem" root 600
  if [ -f "$etc/app.pem" ] && ! cmp -s "$APP_KEY_FILE" "$etc/app.pem"; then printf 'app_key=%s differs from the lane key %s (re-run --apply)\n' "$etc/app.pem" "$APP_KEY_FILE"; bad; fi
  check_file token "$DATA_BASE/$name/gh/hosts.yml" "$ACCOUNT" 600
  for unit in "$(web "$name")" "vibe-dashboard-token@$name.timer" "vibe-dashboard-telemetry@$name.timer"; do check_active "$unit"; done
  for unit in "vibe-dashboard-token@$name.service" "vibe-dashboard-telemetry@$name.service"; do
    if [ "$(systemctl is-failed "$unit" 2>/dev/null || true)" = failed ]; then printf '%s=failed (journalctl -u %s)\n' "$unit" "$unit"; bad; fi
  done
  if healthy "$PORT" "$(web "$name")" 1; then printf 'health=%s answers on 127.0.0.1:%s\n' "$name" "$PORT"
  else printf 'health=%s does not answer on 127.0.0.1:%s from %s\n' "$name" "$PORT" "$(web "$name")"; bad; fi
}

check_host() {
  local file name
  if [ -d "$VV/.git" ]; then printf 'checkout=%s at %s\n' "$VV" "$(git -C "$VV" rev-parse --short HEAD)"; else printf 'checkout=%s absent\n' "$VV"; bad; fi
  for file in "$VV"/dashboard/systemd/*; do
    [ -f "$file" ] || continue
    cmp -s "$file" "$UNIT_DIR/$(basename "$file")" || { printf 'unit=%s absent or DRIFTED from %s (re-run --apply)\n' "$(basename "$file")" "$VV"; bad; }
  done
  LIVE=(); for name in $DASHBOARDS; do load "$name"; ! key_present || LIVE+=("$name"); done
  [ "$(cat "$FOLLOW_DROPIN" 2>/dev/null)" = "$(render_follow_dropin)" ] || { printf 'follower_dropin=%s absent or DRIFTED (re-run --apply)\n' "$FOLLOW_DROPIN"; bad; }
  [ "$(cat "$WEB_DROPIN" 2>/dev/null)" = "$(render_web_dropin)" ] || { printf 'guard_dropin=%s absent or DRIFTED (re-run --apply)\n' "$WEB_DROPIN"; bad; }
  check_active "$FOLLOWER"
  check_active "$GUARD_UNIT"
  check_file guard "$GUARD_FILE" root 600 "$(render_guard)"
  for name in $DASHBOARDS; do
    load "$name"
    nft list table inet "$GUARD_TABLE" 2>/dev/null | grep -q "dport $PORT " || { printf 'guard_live=port %s not guarded (re-run --apply)\n' "$PORT"; bad; }
  done
  if legacy_present; then printf 'legacy=present (re-run --apply to retire it)\n'; bad; else printf 'legacy=retired\n'; fi
}

# ---- main ------------------------------------------------------------------------------------------
log "host=$HOST dashboards=[$(tr '\n' ' ' <<< "$DASHBOARDS" | sed 's/ $//')] apply=$APPLY check=$CHECK remove=$REMOVE"
if [ "$CHECK" = 1 ]; then
  [ -n "$DASHBOARDS" ] || { log "no dashboards declared"; exit 0; }
  for name in $DASHBOARDS; do check_dashboard "$name"; done
  check_host
  [ "$fail" = 0 ] || exit 1
  exit 0
fi
take_apply_lock
if [ "$REMOVE" = 1 ]; then
  for name in $( { installed_names; printf '%s\n' "$DASHBOARDS"; } | sort -u); do remove_dashboard "$name"; done
  remove_shared
  log "Left in place: $VV, the retired legacy install (its unit files, accounts and data, the SSH bridge and the HTTPS routes) and every lane."
  log "done"
  exit 0
fi
for name in $(installed_names); do declared "$name" || remove_dashboard "$name"; done
if [ -z "$DASHBOARDS" ]; then
  [ ! -e "$UNIT_DIR/$GUARD_UNIT" ] || remove_shared
  log "done"
  exit 0
fi
require_tools
ensure_checkout
for name in $DASHBOARDS; do ensure_dashboard "$name"; done
ensure_units
ensure_guard
start_dashboards
print_gates
log "done"
