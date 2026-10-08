#!/bin/bash
# provision-dashboards-test.sh: hermetic tests for bin/provision-dashboards.sh. The system tools are
# stubs on PATH (tests/lib/stubs.sh, with the ones below in their place); Vibe Verifier's checkout is
# a stand-in with the real follower template (the catalog's own dashboard/systemd/, beside this kit)
# and a configuration loader that records what it was asked and refuses a config holding "refuse".
# The host file is the example's, in a stand-in values checkout. The stubs keep unit
# states, enablement, file owners (by path, carried by mv as a rename carries them), accounts,
# loopback listeners and the nftables table under $ST. No real account, unit, firewall, network or GitHub call happens.
set -u
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
# shellcheck source=lib/stubs.sh
. "$ROOT/tests/lib/stubs.sh"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
pass=0; fail=0
expect() { if [ "$1" -eq 0 ]; then pass=$((pass+1)); echo "✓ $2"; else fail=$((fail+1)); echo "✗ $2"; fi; }
line_of() { grep -nF -- "$1" <<< "$out" | head -n 1 | cut -d: -f1; }
before() { local a b; a="$(line_of "$1")"; b="$(line_of "$2")"; [ -n "$a" ] && [ -n "$b" ] && [ "$a" -lt "$b" ]; }
render() { python3 "$ROOT/lib/lanes.py" "$1" "$S/config/hosts/example.yml" "$2"; }
FOLLOW_CMD="/usr/bin/env -i HOME=/root PYTHONNOUSERSITE=1 PATH=/usr/bin:/bin /usr/bin/python3 /opt/vibe-verifier/bin/vibe-dashboard-follow"
MUTATIONS='^(useradd|userdel|chown|git clone|nft --file|systemctl (enable|disable|daemon-reload|start|stop|restart))'
LEGACY_UNITS="greenbauer-dashboard.service greenbauer-dashboard-boundary.service greenbauer-dashboard-telemetry.timer greenbauer-dashboard-telemetry.service greenbauer-dashboard-token-refresh.timer greenbauer-dashboard-token-refresh.service ci-dashboard.service ci-dashboard-boundary.service ci-dashboard-telemetry.timer ci-dashboard-telemetry.service ci-dashboard-token.timer ci-dashboard-token.service"

write_dashboard_stubs() {
  local p="$1"
  write_stubs "$p"
  cat > "$p/getent" <<'SH'
#!/bin/bash
[ "$1" = passwd ] || exit 2
line="$(grep "^$2 " "$ST/users" 2>/dev/null)" || exit 2
echo "$2:x:${line#* }:${line#* }::/nonexistent:/usr/sbin/nologin"
SH
  cat > "$p/useradd" <<'SH'
#!/bin/bash
echo "useradd $*" >> "$CALLLOG"
echo "${@: -1} $((900 + $(wc -l < "$ST/users")))" >> "$ST/users"
SH
  cat > "$p/userdel" <<'SH'
#!/bin/bash
echo "userdel $*" >> "$CALLLOG"
grep -v "^$1 " "$ST/users" > "$ST/users.new"; mv "$ST/users.new" "$ST/users"
SH
  # Owners by path, the last entry winning; root by default. mv gives the destination its source's
  # owner, as a rename keeps the file's, so a draft chowned and then moved over the file keeps it.
  # (By inode, a new file reusing a deleted draft's inode took that draft's owner.)
  cat > "$p/chown" <<'SH'
#!/bin/bash
echo "chown $*" >> "$CALLLOG"
echo "$2 ${1%%:*}" >> "$ST/owners"
SH
  cat > "$p/mv" <<'SH'
#!/bin/bash
src="${@: -2:1}"; dest="${@: -1}"
owner="$(awk -v f="$src" '$1 == f {o = $2} END {print o}' "$ST/owners" 2>/dev/null)"
/bin/mv "$@" || exit
echo "$dest ${owner:-root}" >> "$ST/owners"
SH
  cat > "$p/stat" <<'SH'
#!/bin/bash
[ "$1" = -c ] && [ "$2" = '%U %a' ] && [ -e "$3" ] || exit 1
owner="$(awk -v f="$3" '$1 == f {o = $2} END {print o}' "$ST/owners" 2>/dev/null)"
echo "${owner:-root} $(python3 -c 'import os, sys; print(format(os.stat(sys.argv[1]).st_mode & 0o777, "o"))' "$3")"
SH
  # Unit state in $ST/state/<unit>, enablement in $ST/enabled/<unit>, main pids in $ST/pid/<unit>,
  # and a loopback listener as $ST/listener-<port> holding its pid. A dashboard unit listens on its
  # host.env port unless WEB_FAIL names it; LEGACY_HOLDS=<port> keeps a legacy dashboard bound there.
  cat > "$p/systemctl" <<'SH'
#!/bin/bash
echo "systemctl $*" >> "$CALLLOG"
state_of() { cat "$ST/state/$1" 2>/dev/null || echo inactive; }
legacy_port() { case "$1" in greenbauer-dashboard.service) echo 8765 ;; ci-dashboard.service) echo 8766 ;; esac; }
start() {
  case "$1" in
    vibe-dashboard-token@*.service) [ "${TOKEN_FAIL:-0}" = 1 ] && { echo failed > "$ST/state/$1"; return 1; } ;;
    vibe-dashboard-telemetry@*.service) [ "${TELEMETRY_FAIL:-0}" = 1 ] && { echo failed > "$ST/state/$1"; return 1; } ;;
    runners-dashboard-guard.service) cp "$DASH_GUARD" "$ST/nft" ;;
    vibe-dashboard@*.service)
      name="${1#vibe-dashboard@}"; name="${name%.service}"
      port="$(sed -n 's/^PORT=//p' "$DASH_ETC/$name/host.env")"
      case " ${WEB_FAIL:-} " in *" $name "*) echo failed > "$ST/state/$1"; return 0 ;; esac
      echo $((5000 + RANDOM % 1000)) > "$ST/pid/$1"
      [ "${LEGACY_HOLDS:-}" = "$port" ] || cp "$ST/pid/$1" "$ST/listener-$port" ;;
  esac
  echo active > "$ST/state/$1"
}
stop() {
  local port
  port="$(legacy_port "$1")"
  [ -z "$port" ] || [ "${LEGACY_HOLDS:-}" = "$port" ] || rm -f "$ST/listener-$port"
  [ "$1" != runners-dashboard-guard.service ] || rm -f "$ST/nft"
  echo inactive > "$ST/state/$1"
}
cmd="$1"; shift
now=0; units=()
for a in "$@"; do case "$a" in --now) now=1 ;; -*) ;; *) units+=("$a") ;; esac; done
case "$cmd" in
  is-active) for u in "${units[@]}"; do state_of "$u"; done ;;
  is-enabled) [ -e "$ST/enabled/${units[0]}" ] && echo enabled || echo disabled ;;
  is-failed) state_of "${units[0]}" ;;
  show)
    u="${units[-1]}"
    case "$*" in
      *MainPID*) if [ "$(state_of "$u")" = active ]; then cat "$ST/pid/$u" 2>/dev/null || echo 0; else echo 0; fi ;;
      *) printf 'ActiveState=%s\nSubState=x\nResult=y\nNRestarts=0\n' "$(state_of "$u")" ;;
    esac ;;
  start|restart) for u in "${units[@]}"; do start "$u" || exit 1; done ;;
  stop) for u in "${units[@]}"; do stop "$u"; done ;;
  enable) for u in "${units[@]}"; do touch "$ST/enabled/$u"; [ "$now" = 0 ] || start "$u"; done ;;
  disable) for u in "${units[@]}"; do rm -f "$ST/enabled/$u"; [ "$now" = 0 ] || stop "$u"; done ;;
esac
exit 0
SH
  cat > "$p/ss" <<'SH'
#!/bin/bash
port="${@: -1}"; port="${port##*:}"
[ -f "$ST/listener-$port" ] && echo "LISTEN 0 5 127.0.0.1:$port 0.0.0.0:* users:((\"python3\",pid=$(cat "$ST/listener-$port"),fd=3))"
exit 0
SH
  cat > "$p/curl" <<'SH'
#!/bin/bash
port="${@: -1}"; port="${port#http://127.0.0.1:}"; port="${port%%/*}"
[ -f "$ST/listener-$port" ] || exit 7
echo '{"version": 1, "owner": "x"}'
SH
  cat > "$p/nft" <<'SH'
#!/bin/bash
case "$1" in
  --file) echo "nft $*" >> "$CALLLOG"; cp "$2" "$ST/nft" ;;
  list) cat "$ST/nft" 2>/dev/null ;;
  *) exit 1 ;;
esac
SH
  # git: the clone copies the stand-in checkout; rev-parse names a commit.
  cat > "$p/git" <<'SH'
#!/bin/bash
case "$1" in
  clone) echo "git $*" >> "$CALLLOG"; cp -r "$FX/vv" "${@: -1}"; mkdir "${@: -1}/.git" ;;
  -C) echo 735585a ;;
esac
SH
  printf '#!/bin/bash\nexit 0\n' > "$p/openssl"
  chmod +x "$p/"*
}

setup() {
  S="$TMP/s"
  rm -rf "$S"
  mkdir -p "$S/repo/bin" "$S/repo/lib" "$S/config/hosts" "$S/units" "$S/cron.d" "$S/locks" "$S/keys" "$S/opt" \
    "$S/st/state" "$S/st/enabled" "$S/st/pid" "$S/fx/vv/dashboard/systemd"
  cp "$ROOT"/lib/*.py "$ROOT/lib/provision.sh" "$S/repo/lib/"
  cp "$ROOT/bin/provision-dashboards.sh" "$S/repo/bin/"
  sed -e "s#/etc/greenbauer-ci/app.pem#$S/keys/greenbauer-ci.pem#" -e "s#/etc/acme-ci/app.pem#$S/keys/acme-ci.pem#" \
    "$ROOT/examples/hosts/example.yml" > "$S/config/hosts/example.yml"
  printf 'not a real greenbauer key\n' > "$S/keys/greenbauer-ci.pem"
  printf 'not a real acme key\n' > "$S/keys/acme-ci.pem"
  # The follower's real unit template: the provisioner reads its command from this file's ExecStart.
  cp "$ROOT/../dashboard/systemd/vibe-dashboard-follow.service" "$S/fx/vv/dashboard/systemd/"
  for unit in vibe-dashboard@.service vibe-dashboard-token@.service vibe-dashboard-token@.timer vibe-dashboard-telemetry@.service vibe-dashboard-telemetry@.timer; do
    printf '# stand-in for %s\n' "$unit" > "$S/fx/vv/dashboard/systemd/$unit"
  done
  mkdir -p "$S/fx/vv/dashboard"
  : > "$S/fx/vv/dashboard/__init__.py"
  cat > "$S/fx/vv/dashboard/config.py" <<'PY'
import json, os
def load_config(path, uid=None):
    with open(os.environ["ST"] + "/loader", "a") as log:
        log.write("%s %s\n" % (path, uid))
    if "refuse" in json.load(open(path)):
        raise ValueError("refused")
PY
  printf 'dashboard-forward 992\n' > "$S/st/users"
  : > "$S/st/owners"
  CALLLOG="$S/calls.log"; : > "$CALLLOG"
  export CALLLOG ST="$S/st" FX="$S/fx" UNITS="$S/units" DASH_ETC="$S/etc" DASH_GUARD="$S/guard.nft"
  write_dashboard_stubs "$S/pathbin"
}

# A machine the legacy install still runs on: both legacy families running and enabled, their cron
# files, their deploy locks, and the legacy dashboards bound to 8765 and 8766.
legacy() {
  local unit
  for unit in $LEGACY_UNITS; do
    printf '# Managed by the legacy install\n' > "$S/units/$unit"
    echo active > "$S/st/state/$unit"; touch "$S/st/enabled/$unit"
  done
  printf '*/5 * * * * root true\n' > "$S/cron.d/greenbauer-dashboard"; printf '*/5 * * * * root true\n' > "$S/cron.d/ci-dashboard"
  : > "$S/locks/greenbauer-dashboard.lock"; : > "$S/locks/ci-dashboard.lock"
  echo 111 > "$S/st/pid/greenbauer-dashboard.service"; echo 111 > "$S/st/listener-8765"
  echo 222 > "$S/st/pid/ci-dashboard.service"; echo 222 > "$S/st/listener-8766"
}

run_dash() {
  env PATH="$S/pathbin:$PATH" RUNNERS_ALLOW_NON_ROOT=1 RUNNERS_CONFIG="$S/config" ${HOST_YML_UNDER_TEST:+RUNNERS_HOST_YML="$HOST_YML_UNDER_TEST"} \
    RUNNERS_UNIT_DIR="$S/units" RUNNERS_VV_CHECKOUT="$S/opt/vibe-verifier" RUNNERS_DASHBOARD_ETC="$S/etc" \
    RUNNERS_DASHBOARD_DATA="$S/data" RUNNERS_DASHBOARD_STATE="$S/state" RUNNERS_DASHBOARD_CACHE="$S/cache" RUNNERS_DASHBOARD_GUARD="$S/guard.nft" \
    RUNNERS_CRON_DIR="$S/cron.d" RUNNERS_LEGACY_LOCK_DIR="$S/locks" RUNNERS_APPLY_LOCK="$S/apply.lock" \
    bash "$S/repo/bin/provision-dashboards.sh" "${HOST_UNDER_TEST:-example}" "$@"
}
owner_mode() { "$S/pathbin/stat" -c '%U %a' "$1"; }
uid() { awk -v u="$1" '$1 == u {print $2}' "$S/st/users"; }
legacy_untouched() {
  local unit
  [ -f "$S/cron.d/greenbauer-dashboard" ] && [ -f "$S/cron.d/ci-dashboard" ] || return 1
  for unit in $LEGACY_UNITS; do [ "$(cat "$S/st/state/$unit")" = active ] && [ -e "$S/st/enabled/$unit" ] || return 1; done
}

# ---- dry-run (the default) on a machine the legacy install still runs on ----------------------------
setup; legacy
out="$(run_dash 2>&1)"; rc=$?
[ "$rc" -eq 0 ]; expect $? "default dry-run exits 0"
grep -q "DRY-RUN: git clone --quiet https://github.com/Greenbauer/vibe-verifier.git $S/opt/vibe-verifier" <<< "$out"; expect $? "dry-run plans the Vibe Verifier clone"
grep -q "DRY-RUN: useradd --system --user-group --no-create-home --home-dir $S/data/greenbauer --shell /usr/sbin/nologin vibe-dashboard-greenbauer" <<< "$out" \
  && grep -q "DRY-RUN: useradd .* vibe-dashboard-acme" <<< "$out"; expect $? "dry-run plans each dashboard's own nologin system account"
grep -q "DRY-RUN: write $S/etc/greenbauer/dashboard.json (vibe-dashboard-greenbauer:vibe-dashboard-greenbauer 0600)" <<< "$out" \
  && grep -q "DRY-RUN: install -m 0600 $S/keys/acme-ci.pem $S/etc/acme/app.pem" <<< "$out"; expect $? "dry-run plans each dashboard's files and its copy of the lane key"
grep -q "DRY-RUN: rm -f $S/cron.d/greenbauer-dashboard" <<< "$out" && grep -q "DRY-RUN: systemctl stop greenbauer-dashboard.service" <<< "$out" \
  && grep -q "DRY-RUN: systemctl start vibe-dashboard-token@acme.service" <<< "$out"; expect $? "dry-run plans the token check, the legacy retirement and the starts"
{ ! grep -qE "$MUTATIONS" "$CALLLOG"; } && [ ! -e "$S/etc" ] && [ ! -e "$S/opt/vibe-verifier" ] && [ -z "$(ls "$S/units" | grep vibe)" ] && legacy_untouched
expect $? "dry-run executes no mutation, writes no file and leaves the legacy install running"

# ---- apply: the cutover from the legacy install -------------------------------------------------------
setup; legacy
out="$(run_dash --apply 2>&1)"; rc=$?
[ "$rc" -eq 0 ]; expect $? "--apply cuts the machine over from the legacy install"
grep -qx "git clone --quiet https://github.com/Greenbauer/vibe-verifier.git $S/opt/vibe-verifier" "$CALLLOG"; expect $? "Vibe Verifier is cloned from GitHub when absent"
grep -qx "useradd --system --user-group --no-create-home --home-dir $S/data/greenbauer --shell /usr/sbin/nologin vibe-dashboard-greenbauer" "$CALLLOG"; expect $? "each dashboard gets its own nologin system account"
[ "$(owner_mode "$S/etc/greenbauer")" = "root 755" ] && [ "$(owner_mode "$S/data/greenbauer/gh")" = "root 755" ] && [ "$(owner_mode "$S/state/greenbauer")" = "root 700" ]
expect $? "the runbook's directories: root's, 0755, and the collector state 0700"
cfg="$S/etc/greenbauer/dashboard.json"
[ "$(cat "$cfg")" = "$(render dashboard-config greenbauer)" ] && [ "$(owner_mode "$cfg")" = "vibe-dashboard-greenbauer 600" ]; expect $? "dashboard.json is the host file's config, the account's, 0600"
grep -q "^$S/etc/greenbauer/dashboard.json\.[A-Za-z0-9]* $(uid vibe-dashboard-greenbauer)$" "$S/st/loader"; expect $? "the dashboard's own loader checked a draft, as the dashboard's account, before it went live"
printf -v want '# Managed by the runner lanes kit (bin/provision-dashboards.sh); do not edit on the machine.\n# The loopback port, and the App, installation and login of the lane acme-ci.\nPORT=8766\nAPP=1000002\nINSTALLATION=20000002\nLOGIN=acme-lane-app'
[ "$(cat "$S/etc/acme/host.env")" = "$want" ] && [ "$(owner_mode "$S/etc/acme/host.env")" = "root 644" ]; expect $? "host.env carries the port and the lane's App, root's, 0644"
cmp -s "$S/keys/acme-ci.pem" "$S/etc/acme/app.pem" && [ "$(owner_mode "$S/etc/acme/app.pem")" = "root 600" ]; expect $? "app.pem is a root-only copy of the lane's key"
[ "$(cat "$S/etc/acme/collector.json")" = "$(render dashboard-collector acme)" ] && [ "$(owner_mode "$S/etc/acme/collector.json")" = "root 600" ]; expect $? "collector.json is the lane's local-mode collector config, root-only"
cmp -s "$S/fx/vv/dashboard/systemd/vibe-dashboard-follow.service" "$S/units/vibe-dashboard-follow.service" && cmp -s "$S/fx/vv/dashboard/systemd/vibe-dashboard@.service" "$S/units/vibe-dashboard@.service"
expect $? "the kit's unit templates are installed from the checkout"
grep -qx "ExecStart=" "$S/units/vibe-dashboard-follow.service.d/runners.conf" \
  && grep -qxF "ExecStart=$FOLLOW_CMD --unit vibe-dashboard@greenbauer.service 8765 --unit vibe-dashboard@acme.service 8766" "$S/units/vibe-dashboard-follow.service.d/runners.conf"
expect $? "the follower's drop-in keeps the template's command and lists one --unit pair per dashboard"
grep -qx "Requires=runners-dashboard-guard.service" "$S/units/vibe-dashboard@.service.d/runners-guard.conf" && grep -qx "ExecStart=/usr/sbin/nft --file $S/guard.nft" "$S/units/runners-dashboard-guard.service"
expect $? "every dashboard requires the guard unit, which loads the guard file"
g="$(uid vibe-dashboard-greenbauer)"; p="$(uid vibe-dashboard-acme)"
grep -qxF "  ip daddr 127.0.0.0/8 tcp dport 8765 meta skuid != { 0, $g, 992 } reject" "$S/guard.nft" && grep -qxF "  ip6 daddr ::1 tcp dport 8765 meta skuid != { 0, $g, 992 } reject" "$S/guard.nft" \
  && grep -qxF "  ip daddr 127.0.0.0/8 tcp dport 8766 meta skuid != { 0, $p } reject" "$S/guard.nft" && [ "$(owner_mode "$S/guard.nft")" = "root 600" ]
expect $? "the guard lets only root, the dashboard's account and, where it names one, its bridge account reach each port, over IPv4 and IPv6"
[ "$(cat "$S/st/nft")" = "$(cat "$S/guard.nft")" ] && grep -q "^systemctl enable --now runners-dashboard-guard.service" "$CALLLOG"; expect $? "the guard is loaded and its unit enabled"
[ ! -e "$S/cron.d/greenbauer-dashboard" ] && [ ! -e "$S/cron.d/ci-dashboard" ] && grep -q "RUN: flock -w 600 $S/locks/ci-dashboard.lock true" <<< "$out"
expect $? "the legacy cron files are removed and a deploy they started is waited out"
r=0; for unit in $LEGACY_UNITS; do [ "$(cat "$S/st/state/$unit")" = inactive ] && [ ! -e "$S/st/enabled/$unit" ] && [ -f "$S/units/$unit" ] || r=1; done
[ "$r" = 0 ]; expect $? "every legacy unit is stopped and disabled, and its file is left in place"
before "RUN: systemctl start vibe-dashboard-token@acme.service" "RUN: rm -f $S/cron.d/greenbauer-dashboard" \
  && before "RUN: rm -f $S/cron.d/ci-dashboard" "RUN: systemctl stop greenbauer-dashboard.service" \
  && before "RUN: systemctl stop greenbauer-dashboard.service" "RUN: systemctl enable --now vibe-dashboard@greenbauer.service" \
  && before "RUN: systemctl enable --now vibe-dashboard@acme.service" "RUN: systemctl enable --now vibe-dashboard-follow.service"
expect $? "order: every token first, then the cron files, then the legacy units, then the dashboards, then the follower"
r=0; for unit in vibe-dashboard@greenbauer.service vibe-dashboard-token@greenbauer.timer vibe-dashboard-telemetry@acme.timer vibe-dashboard-follow.service; do [ -e "$S/st/enabled/$unit" ] && [ "$(cat "$S/st/state/$unit")" = active ] || r=1; done
[ "$r" = 0 ] && grep -q "greenbauer answers on 127.0.0.1:8765" <<< "$out" && grep -q "acme answers on 127.0.0.1:8766" <<< "$out"; expect $? "the dashboards, their timers and the follower run, and each dashboard answers on its port"

# ---- a second apply converges without repeating anything -------------------------------------------
: > "$CALLLOG"; rm -f "$S/st/loader"
out="$(run_dash --apply 2>&1)"; rc=$?
[ "$rc" -eq 0 ] && ! grep -qE '^(useradd|userdel|chown|git clone|nft --file|systemctl (daemon-reload|start|stop|restart|disable))' "$CALLLOG" && [ ! -e "$S/st/loader" ]
expect $? "repeat --apply makes no account, writes, loads, starts, restarts or stops nothing"
grep -q "  retired" <<< "$out" && grep -q "greenbauer answers on 127.0.0.1:8765" <<< "$out"; expect $? "repeat --apply finds the legacy install retired and still checks each dashboard answers"

# ---- a configuration change -------------------------------------------------------------------------
sed -i 's#https://worker-1.example.ts.net:8443#https://worker-1.example.ts.net:9443#' "$S/config/hosts/example.yml"; : > "$CALLLOG"
out="$(run_dash --apply 2>&1)"; rc=$?
[ "$rc" -eq 0 ] && grep -q "worker-1.example.ts.net:9443" "$cfg" && grep -qx "systemctl restart vibe-dashboard@greenbauer.service" "$CALLLOG" && ! grep -q "restart vibe-dashboard@acme" "$CALLLOG"
expect $? "a changed config is rewritten and restarts that dashboard alone"
before "dashboard.json" "RUN: systemctl restart vibe-dashboard@greenbauer.service" && grep -q "greenbauer answers" <<< "$out"; expect $? "and the restarted dashboard must answer"
printf 'a rotated acme key\n' > "$S/keys/acme-ci.pem"; : > "$CALLLOG"
out="$(run_dash --apply 2>&1)"; rc=$?
[ "$rc" -eq 0 ] && cmp -s "$S/keys/acme-ci.pem" "$S/etc/acme/app.pem" && [ "$(owner_mode "$S/etc/acme/app.pem")" = "root 600" ] && ! grep -q "restart vibe-dashboard@acme" "$CALLLOG"
expect $? "a rotated lane key is copied again, root-only, for the token timer's next run"
sed -i 's/^      version: 1$/      version: 1\n      refuse: true/' "$S/config/hosts/example.yml"
before_cfg="$(cat "$cfg")"; : > "$CALLLOG"
out="$(run_dash --apply 2>&1)"; rc=$?
[ "$rc" -ne 0 ] && grep -q "the dashboard's own loader refused the new $cfg; the old file stays" <<< "$out" && [ "$(cat "$cfg")" = "$before_cfg" ] \
  && ! grep -q "systemctl restart" "$CALLLOG" && [ -z "$(ls "$S/etc/greenbauer" | grep 'dashboard.json\.')" ]
expect $? "a config the dashboard's loader refuses never replaces the live one, and nothing restarts"

# ---- the token check stops the cutover before the legacy install is touched -------------------------
setup; legacy
out="$(TOKEN_FAIL=1 run_dash --apply 2>&1)"; rc=$?
[ "$rc" -ne 0 ] && grep -q "vibe-dashboard-token@greenbauer.service failed (ActiveState=failed .*); nothing of the legacy install was touched" <<< "$out" \
  && legacy_untouched && ! grep -q "enable --now vibe-dashboard@" "$CALLLOG"
expect $? "a token that cannot be minted fails the run with its unit state, the legacy install still serving"
setup; legacy
out="$(TELEMETRY_FAIL=1 run_dash --apply 2>&1)"; rc=$?
[ "$rc" -eq 0 ] && grep -q "vibe-dashboard-telemetry@greenbauer.service failed" <<< "$out" && grep -q "greenbauer answers" <<< "$out"
expect $? "a telemetry sample that fails warns, and the dashboard goes live without it"

# ---- the health check -------------------------------------------------------------------------------
setup; legacy
out="$(WEB_FAIL=acme run_dash --apply 2>&1)"; rc=$?
[ "$rc" -ne 0 ] && grep -q "dashboard acme did not answer http://127.0.0.1:8766/api/dashboard with 200 and version 1 from vibe-dashboard@acme.service within 30s (ActiveState=failed" <<< "$out" \
  && grep -q "heartbeat" <<< "$out" && ! grep -q "enable --now vibe-dashboard-follow" "$CALLLOG"
expect $? "a dashboard that does not answer fails the run loudly with its unit state and journal, before the follower starts"
[ "$(grep -c '^sleep 1' "$CALLLOG")" -ge 29 ]; expect $? "it was given the 30 seconds first"
setup; legacy
out="$(LEGACY_HOLDS=8766 run_dash --apply 2>&1)"; rc=$?
[ "$rc" -ne 0 ] && grep -q "dashboard acme did not answer" <<< "$out"; expect $? "a legacy dashboard still bound to the port answers, but is not taken for the new one"

# ---- a lane whose key is absent ---------------------------------------------------------------------
setup; legacy; rm "$S/keys/acme-ci.pem"
out="$(run_dash --apply 2>&1)"; rc=$?
[ "$rc" -eq 0 ] && legacy_untouched && grep -q "nothing starts while a dashboard waits for its lane's key and the legacy install holds the ports" <<< "$out" \
  && grep -qF "Dashboard acme mints its token from its lane's App key $S/keys/acme-ci.pem, which is absent" <<< "$out" && ! grep -qE "systemctl (start|enable --now) vibe-dashboard" "$CALLLOG"
expect $? "while one lane's key is absent and the legacy install holds the ports, nothing starts and the gate names the key"
setup; rm "$S/keys/acme-ci.pem"
out="$(run_dash --apply 2>&1)"; rc=$?
[ "$rc" -eq 0 ] && grep -qxF "ExecStart=$FOLLOW_CMD --unit vibe-dashboard@greenbauer.service 8765" "$S/units/vibe-dashboard-follow.service.d/runners.conf" \
  && [ "$(cat "$S/st/state/vibe-dashboard@greenbauer.service")" = active ] && [ ! -e "$S/st/state/vibe-dashboard@acme.service" ] && [ ! -e "$S/etc/acme/app.pem" ]
expect $? "with no legacy install, the dashboards whose keys exist start and the follower follows only them"

# ---- a dashboard taken out of the host file is removed ----------------------------------------------
setup
run_dash --apply >/dev/null 2>&1
python3 - "$S/config/hosts/example.yml" <<'PY'
import sys
path = sys.argv[1]
text = open(path).read()
open(path, "w").write(text[:text.index("  - name: acme\n")])
PY
# What systemd made for each running dashboard (the web unit's CacheDirectory=), with a kept reading in it.
mkdir -p "$S/cache/vibe-dashboard-acme/acme" "$S/cache/vibe-dashboard-greenbauer/greenbauer"
: > "$S/cache/vibe-dashboard-acme/acme/reading.json.z"; : > "$S/cache/vibe-dashboard-greenbauer/greenbauer/reading.json.z"
: > "$CALLLOG"
out="$(run_dash --apply 2>&1)"; rc=$?
[ "$rc" -eq 0 ] && grep -qx "systemctl disable --now vibe-dashboard@acme.service vibe-dashboard-token@acme.timer vibe-dashboard-telemetry@acme.timer" "$CALLLOG" \
  && [ ! -e "$S/etc/acme" ] && [ ! -e "$S/data/acme" ] && [ ! -e "$S/state/acme" ] && grep -qx "userdel vibe-dashboard-acme" "$CALLLOG"
expect $? "an undeclared dashboard's units are disabled and its files and account removed"
[ ! -e "$S/cache/vibe-dashboard-acme" ] && [ -f "$S/cache/vibe-dashboard-greenbauer/greenbauer/reading.json.z" ]
expect $? "its cache directory, with the reading it kept, goes with it, and the remaining dashboard's stays"
grep -qxF "ExecStart=$FOLLOW_CMD --unit vibe-dashboard@greenbauer.service 8765" "$S/units/vibe-dashboard-follow.service.d/runners.conf" && grep -qx "systemctl restart vibe-dashboard-follow.service" "$CALLLOG" \
  && ! grep -q "dport 8766" "$S/st/nft" && [ -d "$S/etc/greenbauer" ]
expect $? "the follower is restarted on the remaining dashboard, the guard drops the port, and the other dashboard stays"

# ---- check ------------------------------------------------------------------------------------------
setup; legacy
out="$(run_dash --check 2>&1)"; rc=$?
[ "$rc" -ne 0 ] && grep -qx "checkout=$S/opt/vibe-verifier absent" <<< "$out" && grep -qx "legacy=present (re-run --apply to retire it)" <<< "$out" \
  && grep -qx "health=greenbauer does not answer on 127.0.0.1:8765 from vibe-dashboard@greenbauer.service" <<< "$out"
expect $? "--check before the cutover fails, reports the legacy install, and does not take the legacy dashboard on the port for the new one"
setup; legacy
run_dash --apply >/dev/null 2>&1; : > "$CALLLOG"
mkdir -p "$S/data/greenbauer/gh" "$S/data/acme/gh"
for n in greenbauer acme; do printf 'github.com:\n' > "$S/data/$n/gh/hosts.yml"; chmod 600 "$S/data/$n/gh/hosts.yml"; "$S/pathbin/chown" "vibe-dashboard-$n:vibe-dashboard-$n" "$S/data/$n/gh/hosts.yml"; done
: > "$CALLLOG"
out="$(run_dash --check 2>&1)"; rc=$?
[ "$rc" -eq 0 ]; expect $? "--check passes on a converged machine"
grep -qx "config=$S/etc/greenbauer/dashboard.json ok" <<< "$out" && grep -qx "app_key=$S/etc/acme/app.pem ok" <<< "$out" && grep -qx "token=$S/data/acme/gh/hosts.yml ok" <<< "$out" \
  && grep -qx "health=acme answers on 127.0.0.1:8766" <<< "$out" && grep -qx "legacy=retired" <<< "$out" && grep -qx "vibe-dashboard-follow.service=active" <<< "$out"
expect $? "--check reports each dashboard's files, token, units and health, the follower and the retired legacy install"
{ ! grep -qE "$MUTATIONS" "$CALLLOG"; }; expect $? "--check mutates nothing"
printf '*/5 * * * * root true\n' > "$S/cron.d/ci-dashboard"
out="$(run_dash --check 2>&1)"; rc=$?
rm "$S/cron.d/ci-dashboard"
[ "$rc" -ne 0 ] && grep -qx "legacy=present (re-run --apply to retire it)" <<< "$out"; expect $? "--check fails when the legacy install comes back"
printf '{}\n' > "$S/etc/acme/collector.json"
out="$(run_dash --check 2>&1)"; rc=$?
render dashboard-collector acme > "$S/etc/acme/collector.json"
[ "$rc" -ne 0 ] && grep -q "collector=$S/etc/acme/collector.json DRIFTED" <<< "$out"; expect $? "--check fails on a file that drifted from the host file"
echo failed > "$S/st/state/vibe-dashboard@greenbauer.service"
out="$(run_dash --check 2>&1)"; rc=$?
echo active > "$S/st/state/vibe-dashboard@greenbauer.service"
[ "$rc" -ne 0 ] && grep -qx "vibe-dashboard@greenbauer.service=failed" <<< "$out" && grep -qx "health=greenbauer does not answer on 127.0.0.1:8765 from vibe-dashboard@greenbauer.service" <<< "$out"
expect $? "--check fails when a dashboard is down"
rm "$S/st/nft"
out="$(run_dash --check 2>&1)"; rc=$?
[ "$rc" -ne 0 ] && grep -qx "guard_live=port 8765 not guarded (re-run --apply)" <<< "$out"; expect $? "--check fails when the guard is not loaded"

# ---- remove -----------------------------------------------------------------------------------------
setup; legacy
run_dash --apply >/dev/null 2>&1; : > "$CALLLOG"
out="$(run_dash --remove 2>&1)"; rc=$?
[ "$rc" -eq 0 ] && grep -q "DRY-RUN: userdel vibe-dashboard-greenbauer" <<< "$out" && { ! grep -qE "$MUTATIONS" "$CALLLOG"; } && [ -d "$S/etc/greenbauer" ]; expect $? "--remove alone plans the teardown and mutates nothing"
mkdir -p "$S/cache/vibe-dashboard-greenbauer/greenbauer" "$S/cache/vibe-dashboard-acme" "$S/cache/apt"
out="$(run_dash --remove --apply 2>&1)"; rc=$?
[ "$rc" -eq 0 ] && [ ! -e "$S/etc/greenbauer" ] && [ ! -e "$S/etc/acme" ] && ! grep -q vibe-dashboard "$S/st/users" && [ -z "$(ls "$S/units" | grep -E 'vibe-dashboard|runners-dashboard')" ] && [ ! -e "$S/guard.nft" ] && [ ! -e "$S/st/nft" ]
expect $? "--remove --apply removes every dashboard, its account, the follower, the guard and the templates"
[ "$(ls "$S/cache")" = apt ]; expect $? "--remove --apply removes every dashboard's cache directory and nothing else under the cache base"
[ -d "$S/opt/vibe-verifier" ] && [ -f "$S/units/ci-dashboard.service" ] && grep -q "Left in place: $S/opt/vibe-verifier, the retired legacy install" <<< "$out"; expect $? "--remove keeps the checkout and the retired legacy unit files, and says so"

# ---- refusals ---------------------------------------------------------------------------------------
setup; legacy
sed -i '/^dashboard-forward /d' "$S/st/users"
out="$(run_dash --apply 2>&1)"; rc=$?
[ "$rc" -ne 0 ] && grep -q "names the bridge account dashboard-forward, which this machine lacks" <<< "$out" && { ! grep -qE "$MUTATIONS" "$CALLLOG"; } && legacy_untouched
expect $? "a bridge account the machine lacks is refused before anything changes"
setup
sed -i 's/ --unit vibe-dashboard@example.service 8765$/ --unit-of vibe-dashboard@example.service/' "$S/fx/vv/dashboard/systemd/vibe-dashboard-follow.service"
out="$(run_dash --apply 2>&1)"; rc=$?
[ "$rc" -ne 0 ] && grep -q "cannot read the follower's command from" <<< "$out" && ! grep -q "enable --now vibe-dashboard@" "$CALLLOG"; expect $? "a follower template whose command changed shape stops the run before anything starts"
setup
sed -i 's/^    lane: acme-ci$/    lane: nowhere-ci/' "$S/config/hosts/example.yml"
out="$(run_dash --apply 2>&1)"; rc=$?
[ "$rc" -ne 0 ] && grep -q "invalid lane config\|invalid host config" <<< "$out" && [ ! -s "$CALLLOG" ]; expect $? "an invalid host config stops the run before anything"
out="$(run_dash --check --apply 2>&1)"; rc=$?
[ "$rc" -ne 0 ] && grep -q "do not combine it" <<< "$out"; expect $? "--check refuses --apply"
setup
python3 - "$S/config/hosts/example.yml" <<'PY'
import sys
path = sys.argv[1]
text = open(path).read()
open(path, "w").write(text[:text.index("dashboards:\n")])
PY
out="$(run_dash --apply 2>&1)"; rc=$?
[ "$rc" -eq 0 ] && [ ! -s "$CALLLOG" ] && [ ! -e "$S/etc" ]; expect $? "a host file without dashboards installs nothing"

# ---- a host file named outright, and the default one ----------------------------------------------
setup
out="$(HOST_UNDER_TEST=vps-1 HOST_YML_UNDER_TEST="$ROOT/tests/fixtures/hosts/vps-1.yml" run_dash 2>&1)"; rc=$?
[ "$rc" -eq 0 ] && grep -q "Phase B: dashboard orbit (port 8766, lane orbit-ci, account vibe-dashboard-orbit)$" <<< "$out" \
  && grep -qF "Dashboard orbit mints its token from its lane's App key /etc/orbit-ci/app.pem" <<< "$out"
expect $? "vps-1 serves the dashboard orbit on 8766 from the orbit-ci lane's key, with no bridge account"
out="$(HOST_UNDER_TEST=nowhere run_dash 2>&1)"; rc=$?
[ "$rc" -ne 0 ] && grep -q "no host config at $S/config/hosts/nowhere.yml" <<< "$out"; expect $? "without a file named outright, the host file is the values checkout's hosts/<host>.yml"
grep -qx 'HOST_YML="${RUNNERS_HOST_YML:-${RUNNERS_CONFIG:-/opt/runner-lanes-config}/hosts/$HOST.yml}"' "$ROOT/bin/provision-dashboards.sh"; expect $? "on a machine that checkout is /opt/runner-lanes-config"

echo
echo "provision-dashboards-test: $pass passed, $fail failed"
[ "$fail" -eq 0 ]
