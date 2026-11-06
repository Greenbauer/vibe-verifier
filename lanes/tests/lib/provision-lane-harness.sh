# shellcheck shell=bash
# provision-lane-harness.sh: the hermetic setup bin/provision-lane.sh's tests share: the stubs on
# PATH (stubs.sh), a made-up host file, a copy of the kit under $S/repo, the fixtures the stubs read,
# and run_lane, which runs the provisioner on them. Sourced by provision-lane-test.sh and
# provision-lane-plan-test.sh after they set ROOT (the kit's root, lanes/); never run.

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

converged_org() {
  setup
  run_org --apply >/dev/null 2>&1
  mkdir -p "$GHDIR"; printf 'github.com:\n    oauth_token: %s\n' "$APP_SESSION_MARKER" > "$GHDIR/hosts.yml"
  login_json > "$S/state/codex/auth.json"
  : > "$CALLLOG"
}

# Sets store_fs for box-ci in the host file under test.
host_store_fs() {
  awk -v fs="$1" '{ print } /^    store_disk_gb: 40$/ { print "    store_fs: " fs }' "$S/host.yml" > "$S/host.yml.new" && mv "$S/host.yml.new" "$S/host.yml"
}
# A stand-in, run as the scripts run it.
stub() { env PATH="$S/pathbin:$PATH" "$@"; }
is_subvolume() { [ -d "$1" ] && [ ! -L "$1" ] && grep -qx -- "$(ls -di "$1" | awk '{ print $1 }')" "$S/st/subvolumes" 2>/dev/null; }
# An org lane provisioned from nothing, so that its store is an image the stand-ins mounted (an
# unmount takes its files with it): then tag1's preloaded store, as the image build leaves one on
# that filesystem, and a second apply, which starts the listener. $1: the host file's store_fs.
mounted_org() {
  setup
  rm -rf "$STORE/golden-tag1"; printf '%s\n' "$S/sysbox" > "$S/st/mounts"
  [ -z "${1:-}" ] || host_store_fs "$1"
  run_org --apply >/dev/null 2>&1
  if [ "${1:-}" = btrfs ]; then stub btrfs subvolume create "$STORE/golden-tag1" >/dev/null; else mkdir "$STORE/golden-tag1"; fi
  mkdir -p "$STORE/golden-tag1/overlay2/layer1/diff/usr/bin"; printf 'bin\n' > "$STORE/golden-tag1/overlay2/layer1/diff/usr/bin/postgres"
  run_org --apply >/dev/null 2>&1
  mkdir -p "$GHDIR"; printf 'github.com:\n    oauth_token: %s\n' "$APP_SESSION_MARKER" > "$GHDIR/hosts.yml"
  login_json > "$S/state/codex/auth.json"
  : > "$CALLLOG"
}

# The plan. The store holds the current tag's preloaded store, another whose image is kept, one no
# image names, an unfinished one, a job's copy and a retired copy.
conversion_fixture() {
  mounted_org
  mkdir -p "$STORE/golden-tag9/overlay2" "$STORE/golden-tag0/overlay2" "$STORE/golden-tag7.new" "$STORE/slot-qae-2/overlay2" "$STORE/trash/1700000100.slot-ci-1.7"
  printf 'nine\n' > "$STORE/golden-tag9/overlay2/file"; /bin/chmod 0710 "$STORE/golden-tag1"
  host_store_fs btrfs
  export IMAGES_ABSENT="box-ci-runner:tag0"
}
