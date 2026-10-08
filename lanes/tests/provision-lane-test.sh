#!/bin/bash
# provision-lane-test.sh: hermetic tests for bin/provision-lane.sh. The system tools are stubs on
# PATH (tests/lib/stubs.sh), and the listener's build.sh in the copied checkout builds a stand-in
# that records how it was run; the gh stub applies the script's own jq filters to fixtures with the
# real jq. No real package, mount, unit, container, firewall, network or GitHub call happens.
set -u
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
# shellcheck source=lib/stubs.sh
. "$ROOT/tests/lib/stubs.sh"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
pass=0; fail=0
expect() { if [ "$1" -eq 0 ]; then pass=$((pass+1)); echo "✓ $2"; else fail=$((fail+1)); echo "✗ $2"; fi; }
mode_of() { python3 -c 'import os, sys; print(format(os.stat(sys.argv[1]).st_mode & 0o777, "o"))' "$1" 2>/dev/null; }

# Obviously fake markers, not credential-shaped: the assertions only need to find them verbatim.
APP_SESSION_MARKER="fixture-app-session-aaaa"
KEY_MARKER="fixture-app-key-cccc"

# The smoke output for a port list: every probe passes.
smoke_out() {
  local now target port d
  now="$(date +%s)"
  echo "probe container-ready ok $now"
  for target in gateway public tailnet; do
    for port in $1; do echo "probe net-$target-$port ok unreachable"; done
  done
  echo "probe net-metadata ok 169.254.169.254:80 unreachable"
  echo "probe net-github ok github.com resolves and answers on 443"
  for d in etc usr opt var root; do echo "probe write-$d ok refuses uid 1001"; done
  echo "probe write-home/runner ok writable"
  echo "probe write-tmp ok writable"
  echo "probe cpus ok nproc=4"
  echo "probe tmp-exec ok a job can execute from /tmp"
  echo "probe psql ok psql (PostgreSQL) 16.15 (Ubuntu 16.15-0ubuntu0.24.04.1)"
  echo "probe inner-docker ok 29.5.2 overlay2"
  echo "probe supabase-alpha ok started in 41s"
  echo "probe inner-pulls ok 0 image pulls across 1 preloaded project(s)"
}

write_host() {
  cat > "$S/host.yml" <<'YML'
hostname: box-1
sysbox: {image: /var/lib/runners/sysbox.img, disk_gb: 20}
lanes:
  - name: box-ci
    scope: org
    owner: acme
    runner_group: box-ci
    repos: [alpha, beta]
    labels: {ci: box-ci, qae: box-qae}
    app: {id: 123456, installation_id: 7654321, key_file: /etc/box-ci/app.pem, login: acme-bot}
    codex_store: /var/lib/box-ci/codex
    slots: 2
    min_runners: 1
    qae_concurrency: 1
    name_prefix: box-lane
    image: box-ci-runner
    preload: {alpha: supabase, beta: "-"}
    slice: {memory_max: 16G, memory_high: 14G, cpu_quota: "800%", cpu_weight: 50}
    container: {memory: 4g, pids: 2048, tmp_size: 2g}
    runtime_max_sec: 3600
    slot_disk_gb: 10
    store_disk_gb: 40
    check_ports: [22, 54321, 54322]
  - name: own-ci
    scope: user
    owner: solo
    labels: {ci: own-ci, qae: own-qae}
    exclude: [fleet]
    app: {id: 234567, installation_id: 8765432, key_file: /etc/own-ci/app.pem, login: solo-bot}
    codex_store: /var/lib/own-ci/codex
    slots: 2
    qae_concurrency: 1
    name_prefix: own-lane
    image: own-ci-runner
    preload: {}
    slice: {memory_max: 8G, memory_high: 7G, cpu_quota: "400%", cpu_weight: 30}
    container: {memory: 4g, pids: 2048, tmp_size: 2g}
    runtime_max_sec: 3600
    slot_disk_gb: 6
    store_disk_gb: 20
    check_ports: [22, 3101, 54329]
YML
}

setup() {
  S="$TMP/s"
  rm -rf "$S"
  mkdir -p "$S/repo/bin" "$S/repo/lib" "$S/repo/listener" "$S/units" "$S/state" "$S/run" "$S/sysbox" "$S/st/state" "$S/fx"
  install -d -m 0700 "$S/etc/box-ci"
  cp "$ROOT/bin/provision-lane.sh" "$ROOT/bin/lane-smoke.sh" "$ROOT/bin/lane-slot.sh" "$S/repo/bin/"
  cp "$ROOT"/lib/*.py "$ROOT/lib/provision.sh" "$S/repo/lib/"
  # The listener's module, as far as the build hash reads it, and a build.sh that "builds" a stand-in
  # binary which records how it was run.
  L="$S/repo/listener"
  printf 'module example/listener\n' > "$L/go.mod"; printf 'sum\n' > "$L/go.sum"
  printf 'package main\n' > "$L/main.go"; printf 'package main // a test\n' > "$L/main_test.go"
  cat > "$L/build.sh" <<'SH'
#!/bin/bash
echo "build.sh $*" >> "$CALLLOG"
[ "${BUILD_RC:-0}" = 0 ] || { echo "go: build failed" >&2; exit 1; }
d="$(cd "$(dirname "$0")" && pwd)"
mkdir -p "$d/out"
printf '#!/bin/bash\necho "listener $*" >> "$CALLLOG"\n' > "$d/out/lane-listener"
chmod +x "$d/out/lane-listener"
echo "binary=$d/out/lane-listener"
echo "sha256=0000"
SH
  chmod +x "$L/build.sh"
  write_host
  # The host phases converged: Sysbox installed and its filesystem mounted.
  printf '0.7.1.linux\n' > "$S/st/sysbox"
  # The org lane's App key, piped by the operator.
  printf '%s\n' "$KEY_MARKER" > "$S/etc/box-ci/app.pem"; /bin/chmod 0600 "$S/etc/box-ci/app.pem"
  printf 'KNOWN_CI_IMAGE_TAG=tag1\n' > "$S/state/image.env"
  # The image build's preloaded store for tag1: a layer directory under overlay2/ and the rest.
  STORE="$S/state/store"
  mkdir -p "$STORE/golden-tag1/overlay2/layer1/diff/usr/bin" "$STORE/golden-tag1/overlay2/l" "$STORE/golden-tag1/image/overlay2"
  printf 'bin\n' > "$STORE/golden-tag1/overlay2/layer1/diff/usr/bin/postgres"
  printf 'meta\n' > "$STORE/golden-tag1/image/overlay2/repositories.json"
  printf 'docker-ce\n' > "$S/st/holds"
  # The store filesystem is mounted, as on any machine where the image build has written a store into it.
  printf '%s\n%s\n' "$S/sysbox" "$STORE" > "$S/st/mounts"
  cat > "$S/fx/groups.json" <<'JSON'
{"total_count": 3, "runner_groups": [{"id": 1, "name": "Default"}, {"id": 7, "name": "box-ci"}, {"id": 11, "name": "other-ci"}]}
JSON
  cat > "$S/fx/repos.json" <<'JSON'
{"total_count": 2, "repositories": [{"name": "beta"}, {"name": "alpha"}]}
JSON
  # One of each: an offline lane leftover of each name shape (swept), the names a slot is using
  # right now (kept), busy and online lane runners (kept), runners of other prefixes (never lane),
  # and names that only start like the lane's (not lane-shaped, never touched).
  cat > "$S/fx/runners.json" <<'JSON'
{"total_count": 10, "runners": [
  {"id": 501, "name": "box-lane-1-1727000000", "status": "offline", "busy": false},
  {"id": 502, "name": "box-lane-2-1727000900", "status": "offline", "busy": false},
  {"id": 503, "name": "box-lane-1-1727001000", "status": "online", "busy": true},
  {"id": 504, "name": "box-lane-2-1727001100", "status": "online", "busy": false},
  {"id": 505, "name": "box-vps-1", "status": "offline", "busy": false},
  {"id": 506, "name": "box-qae-1", "status": "offline", "busy": false},
  {"id": 507, "name": "box-lane-extra-1", "status": "offline", "busy": false},
  {"id": 508, "name": "box-lane-ci-1-1727002000", "status": "offline", "busy": false},
  {"id": 509, "name": "box-lane-ci-2-1727002100", "status": "offline", "busy": false},
  {"id": 510, "name": "box-lane-build-1-1727002200", "status": "offline", "busy": false}
]}
JSON
  printf '{"runners": []}\n' > "$S/fx/runners-none.json"
  # A user lane's App installation: two repositories of the owner, one of another account.
  cat > "$S/fx/install-repos.json" <<'JSON'
{"total_count": 3, "repositories": [
  {"full_name": "solo/widgets", "owner": {"login": "Solo"}},
  {"full_name": "solo/site", "owner": {"login": "solo"}},
  {"full_name": "other/thing", "owner": {"login": "other"}}
]}
JSON
  cat > "$S/fx/runners-solo_site.json" <<'JSON'
{"runners": [
  {"id": 601, "name": "own-lane-qae-1-1727004000", "status": "offline", "busy": false},
  {"id": 602, "name": "own-lane-ci-2-1727004100", "status": "online", "busy": true}
]}
JSON
  # A slot the listener started for the org lane's warm pool: its job file names runner 508, and
  # its unit runs; and a retiring always-on slot's name file.
  mkdir -p "$S/run/2" "$S/run/ci/1"
  printf 'box-lane-2-1727000900\n' > "$S/run/2/name"
  printf 'acme ci 9 508 box-lane-ci-1-1727002000\n' > "$S/run/ci/1/job"
  echo active > "$S/st/state/box-ci-ci@1.service"
  smoke_out "22 54321 54322" > "$S/fx/smoke.out"
  cat > "$S/harden.sh" <<'SH'
#!/bin/bash
echo "harden${*:+ $*} bridge=${KNOWN_CI_BRIDGE:-unset}" >> "$CALLLOG"
exit "${HARDEN_RC:-0}"
SH
  chmod +x "$S/harden.sh"
  CALLLOG="$S/calls.log"; : > "$CALLLOG"
  export CALLLOG ST="$S/st" FX="$S/fx" UNITS="$S/units" RUN_T="$S/run"
  write_stubs "$S/pathbin"
}

# run_lane <lane> args: the lane's paths under $S (one lane at a time), the units against this
# checkout's own helper scripts: the engine is the directory holding this kit, lanes/.
run_lane() {
  local lane="$1"
  shift
  env PATH="$S/pathbin:$PATH" GH_TOKEN="ambient-token-must-not-be-used" RUNNERS_ALLOW_NON_ROOT=1 \
    RUNNERS_HOST_YML="${HOST_YML_UNDER_TEST:-$S/host.yml}" RUNNERS_UNIT_DIR="$S/units" RUNNERS_SYSBOX_DIR="$S/sysbox" \
    RUNNERS_ENGINE="$(dirname "$ROOT")" RUNNERS_FIREWALL="$S/harden.sh" RUNNERS_APPLY_LOCK="$S/apply.lock" \
    RUNNERS_LANE_STATE_DIR="${STATE_T:-$S/state}" RUNNERS_LANE_RUN_DIR="${RUN_UNDER_TEST:-$S/run}" \
    RUNNERS_LANE_ETC_DIR="$S/etc/$lane" RUNNERS_LANE_LIB_DIR="$S/lib/$lane" RUNNERS_LANE_KEY_FILE="$S/etc/$lane/app.pem" \
    RUNNERS_LANE_CODEX_STORE="${STATE_T:-$S/state}/codex" RUNNERS_LANE_HEALTH_DIR="$S/healthrun" \
    bash "$S/repo/bin/provision-lane.sh" box "$lane" "$@"
}
run_org() { run_lane box-ci "$@"; }
run_user() { run_lane own-ci "$@"; }

# The listener config the provisioner must write: lib/lanes.py's rendering, with the paths it manages.
expected_config() {  # $1: the lane
  python3 - "$S/host.yml" "$1" "$S/etc/$1/app.pem" "$S/state/codex" "$ROOT/lib" <<'PY'
import sys
sys.path.insert(0, sys.argv[5])
from lane_load import load_host
from lane_render import listener_config
sys.stdout.write(listener_config(load_host(sys.argv[1]).lane(sys.argv[2]), key_file=sys.argv[3], codex_store=sys.argv[4]))
PY
}

# A Codex login as Codex writes one, last refreshed $1 days ago (default 0); its tokens are a fake
# marker --check must never print.
LOGIN_MARKER="fixture-codex-token-eeee"
login_json() {
  printf '{"auth_mode": "chatgpt", "last_refresh": "%s", "tokens": {"refresh_token": "%s"}}\n' \
    "$(date -u -d "${1:-0} days ago" +%Y-%m-%dT%H:%M:%S.123456789Z)" "$LOGIN_MARKER"
}

MUTATIONS='^(docker network (create|rm)|docker rm|dockerd|apt-get|apt-mark hold|systemctl (enable|disable|daemon-reload|start|stop|restart)|mkfs|truncate|mount |umount|curl|harden|gh api -X DELETE|build\.sh|listener |useradd|chown|chgrp)'
line_of() { grep -nE -- "$1" "$CALLLOG" | head -n 1 | cut -d: -f1; }
before() { local a b; a="$(line_of "$1")"; b="$(line_of "$2")"; [ -n "$a" ] && [ -n "$b" ] && [ "$a" -lt "$b" ]; }
# True when a line matching $2 falls between the first lines matching $1 and $3.
between() { local a c; a="$(line_of "$1")"; c="$(line_of "$3")"; [ -n "$a" ] && [ -n "$c" ] && grep -nE -- "$2" "$CALLLOG" | cut -d: -f1 | awk -v a="$a" -v c="$c" '$1 > a && $1 < c { found = 1 } END { exit !found }'; }
# The same for two lines of a run's own output ($out), matched as fixed strings.
out_line() { grep -nF -- "$1" <<< "$out" | head -n 1 | cut -d: -f1; }
out_before() { local a b; a="$(out_line "$1")"; b="$(out_line "$2")"; [ -n "$a" ] && [ -n "$b" ] && [ "$a" -lt "$b" ]; }
HELPER="$ROOT/bin/lane-slot.sh"
# Runs the slot helper the way systemd runs it from a unit this provisioner wrote: nothing in its
# environment but that unit's own Environment= lines (%i expanded to the instance), the image tag
# its EnvironmentFile holds, and the stand-ins the stubs need.
as_unit() {  # <unit file> <instance> <helper arguments...>
  local unit="$1" n="$2" line envs=()
  shift 2
  while IFS= read -r line; do envs+=("${line//%i/$n}"); done < <(sed -n 's/^Environment=//p' "$unit")
  env -i PATH="$S/pathbin:$PATH" CALLLOG="$CALLLOG" ST="$ST" FX="$FX" UNITS="$UNITS" "${envs[@]}" KNOWN_CI_IMAGE_TAG=tag1 \
    KNOWN_CI_HARDEN="$S/harden.sh" KNOWN_CI_API=https://api.example.invalid SERVICE_RESULT=success bash "$HELPER" "$@"
}
# What the listener writes before it starts a slot unit (listener/README.md, "The run-dir contract").
listener_files() {  # kind n runner-id
  mkdir -p "$S/run/$1/$2"
  printf 'fixture-jit\n' > "$S/run/$1/$2/jit"
  printf 'acme %s 9 %s box-lane-%s-%s-1727005000\n' "$1" "$3" "$1" "$2" > "$S/run/$1/$2/job"
}
trash_count() { find "$STORE/trash" -mindepth 1 -maxdepth 1 2>/dev/null | wc -l | tr -d ' '; }
GHDIR="$TMP/s/state/home/.config/gh"
STORE_UNIT="$(printf '%s' "${TMP#/}/s/state/store" | sed 's/-/\\x2d/g; s#/#-#g').mount"

# ---- the host phases come first ----------------------------------------------------------------------
setup
rm "$S/st/sysbox"
for mode in "" --apply --check; do
  : > "$CALLLOG"
  out="$(run_org $mode 2>&1)"; rc=$?
  [ "$rc" -ne 0 ] && grep -q "the host phases have not converged (missing: sysbox-ce 0.7.1, Docker's sysbox-runc runtime); run bin/provision-host.sh box --apply first" <<< "$out" \
    && ! grep -qE "$MUTATIONS" "$CALLLOG" && [ -z "$(ls -A "$S/units")" ]
  expect $? "without Sysbox, ${mode:-the dry run} refuses before any lane phase, naming provision-host.sh"
done
setup
printf '%s\n' "$STORE" > "$S/st/mounts"
out="$(run_org --apply 2>&1)"; rc=$?
[ "$rc" -ne 0 ] && grep -q "missing: the Sysbox filesystem on $S/sysbox" <<< "$out" && ! grep -qE "$MUTATIONS" "$CALLLOG"; expect $? "without the Sysbox filesystem the lane refuses"
setup
out="$(NO_RUNTIME=1 run_org --apply 2>&1)"; rc=$?
[ "$rc" -ne 0 ] && grep -q "missing: Docker's sysbox-runc runtime" <<< "$out"; expect $? "without the sysbox-runc runtime the lane refuses"
setup
rm "$S/st/sysbox"
out="$(run_org --remove 2>&1)"; rc=$?
[ "$rc" -eq 0 ] && grep -q "DRY-RUN: systemctl disable --now box-ci-listener.service" <<< "$out"; expect $? "--remove still plans the teardown on a host whose Sysbox is gone"

# ---- dry-run (the default) ---------------------------------------------------------------------------
setup
out="$(run_org 2>&1)"; rc=$?
[ "$rc" -eq 0 ]; expect $? "default dry-run exits 0"
grep -q "DRY-RUN: useradd --system --user-group --home-dir $S/state/home --create-home --shell /usr/sbin/nologin box-ci" <<< "$out" && grep -q "DRY-RUN: install -d -m 0710 $S/state$" <<< "$out"
expect $? "dry-run plans the org lane's own user, named for the lane, and its 0710 state directory"
grep -q "DRY-RUN: truncate -s 40G $S/state/store.img" <<< "$out" && grep -q "DRY-RUN: mkfs.xfs -q -m reflink=1 $S/state/store.img" <<< "$out" && grep -qF "DRY-RUN: write $S/units/$STORE_UNIT" <<< "$out"; expect $? "dry-run plans the reflink-capable XFS store filesystem and its mount unit, named the way systemd names one"
grep -q "DRY-RUN: install -d -m 0700 $STORE/trash$" <<< "$out" && grep -q "DRY-RUN: write $S/units/box-ci-store-reaper.service" <<< "$out" && grep -q "DRY-RUN: write $S/units/box-ci-store-reaper.timer" <<< "$out" \
  && grep -q "DRY-RUN: systemctl enable --now box-ci-store-reaper.timer" <<< "$out"
expect $? "dry-run plans the store's trash and the store reaper's unit and timer (Phase D)"
grep -q "DRY-RUN: docker network create --driver bridge --opt com.docker.network.bridge.name=box-ci0 --opt com.docker.network.bridge.enable_icc=false box-ci" <<< "$out"; expect $? "dry-run plans the lane network on its named bridge with inter-container traffic off"
grep -q "DRY-RUN: env KNOWN_CI_BRIDGE=box-ci0 $S/harden.sh$" <<< "$out"; expect $? "dry-run plans the lane firewall rules"
grep -q "DRY-RUN: write $S/units/box-ci-ci@.service" <<< "$out" && grep -q "DRY-RUN: write $S/units/box-ci-qae@.service" <<< "$out" && grep -q "DRY-RUN: write $S/units/box-ci.slice" <<< "$out" && grep -q "DRY-RUN: write $S/units/box.slice" <<< "$out"; expect $? "dry-run plans both slot templates of an org lane with QAE, the slice and its parent"
{ ! grep -q "write $S/units/box-ci@.service" <<< "$out"; }; expect $? "no always-on template is ever written"
grep -q "DRY-RUN: gh_lane api -X DELETE orgs/acme/actions/runners/501 --silent" <<< "$out"; expect $? "dry-run plans the sweep of the offline leftover"
grep -q "DRY-RUN: write $S/units/box-ci-token-refresh.service" <<< "$out" && grep -q "DRY-RUN: write $S/units/box-ci-image-build.timer" <<< "$out"; expect $? "dry-run plans an org lane's token and image-build timers"
grep -q "DRY-RUN: build the listener ($S/repo/listener/build.sh, source [0-9a-f]*) and install it at $S/lib/box-ci/listener" <<< "$out" \
  && grep -q "DRY-RUN: write $S/etc/box-ci/listener.json" <<< "$out" && grep -q "DRY-RUN: write $S/units/box-ci-listener.service" <<< "$out" \
  && grep -q "DRY-RUN: systemctl enable box-ci-listener.service" <<< "$out" && grep -q "DRY-RUN: systemctl start box-ci-listener.service" <<< "$out"
expect $? "dry-run plans the listener's build, config and unit, and its start"
grep -q "DRY-RUN: write $S/units/box-ci-listener-health.timer" <<< "$out" && grep -q "DRY-RUN: systemctl enable --now box-ci-listener-health.timer" <<< "$out"; expect $? "dry-run plans the listener's health timer (Phase M)"
grep -q "DRY-RUN: write $S/units/box-ci-codex-keepalive.service" <<< "$out" && grep -q "DRY-RUN: systemctl enable --now box-ci-codex-keepalive.timer" <<< "$out"; expect $? "dry-run plans a QAE lane's Codex keepalive timer (Phase N)"
{ ! grep -qE "$MUTATIONS" "$CALLLOG"; }; expect $? "dry-run executes no file-system, unit, network, firewall, build, user or GitHub mutation"
[ -z "$(ls -A "$S/units")" ] && [ ! -e "$S/state/store.img" ] && [ ! -e "$S/lib" ] && [ ! -e "$S/etc/box-ci/listener.json" ] && [ ! -e "$STORE/trash" ]; expect $? "dry-run writes no file"
{ ! grep -q "sysbox" <<< "$(grep -E 'DRY-RUN: (write|truncate|mkfs|systemctl)' <<< "$out")"; }; expect $? "the lane provisioner plans nothing of the host's Sysbox filesystem"
mv "$S/state" "$S/state.keep"
chk="$(run_org --check 2>&1)"
mv "$S/state.keep" "$S/state"
grep -q "disk free=62G lane_worst_case=60G" <<< "$chk" && grep -qx "df $S" "$CALLLOG"; expect $? "--check reads the disk headroom from the state directory's parent while that directory does not exist yet"

# ---- apply from nothing: an org lane with QAE ---------------------------------------------------------
setup
out="$(run_org --apply 2>&1)"; rc=$?
[ "$rc" -eq 0 ]; expect $? "--apply provisions an org lane"
grep -qx "useradd --system --user-group --home-dir $S/state/home --create-home --shell /usr/sbin/nologin box-ci" "$CALLLOG" && grep -qx "chgrp box-ci $S/state" "$CALLLOG" && [ "$(mode_of "$S/state")" = 710 ]
expect $? "Phase A0 gives the org lane its own system user, and its state directory is root:<lane user> 0710"
[ "$(mode_of "$S/etc/box-ci/app.pem")" = 600 ] && grep -qx "chown root:root $S/etc/box-ci/app.pem" "$CALLLOG"; expect $? "the piped App key is kept root:root 0600"
ts="$S/units/box-ci-token-refresh.service"; tt="$S/units/box-ci-token-refresh.timer"
grep -qx "ExecStart=/usr/bin/python3 $ROOT/bin/lane-token-refresh.py --app-id 123456 --installation-id 7654321 --key-file $S/etc/box-ci/app.pem --hosts-out $GHDIR/hosts.yml --owner box-ci --gh-user acme-bot" "$ts" && grep -qx "Type=oneshot" "$ts"
expect $? "an org lane's token service writes its own user's hosts.yml from its key, under its App login"
grep -qx "OnBootSec=1min" "$tt" && grep -qx "OnUnitActiveSec=10min" "$tt" && grep -qx "systemctl enable --now box-ci-token-refresh.timer" "$CALLLOG" && grep -qx "systemctl start box-ci-token-refresh.service" "$CALLLOG"
expect $? "the token timer fires every 10 minutes, and the token is written now, before the GitHub reads need it"
bs="$S/units/box-ci-image-build.service"; bt="$S/units/box-ci-image-build.timer"
grep -qxF "ExecStart=/usr/bin/flock -o /run/box-ci-runner-build.lock $ROOT/bin/build-runner-image.sh box box-ci --refresh --if-provisioned" "$bs" \
  && grep -qx "WorkingDirectory=$ROOT" "$bs" && grep -qx "After=docker.service network-online.target" "$bs" && ! grep -q "^\[Install\]" "$bs"
expect $? "an org lane's image-build service runs its weekly --refresh --if-provisioned build from the checkout, under its build lock"
grep -qx "OnCalendar=weekly" "$bt" && grep -qx "Persistent=true" "$bt" && grep -qx "RandomizedDelaySec=1h" "$bt" && grep -qx "systemctl enable --now box-ci-image-build.timer" "$CALLLOG" && ! grep -q "systemctl start box-ci-image-build.service" "$CALLLOG"
expect $? "the image-build timer fires weekly and no build is started by the provisioner"
grep -qx "mkfs.xfs -q -m reflink=1 $S/state/store.img" "$CALLLOG" && ! grep -qF "systemctl enable --now $STORE_UNIT" "$CALLLOG"; expect $? "the store image is formatted XFS with reflinks; an already mounted store is not mounted again"
before '^useradd' '^systemctl enable --now box-ci-token-refresh.timer' && before '^systemctl enable --now box-ci-token-refresh.timer' '^mkfs.xfs' \
  && before '^mkfs.xfs' '^docker network create' && before '^docker network create' '^harden bridge=box-ci0' \
  && before '^harden bridge=box-ci0' '^gh api -X DELETE' && before '^gh api -X DELETE' '^build\.sh' && before '^build\.sh' '^systemctl start box-ci-listener.service'
expect $? "--apply runs user, token, store, network, firewall, units, sweep and the listener in that order"
# Phase D: the store's trash and the reaper that empties it.
rs="$S/units/box-ci-store-reaper.service"; rt="$S/units/box-ci-store-reaper.timer"
[ "$(mode_of "$STORE/trash")" = 700 ] && [ "$(trash_count)" = 0 ] && [ -d "$STORE/golden-tag1" ]; expect $? "Phase D makes the store's trash: an empty 0700 directory inside the store, beside the preloaded store"
grep -qx "ExecStart=$HELPER reap" "$rs" && grep -qx "Type=oneshot" "$rs" && grep -qx "RequiresMountsFor=$STORE" "$rs" && grep -qx "Environment=KNOWN_CI_NAME=box-ci" "$rs" \
  && grep -qx "Environment=KNOWN_CI_STORE_DIR=$STORE" "$rs" && ! grep -q "^User=\|^\[Install\]\|^TimeoutStartSec=" "$rs"
expect $? "the reaper service runs the checkout's slot helper's reap as root, oneshot, on the lane's store, only while that store is mounted, with no start timeout"
grep -qx "Nice=19" "$rs" && ! grep -q "^Slice=" "$rs" && grep -qx "StartLimitIntervalSec=0" "$rs"
expect $? "the reaper runs at the lowest CPU priority, outside the lane's slice, and is never refused a start however often the cleanups start it"
grep -qx "OnBootSec=1min" "$rt" && grep -qx "OnUnitActiveSec=5min" "$rt" && grep -qx "WantedBy=timers.target" "$rt" && grep -qx "systemctl enable --now box-ci-store-reaper.timer" "$CALLLOG" \
  && ! grep -q "systemctl start.* box-ci-store-reaper.service" "$CALLLOG"
expect $? "the reaper timer fires a minute after boot and every 5 minutes, and the provisioner starts no reaper itself"
out_before "RUN: install -d -m 0700 $STORE/trash" "wrote $rs" && out_before "wrote $rt" "RUN: systemctl enable --now box-ci-store-reaper.timer" \
  && out_before "RUN: systemctl enable --now box-ci-store-reaper.timer" "wrote $S/units/box-ci-ci@.service" && before '^mkfs.xfs' '^systemctl enable --now box-ci-store-reaper.timer'
expect $? "Phase D follows the store and precedes the slot templates: the trash is made before the reaper's units, and its timer is enabled before any template is written"
[ "$(sed -n 's/^REAPER_UNIT="\${KNOWN_CI_NAME:-}\(.*\)"$/box-ci\1/p' "$HELPER")" = "$(basename "$rs")" ] && [ "$(sed -n 's/^TRASH_DIR="\$STORE_DIR\(.*\)"$/\1/p' "$HELPER")" = /trash ]
expect $? "the reaper unit and the trash are where the slot helper derives them: <lane>-store-reaper.service and <store>/trash"
cu="$S/units/box-ci-ci@.service"; qu="$S/units/box-ci-qae@.service"
grep -qxF "ExecStart=/usr/bin/docker run --rm --runtime=sysbox-runc --network box-ci --cgroup-parent box-ci.slice --memory 4g --pids-limit 2048 --tmpfs /tmp:size=2g,exec --oom-score-adj 1000 --dns 1.1.1.1 --dns 8.8.8.8 --cpuset-cpus \${KNOWN_CI_CPUSET_%i} -v $S/state/slot-ci-%i:/home/runner -v $STORE/slot-ci-%i:/var/lib/docker -v $S/run/ci/%i/jit:/run/jit:ro --name box-ci-ci-%i box-ci-runner:\${KNOWN_CI_IMAGE_TAG}" "$cu"
expect $? "ExecStart runs one Sysbox container with the lane's limits and --oom-score-adj 1000, the kind's slot mount, its store snapshot on /var/lib/docker and the listener's read-only JIT file"
grep -qxF "ExecStart=/usr/bin/flock -F -w 60 $S/run/qae/%i.codex.lock /usr/bin/docker run --rm --runtime=sysbox-runc --network box-ci --cgroup-parent box-ci.slice --memory 4g --pids-limit 2048 --tmpfs /tmp:size=2g,exec --oom-score-adj 1000 --dns 1.1.1.1 --dns 8.8.8.8 --cpuset-cpus \${KNOWN_CI_CPUSET_%i} -v $S/state/slot-qae-%i:/home/runner -v $STORE/slot-qae-%i:/var/lib/docker -v $S/run/qae/%i/jit:/run/jit:ro -v \${KNOWN_CI_CODEX_STORE_%i}:/home/runner/.codex-qae --name box-ci-qae-%i box-ci-runner:\${KNOWN_CI_IMAGE_TAG}" "$qu"
expect $? "an org lane's qae template mounts each instance's own Codex store at /home/runner/.codex-qae, on its own slot and store paths, the container run holding the store's lock"
grep -qx "Environment=KNOWN_CI_CODEX_STORE_1=$S/state/codex" "$qu" && ! grep -q "KNOWN_CI_CODEX_STORE_2=\|KNOWN_CI_CODEX_STORE=" "$qu" && grep -qx "Environment=KNOWN_CI_CODEX_CONFIG=$S/etc/box-ci/codex-config.toml" "$qu" && grep -qx "Environment=KNOWN_CI_CODEX_OWNER=box-ci" "$qu" \
  && grep -qx "ExecStartPre=+$HELPER prepare qae %i" "$qu" && grep -qx "Environment=KNOWN_CI_SCOPE=org" "$qu"
expect $? "the org lane's qae slot environment tells the helper instance 1's store (the one store at qae_concurrency 1), its tracked config and the lane user"
# Run as systemd runs it for instance 1, the qae ExecStart's prefix holds that store's lock for as
# long as its command (the container) runs, and frees it when the command exits.
lock_prefix="$(sed -n 's|^ExecStart=\(/usr/bin/flock .*\)/usr/bin/docker run .*|\1|p' "$qu" | sed 's/%i/1/g')"
mkdir -p "$S/run/qae"
# shellcheck disable=SC2086 # the prefix is a word list, as systemd splits it
[ "$lock_prefix" = "/usr/bin/flock -F -w 60 $S/run/qae/1.codex.lock " ] && ! $lock_prefix bash -c 'flock -n "$1" true' _ "$S/run/qae/1.codex.lock" \
  && flock -n "$S/run/qae/1.codex.lock" true && ! grep -q 'flock' "$cu"
expect $? "a qae job holds its store's lock while its container runs, and only a qae job takes one"
grep -qx "Environment=KNOWN_CI_CPUSET_1=0-3" "$cu" && grep -qx "Environment=KNOWN_CI_CPUSET_2=4-7" "$cu"; expect $? "each slot is pinned to its own 4-core set on a 16-core host, so a job sees nproc=4 like a hosted runner"
grep -qx "Environment=KNOWN_CI_STORE_DIR=$STORE" "$cu" && grep -qx "RequiresMountsFor=$S/state $STORE" "$cu"; expect $? "the unit tells the helper where the store is and waits for both filesystems"
grep -qx "Slice=box-ci.slice" "$cu" && grep -qx "RuntimeMaxSec=4800" "$cu" && grep -qx "RuntimeMaxSec=4800" "$qu" && grep -qx "Restart=no" "$cu" && grep -qx "StartLimitIntervalSec=0" "$cu"
expect $? "the slot unit runs one job per start, is never refused a start, and is killed past warm_max_age_sec + runtime_max_sec on a warm-pool lane"
{ ! grep -q "RestartSec\|RestartSteps\|^\[Install\]\|WantedBy" "$cu"; }; expect $? "the slot template has no restart delay and no [Install]: nothing but the listener starts it"
grep -qx "TimeoutStopSec=300" "$cu" && grep -qx "TimeoutStopSec=300" "$qu" && ! grep -qx "TimeoutStopSec=60" "$cu"
expect $? "the slot units give their cleanup 300 s: a store the trash could not take is still deleted in the stop path, and 60 s killed 195 of 332 cleanups that deleted there (one machine, 2026-10-05)"
grep -qx "ExecStartPre=+$HELPER prepare ci %i" "$cu" && grep -qx "ExecStopPost=+$HELPER cleanup ci %i" "$cu"; expect $? "the slot helper of the machine's checkout prepares and cleans up the instance as root"
grep -qx "Environment=KNOWN_CI_SCOPE=org" "$cu" && grep -qx "Environment=KNOWN_CI_GH_HOSTS=$GHDIR/hosts.yml" "$cu" && grep -qx "EnvironmentFile=$S/state/image.env" "$cu" && grep -qx "Environment=KNOWN_CI_BRIDGE=box-ci0" "$cu" \
  && ! grep -q "KNOWN_CI_CODEX" "$cu"
expect $? "the ci unit carries the scope, the lane user's own App session and the image tag file, and never the Codex store"
{ ! grep -qE "systemctl (enable|start).*box-ci-(ci|qae)@" "$CALLLOG"; }; expect $? "no slot instance is enabled or started by the provisioner"
grep -qx "MemoryMax=16G" "$S/units/box-ci.slice" && grep -qx "MemoryHigh=14G" "$S/units/box-ci.slice" && grep -qx "CPUQuota=800%" "$S/units/box-ci.slice" && grep -qx "CPUWeight=50" "$S/units/box-ci.slice"; expect $? "the slice carries the aggregate limits, throttling at MemoryHigh"
grep -qx "CPUWeight=50" "$S/units/box.slice"; expect $? "the dashed slice's root-level parent carries the weight too"
grep -qx "gh api -X DELETE orgs/acme/actions/runners/501 --silent \[GH_CONFIG_DIR=$GHDIR GH_TOKEN=unset as=box-ci\]" "$CALLLOG" \
  && grep -qx "gh api -X DELETE orgs/acme/actions/runners/509 --silent \[GH_CONFIG_DIR=$GHDIR GH_TOKEN=unset as=box-ci\]" "$CALLLOG"
expect $? "the sweep deletes the offline, unused lane registrations of both name shapes as the lane user, never an ambient token"
[ "$(grep -c '^gh api -X DELETE' "$CALLLOG" | tr -d ' ')" = 2 ]; expect $? "the sweep spares the names a slot holds, busy and online lane runners, and every non-lane runner"
[ "$(grep -c '^gh ' "$CALLLOG")" -gt 0 ] && [ "$(grep -c '^gh ' "$CALLLOG")" = "$(grep -c ' as=box-ci\]$' "$CALLLOG")" ]; expect $? "every gh call of an org lane runs as its own user, on its own session"
[ -x "$S/lib/box-ci/listener" ] && [ -s "$S/lib/box-ci/listener.sha256" ] && [ "$(grep -c '^build\.sh' "$CALLLOG" | tr -d ' ')" = 1 ]; expect $? "the listener is built once and installed with its source hash beside it"
[ "$(cat "$S/etc/box-ci/listener.json")" = "$(expected_config box-ci)" ] && [ "$(mode_of "$S/etc/box-ci/listener.json")" = 600 ]; expect $? "listener.json is lib/lanes.py's rendering with the managed paths, mode 0600"
[ "$(jq -c .kinds.qae "$S/etc/box-ci/listener.json")" = "{\"set_name\":\"box-qae\",\"labels\":[\"box-qae\"],\"requires_files\":[\"$S/state/codex/auth.json\"]}" ] && [ "$(jq .budget.qae_concurrency "$S/etc/box-ci/listener.json")" = 1 ]
expect $? "the org lane's listener serves a qae kind that waits for the Codex login"
[ "$(jq .warm_max_age_sec "$S/etc/box-ci/listener.json")" = 1200 ]; expect $? "listener.json carries warm_max_age_sec, 1200 when the lane leaves it out"
lu="$S/units/box-ci-listener.service"
grep -qx "ExecStart=$S/lib/box-ci/listener $S/etc/box-ci/listener.json" "$lu" && grep -qx "After=docker.service sysbox.service network-online.target" "$lu" \
  && grep -qx "Restart=always" "$lu" && grep -qx "RestartSec=10" "$lu" && grep -qx "WantedBy=multi-user.target" "$lu"; expect $? "the listener unit runs the binary on its config, after Docker and Sysbox, and always restarts"
grep -qx "systemctl enable box-ci-listener.service" "$CALLLOG" && grep -qx "systemctl start box-ci-listener.service" "$CALLLOG"; expect $? "the listener is enabled and started"
hs="$S/units/box-ci-listener-health.service"; ht="$S/units/box-ci-listener-health.timer"
grep -qx "ExecStart=$ROOT/bin/listener-health.sh box-ci" "$hs" && grep -qx "Type=oneshot" "$hs" && ! grep -q "^User=\|^\[Install\]" "$hs" && [ -x "$ROOT/bin/listener-health.sh" ]
expect $? "the health service runs the health check on the lane's name, as root, oneshot"
grep -qx "OnBootSec=1min" "$ht" && grep -qx "OnUnitActiveSec=5min" "$ht" && grep -qx "WantedBy=timers.target" "$ht"; expect $? "the health timer fires a minute after boot and every 5 minutes"
before '^systemctl start box-ci-listener.service' '^systemctl enable --now box-ci-listener-health.timer' && between '^systemctl start box-ci-listener.service' '^systemctl daemon-reload' '^systemctl enable --now box-ci-listener-health.timer'
expect $? "the health timer is written, reloaded and enabled only after the listener started"
grep -A1 "Phase L: retire the always-on template box-ci@.service" <<< "$out" | tail -n 1 | grep -q "   none$"; expect $? "a lane that never ran always-on slots has nothing to retire"
ks="$S/units/box-ci-codex-keepalive.service"; kt="$S/units/box-ci-codex-keepalive.timer"
grep -qx "ExecStart=$HELPER codex-keepalive" "$ks" && grep -qx "Type=oneshot" "$ks" && grep -qx "TimeoutStartSec=30min" "$ks" && ! grep -q "^User=\|^\[Install\]" "$ks" \
  && grep -qx "Environment=KNOWN_CI_NAME=box-ci" "$ks" && grep -qx "Environment=KNOWN_CI_STATE_DIR=$S/state" "$ks" && grep -qx "Environment=KNOWN_CI_RUN_DIR=$S/run" "$ks" \
  && grep -qx "Environment=KNOWN_CI_CODEX_STORE_1=$S/state/codex" "$ks" && ! grep -q "KNOWN_CI_CODEX_STORE_2=" "$ks" \
  && grep -qx "Environment=KNOWN_CI_CODEX_CONFIG=$S/etc/box-ci/codex-config.toml" "$ks" && grep -qx "Environment=KNOWN_CI_CODEX_OWNER=box-ci" "$ks"
expect $? "Phase N: the keepalive service runs the checkout's slot helper as root, oneshot, on the lane's stores, its tracked config, its user and the run dir that holds the store locks"
grep -qx "OnCalendar=daily" "$kt" && grep -qx "RandomizedDelaySec=1h" "$kt" && grep -qx "Persistent=true" "$kt" && grep -qx "WantedBy=timers.target" "$kt" \
  && grep -qx "systemctl enable --now box-ci-codex-keepalive.timer" "$CALLLOG" && ! grep -q "systemctl start box-ci-codex-keepalive.service" "$CALLLOG"
expect $? "the keepalive timer fires daily, up to an hour late at random, catches up a missed day at boot, and the provisioner runs no keepalive itself"
grep -qx "chown box-ci: $S/state/codex" "$CALLLOG" && [ "$(mode_of "$S/state/codex")" = 700 ] && [ "$(grep -v '^#' "$S/etc/box-ci/codex-config.toml")" = 'cli_auth_credentials_store = "file"' ]
expect $? "an org lane with QAE gets its Codex store, for its lane user, 0700, and the tracked config"
[ "$(grep -c "OPERATOR ACTION" <<< "$out")" = 1 ] && grep -qF "OPERATOR ACTION" <<< "$out" && grep -qF "sudo -u box-ci env CODEX_HOME=/var/lib/box-ci/codex HOME=$S/state/home PATH=/usr/bin:/bin codex login --device-auth" <<< "$out"
expect $? "with the key, the group, the image and a registration in place, the one gate is the Codex login, as the lane user with its home and the system PATH"
{ ! grep -qF "$APP_SESSION_MARKER" "$CALLLOG" && ! grep -q "ambient-token-must-not-be-used" "$CALLLOG" && ! grep -qF "$KEY_MARKER" "$CALLLOG" && ! grep -qF "$KEY_MARKER" <<< "$out"; }; expect $? "no token or key appears in any argv or log line"
{ ! grep -qE "var-lib-sysbox|sysbox-mgr" "$CALLLOG" && [ ! -e "$S/units/var-lib-sysbox.mount" ] && [ ! -e "$S/units/sysbox-mgr.service.d" ]; }; expect $? "the lane provisioner never touches var-lib-sysbox.mount or the sysbox-mgr drop-in"
login_json > "$S/state/codex/auth.json"
out="$(run_org --apply 2>&1)"
{ ! grep -q "OPERATOR ACTION" <<< "$out"; }; expect $? "no gate once the Codex store holds a login"

# ---- a second apply converges without repeating anything ------------------------------------------
: > "$CALLLOG"
out="$(run_org --apply 2>&1)"; rc=$?
[ "$rc" -eq 0 ]; expect $? "repeat --apply exits 0"
{ ! grep -qE '^(useradd|truncate|mkfs|curl|docker network create|systemctl daemon-reload|systemctl enable --now .*\.mount|build\.sh|systemctl (start|restart))' "$CALLLOG"; }; expect $? "repeat --apply creates, writes, formats, reloads, builds and restarts nothing"
grep -q "box-ci present" <<< "$out" && grep -q "running, unchanged" <<< "$out"; expect $? "repeat --apply reports the lane user and the listener converged"

# ---- the listener restarts when its binary or config changed ----------------------------------------
printf 'package main // changed\n' > "$S/repo/listener/main_test.go"; : > "$CALLLOG"
run_org --apply >/dev/null 2>&1
{ ! grep -q '^build\.sh' "$CALLLOG" && ! grep -q 'systemctl restart' "$CALLLOG"; }; expect $? "a change to a test file only rebuilds nothing"
printf 'package main // changed\n' > "$S/repo/listener/main.go"; : > "$CALLLOG"
out="$(run_org --apply 2>&1)"; rc=$?
[ "$rc" -eq 0 ] && grep -q '^build\.sh' "$CALLLOG" && grep -qx "systemctl restart box-ci-listener.service" "$CALLLOG"; expect $? "a changed listener source is rebuilt and the listener restarted"
: > "$CALLLOG"; run_org --apply >/dev/null 2>&1
{ ! grep -q '^build\.sh' "$CALLLOG"; }; expect $? "the new hash gates the next apply: no second build"
sed 's/^    slots: 2$/    slots: 3/' "$S/host.yml" > "$S/host-3.yml"; : > "$CALLLOG"
out="$(HOST_YML_UNDER_TEST="$S/host-3.yml" run_org --apply 2>&1)"; rc=$?
[ "$rc" -eq 0 ] && [ "$(jq .budget.slots "$S/etc/box-ci/listener.json")" = 3 ] && grep -qx "systemctl restart box-ci-listener.service" "$CALLLOG" && ! grep -q '^build\.sh' "$CALLLOG"; expect $? "a changed config is rewritten and the listener restarted, without a rebuild"
sed 's/^    min_runners: 1$/    min_runners: 0/' "$S/host.yml" > "$S/host-cold.yml"; : > "$CALLLOG"
out="$(HOST_YML_UNDER_TEST="$S/host-cold.yml" run_org --apply 2>&1)"; rc=$?
[ "$rc" -eq 0 ] && [ "$(jq .budget.min_runners "$S/etc/box-ci/listener.json")" = 0 ] && grep -qx "RuntimeMaxSec=3600" "$S/units/box-ci-ci@.service" && grep -qx "systemctl daemon-reload" "$CALLLOG"
expect $? "an org lane without a warm pool writes its slots' RuntimeMaxSec as runtime_max_sec alone"
setup
out="$(BUILD_RC=1 run_org --apply 2>&1)"; rc=$?
[ "$rc" -ne 0 ] && grep -q "the listener build failed" <<< "$out" && [ ! -e "$S/lib/box-ci/listener" ] && ! grep -q 'box-ci-listener.service' "$CALLLOG"; expect $? "a failed build installs nothing and starts nothing"

# ---- the first apply of the store trash on a machine whose lane is running ------------------------
# The lane as the kit left it before the trash: no trash directory, no reaper units, and slot
# templates another rendering wrote. Instance ci@1 is on a job: its unit active, its job file and its
# store copy there.
setup
run_org --apply >/dev/null 2>&1
login_json > "$S/state/codex/auth.json"
rm -rf "$STORE/trash" "$S/units"/box-ci-store-reaper.* "$S/units/multi-user.target.wants"/box-ci-store-reaper.* "$S/st/state"/box-ci-store-reaper.*
sed -i 's/^# Managed by the runner lanes kit.*/# Managed by an earlier rendering of the kit./' "$S/units/box-ci-ci@.service" "$S/units/box-ci-qae@.service"
cp "$S/units/box-ci-ci@.service" "$S/earlier-ci-template"
mkdir -p "$STORE/slot-ci-1/overlay2"; printf 'live\n' > "$STORE/slot-ci-1/overlay2/file"
: > "$CALLLOG"
out="$(run_org --apply 2>&1)"; rc=$?
[ "$rc" -eq 0 ] && [ "$(mode_of "$STORE/trash")" = 700 ] && [ -f "$S/units/box-ci-store-reaper.service" ] && [ -f "$S/units/box-ci-store-reaper.timer" ] && grep -qx "systemctl enable --now box-ci-store-reaper.timer" "$CALLLOG"
expect $? "the first apply on a lane that is already running makes the trash and the reaper's unit and timer, and enables the timer"
{ ! grep -qE '^systemctl (start|restart).*box-ci-store-reaper\.service' "$CALLLOG"; }; expect $? "and it starts no reaper: one that is not running has nothing of an older unit to shed"
out_before "RUN: install -d -m 0700 $STORE/trash" "wrote $S/units/box-ci-store-reaper.service" && out_before "RUN: systemctl enable --now box-ci-store-reaper.timer" "wrote $S/units/box-ci-ci@.service" \
  && grep -qx "# Managed by the runner lanes kit (bin/provision-lane.sh); do not edit on the machine." "$S/units/box-ci-ci@.service"
expect $? "first-apply order: the trash exists and the reaper's timer runs before the slot templates are rewritten"
{ ! grep -qE "^systemctl (start|stop|restart|kill|disable) [^ ]*box-ci-(ci|qae)@|^docker rm|^umount|^mount |^systemctl (restart|stop) box-ci-listener|^mkfs|^truncate" "$CALLLOG"; } \
  && [ "$(cat "$STORE/slot-ci-1/overlay2/file")" = live ] && [ -f "$S/run/ci/1/job" ] && [ "$(cat "$S/st/state/box-ci-ci@1.service")" = active ] && [ -d "$STORE/golden-tag1" ]
expect $? "that apply touches nothing that is running: no slot is stopped or restarted, the store is not remounted, the listener is not restarted, and a running job's store copy and job file stay"
[ "$(sed -n 's/^Environment=\([A-Z0-9_]*\)=.*/\1/p' "$S/units/box-ci-ci@.service" | tr '\n' ' ')" = "KNOWN_CI_NAME KNOWN_CI_SCOPE KNOWN_CI_SLOT_GB KNOWN_CI_BRIDGE KNOWN_CI_GH_HOSTS KNOWN_CI_STATE_DIR KNOWN_CI_STORE_DIR KNOWN_CI_RUN_DIR KNOWN_CI_CPUSET_1 KNOWN_CI_CPUSET_2 " ] \
  && [ "$(sed -n 's/^Environment=//p' "$S/units/box-ci-ci@.service")" = "$(sed -n 's/^Environment=//p' "$S/earlier-ci-template")" ]
expect $? "the rewritten slot template sets the variables the template always set and no new one, so a slot started before the apply has all its cleanup reads"
# The slot that was running finishes after the apply, with the environment of the template it started from.
: > "$CALLLOG"
out="$(as_unit "$S/earlier-ci-template" 1 cleanup ci 1 2>&1)"; rc=$?
entry="$(ls -A "$STORE/trash")"
[ "$rc" -eq 0 ] && [ ! -e "$STORE/slot-ci-1" ] && [ ! -e "$S/run/ci/1/job" ] && [ "$(trash_count)" = 1 ] && [ "$(cat "$STORE/trash/$entry/overlay2/file")" = live ]
expect $? "a slot started from the earlier template and finishing after that apply is cleaned up from that template's own environment: its store copy is renamed into the trash and its instance freed"
started="$(sed -n 's/^systemctl start --no-block //p' "$CALLLOG")"
[ "$started" = box-ci-store-reaper.service ] && [ -f "$S/units/$started" ]; expect $? "and the reaper its cleanup starts is the unit the provisioner wrote"
listener_files ci 2 7002
out="$(as_unit "$S/units/box-ci-ci@.service" 2 prepare ci 2 2>&1)"; rc_prepare=$?
[ "$rc_prepare" -eq 0 ] && cmp -s "$STORE/golden-tag1/overlay2/layer1/diff/usr/bin/postgres" "$STORE/slot-ci-2/overlay2/layer1/diff/usr/bin/postgres"
expect $? "a slot of the rewritten template is prepared from its own environment: a fresh copy of the preloaded store"
out="$(as_unit "$S/units/box-ci-ci@.service" 2 cleanup ci 2 2>&1)"; rc=$?
[ "$rc" -eq 0 ] && [ ! -e "$STORE/slot-ci-2" ] && [ ! -e "$S/run/ci/2/job" ] && [ "$(trash_count)" = 2 ]; expect $? "and cleaned up the same way: its copy joins the first in the trash"
out="$(as_unit "$S/units/box-ci-store-reaper.service" 0 reap 2>&1)"; rc=$?
[ "$rc" -eq 0 ] && [ "$(trash_count)" = 0 ] && [ -d "$STORE/trash" ] && [ "$(cat "$STORE/golden-tag1/overlay2/layer1/diff/usr/bin/postgres")" = bin ] && [ "$(grep -c ': deleted ' <<< "$out")" = 2 ]
expect $? "the reaper, run from its own unit's environment, deletes both retired copies and leaves the preloaded store"
# A reaper that is deleting when an apply changes its unit would run as the old unit until it ended,
# and on a busy lane it does not end.
echo activating > "$S/st/state/box-ci-store-reaper.service"; : > "$CALLLOG"
out="$(run_org --apply 2>&1)"; rc=$?
[ "$rc" -eq 0 ] && ! grep -qE '^systemctl (start|stop|restart).*box-ci-store-reaper\.service' "$CALLLOG"; expect $? "an apply that changes nothing of the reaper's unit leaves a running reaper alone"
sed -i 's/^Nice=19$/Nice=10/' "$S/units/box-ci-store-reaper.service"; : > "$CALLLOG"
out="$(run_org --apply 2>&1)"; rc=$?
[ "$rc" -eq 0 ] && grep -qx "Nice=19" "$S/units/box-ci-store-reaper.service" && grep -qx "systemctl restart --no-block box-ci-store-reaper.service" "$CALLLOG" \
  && before '^systemctl daemon-reload' '^systemctl restart --no-block box-ci-store-reaper.service'
expect $? "an apply that changes the reaper's unit restarts a reaper that is running, after the reload and without waiting for the new run"

# ---- without its App key: both scopes wait at the same gate ------------------------------------------
for lane in box-ci own-ci; do
  setup
  rm -f "$S/etc/box-ci/app.pem"
  out="$(run_lane "$lane" --apply 2>&1)"; rc=$?
  [ "$rc" -eq 0 ] && grep -qx "useradd --system --user-group --home-dir $S/state/home --create-home --shell /usr/sbin/nologin $lane" "$CALLLOG"; expect $? "$lane: Phase A0 creates the lane's own user even before its key"
  grep -qF "OPERATOR ACTION" <<< "$out" && grep -qF "ssh root@box-1 'umask 077; cat > /etc/$lane/app.pem' < key.pem" <<< "$out" && [ -d "$S/etc/$lane" ] && [ "$(mode_of "$S/etc/$lane")" = 700 ]
  expect $? "$lane: without its App key the lane prints the exact pipe command, and its directory exists to take the key"
  [ ! -e "$S/units/$lane-token-refresh.timer" ] && [ ! -e "$S/units/$lane-image-build.timer" ] && grep -A1 "Phase A3" <<< "$out" | grep -q "not written: the App key is absent" && grep -A1 "Phase A4" <<< "$out" | grep -q "not written: the App key is absent"
  expect $? "$lane: without its key neither the token timer nor the image-build timer is written"
  grep -q "skipped: the App key $S/etc/$lane/app.pem is absent" <<< "$out" && [ ! -e "$S/units/$lane-listener.service" ] && ! grep -q '^build\.sh' "$CALLLOG"; expect $? "$lane: without its key the listener phase is skipped"
  { ! grep -q '^gh ' "$CALLLOG"; } && grep -q "skipped: no token until the App key is piped" <<< "$out"; expect $? "$lane: without its key nothing reads GitHub (there is no token yet)"
done
setup
rm -f "$S/etc/box-ci/app.pem"
out="$(run_org --apply 2>&1)"
{ ! grep -q "runner group\|Self-hosted runners" <<< "$(grep "OPERATOR ACTION" <<< "$out")"; }; expect $? "an org lane without its key prints no runner-group or permission gate it could not have checked"

# ---- the lane's gh never leaves a root-owned file in its session's directory ------------------------
setup
mkdir -p "$GHDIR"; : > "$GHDIR/config.yml"; echo "$GHDIR/config.yml" > "$S/st/root-owned"
out="$(GH_WRITES_CONFIG=1 run_org --apply 2>&1)"; rc=$?
[ "$rc" -eq 0 ] && grep -qx "chown -h --reference=$GHDIR $GHDIR/config.yml" "$CALLLOG" \
  && before "^chown -h --reference=$GHDIR" '^runuser -u box-ci' && [ ! -s "$S/st/root-owned" ] && [ "$(grep -c '^chown -h --reference' "$CALLLOG")" = 1 ]
expect $? "a root-owned config.yml in the lane user's gh directory goes back to that user before the first GitHub read"
rm -f "$GHDIR/config.yml"; : > "$S/st/root-owned"; : > "$CALLLOG"
out="$(GH_WRITES_CONFIG=1 run_org --apply 2>&1)"; rc=$?
[ "$rc" -eq 0 ] && [ -e "$GHDIR/config.yml" ] && [ ! -s "$S/st/root-owned" ] && ! grep -q '^chown -h --reference' "$CALLLOG"
expect $? "gh as the lane user writes its missing config.yml as that user, so nothing needs repair"
setup
mkdir -p "$GHDIR"; : > "$GHDIR/config.yml"; echo "$GHDIR/config.yml" > "$S/st/root-owned"
out="$(run_org 2>&1)"; rc=$?
[ "$rc" -eq 0 ] && grep -q "DRY-RUN: chown -h --reference=$GHDIR $GHDIR/config.yml" <<< "$out" && ! grep -q '^chown' "$CALLLOG" && grep -qxF "$GHDIR/config.yml" "$S/st/root-owned"
expect $? "a dry run plans the repair and changes no ownership"

# ---- the store filesystem: mounted when absent, never mounted over files -------------------------
setup
rm -rf "$STORE/golden-tag1"; printf '%s\n' "$S/sysbox" > "$S/st/mounts"
out="$(run_org --apply 2>&1)"; rc=$?
[ "$rc" -eq 0 ] && grep -qxF "systemctl enable --now $STORE_UNIT" "$CALLLOG" && grep -qx "$STORE" "$S/st/mounts"; expect $? "an empty, unmounted store directory is mounted through its unit"
setup
printf '%s\n' "$S/sysbox" > "$S/st/mounts"
out="$(run_org --apply 2>&1)"; rc=$?
[ "$rc" -ne 0 ] && grep -q "$STORE holds files but is not a mount" <<< "$out" && ! grep -qF "systemctl enable --now $STORE_UNIT" "$CALLLOG"; expect $? "a store directory holding files on the root filesystem is never mounted over"
setup
run_org --apply >/dev/null 2>&1
sed -i 's/^Options=loop,discard$/Options=loop/' "$S/units/$STORE_UNIT"; : > "$CALLLOG"
out="$(run_org --apply 2>&1)"; rc=$?
[ "$rc" -eq 0 ] && grep -qx "Options=loop,discard" "$S/units/$STORE_UNIT" && grep -qx "mount -o remount,discard $STORE" "$CALLLOG" && ! grep -qF "systemctl enable --now $STORE_UNIT" "$CALLLOG"
expect $? "an older store mount unit is rewritten and remounted in place, never restarted under the slots"

# ---- the listener waits for the runner group and the image ---------------------------------------
setup
printf '{"runner_groups": [{"id": 1, "name": "Default"}]}\n' > "$S/fx/groups-none.json"
out="$(GROUPS_JSON="$S/fx/groups-none.json" run_org --apply 2>&1)"; rc=$?
[ "$rc" -eq 0 ] && grep -q "OPERATOR ACTION.*Create the organization runner group 'box-ci' in acme (Settings > Actions > Runner groups), visible to selected repositories only: alpha beta" <<< "$out"; expect $? "an absent runner group ends at the create-group gate, naming the lane's repositories"
grep -q "not started: runner group absent" <<< "$out" && [ -f "$S/units/box-ci-listener.service" ] && ! grep -qE "systemctl (enable|start) box-ci-listener" "$CALLLOG"; expect $? "without the group the listener is built and written but not started"
grep -A1 "Phase M: box-ci-listener-health.timer" <<< "$out" | tail -n 1 | grep -q "not written: the listener is not running" && [ ! -e "$S/units/box-ci-listener-health.timer" ] && ! grep -q "listener-health" "$CALLLOG"
expect $? "a listener held back at a gate gets no health timer (its restart would start the listener past the gate)"
setup
out="$(GH_GROUPS_RC=1 run_org --apply 2>&1)"; rc=$?
[ "$rc" -eq 0 ] && grep -q "OPERATOR ACTION.*The App acme-bot cannot read acme's runner groups. Grant it the organization permission 'Self-hosted runners: Read and write'" <<< "$out" && ! grep -q "systemctl start box-ci-listener" "$CALLLOG"; expect $? "an unreadable group API ends at the App permission gate and starts no listener"
setup
printf '{"runners": []}\n' > "$S/fx/runners-empty.json"
out="$(RUNNERS_JSON="$S/fx/runners-empty.json" run_org --apply 2>&1)"; rc=$?
[ "$rc" -eq 0 ] && grep -q "OPERATOR ACTION.*Grant the App acme-bot the organization permission 'Self-hosted runners: Read and write' in acme if it is not granted yet.*journalctl -u box-ci-listener.service" <<< "$out"; expect $? "before any lane registration exists the write-permission gate prints, pointing at the listener's journal"
# A new lane has no image.env until its first image build: the run must still reach the listener
# phase and print the build gate, and --check must report the unset tag, not die.
setup
rm -f "$S/state/image.env"
out="$(run_org 2>&1)"; rc=$?
[ "$rc" -eq 0 ] && grep -q "names no KNOWN_CI_IMAGE_TAG" <<< "$out" && grep -q "Phase K" <<< "$out" && grep -qF "OPERATOR ACTION" <<< "$out" \
  && grep -qF "Build the runner image first: flock -o /run/box-ci-runner-build.lock $ROOT/bin/build-runner-image.sh box box-ci writes $S/state/image.env" <<< "$out"
expect $? "without image.env (a lane never built) the run reaches the build gate, with the exact command"
out="$(run_org --check 2>&1)"; rc=$?
[ "$rc" -eq 1 ] && grep -q "image=box-ci-runner tag=unset" <<< "$out" && grep -q "smoke=skipped" <<< "$out"
expect $? "without image.env --check reports the unset tag and runs to the end"
setup
out="$(IMAGE_PRESENT=0 run_org --apply 2>&1)"; rc=$?
[ "$rc" -eq 0 ] && grep -q "OPERATOR ACTION.*Build the runner image first" <<< "$out" && grep -q "not started: image absent" <<< "$out" && ! grep -q "systemctl start box-ci-listener" "$CALLLOG"; expect $? "a missing image ends at the build gate and starts no listener"

# ---- migration: retiring the always-on template ---------------------------------------------------
# A lane converged before its listener: box-ci@.service with Restart=always, both instances enabled
# and running; slot 1's runner is on a job (runner 503 is busy), slot 2's is idle (504).
old_lane() {
  setup
  printf '[Unit]\nDescription=always-on slot\n\n[Service]\nExecStartPre=+/opt/old/lane-slot.sh prepare %%i\nExecStopPost=+/opt/old/lane-slot.sh cleanup %%i\nRestart=always\nRestartSec=10\n\n[Install]\nWantedBy=multi-user.target\n' > "$S/units/box-ci@.service"
  mkdir -p "$S/units/multi-user.target.wants" "$S/run/1"
  ln -sf "$S/units/box-ci@.service" "$S/units/multi-user.target.wants/box-ci@1.service"
  ln -sf "$S/units/box-ci@.service" "$S/units/multi-user.target.wants/box-ci@2.service"
  echo active > "$S/st/state/box-ci@1.service"; echo active > "$S/st/state/box-ci@2.service"
  printf 'box-lane-1-1727001000\n' > "$S/run/1/name"; printf 'box-lane-2-1727001100\n' > "$S/run/2/name"
  # The listener has not run yet: no warm slot of its own.
  rm -rf "$S/run/ci"; rm -f "$S/st/state/box-ci-ci@1.service"
}
old_lane
out="$(WARM_POOL=1 run_org --apply 2>&1)"; rc=$?
[ "$rc" -eq 0 ]; expect $? "--apply migrates an always-on lane"
grep -qx "systemctl disable box-ci@1.service" "$CALLLOG" && grep -qx "systemctl disable box-ci@2.service" "$CALLLOG" && [ ! -e "$S/units/multi-user.target.wants/box-ci@1.service" ]; expect $? "every always-on instance is disabled, without --now"
grep -qx "Restart=no" "$S/units/box-ci@.service" && ! grep -q "Restart=always" "$S/units/box-ci@.service"; expect $? "the always-on template is rewritten with Restart=no, so no instance restarts after its current job"
before '^systemctl start box-ci-listener.service' '^systemctl disable box-ci@1.service' && between '^systemctl disable box-ci@2.service' '^systemctl daemon-reload' '^systemctl stop box-ci@2.service'; expect $? "the retirement starts only after the listener started a slot, and the rewrite is reloaded before any stop"
grep -qx "systemctl stop box-ci@2.service" "$CALLLOG" && ! grep -q "systemctl stop box-ci@1.service" "$CALLLOG"; expect $? "the idle always-on slot is stopped now; the one whose runner is on a job is left to finish"
[ -f "$S/units/box-ci@.service" ] && grep -q "box-ci@.service stays until box-ci@1 finish; re-run --apply to remove it" <<< "$out"; expect $? "the template stays while an instance still runs from it (its cleanup needs it)"
out="$(run_org --check 2>&1)"; rc=$?
[ "$rc" -ne 0 ] && grep -q "always_on_template=box-ci@.service present (retiring" <<< "$out"; expect $? "--check fails while the always-on template is still there"
echo inactive > "$S/st/state/box-ci@1.service"; : > "$CALLLOG"
out="$(run_org --apply 2>&1)"; rc=$?
[ "$rc" -eq 0 ] && [ ! -e "$S/units/box-ci@.service" ] && grep -qx "systemctl daemon-reload" "$CALLLOG" && grep -q "retired: the listener starts every slot now" <<< "$out"; expect $? "once no instance runs, the next apply removes the template and reloads"
old_lane
out="$(WARM_POOL=0 run_org --apply 2>&1)"; rc=$?
[ "$rc" -eq 0 ] && grep -q "kept: the listener started no slot within 180s" <<< "$out" && grep -qx "Restart=always" "$S/units/box-ci@.service" \
  && ! grep -qE "systemctl (disable|stop) box-ci@" "$CALLLOG" && [ -e "$S/units/multi-user.target.wants/box-ci@1.service" ]; expect $? "a listener that starts no slot leaves every always-on slot serving"
old_lane
out="$(WARM_POOL=1 WARM_STATE=activating run_org --apply 2>&1)"; rc=$?
[ "$rc" -eq 0 ] && grep -q "kept: the listener started no slot within 180s" <<< "$out" && ! grep -qE "systemctl (disable|stop) box-ci@" "$CALLLOG"; expect $? "a warm slot still in its prepare (activating) is not yet proof: nothing is retired"
old_lane
out="$(IMAGE_PRESENT=0 run_org --apply 2>&1)"; rc=$?
[ "$rc" -eq 0 ] && grep -q "kept: the listener is not running, so the always-on slots keep serving the lane" <<< "$out" && ! grep -qE "systemctl (disable|stop) box-ci@" "$CALLLOG"; expect $? "a listener that cannot start leaves every always-on slot serving"
old_lane
out="$(WARM_POOL=1 GH_RUNNERS_RC=1 run_org --apply 2>&1)"; rc=$?
[ "$rc" -eq 0 ] && ! grep -qE "systemctl stop box-ci@" "$CALLLOG" && grep -q "box-ci@1 keeps running: the runner list is unreadable" <<< "$out" && [ -f "$S/units/box-ci@.service" ]; expect $? "with the runner list unreadable no always-on slot is stopped (any may be on a job)"

# ---- check ------------------------------------------------------------------------------------------
converged_org() {
  setup
  run_org --apply >/dev/null 2>&1
  mkdir -p "$GHDIR"; printf 'github.com:\n    oauth_token: %s\n' "$APP_SESSION_MARKER" > "$GHDIR/hosts.yml"
  login_json > "$S/state/codex/auth.json"
  : > "$CALLLOG"
}
converged_org
out="$(run_org --check 2>&1)"; rc=$?
[ "$rc" -eq 0 ]; expect $? "--check passes on a converged org lane"
grep -q "network=box-ci bridge=box-ci0 icc=false subnet=172.20.0.0/16 gateway=172.20.0.1" <<< "$out" && grep -q "firewall docker_user_reject=present input_reject=present" <<< "$out"; expect $? "--check reports the network, its bridge name and both firewall rules"
grep -q "store_fs=mounted (/dev/loop8 xfs 40G)" <<< "$out" && grep -q "disk free=62G lane_worst_case=60G (store 40G + 2 slots x 10G, all sparse)" <<< "$out"; expect $? "--check reports the store mount and the lane's disk headroom"
grep -q "store=$STORE/golden-tag1 present" <<< "$out"; expect $? "--check reports the current tag's preloaded store"
grep -qx "store_trash=$STORE/trash entries=0 oldest_age_s=none" <<< "$out" && grep -qx "store_reaper_timer=box-ci-store-reaper.timer active" <<< "$out" && grep -qx "store_reaper=box-ci-store-reaper.service inactive" <<< "$out"
expect $? "--check reports the store's trash (empty on a converged lane), the reaper's timer, and the reaper's last run"
grep -q "slice=box-ci.slice MemoryMax=17179869184 MemoryHigh=15032385536 CPUQuotaPerSecUSec=8s CPUWeight=50" <<< "$out" && grep -q "parent_slice=box.slice CPUWeight=50" <<< "$out"; expect $? "--check reports the slice limits as systemd applies them"
grep -qx "template=box-ci-ci@.service current running=1" <<< "$out" && grep -qx "template=box-ci-qae@.service current running=none" <<< "$out" && grep -q "always_on_template=absent" <<< "$out"; expect $? "--check reports both templates, the instances running now, and no always-on template"
grep -q "app_key=$S/etc/box-ci/app.pem present mode=0600" <<< "$out" && ! grep -qF "$KEY_MARKER" <<< "$out"; expect $? "--check reports the App key's presence and mode, never its content"
grep -q "listener_binary=$S/lib/box-ci/listener current" <<< "$out" && grep -q "listener_config=$S/etc/box-ci/listener.json parseable" <<< "$out" && grep -qE "listener=active journal_newest_line_age_s=[0-9]+$" <<< "$out"; expect $? "--check reports the listener's binary, config, unit state and journal age"
grep -q "warm_pool=1 slots hold a job file (min_runners 1)" <<< "$out"; expect $? "--check counts the warm pool's job files against min_runners"
grep -qx "token_timer=box-ci-token-refresh.timer active" <<< "$out" && grep -qx "image_build_timer=box-ci-image-build.timer active" <<< "$out" && grep -q "token_file=$GHDIR/hosts.yml younger than 15 minutes" <<< "$out"; expect $? "--check reports an org lane's token and image-build timers and its token file"
grep -q "codex_config=$S/etc/box-ci/codex-config.toml current" <<< "$out" && grep -q "codex_login=$S/state/codex/auth.json present" <<< "$out" && ! grep -q '"auth_mode"' <<< "$out"; expect $? "--check reports an org lane's Codex config and login, without printing the login"
grep -qx "codex_keepalive_timer=box-ci-codex-keepalive.timer active" <<< "$out" && grep -qx "codex_keepalive=box-ci-codex-keepalive.service inactive" <<< "$out" \
  && grep -qE "^codex_login=$S/state/codex/auth.json present last_refresh=[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9:]+\.123456789Z age_days=0\.0$" <<< "$out" && ! grep -qF "$LOGIN_MARKER" <<< "$out"
expect $? "--check reports the keepalive timer, its last run, and each store's last refresh and age, read from last_refresh alone"
grep -q "image=box-ci-runner:tag1 present" <<< "$out" && grep -q "runner_group=box-ci id=7 repositories=alpha beta" <<< "$out"; expect $? "--check reports the runner image and the runner group's repository list"
grep -q "registrations online=1 busy=1 offline=4 offline_leftovers=box-lane-1-1727000000 box-lane-ci-2-1727002100" <<< "$out"; expect $? "--check reports lane registrations of both shapes and the offline leftovers"
grep -q "smoke=PASS" <<< "$out" && grep -q "smoke net-gateway-54321 ok" <<< "$out" && grep -q "smoke inner-pulls ok 0 image pulls" <<< "$out" && grep -q "smoke container_ready_s=" <<< "$out" && grep -q "smoke store_snapshot_s=" <<< "$out"; expect $? "--check runs the smoke proofs and records the snapshot and container start times"
cmp -s "$S/st/smoke-stdin" "$S/repo/bin/lane-smoke.sh"; expect $? "the smoke container receives lane-smoke.sh on stdin"
grep -qE -- "docker run --rm -i --runtime=sysbox-runc --network box-ci --cgroup-parent box-ci.slice --memory 4g --pids-limit 2048 --tmpfs /tmp:size=2g,exec --oom-score-adj 1000 --dns 1.1.1.1 --dns 8.8.8.8 --cpuset-cpus 0-3 -e KNOWN_CI_CPUS=4 --tmpfs /home/runner:size=1g -v $STORE/smoke-[0-9]+:/var/lib/docker --name box-ci-smoke-" "$CALLLOG" \
  && grep -qF -- "--entrypoint /bin/bash box-ci-runner:tag1 -s -- 172.20.0.1 203.0.113.7 100.64.0.9 22 54321 54322" "$CALLLOG"
expect $? "the smoke container uses the slot's flags, a store snapshot on /var/lib/docker, and probes the lane's check_ports on the gateway, public and tailnet addresses"
grep -qE "^cp --reflink=always -a $STORE/golden-tag1/overlay2/./layer1 $STORE/smoke-[0-9]+/overlay2/./layer1$" "$CALLLOG" && ! compgen -G "$STORE/smoke-*" >/dev/null; expect $? "the smoke's snapshot is a reflink copy of the current tag's store, removed after the smoke"
{ ! grep -qE "$MUTATIONS" "$CALLLOG"; }; expect $? "--check mutates nothing"
grep -q "GH_CONFIG_DIR=$GHDIR GH_TOKEN=unset as=box-ci" "$CALLLOG"; expect $? "--check reads GitHub as the lane user"
rm -rf "$STORE/golden-tag1.aside"; mv "$STORE/golden-tag1" "$STORE/golden-tag1.aside"; : > "$CALLLOG"
out="$(run_org --check 2>&1)"; rc=$?
mv "$STORE/golden-tag1.aside" "$STORE/golden-tag1"
[ "$rc" -ne 0 ] && grep -q "store=$STORE/golden-tag1 absent" <<< "$out" && grep -q "smoke=skipped" <<< "$out" && ! grep -q "docker run --rm -i" "$CALLLOG"; expect $? "--check fails, and runs no smoke container, when the current tag's store is missing"
out="$(SMOKE_OUT=<(sed 's/^probe net-gateway-54321 ok.*/probe net-gateway-54321 fail 172.20.0.1:54321 answered/' "$S/fx/smoke.out") run_org --check 2>&1)"; rc=$?
[ "$rc" -ne 0 ] && grep -q "smoke=FAIL failed=net-gateway-54321" <<< "$out"; expect $? "--check fails when a slot reaches a declared host port"
out="$(SMOKE_OUT=<(grep -v inner-pulls "$S/fx/smoke.out") run_org --check 2>&1)"; rc=$?
[ "$rc" -ne 0 ] && grep -q "missing=inner-pulls" <<< "$out"; expect $? "--check fails when a proof is missing from the smoke output"
out="$(SMOKE_OUT=<(grep -v '^probe psql ' "$S/fx/smoke.out") run_org --check 2>&1)"; rc=$?
[ "$rc" -ne 0 ] && grep -q "missing=psql" <<< "$out"; expect $? "--check fails when the image has no psql proof (a job's own tests may need psql)"
out="$(SMOKE_OUT=<(sed 's/^probe write-etc ok.*/probe write-etc fail the root filesystem took a write at \/etc/' "$S/fx/smoke.out") run_org --check 2>&1)"; rc=$?
[ "$rc" -ne 0 ] && grep -q "failed=write-etc" <<< "$out"; expect $? "--check fails when a job can write outside its slot and tmpfs"
printf '{"repositories": [{"name": "alpha"}]}\n' > "$S/fx/repos-short.json"
out="$(REPOS_JSON="$S/fx/repos-short.json" run_org --check 2>&1)"; rc=$?
[ "$rc" -ne 0 ] && grep -q "runner_group_repositories_expected=alpha beta" <<< "$out"; expect $? "--check fails when the runner group lists other repositories than the lane"
out="$(FW_PRESENT=0 run_org --check 2>&1)"; rc=$?
[ "$rc" -ne 0 ] && grep -q "docker_user_reject=absent input_reject=absent" <<< "$out" && grep -q "smoke=skipped" <<< "$out"; expect $? "--check fails, and runs no smoke container, without the firewall rules"
out="$(SLICE_MEM=infinity run_org --check 2>&1)"; rc=$?
[ "$rc" -ne 0 ]; expect $? "--check fails when the slice memory cap is not applied"
out="$(SLICE_HIGH=infinity run_org --check 2>&1)"; rc=$?
[ "$rc" -ne 0 ]; expect $? "--check fails when the slice's MemoryHigh is not applied"
out="$(GH_GROUPS_RC=1 run_org --check 2>&1)"; rc=$?
[ "$rc" -ne 0 ] && grep -q "runner_group=box-ci unreadable" <<< "$out"; expect $? "--check fails when the App cannot read the runner groups"
out="$(JOURNAL_TS=$(( $(date +%s) - 600 )) run_org --check 2>&1)"; rc=$?
[ "$rc" -ne 0 ] && grep -q "listener_journal=silent for more than 180s" <<< "$out"; expect $? "--check fails when the listener's newest journal line is ten minutes old"
out="$(JOURNAL_TS=none run_org --check 2>&1)"; rc=$?
[ "$rc" -ne 0 ] && grep -q "journal_newest_line_age_s=none" <<< "$out"; expect $? "--check fails when the listener has no journal at all"
echo failed > "$S/st/state/box-ci-listener.service"
out="$(run_org --check 2>&1)"; rc=$?
echo active > "$S/st/state/box-ci-listener.service"
[ "$rc" -ne 0 ] && grep -q "listener=failed" <<< "$out"; expect $? "--check fails when the listener is not active"
out="$(run_org --check 2>&1)"; rc=$?
[ "$rc" -eq 0 ] && grep -qx "listener_health_timer=box-ci-listener-health.timer active" <<< "$out" && grep -qx "listener_health=absent (the timer has not run yet)" <<< "$out"
expect $? "--check reports the health timer active, and a verdict not yet written without failing"
mkdir -p "$S/healthrun"; printf 'ok 2026-09-29T12:00:00Z\n' > "$S/healthrun/box-ci-listener-health"
out="$(run_org --check 2>&1)"; rc=$?
[ "$rc" -eq 0 ] && grep -qx "listener_health=ok 2026-09-29T12:00:00Z" <<< "$out"; expect $? "--check prints the health check's verdict line"
printf 'unhealthy 2026-09-29T12:00:00Z credentials: 2 GitHub 401/403 lines in 10 minutes\n' > "$S/healthrun/box-ci-listener-health"
out="$(run_org --check 2>&1)"; rc=$?
[ "$rc" -ne 0 ] && grep -qx "listener_health=unhealthy 2026-09-29T12:00:00Z credentials: 2 GitHub 401/403 lines in 10 minutes" <<< "$out"; expect $? "--check fails on an unhealthy verdict, printing it"
rm -f "$S/healthrun/box-ci-listener-health"
# The store's trash: its backlog, and a reaper that is stuck.
trash_age() { sed -n 's/^store_trash=.* oldest_age_s=//p' <<< "$out"; }
mkdir "$STORE/trash/$(( $(date +%s) - 60 )).slot-ci-1.4242" "$STORE/trash/$(date +%s).slot-wait-2.4243"
out="$(run_org --check 2>&1)"; rc=$?
[ "$rc" -eq 0 ] && grep -qE "^store_trash=$STORE/trash entries=2 oldest_age_s=[0-9]+$" <<< "$out" && [ "$(trash_age)" -ge 60 ] && [ "$(trash_age)" -lt 1800 ] && ! grep -q "store_trash_stale" <<< "$out"
expect $? "--check reports the trash's backlog and the age of its oldest entry, read from the entry's name, and passes while the reaper keeps up"
mkdir "$STORE/trash/$(( $(date +%s) - 3600 )).slot-ci-2.4244"
out="$(run_org --check 2>&1)"; rc=$?
rm -rf "$STORE/trash"; mkdir -m 0700 "$STORE/trash"
[ "$rc" -ne 0 ] && grep -qE "^store_trash=$STORE/trash entries=3 oldest_age_s=[0-9]+$" <<< "$out" && [ "$(trash_age)" -ge 3600 ] \
  && grep -qE "^store_trash_stale=the oldest retired store copy has waited [0-9]+s, past 1800s: the reaper is stuck or behind \(journalctl -u box-ci-store-reaper\.service\)$" <<< "$out"
expect $? "--check fails when the trash's oldest entry has waited more than 30 minutes: the reaper is stuck or behind"
echo failed > "$S/st/state/box-ci-store-reaper.service"
out="$(run_org --check 2>&1)"; rc=$?
rm -f "$S/st/state/box-ci-store-reaper.service"
[ "$rc" -ne 0 ] && grep -qx "store_reaper=box-ci-store-reaper.service failed" <<< "$out" \
  && grep -qx "store_reaper_failed=its last run could not delete a retired store copy (journalctl -u box-ci-store-reaper.service)" <<< "$out"
expect $? "--check fails when the reaper's last run could not delete a copy"
rmdir "$STORE/trash"
out="$(run_org --check 2>&1)"; rc=$?
mkdir -m 0700 "$STORE/trash"
[ "$rc" -ne 0 ] && grep -qx "store_trash=$STORE/trash absent (re-run --apply)" <<< "$out"; expect $? "--check fails on a store without its trash directory"
for t in box-ci-listener-health.timer box-ci-token-refresh.timer box-ci-image-build.timer box-ci-codex-keepalive.timer box-ci-store-reaper.timer; do
  echo inactive > "$S/st/state/$t"
  out="$(run_org --check 2>&1)"; rc=$?
  echo active > "$S/st/state/$t"
  [ "$rc" -ne 0 ] && grep -qE "=$t inactive$" <<< "$out"; expect $? "--check fails when $t is not active"
done
touch -t 202601010000 "$GHDIR/hosts.yml"
out="$(run_org --check 2>&1)"; rc=$?
touch "$GHDIR/hosts.yml"
[ "$rc" -ne 0 ] && grep -q "token_file=$GHDIR/hosts.yml absent or older than 15 minutes" <<< "$out"; expect $? "--check fails on a token file the timer stopped refreshing"
rm "$S/state/codex/auth.json"
out="$(run_org --check 2>&1)"; rc=$?
login_json > "$S/state/codex/auth.json"
[ "$rc" -ne 0 ] && grep -qxF "codex_login=absent: sudo -u box-ci env CODEX_HOME=/var/lib/box-ci/codex HOME=$S/state/home PATH=/usr/bin:/bin codex login --device-auth" <<< "$out"; expect $? "--check fails, with the login command, until the Codex store holds a login"
echo failed > "$S/st/state/box-ci-codex-keepalive.service"
out="$(run_org --check 2>&1)"; rc=$?
rm -f "$S/st/state/box-ci-codex-keepalive.service"
[ "$rc" -ne 0 ] && grep -qx "codex_keepalive=box-ci-codex-keepalive.service failed" <<< "$out" \
  && grep -qx "codex_keepalive_failed=its last run did not refresh every store it ran Codex on (journalctl -u box-ci-codex-keepalive.service)" <<< "$out"
expect $? "--check fails when the keepalive's last run could not refresh a store"
login_json 11 > "$S/state/codex/auth.json"
out="$(run_org --check 2>&1)"; rc=$?
[ "$rc" -eq 0 ] && grep -qE "^codex_login=$S/state/codex/auth.json present last_refresh=.* age_days=11\.0$" <<< "$out" && ! grep -q "codex_login_stale" <<< "$out"
expect $? "--check passes a store 11 days old: past Codex's refresh point, inside the day the keepalive has to reach it"
login_json 13 > "$S/state/codex/auth.json"
out="$(run_org --check 2>&1)"; rc=$?
[ "$rc" -ne 0 ] && grep -qx "codex_login_stale=$S/state/codex last refreshed 13.0 days ago, past 12: the keepalive refreshes a store at 10 days (journalctl -u box-ci-codex-keepalive.service)" <<< "$out"
expect $? "--check fails on a store no job or keepalive has refreshed for 13 days"
printf '{"auth_mode": "chatgpt"}\n' > "$S/state/codex/auth.json"
out="$(run_org --check 2>&1)"; rc=$?
login_json > "$S/state/codex/auth.json"
[ "$rc" -ne 0 ] && grep -qx "codex_login=$S/state/codex/auth.json present last_refresh=unreadable (not a ChatGPT login Codex refreshes)" <<< "$out"
expect $? "--check fails on a login without a readable last_refresh"
/bin/chmod 0644 "$S/etc/box-ci/app.pem"
out="$(run_org --check 2>&1)"; rc=$?
/bin/chmod 0600 "$S/etc/box-ci/app.pem"
[ "$rc" -ne 0 ] && grep -q "app_key=$S/etc/box-ci/app.pem present mode=not-0600" <<< "$out"; expect $? "--check fails on an App key other users can read"
printf 'not json\n' > "$S/etc/box-ci/listener.json.good"; mv "$S/etc/box-ci/listener.json" "$S/etc/box-ci/listener.json.keep"; mv "$S/etc/box-ci/listener.json.good" "$S/etc/box-ci/listener.json"
out="$(run_org --check 2>&1)"; rc=$?
mv "$S/etc/box-ci/listener.json.keep" "$S/etc/box-ci/listener.json"
[ "$rc" -ne 0 ] && grep -q "listener_config=$S/etc/box-ci/listener.json absent or not JSON" <<< "$out"; expect $? "--check fails on a listener config that does not parse"
printf 'package main // newer\n' > "$S/repo/listener/main.go"
out="$(run_org --check 2>&1)"; rc=$?
printf 'package main\n' > "$S/repo/listener/main.go"
[ "$rc" -ne 0 ] && grep -q "listener_binary=$S/lib/box-ci/listener stale" <<< "$out"; expect $? "--check fails when the installed listener was built from other source than this checkout"
rm -rf "$S/run/ci"
out="$(run_org --check 2>&1)"; rc=$?
grep -q "warm_pool=0 of 1: filling" <<< "$out"; expect $? "--check says the warm pool is filling while it is short"
printf '# edited on the machine\n' >> "$S/units/box-ci-qae@.service"
out="$(run_org --check 2>&1)"; rc=$?
[ "$rc" -ne 0 ] && grep -q "template=box-ci-qae@.service DRIFTED" <<< "$out"; expect $? "--check fails on a template that drifted from the checkout"
mv "$S/units/box-ci-ci@.service" "$S/unit.aside"
out="$(run_org --check 2>&1)"; rc=$?
mv "$S/unit.aside" "$S/units/box-ci-ci@.service"
[ "$rc" -eq 1 ] && grep -q "template=box-ci-ci@.service absent" <<< "$out" && grep -q "smoke=" <<< "$out"; expect $? "--check reports a missing template and still runs to the end"

# ---- remove -----------------------------------------------------------------------------------------
converged_org
# A retiring always-on slot is still enabled when the lane is removed.
mkdir -p "$S/units/multi-user.target.wants"; ln -sf "$S/units/x" "$S/units/multi-user.target.wants/box-ci@3.service"
out="$(run_org --remove 2>&1)"; rc=$?
[ "$rc" -eq 0 ] && grep -q "DRY-RUN: systemctl disable --now box-ci-listener.service" <<< "$out" && grep -q "DRY-RUN: docker network rm box-ci" <<< "$out" && grep -q "DRY-RUN: $S/lib/box-ci/listener -delete-sets $S/etc/box-ci/listener.json" <<< "$out"; expect $? "--remove alone plans the teardown, the scale sets' deletion included"
grep -q "DRY-RUN: systemctl disable --now box-ci-store-reaper.timer" <<< "$out" && grep -q "DRY-RUN: systemctl stop box-ci-store-reaper.service" <<< "$out" && grep -q "DRY-RUN: rm -rf $STORE/trash$" <<< "$out"; expect $? "--remove alone plans the store reaper's stop and the emptying of the trash"
{ ! grep -qE "$MUTATIONS" "$CALLLOG"; } && [ -f "$S/units/box-ci-ci@.service" ]; expect $? "--remove alone mutates nothing"
mkdir -p "$STORE/slot-ci-1" "$STORE/slot-qae-2" "$S/state/slot-ci-1"; echo "$S/state/slot-ci-1" >> "$S/st/mounts"
mkdir -p "$STORE/trash/1700000100.slot-ci-1.7/overlay2"; printf 'retired\n' > "$STORE/trash/1700000100.slot-ci-1.7/overlay2/file"
mkdir -p "$S/healthrun"; printf 'ok 2026-09-29T12:00:00Z\n' > "$S/healthrun/box-ci-listener-health"; date +%s > "$S/healthrun/box-ci-listener-health.restarted"
: > "$CALLLOG"
out="$(run_org --remove --apply 2>&1)"; rc=$?
[ "$rc" -eq 0 ]; expect $? "--remove --apply exits 0"
[ "$(line_of '^systemctl disable --now box-ci-listener-health.timer')" = 1 ] && [ "$(line_of '^systemctl stop box-ci-listener-health.service')" = 2 ] && [ "$(line_of '^systemctl disable --now box-ci-listener.service')" = 3 ]
expect $? "--remove stops the health check first (so nothing restarts the listener), then the listener, before anything else"
grep -qx "systemctl disable --now box-ci-token-refresh.timer" "$CALLLOG" && grep -qx "systemctl disable --now box-ci-image-build.timer" "$CALLLOG"; expect $? "--remove disables an org lane's token and image-build timers"
grep -qx "systemctl disable --now box-ci-codex-keepalive.timer" "$CALLLOG" && ! grep -q "systemctl stop box-ci-codex-keepalive.service" "$CALLLOG" \
  && before '^systemctl disable --now box-ci-codex-keepalive.timer' '^systemctl daemon-reload'
expect $? "--remove disables the Codex keepalive timer before the reload, and lets a keepalive already running finish"
grep -qx "systemctl stop box-ci-ci@1.service" "$CALLLOG" && grep -qx "systemctl stop box-ci-qae@2.service" "$CALLLOG" && grep -qx "docker rm -f box-ci-ci-1" "$CALLLOG" && grep -qx "docker rm -f box-ci-qae-2" "$CALLLOG"; expect $? "--remove stops every slot unit of both kinds and removes its container"
grep -qx "systemctl disable --now box-ci@3.service" "$CALLLOG" && grep -qx "docker rm -f box-ci-3" "$CALLLOG"; expect $? "--remove also stops and disables a leftover always-on instance"
grep -qx "umount $S/state/slot-ci-1" "$CALLLOG" && [ ! -e "$S/state/slot-ci-1" ] && [ ! -e "$STORE/slot-ci-1" ] && [ ! -e "$STORE/slot-qae-2" ] && [ -d "$STORE/golden-tag1" ] && [ -f "$S/units/$STORE_UNIT" ]; expect $? "--remove unmounts and deletes the slot filesystems and snapshots and leaves the store filesystem and the preloaded store"
grep -qx "systemctl disable --now box-ci-store-reaper.timer" "$CALLLOG" && before '^systemctl stop box-ci-qae@2.service' '^systemctl disable --now box-ci-store-reaper.timer' \
  && before '^systemctl disable --now box-ci-store-reaper.timer' '^systemctl stop box-ci-store-reaper.service' && before '^systemctl stop box-ci-store-reaper.service' '^systemctl daemon-reload'
expect $? "--remove stops the store reaper, its timer first, once every slot has stopped, and before the units are deleted"
[ ! -e "$STORE/trash" ] && [ -f "$STORE/golden-tag1/overlay2/layer1/diff/usr/bin/postgres" ]; expect $? "--remove empties the store's trash and leaves the preloaded store"
grep -qx "listener -delete-sets $S/etc/box-ci/listener.json" "$CALLLOG" && before '^systemctl stop box-ci-qae@2.service' '^listener -delete-sets'; expect $? "--remove has the listener delete the lane's scale sets, after the slots stopped"
[ "$(grep -c '^gh api -X DELETE' "$CALLLOG" | tr -d ' ')" = 6 ] && ! grep -q 'runners/50[567]\|runners/510' "$CALLLOG"; expect $? "--remove deregisters every lane registration of both shapes and nothing else"
for f in box-ci-ci@.service box-ci-qae@.service box-ci-listener.service box-ci.slice box.slice box-ci-token-refresh.service box-ci-token-refresh.timer box-ci-image-build.service box-ci-image-build.timer box-ci-listener-health.service box-ci-listener-health.timer \
  box-ci-codex-keepalive.service box-ci-codex-keepalive.timer box-ci-store-reaper.service box-ci-store-reaper.timer; do
  [ ! -e "$S/units/$f" ] || { echo "  left: $f"; false; } || break
done
expect $? "--remove deletes every lane unit: templates, listener, slices, token, image-build, health, keepalive and store-reaper units"
[ ! -e "$S/run" ] && [ ! -e "$S/lib/box-ci/listener" ] && [ ! -e "$S/etc/box-ci/listener.json" ] && [ ! -e "$S/etc/box-ci/codex-config.toml" ] && grep -qx "docker network rm box-ci" "$CALLLOG"; expect $? "--remove deletes the run dir, the listener binary, its config, the tracked Codex config and the network"
[ ! -e "$S/healthrun/box-ci-listener-health" ] && [ ! -e "$S/healthrun/box-ci-listener-health.restarted" ]; expect $? "--remove deletes the health verdict and its cooldown"
[ -f "$S/etc/box-ci/app.pem" ] && [ -f "$S/state/codex/auth.json" ] && ! grep -qE '^(apt-get|userdel)' "$CALLLOG" && grep -q "Left in place: the host's Docker and Sysbox, .*the App key ($S/etc/box-ci/app.pem), the lane user box-ci and its Codex stores $S/state/codex" <<< "$out"
expect $? "--remove keeps the App key, the lane user and its Codex login, and says so"
setup
printf '{"runner_groups": [{"id": 1, "name": "Default"}]}\n' > "$S/fx/groups-none.json"
GROUPS_JSON="$S/fx/groups-none.json" run_org --apply >/dev/null 2>&1
rm -f "$S/units/box-ci-token-refresh.timer" "$S/units/box-ci-image-build.timer" "$S/units/box-ci-codex-keepalive.timer" "$S/units/box-ci-store-reaper.timer"
: > "$CALLLOG"
out="$(run_org --remove --apply 2>&1)"; rc=$?
[ "$rc" -eq 0 ] && ! grep -q "listener-health\|token-refresh.timer\|image-build.timer\|codex-keepalive.timer\|store-reaper" <<< "$(grep '^systemctl \(disable\|stop\)' "$CALLLOG")" && [ "$(line_of '^systemctl disable --now box-ci-listener.service')" = 1 ] && grep -qx "docker network rm box-ci" "$CALLLOG"
expect $? "--remove of a lane whose listener and timers never ran disables no unit file that does not exist, and runs to the end"

# ---- qae_concurrency 2: one Codex login store per qae instance -----------------------------------
setup
sed 's/^    qae_concurrency: 1$/    qae_concurrency: 2/' "$S/host.yml" > "$S/host-qae2.yml"
run_qae2() { HOST_YML_UNDER_TEST="$S/host-qae2.yml" run_org "$@"; }
out="$(run_qae2 --apply 2>&1)"; rc=$?
[ "$rc" -eq 0 ] && grep -qx "chown box-ci: $S/state/codex" "$CALLLOG" && grep -qx "chown box-ci: $S/state/codex-2" "$CALLLOG" \
  && [ "$(mode_of "$S/state/codex")" = 700 ] && [ "$(mode_of "$S/state/codex-2")" = 700 ] && [ ! -e "$S/state/codex-3" ]
expect $? "qae_concurrency 2 gives each qae instance its own Codex store, the first at codex_store and the second at codex_store-2, each 0700 for the lane user"
qu="$S/units/box-ci-qae@.service"
grep -qx "Environment=KNOWN_CI_CODEX_STORE_1=$S/state/codex" "$qu" && grep -qx "Environment=KNOWN_CI_CODEX_STORE_2=$S/state/codex-2" "$qu" \
  && ! grep -q "KNOWN_CI_CODEX_STORE_3=\|KNOWN_CI_CODEX_STORE=" "$qu" && [ "$(grep -c -- '-v [^ ]*:/home/runner/.codex-qae ' "$qu")" = 1 ] \
  && grep -qF -- '-v ${KNOWN_CI_CODEX_STORE_%i}:/home/runner/.codex-qae ' "$qu" && grep -qx "ExecStartPre=+$HELPER prepare qae %i" "$qu"
expect $? "a qae instance mounts only its own store: the template's one store mount resolves through %i to KNOWN_CI_CODEX_STORE_<n>, and instance n's prepare resets that same store"
grep -qx "Environment=KNOWN_CI_CODEX_STORE_1=$S/state/codex" "$S/units/box-ci-codex-keepalive.service" && grep -qx "Environment=KNOWN_CI_CODEX_STORE_2=$S/state/codex-2" "$S/units/box-ci-codex-keepalive.service"
expect $? "the keepalive covers every qae instance's store"
[ "$(jq -c .kinds.qae.requires_files "$S/etc/box-ci/listener.json")" = "[\"$S/state/codex/auth.json\",\"$S/state/codex-2/auth.json\"]" ] && [ "$(jq .budget.qae_concurrency "$S/etc/box-ci/listener.json")" = 2 ]
expect $? "the listener waits for each qae instance's own login: the n-th required file is store n's auth.json"
[ "$(grep -c "OPERATOR ACTION" <<< "$out")" = 2 ] && grep -qF "sudo -u box-ci env CODEX_HOME=/var/lib/box-ci/codex HOME=$S/state/home PATH=/usr/bin:/bin codex login --device-auth" <<< "$out" \
  && grep -qF "sudo -u box-ci env CODEX_HOME=/var/lib/box-ci/codex-2 HOME=$S/state/home PATH=/usr/bin:/bin codex login --device-auth" <<< "$out"
expect $? "with neither store logged in, the gates print one login command per store, each with the machine's path"
login_json > "$S/state/codex/auth.json"
out="$(run_qae2 --apply 2>&1)"
[ "$(grep -c "OPERATOR ACTION" <<< "$out")" = 1 ] && grep -qF "Log qae instance 2's Codex store /var/lib/box-ci/codex-2 in once" <<< "$out" \
  && grep -qF "sudo -u box-ci env CODEX_HOME=/var/lib/box-ci/codex-2 HOME=$S/state/home PATH=/usr/bin:/bin codex login --device-auth" <<< "$out"
expect $? "with the first store logged in, the one gate left is the second store's login"
mkdir -p "$GHDIR"; printf 'github.com:\n    oauth_token: %s\n' "$APP_SESSION_MARKER" > "$GHDIR/hosts.yml"
out="$(run_qae2 --check 2>&1)"; rc=$?
[ "$rc" -ne 0 ] && grep -qE "^codex_login=$S/state/codex/auth.json present last_refresh=.* age_days=0\.0$" <<< "$out" \
  && grep -qxF "codex_login=absent: sudo -u box-ci env CODEX_HOME=/var/lib/box-ci/codex-2 HOME=$S/state/home PATH=/usr/bin:/bin codex login --device-auth" <<< "$out"
expect $? "--check reports each store's login, and fails with the login command while the second store has none"
login_json > "$S/state/codex-2/auth.json"
out="$(run_qae2 --check 2>&1)"; rc=$?
[ "$rc" -eq 0 ] && grep -qE "^codex_login=$S/state/codex/auth.json present last_refresh=.* age_days=0\.0$" <<< "$out" && grep -qE "^codex_login=$S/state/codex-2/auth.json present last_refresh=.* age_days=0\.0$" <<< "$out" && ! grep -qF "$LOGIN_MARKER" <<< "$out"
expect $? "--check passes once every qae instance's store holds a login, printing none of them"

# ---- refusals ---------------------------------------------------------------------------------------
setup
out="$(run_org --check --apply 2>&1)"; rc=$?
[ "$rc" -ne 0 ] && grep -q "do not combine it" <<< "$out"; expect $? "--check refuses --apply"
out="$(run_lane nope --apply 2>&1)"; rc=$?
[ "$rc" -ne 0 ] && grep -q "no lane named 'nope'" <<< "$out" && ! grep -qE "$MUTATIONS" "$CALLLOG"; expect $? "a lane the host config does not declare is refused before any mutation"
out="$(env PATH="$S/pathbin:$PATH" RUNNERS_ALLOW_NON_ROOT=1 bash "$S/repo/bin/provision-lane.sh" box 2>&1)"; rc=$?
[ "$rc" -eq 2 ] && grep -q "usage:" <<< "$out"; expect $? "a missing lane argument prints the usage and exits 2"
setup
printf 'docker0\n' > "$S/st/network-box-ci"
out="$(run_org --apply 2>&1)"; rc=$?
[ "$rc" -ne 0 ] && grep -q "exists on bridge 'docker0', not box-ci0" <<< "$out"; expect $? "a lane network on another bridge name is refused (the firewall names box-ci0)"

# ---- the user lane ----------------------------------------------------------------------------------
setup
smoke_out "22 3101 54329" > "$S/fx/smoke.out"
mkdir -p "$S/etc/own-ci"; printf '%s\n' "$KEY_MARKER" > "$S/etc/own-ci/app.pem"; /bin/chmod 0640 "$S/etc/own-ci/app.pem"
rm -rf "$S/run/ci" "$S/run/2"
out="$(run_user --apply 2>&1)"; rc=$?
[ "$rc" -eq 0 ]; expect $? "--apply provisions a user lane"
grep -qx "useradd --system --user-group --home-dir $S/state/home --create-home --shell /usr/sbin/nologin own-ci" "$CALLLOG" && grep -qx "chgrp own-ci $S/state" "$CALLLOG" && [ "$(mode_of "$S/state")" = 710 ]; expect $? "the user lane gets its own system user and a 0710 state directory too"
[ "$(mode_of "$S/etc/own-ci/app.pem")" = 600 ] && grep -qx "chown root:root $S/etc/own-ci/app.pem" "$CALLLOG"; expect $? "a piped key is made root:root 0600"
qu="$S/units/own-ci-qae@.service"; cu="$S/units/own-ci-ci@.service"
grep -qxF "ExecStart=/usr/bin/flock -F -w 60 $S/run/qae/%i.codex.lock /usr/bin/docker run --rm --runtime=sysbox-runc --network own-ci --cgroup-parent own-ci.slice --memory 4g --pids-limit 2048 --tmpfs /tmp:size=2g,exec --oom-score-adj 1000 --dns 1.1.1.1 --dns 8.8.8.8 --cpuset-cpus \${KNOWN_CI_CPUSET_%i} -v $S/state/slot-qae-%i:/home/runner -v $STORE/slot-qae-%i:/var/lib/docker -v $S/run/qae/%i/jit:/run/jit:ro -v \${KNOWN_CI_CODEX_STORE_%i}:/home/runner/.codex-qae --name own-ci-qae-%i own-ci-runner:\${KNOWN_CI_IMAGE_TAG}" "$qu"; expect $? "the user lane's qae template mounts each instance's own Codex store at /home/runner/.codex-qae, on its own slot and store paths"
grep -qx "Environment=KNOWN_CI_CODEX_OWNER=own-ci" "$qu" && grep -qx "Environment=KNOWN_CI_SCOPE=user" "$cu" && grep -qx "Environment=KNOWN_CI_GH_HOSTS=$GHDIR/hosts.yml" "$cu" && ! grep -q "codex" "$cu"; expect $? "the ci template never sees the store, and reads the lane user's own token"
grep -qx "RuntimeMaxSec=3600" "$cu" && grep -qx "RuntimeMaxSec=3600" "$qu"; expect $? "a user lane keeps no warm pool: both templates' RuntimeMaxSec is runtime_max_sec alone"
grep -qx "MemoryMax=8G" "$S/units/own-ci.slice" && grep -qx "MemoryHigh=7G" "$S/units/own-ci.slice" && grep -qx "CPUWeight=30" "$S/units/own.slice"; expect $? "the user lane's slice throttles at MemoryHigh below its MemoryMax, and its parent carries its weight"
grep -qx "ExecStart=/usr/bin/python3 $ROOT/bin/lane-token-refresh.py --app-id 234567 --installation-id 8765432 --key-file $S/etc/own-ci/app.pem --hosts-out $GHDIR/hosts.yml --owner own-ci --gh-user solo-bot" "$S/units/own-ci-token-refresh.service"
expect $? "the user lane's token service runs the refresher with the lane's App, key, token file, user and login"
grep -qxF "ExecStart=/usr/bin/flock -o /run/own-ci-runner-build.lock $ROOT/bin/build-runner-image.sh box own-ci --refresh --if-provisioned" "$S/units/own-ci-image-build.service" && grep -qx "systemctl enable --now own-ci-image-build.timer" "$CALLLOG"
expect $? "the user lane's image-build timer runs its own weekly build"
[ "$(cat "$S/etc/own-ci/listener.json")" = "$(expected_config own-ci)" ] && [ "$(jq -r .scope "$S/etc/own-ci/listener.json")" = user ] && [ "$(jq -c .kinds.qae.labels "$S/etc/own-ci/listener.json")" = '["own-qae"]' ]
expect $? "listener.json is the user lane's rendering: its own App ids, both kinds with one label each, the store's auth.json as the qae requirement"
grep -qx "systemctl start own-ci-listener.service" "$CALLLOG" && grep -qx "ExecStart=$ROOT/bin/listener-health.sh own-ci" "$S/units/own-ci-listener-health.service" && grep -qx "systemctl enable --now own-ci-listener-health.timer" "$CALLLOG"
expect $? "the user lane's listener starts and gets the same health timer"
grep -qx "Environment=KNOWN_CI_CODEX_OWNER=own-ci" "$S/units/own-ci-codex-keepalive.service" && grep -qx "Environment=KNOWN_CI_CODEX_STORE_1=$S/state/codex" "$S/units/own-ci-codex-keepalive.service" \
  && grep -qx "systemctl enable --now own-ci-codex-keepalive.timer" "$CALLLOG"
expect $? "the user lane gets its own Codex keepalive timer, on its own store and user"
grep -qx "gh api -X DELETE repos/solo/site/actions/runners/601 --silent \[GH_CONFIG_DIR=$GHDIR GH_TOKEN=unset as=own-ci\]" "$CALLLOG" && [ "$(grep -c '^gh api -X DELETE' "$CALLLOG" | tr -d ' ')" = 1 ]; expect $? "the sweep deletes a user lane's offline leftover at its repository, as the lane user"
{ ! grep -q "other/thing" "$CALLLOG"; }; expect $? "the sweep reads only the owner's repositories"
[ "$(grep -c '^gh ' "$CALLLOG")" -gt 0 ] && [ "$(grep -c '^gh ' "$CALLLOG")" = "$(grep -cx 'runuser -u own-ci' "$CALLLOG")" ] && grep -q "^gh api installation/repositories .*\[GH_CONFIG_DIR=$GHDIR GH_TOKEN=unset as=own-ci\]$" "$CALLLOG"
expect $? "a user lane's every gh call runs as the lane user, on its own session, never an ambient token"
{ ! grep -q "runner group\|Self-hosted runners" <<< "$out"; }; expect $? "a user lane has no runner-group gate"
mkdir -p "$GHDIR"; printf 'github.com:\n    oauth_token: %s\n' "$APP_SESSION_MARKER" > "$GHDIR/hosts.yml"
login_json > "$S/state/codex/auth.json"
out="$(SLICE_MEM=8589934592 SLICE_HIGH=7516192768 SLICE_WEIGHT=30 TOP_WEIGHT=30 run_user --check 2>&1)"; rc=$?
[ "$rc" -eq 0 ] && grep -q "codex_login=$S/state/codex/auth.json present" <<< "$out"; expect $? "--check passes on a converged user lane"
grep -qF -- "--entrypoint /bin/bash own-ci-runner:tag1 -s -- 172.20.0.1 203.0.113.7 100.64.0.9 22 3101 54329" "$CALLLOG"; expect $? "the user lane's smoke probes its own check_ports"
{ ! grep -q "warm_pool\|runner_group" <<< "$out"; } && grep -q "registrations online=0 busy=1 offline=1 offline_leftovers=own-lane-qae-1-1727004000" <<< "$out"; expect $? "a user lane has no warm pool and no runner group, and its registrations are read on every repository of the owner"
: > "$CALLLOG"
out="$(run_user --apply 2>&1)"; rc=$?
[ "$rc" -eq 0 ] && ! grep -q "wrote $S/units/own-ci-image-build" <<< "$out" && grep -qx "systemctl enable --now own-ci-image-build.timer" "$CALLLOG"; expect $? "a repeat --apply rewrites neither image-build unit and keeps the timer enabled"
sed -i 's/^OnCalendar=weekly$/OnCalendar=daily/' "$S/units/own-ci-image-build.timer"; : > "$CALLLOG"
out="$(run_user --apply 2>&1)"; rc=$?
[ "$rc" -eq 0 ] && grep -qx "OnCalendar=weekly" "$S/units/own-ci-image-build.timer" && grep -q "wrote $S/units/own-ci-image-build.timer" <<< "$out" && before '^systemctl daemon-reload' '^systemctl enable --now own-ci-image-build.timer'
expect $? "an image-build timer changed on the machine is rewritten, then systemd reloaded before the timer is enabled"
: > "$CALLLOG"
out="$(run_user --remove --apply 2>&1)"; rc=$?
[ "$rc" -eq 0 ] && grep -qx "systemctl disable --now own-ci-token-refresh.timer" "$CALLLOG" && grep -qx "systemctl stop own-ci-qae@1.service" "$CALLLOG" && grep -qx "docker rm -f own-ci-qae-2" "$CALLLOG"; expect $? "--remove stops the token timer and every slot unit of both kinds"
grep -qx "systemctl disable --now own-ci-image-build.timer" "$CALLLOG" && [ ! -e "$S/units/own-ci-image-build.timer" ] && [ ! -e "$S/units/own-ci-image-build.service" ] && before '^systemctl disable --now own-ci-image-build.timer' '^systemctl daemon-reload'
expect $? "--remove disables the image-build timer and deletes both its units before the daemon reload"
[ ! -e "$S/units/own-ci-token-refresh.timer" ] && [ ! -e "$S/units/own-ci-codex-keepalive.timer" ] && grep -qx "systemctl disable --now own-ci-codex-keepalive.timer" "$CALLLOG" && [ ! -e "$S/units/own-ci-qae@.service" ] && [ ! -e "$S/etc/own-ci/codex-config.toml" ] && [ -f "$S/state/codex/auth.json" ] && [ -f "$S/etc/own-ci/app.pem" ]
expect $? "--remove deletes the timers, the templates and the tracked config, and keeps the key, the lane user and its login store"

# ---- two lanes on one host -----------------------------------------------------------------------
# Each lane under its own state, run, etc and lib directories; one unit directory and one Sysbox
# filesystem between them, as on a machine.
setup
mkdir -p "$S/etc/own-ci"; printf '%s\n' "$KEY_MARKER" > "$S/etc/own-ci/app.pem"
for lane in box-ci own-ci; do
  mkdir -p "$S/two/$lane/state/store/golden-tag1"; printf 'KNOWN_CI_IMAGE_TAG=tag1\n' > "$S/two/$lane/state/image.env"
  echo "$S/two/$lane/state/store" >> "$S/st/mounts"
done
: > "$CALLLOG"
STATE_T="$S/two/box-ci/state" RUN_UNDER_TEST="$S/two/box-ci/run" run_org --apply > "$S/two/box-ci.out" 2>&1; rc_org=$?
STATE_T="$S/two/own-ci/state" RUN_UNDER_TEST="$S/two/own-ci/run" run_user --apply > "$S/two/own-ci.out" 2>&1; rc_user=$?
[ "$rc_org" -eq 0 ] && [ "$rc_user" -eq 0 ]; expect $? "two lanes apply on one host"
for f in box-ci-ci@.service box-ci-qae@.service own-ci-ci@.service own-ci-qae@.service box-ci.slice box.slice own-ci.slice own.slice \
  box-ci-listener.service own-ci-listener.service box-ci-token-refresh.timer own-ci-token-refresh.timer box-ci-image-build.timer own-ci-image-build.timer \
  box-ci-listener-health.timer own-ci-listener-health.timer box-ci-codex-keepalive.timer own-ci-codex-keepalive.timer box-ci-store-reaper.timer own-ci-store-reaper.timer; do
  [ -f "$S/units/$f" ] || { echo "  missing: $f"; false; } || break
done
expect $? "each lane writes its own templates, slices, listener and timers"
grep -qx "Environment=KNOWN_CI_STORE_DIR=$S/two/box-ci/state/store" "$S/units/box-ci-store-reaper.service" && grep -qx "Environment=KNOWN_CI_STORE_DIR=$S/two/own-ci/state/store" "$S/units/own-ci-store-reaper.service" \
  && [ -d "$S/two/box-ci/state/store/trash" ] && [ -d "$S/two/own-ci/state/store/trash" ]
expect $? "each lane's reaper deletes from the trash of its own store, never another lane's"
box_store="$(printf '%s' "${S#/}/two/box-ci/state/store" | sed 's/-/\\x2d/g; s#/#-#g').mount"; own_store="$(printf '%s' "${S#/}/two/own-ci/state/store" | sed 's/-/\\x2d/g; s#/#-#g').mount"
[ -f "$S/units/$box_store" ] && [ -f "$S/units/$own_store" ] && [ "$box_store" != "$own_store" ]; expect $? "each lane mounts its own store filesystem through its own unit"
grep -qx "Environment=KNOWN_CI_STATE_DIR=$S/two/box-ci/state" "$S/units/box-ci-ci@.service" && grep -qx "Environment=KNOWN_CI_STATE_DIR=$S/two/own-ci/state" "$S/units/own-ci-ci@.service" \
  && grep -qx "Environment=KNOWN_CI_GH_HOSTS=$S/two/box-ci/state/home/.config/gh/hosts.yml" "$S/units/box-ci-ci@.service"
expect $? "each lane's slots use its own state directory and its own user's token"
grep -qx "docker network create --driver bridge --opt com.docker.network.bridge.name=box-ci0 --opt com.docker.network.bridge.enable_icc=false box-ci" "$CALLLOG" \
  && grep -qx "docker network create --driver bridge --opt com.docker.network.bridge.name=own-ci0 --opt com.docker.network.bridge.enable_icc=false own-ci" "$CALLLOG" \
  && grep -qx "harden bridge=box-ci0" "$CALLLOG" && grep -qx "harden bridge=own-ci0" "$CALLLOG"
expect $? "each lane gets its own network and bridge, and asserts its own bridge's firewall rules"
[ ! -e "$S/units/var-lib-sysbox.mount" ] && [ ! -e "$S/units/sysbox-mgr.service.d" ] && ! grep -qE "var-lib-sysbox|sysbox-mgr|$S/sysbox" "$CALLLOG"; expect $? "neither lane writes, enables or remounts var-lib-sysbox.mount: the Sysbox filesystem is the host's"
[ "$(sed -n 's/^ExecStart=//p' "$S/units/box-ci-listener.service")" != "$(sed -n 's/^ExecStart=//p' "$S/units/own-ci-listener.service")" ] && [ -f "$S/etc/box-ci/listener.json" ] && [ -f "$S/etc/own-ci/listener.json" ]
expect $? "each lane runs its own listener on its own config"

# ---- adoption: a lane provisioned from an earlier checkout converges in place ----------------------
# The machine already runs the lane: its user, key, token, Codex login, image and store, listener
# binary, and units an earlier checkout's provisioner wrote (other comments, that checkout's helper
# path): the state of a machine moving to this kit from one at /opt/runners.
setup
rm -rf "$S/run/ci" "$S/run/2"
mkdir -p "$S/etc/own-ci" "$GHDIR" "$S/state/codex" "$S/lib/own-ci"
printf '%s\n' "$KEY_MARKER" > "$S/etc/own-ci/app.pem"; /bin/chmod 0600 "$S/etc/own-ci/app.pem"
printf 'github.com:\n    oauth_token: %s\n' "$APP_SESSION_MARKER" > "$GHDIR/hosts.yml"
login_json > "$S/state/codex/auth.json"
printf 'own-ci\n' > "$S/st/users"; printf 'old store image\n' > "$S/state/store.img"
run_user --apply >/dev/null 2>&1
for f in "$S"/units/own-ci*; do
  sed -i "s#^\# Managed by the runner lanes kit.*#\# Managed by an earlier provisioner; do not edit on the box.#; s#$ROOT/bin/lane-slot.sh#/opt/runners/bin/lane-slot.sh#" "$f"
done
ls -R "$STORE" > "$S/store-before"; cp "$S/state/image.env" "$S/image.env-before"; cp "$S/state/codex/auth.json" "$S/auth-before"; ls "$S/units" > "$S/units-before"
: > "$CALLLOG"
out="$(run_user --apply 2>&1)"; rc=$?
[ "$rc" -eq 0 ]; expect $? "adoption: --apply converges a lane an earlier provisioner made"
cmp -s "$S/state/image.env" "$S/image.env-before" && grep -qx "KNOWN_CI_IMAGE_TAG=tag1" "$S/state/image.env"; expect $? "adoption keeps the image tag in image.env"
[ "$(ls -R "$STORE")" = "$(cat "$S/store-before")" ] && [ "$(cat "$S/state/store.img")" = "old store image" ] && ! grep -qE '^(truncate|mkfs)' "$CALLLOG"; expect $? "adoption writes no new store: the store image and the preloaded store stay as they were"
{ ! grep -qE '^(useradd|build\.sh|docker build|docker image rm)' "$CALLLOG"; } && ! grep -q "build-runner-image" "$CALLLOG"; expect $? "adoption creates no user, rebuilds no listener and builds no image"
cmp -s "$S/state/codex/auth.json" "$S/auth-before" && ! grep -q "install -d -m 0700 $S/state/codex" <<< "$out"; expect $? "adoption keeps the Codex login and makes no new store for it"
grep -qx "ExecStartPre=+$HELPER prepare ci %i" "$S/units/own-ci-ci@.service" && grep -qx "systemctl daemon-reload" "$CALLLOG" && grep -qx "systemctl restart own-ci-listener.service" "$CALLLOG"
expect $? "adoption rewrites the units to this checkout's helpers, reloads systemd and restarts the listener once"
[ "$(ls "$S/units")" = "$(cat "$S/units-before")" ]; expect $? "adoption keeps every unit name: nothing added, nothing renamed"

# ---- lane-keyed paths: every lane path derives from the lane's name ------------------------------
# Without the path overrides, dry-run and --remove (a dry-run too) print every lane path they would
# touch while writing nothing, so they show the derived paths on any machine.
run_derived() {  # <host yml> <host> <lane> args
  local yml="$1" host="$2" lane="$3"
  shift 3
  env PATH="$S/pathbin:$PATH" GH_TOKEN="ambient-token-must-not-be-used" RUNNERS_ALLOW_NON_ROOT=1 \
    RUNNERS_HOST_YML="$yml" RUNNERS_UNIT_DIR="$S/units" RUNNERS_SYSBOX_DIR="$S/sysbox" RUNNERS_FIREWALL="$S/harden.sh" \
    ${KEY_UNDER_TEST:+RUNNERS_LANE_KEY_FILE="$KEY_UNDER_TEST"} bash "$S/repo/bin/provision-lane.sh" "$host" "$lane" "$@"
}
setup
# The one override: the piped key, so the dry run reaches the listener phase.
out="$(KEY_UNDER_TEST="$S/etc/box-ci/app.pem" run_derived "$S/host.yml" box box-ci 2>&1)"; rc=$?
[ "$rc" -eq 0 ] && grep -q "DRY-RUN: install -d -m 0710 /var/lib/box-ci$" <<< "$out" && grep -q "DRY-RUN: truncate -s 40G /var/lib/box-ci/store.img" <<< "$out" \
  && grep -qF 'DRY-RUN: write '"$S"'/units/var-lib-box\x2dci-store.mount' <<< "$out" && grep -q "DRY-RUN: write /etc/box-ci/listener.json" <<< "$out" && grep -q "install it at /usr/local/lib/box-ci/listener" <<< "$out"
expect $? "a lane keeps its state, store mount, config and binary under its own name"
grep -qx 'CHECKOUT="${RUNNERS_ENGINE:-/opt/runner-lanes}/lanes"' "$ROOT/bin/provision-lane.sh" && grep -qx 'HELPER="$CHECKOUT/bin/lane-slot.sh"' "$ROOT/bin/provision-lane.sh" \
  && grep -q 'STATE_DIR="${RUNNERS_LANE_STATE_DIR:-/var/lib/$NAME}"' "$ROOT/bin/provision-lane.sh" && grep -q 'RUN_DIR="${RUNNERS_LANE_RUN_DIR:-/run/$NAME}"' "$ROOT/bin/provision-lane.sh" \
  && grep -q 'ETC_DIR="${RUNNERS_LANE_ETC_DIR:-/etc/$NAME}"' "$ROOT/bin/provision-lane.sh" && grep -q 'LIB_DIR="${RUNNERS_LANE_LIB_DIR:-/usr/local/lib/$NAME}"' "$ROOT/bin/provision-lane.sh"
expect $? "on a machine the units run the kit in the engine checkout, /opt/runner-lanes/lanes, and the state, run, etc and lib paths are /var/lib, /run, /etc and /usr/local/lib under the lane's name"
grep -qx 'HOST_YML="${RUNNERS_HOST_YML:-${RUNNERS_CONFIG:-/opt/runner-lanes-config}/hosts/$HOST.yml}"' "$ROOT/bin/provision-lane.sh"; expect $? "and the host file is the values checkout's, /opt/runner-lanes-config/hosts/<host>.yml"
mkdir -p "$S/config/hosts"; cp "$S/host.yml" "$S/config/hosts/box.yml"
out="$(env PATH="$S/pathbin:$PATH" RUNNERS_ALLOW_NON_ROOT=1 RUNNERS_CONFIG="$S/config" RUNNERS_UNIT_DIR="$S/units" RUNNERS_SYSBOX_DIR="$S/sysbox" RUNNERS_FIREWALL="$S/harden.sh" bash "$S/repo/bin/provision-lane.sh" box box-ci 2>&1)"; rc=$?
[ "$rc" -eq 0 ] && grep -q "DRY-RUN: install -d -m 0710 /var/lib/box-ci$" <<< "$out"; expect $? "with no file named outright, a lane is read from hosts/<host>.yml of the values checkout"
out="$(env PATH="$S/pathbin:$PATH" RUNNERS_ALLOW_NON_ROOT=1 RUNNERS_CONFIG="$S/config" bash "$S/repo/bin/provision-lane.sh" nowhere box-ci 2>&1)"; rc=$?
[ "$rc" -ne 0 ] && grep -q "no host config at $S/config/hosts/nowhere.yml" <<< "$out"; expect $? "and a host that checkout has no file for is refused, naming the path"
out="$(run_derived "$S/host.yml" box box-ci --remove 2>&1)"; rc=$?
[ "$rc" -eq 0 ] && grep -q "DRY-RUN: rm -rf /var/lib/box-ci/slot-ci-1 /var/lib/box-ci/slot-ci-1.img /var/lib/box-ci/store/slot-ci-1" <<< "$out" \
  && grep -q "DRY-RUN: rm -rf /var/lib/box-ci/slot-qae-2 /var/lib/box-ci/slot-qae-2.img /var/lib/box-ci/store/slot-qae-2" <<< "$out" \
  && grep -q "DRY-RUN: rm -rf /var/lib/box-ci/slot-1 /var/lib/box-ci/slot-1.img /var/lib/box-ci/store/slot-1" <<< "$out" && grep -q "DRY-RUN: rm -rf /run/box-ci$" <<< "$out" \
  && grep -q "DRY-RUN: rm -f /run/box-ci-listener-health /run/box-ci-listener-health.restarted$" <<< "$out" && grep -q "DRY-RUN: systemctl disable --now box-ci@1.service" <<< "$out"
expect $? "its slot images, store snapshots, run dir and health verdict live under its name, and --remove covers the retiring <name>@<n> instances too"
grep -q "DRY-RUN: rm -rf /var/lib/box-ci/store/trash$" <<< "$out" && grep -qx 'TRASH_DIR="$STORE_DIR/trash"' "$ROOT/bin/provision-lane.sh" && grep -qx 'REAPER_SERVICE="$NAME-store-reaper.service"' "$ROOT/bin/provision-lane.sh"
expect $? "the store's trash is /var/lib/<lane>/store/trash and its reaper <lane>-store-reaper, derived from the lane's name like every other lane path"
grep -qx 'HEALTH_DIR="${KNOWN_CI_LISTENER_HEALTH_DIR:-/run}"' "$ROOT/bin/listener-health.sh" && grep -qx 'STATE="$HEALTH_DIR/$1-listener-health"' "$ROOT/bin/listener-health.sh"; expect $? "the health verdict path is the one the health check writes"
# The example host file's gates, with the paths a machine has: the exact key pipe and login commands.
for lane in greenbauer-ci acme-ci; do
  out="$(run_derived "$ROOT/examples/hosts/example.yml" example "$lane" 2>&1)"; rc=$?
  [ "$rc" -eq 0 ] && grep -qF "ssh root@worker-1 'umask 077; cat > /etc/$lane/app.pem' < key.pem" <<< "$out" \
    && grep -qF "sudo -u $lane env CODEX_HOME=/var/lib/$lane/codex HOME=/var/lib/$lane/home PATH=/usr/bin:/bin codex login --device-auth" <<< "$out"
  expect $? "the example's $lane prints the exact key pipe and login commands"
  grep -q "DRY-RUN: useradd --system --user-group --home-dir /var/lib/$lane/home --create-home --shell /usr/sbin/nologin $lane" <<< "$out" \
    && grep -q "DRY-RUN: write $S/units/$lane-ci@.service" <<< "$out" && grep -q "DRY-RUN: write $S/units/$lane-qae@.service" <<< "$out" && grep -q "DRY-RUN: install -d -m 0710 /var/lib/$lane$" <<< "$out"
  expect $? "the example's $lane plans its user, its ci and qae templates and a 0710 state directory"
done
# The fixture's orbit-ci is an org lane without QAE: no qae template, no Codex store, no login gate.
out="$(run_derived "$ROOT/tests/fixtures/hosts/vps-1.yml" vps-1 orbit-ci 2>&1)"; rc=$?
[ "$rc" -eq 0 ] && grep -qF "ssh root@vps-1 'umask 077; cat > /etc/orbit-ci/app.pem' < key.pem" <<< "$out" && ! grep -q "codex" <<< "$out"
expect $? "vps-1's orbit-ci prints the exact key pipe command and nothing of a Codex store"
grep -q "DRY-RUN: useradd --system --user-group --home-dir /var/lib/orbit-ci/home --create-home --shell /usr/sbin/nologin orbit-ci" <<< "$out" \
  && grep -q "DRY-RUN: write $S/units/orbit-ci-ci@.service" <<< "$out" && ! grep -q "orbit-ci-qae@" <<< "$out" && grep -q "DRY-RUN: install -d -m 0710 /var/lib/orbit-ci$" <<< "$out" \
  && grep -q "DRY-RUN: write $S/units/orbit-ci.slice" <<< "$out" && grep -q "DRY-RUN: write $S/units/orbit.slice" <<< "$out"
expect $? "vps-1's orbit-ci plans its user, its one ci template, its slice and parent, and a 0710 state directory"
# The same lane rendered by --apply under the hermetic paths: what its units will carry on the machine.
setup
out="$(HOST_YML_UNDER_TEST="$ROOT/tests/fixtures/hosts/vps-1.yml" run_lane orbit-ci --apply 2>&1)"; rc=$?
ku="$S/units/orbit-ci-ci@.service"
[ "$rc" -eq 0 ] && grep -qx "MemoryMax=20G" "$S/units/orbit-ci.slice" && grep -qx "MemoryHigh=18G" "$S/units/orbit-ci.slice" && grep -qx "CPUQuota=1200%" "$S/units/orbit-ci.slice" \
  && grep -qx "CPUWeight=50" "$S/units/orbit-ci.slice" && grep -qx "CPUWeight=50" "$S/units/orbit.slice"
expect $? "vps-1's orbit-ci slice carries MemoryMax 20G, MemoryHigh 18G and CPUQuota 1200%, and its parent orbit.slice the weight"
grep -qx "Environment=KNOWN_CI_CPUSET_8=12-15" "$ku" && ! grep -q "KNOWN_CI_CPUSET_9=" "$ku" && grep -qx "RuntimeMaxSec=4800" "$ku" && grep -qx "TimeoutStopSec=300" "$ku" \
  && [ ! -e "$S/units/orbit-ci-qae@.service" ] && ! compgen -G "$S/units/orbit-ci-codex-keepalive.*" >/dev/null
expect $? "vps-1's orbit-ci renders 8 slots on a 16-core host, the eighth on the fourth 4-core set and no ninth, RuntimeMaxSec 4800, the 300 s cleanup, and no qae template or Codex keepalive"
# The wait kind: jobs that only poll for another job's result, on their own template,
# budget and container memory, in the lane's slice and network.
wu="$S/units/orbit-ci-wait@.service"
grep -qF -- "--memory 1g --pids-limit 2048 --tmpfs /tmp:size=2g,exec" "$wu" && grep -qF -- "--network orbit-ci --cgroup-parent orbit-ci.slice" "$wu" \
  && grep -qF -- "-v $S/state/slot-wait-%i:/home/runner -v $S/state/store/slot-wait-%i:/var/lib/docker -v $S/run/wait/%i/jit:/run/jit:ro --name orbit-ci-wait-%i " "$wu" \
  && grep -qF -- "--memory 4g --pids-limit 2048" "$ku" && ! grep -qF -- "--memory 1g" "$ku"
expect $? "vps-1's orbit-ci renders orbit-ci-wait@.service: a ci slot's container in the same slice and network at the wait kind's 1g, on its own slot, store and run paths, and the ci template keeps 4g"
grep -qx "ExecStartPre=+$ROOT/bin/lane-slot.sh prepare wait %i" "$wu" && grep -qx "ExecStopPost=+$ROOT/bin/lane-slot.sh cleanup wait %i" "$wu" \
  && grep -qx "Slice=orbit-ci.slice" "$wu" && grep -qx "Restart=no" "$wu" && grep -qx "RuntimeMaxSec=4800" "$wu" && grep -qx "TimeoutStopSec=300" "$wu"
expect $? "the wait template is prepared and cleaned up as the wait kind, one job per start, in the lane's slice, with the lane's runtime and cleanup budgets"
# A wait slot run from that template: the helper makes the empty store at the path the template
# mounts on the container's /var/lib/docker, and no copy of the preloaded store.
listener_files wait 1 7101; : > "$CALLLOG"
out="$(as_unit "$wu" 1 prepare wait 1 2>&1)"; rc=$?
wait_store="$(sed -n 's/^ExecStart=.* -v \([^ ]*\):\/var\/lib\/docker .*/\1/p' "$wu" | sed 's/%i/1/')"
[ "$rc" -eq 0 ] && [ "$wait_store" = "$S/state/store/slot-wait-1" ] && [ -d "$wait_store" ] && [ -z "$(ls -A "$wait_store")" ] && [ "$(mode_of "$wait_store")" = 700 ] && ! grep -q '^cp --reflink' "$CALLLOG"
expect $? "a wait slot started from its template gets an empty 0700 store at the path the template mounts on /var/lib/docker, and no copy of the preloaded store is made"
out="$(as_unit "$wu" 1 cleanup wait 1 2>&1)"; rc=$?
[ "$rc" -eq 0 ] && [ ! -e "$wait_store" ] && [ ! -e "$S/run/wait/1/job" ] && grep -qx "systemctl start --no-block orbit-ci-store-reaper.service" "$CALLLOG" && [ -f "$S/units/orbit-ci-store-reaper.service" ]
expect $? "and its cleanup retires that store, starts the lane's reaper and frees the instance"
# The listener's config is written once the lane's App key is in place (the operator pipes it in).
install -d -m 0700 "$S/etc/orbit-ci"; printf 'fake-key-marker\n' > "$S/etc/orbit-ci/app.pem"; chmod 0600 "$S/etc/orbit-ci/app.pem"
HOST_YML_UNDER_TEST="$ROOT/tests/fixtures/hosts/vps-1.yml" run_lane orbit-ci --apply >/dev/null 2>&1
ci_slots="$(jq .budget.slots "$S/etc/orbit-ci/listener.json")"
grep -qx "Environment=KNOWN_CI_CPUSET_8=12-15" "$wu" && ! grep -q "KNOWN_CI_CPUSET_9=" "$wu" && grep -qx "Environment=KNOWN_CI_CPUSET_${ci_slots}=12-15" "$ku" && ! grep -q "KNOWN_CI_CPUSET_$((ci_slots + 1))=" "$ku"
expect $? "the wait template has a cpuset for each of its own instances and the ci template one for each of the lane's slots"
[ "$(jq -c .kinds.wait "$S/etc/orbit-ci/listener.json")" = '{"set_name":"orbit-ci-wait","labels":["self-hosted","orbit-ci-wait"],"slots":8,"container_memory_bytes":1073741824}' ] && [ "$ci_slots" = 8 ] \
  && [ "$(jq -S '.app.key_file = "/etc/orbit-ci/app.pem"' "$S/etc/orbit-ci/listener.json")" = "$(jq -S . "$ROOT/tests/fixtures/listener/orbit-ci.json")" ]
expect $? "the listener is configured to serve [self-hosted, orbit-ci-wait] with 8 slots of 1 GiB of its own, apart from the ci budget, and the whole config is the fixture's (the key path aside, which the test relocates)"
# A wait slot is a lane registration the sweep may delete: <prefix>-wait-<slot>-<epoch>.
grep -qF 'test(\"^((ci|qae|wait)-)?[0-9]+-[0-9]+' "$ROOT/bin/provision-lane.sh"; expect $? "the registration sweep recognises the wait kind's runner names"
: > "$CALLLOG"
out="$(HOST_YML_UNDER_TEST="$ROOT/tests/fixtures/hosts/vps-1.yml" run_lane orbit-ci --check 2>&1)"
grep -qx "template=orbit-ci-wait@.service current running=none" <<< "$out" && grep -q "lane_worst_case=200G (store 40G + 16 slots x 10G" <<< "$out" && grep -q "kinds=\[ci wait\] slots=8 wait_slots=8" <<< "$out"
expect $? "--check reads the wait template against its rendering, and counts the wait slots in the disk worst case and its report line"
printf 'tampered\n' >> "$wu"
out="$(HOST_YML_UNDER_TEST="$ROOT/tests/fixtures/hosts/vps-1.yml" run_lane orbit-ci --check 2>&1)"
grep -q "template=orbit-ci-wait@.service DRIFTED from this checkout" <<< "$out" && grep -q "template=orbit-ci-ci@.service current" <<< "$out"
expect $? "--check calls an edited wait template DRIFTED while the ci one stays current"
out="$(HOST_YML_UNDER_TEST="$ROOT/tests/fixtures/hosts/vps-1.yml" run_lane orbit-ci --remove 2>&1)"; rc=$?
[ "$rc" -eq 0 ] && grep -q "DRY-RUN: systemctl stop orbit-ci-wait@8.service" <<< "$out" && ! grep -q "orbit-ci-wait@9.service" <<< "$out" && grep -qF "$wu" <<< "$out"
expect $? "its --remove stops all 8 wait instances and removes the wait template"

echo
echo "provision-lane-test: $pass passed, $fail failed"
[ "$fail" -eq 0 ]
