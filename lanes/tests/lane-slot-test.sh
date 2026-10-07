#!/bin/bash
# lane-slot-test.sh: hermetic tests for bin/lane-slot.sh, run as the listener's units run it
# (prepare|cleanup <kind> <n>) and as the smoke and the image build run it (snapshot|discard).
# The system tools are stubs on PATH (tests/lib/stubs.sh); the firewall is a stand-in.
set -u
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
# shellcheck source=lib/stubs.sh
. "$ROOT/tests/lib/stubs.sh"
HELPER="$ROOT/bin/lane-slot.sh"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
pass=0; fail=0
expect() { if [ "$1" -eq 0 ]; then pass=$((pass+1)); echo "✓ $2"; else fail=$((fail+1)); echo "✗ $2"; fi; }
mode_of() { python3 -c 'import os, sys; print(format(os.stat(sys.argv[1]).st_mode & 0o777, "o"))' "$1" 2>/dev/null; }

# Obviously fake markers, not credential-shaped: the assertions only need to find them verbatim.
APP_SESSION_MARKER="fixture-app-session-aaaa"
JIT_MARKER="fixture-jit-config-bbbb"

setup() {
  S="$TMP/s"
  rm -rf "$S"
  mkdir -p "$S/state" "$S/run" "$S/st/state" "$S/fx" "$S/ghhome"
  STORE="$S/state/store"
  mkdir -p "$STORE/golden-tag1/overlay2/layer1/diff/usr/bin" "$STORE/golden-tag1/overlay2/l" "$STORE/golden-tag1/image/overlay2"
  printf 'bin\n' > "$STORE/golden-tag1/overlay2/layer1/diff/usr/bin/postgres"
  printf 'meta\n' > "$STORE/golden-tag1/image/overlay2/repositories.json"
  printf '%s\n' "$STORE" > "$S/st/mounts"
  printf 'github.com:\n    users:\n        acme-bot:\n            oauth_token: %s\n    user: acme-bot\n    git_protocol: https\n    oauth_token: %s\n' "$APP_SESSION_MARKER" "$APP_SESSION_MARKER" > "$S/ghhome/hosts.yml"
  printf '# tracked\ncli_auth_credentials_store = "file"\n' > "$S/codex-config.toml"
  cat > "$S/harden.sh" <<'SH'
#!/bin/bash
echo "harden${*:+ $*} bridge=${KNOWN_CI_BRIDGE:-unset}" >> "$CALLLOG"
exit "${HARDEN_RC:-0}"
SH
  chmod +x "$S/harden.sh"
  CALLLOG="$S/calls.log"; : > "$CALLLOG"
  export CALLLOG ST="$S/st" FX="$S/fx" UNITS="$S/units" EXPECTED_TOKEN="$APP_SESSION_MARKER"
  write_stubs "$S/pathbin"
}

# What the listener writes before it starts a unit (listener/README.md, "The run-dir contract").
listener_files() {  # kind n target runner-id
  mkdir -p "$S/run/$1/$2"
  printf '%s\n' "$JIT_MARKER" > "$S/run/$1/$2/jit"; /bin/chmod 0400 "$S/run/$1/$2/jit"
  printf '%s %s 9 %s box-lane-%s-%s-1727003000\n' "$3" "$1" "$4" "$1" "$2" > "$S/run/$1/$2/job"
}

run_slot() {
  env PATH="$S/pathbin:$PATH" \
    KNOWN_CI_NAME=box-ci KNOWN_CI_SCOPE="${HELPER_SCOPE:-org}" KNOWN_CI_SLOT_GB=10 KNOWN_CI_BRIDGE=box-ci0 \
    KNOWN_CI_GH_HOSTS="$S/ghhome/hosts.yml" KNOWN_CI_STATE_DIR="$S/state" KNOWN_CI_RUN_DIR="$S/run" \
    KNOWN_CI_STORE_DIR="$STORE" KNOWN_CI_IMAGE_TAG="${HELPER_TAG:-tag1}" \
    KNOWN_CI_API=https://api.example.invalid KNOWN_CI_HARDEN="$S/harden.sh" \
    KNOWN_CI_CODEX_STORE_1="$S/codex" KNOWN_CI_CODEX_CONFIG="$S/codex-config.toml" KNOWN_CI_CODEX_OWNER=box-lane-user \
    bash "$HELPER" "$@"
}
line_of() { grep -nE -- "$1" "$CALLLOG" | head -n 1 | cut -d: -f1; }
before() { local a b; a="$(line_of "$1")"; b="$(line_of "$2")"; [ -n "$a" ] && [ -n "$b" ] && [ "$a" -lt "$b" ]; }

# ---- prepare <kind> <n> -----------------------------------------------------------------------------
setup
out="$(run_slot prepare ci 1 2>&1)"; rc=$?
[ "$rc" -ne 0 ] && grep -q "no job here: $S/run/ci/1/jit and $S/run/ci/1/job are absent. The lane's listener starts its slots, one per job; a unit started by hand has nothing to run" <<< "$out" \
  && ! grep -qE '^(harden|truncate|mount|curl|cp)' "$CALLLOG"; expect $? "prepare refuses, touching nothing, when the listener wrote no job for the instance"
setup
listener_files ci 1 acme 4242
out="$(run_slot prepare ci 1 2>&1)"; rc=$?
[ "$rc" -eq 0 ]; expect $? "prepare exits 0 on the listener's files"
before '^harden bridge=box-ci0' '^docker rm -f box-ci-ci-1' && before '^docker rm -f box-ci-ci-1' '^truncate -s 10G' \
  && before '^truncate -s 10G' '^mkfs.ext4 -q -F -m 0' && before '^mkfs.ext4' '^mount -o loop,nodev,nosuid' && before '^mount -o' '^cp --reflink=always'
expect $? "prepare asserts the lane's own bridge's firewall rules, clears the old container, re-creates and mounts the slot, then snapshots the store"
grep -qx "truncate -s 10G $S/state/slot-ci-1.img" "$CALLLOG" && grep -qx "mount -o loop,nodev,nosuid $S/state/slot-ci-1.img $S/state/slot-ci-1" "$CALLLOG"; expect $? "the instance's slot is its kind's: a fresh sparse ext4 image at slot-ci-1, mounted nodev,nosuid"
grep -qx "cp --reflink=always -a $STORE/golden-tag1/overlay2/./layer1 $STORE/slot-ci-1/overlay2/./layer1" "$CALLLOG" && grep -qx "cp --reflink=always -a ./image $STORE/slot-ci-1/" "$CALLLOG" \
  && cmp -s "$STORE/golden-tag1/overlay2/layer1/diff/usr/bin/postgres" "$STORE/slot-ci-1/overlay2/layer1/diff/usr/bin/postgres" && [ -f "$STORE/slot-ci-1/image/overlay2/repositories.json" ] && [ -d "$STORE/slot-ci-1/overlay2/l" ] \
  && [ "$(mode_of "$STORE/slot-ci-1")" = 700 ]
expect $? "the slot's store is a reflink copy of the image tag's preloaded store at slot-ci-1 (layer directories one by one, the rest whole), mode 0700"
{ ! grep -q '^curl' "$CALLLOG"; }; expect $? "prepare mints no runner: the listener did"
[ "$(cat "$S/run/ci/1/jit")" = "$JIT_MARKER" ] && [ "$(mode_of "$S/run/ci/1/jit")" = 400 ] && [ -f "$S/run/ci/1/job" ]; expect $? "prepare leaves the listener's jit and job files as they were"
grep -q "runner box-lane-ci-1-1727003000 (id 4242) of acme set 9" <<< "$out" && ! grep -qF "$JIT_MARKER" <<< "$out"; expect $? "prepare logs the job file's runner, never the JIT config"
setup
listener_files ci 1 acme 4242
out="$(env -u KNOWN_CI_BRIDGE PATH="$S/pathbin:$PATH" KNOWN_CI_NAME=box-ci KNOWN_CI_SCOPE=org KNOWN_CI_GH_HOSTS="$S/ghhome/hosts.yml" KNOWN_CI_HARDEN="$S/harden.sh" bash "$HELPER" prepare ci 1 2>&1)"; rc=$?
[ "$rc" -ne 0 ] && ! grep -q '^harden' "$CALLLOG"; expect $? "prepare refuses a unit environment without the lane's bridge, before any firewall call"

# The QAE kind: the Codex store is reset to the login and the tracked config, and handed to the job.
setup
listener_files qae 1 acme/widgets 4343
mkdir -p "$S/codex/skills/x" "$S/codex/sessions"
printf '{"auth_mode": "chatgpt"}\n' > "$S/codex/auth.json"
printf 'model = "left-by-a-job"\n' > "$S/codex/config.toml"
printf 'obey me\n' > "$S/codex/AGENTS.md"
out="$(HELPER_SCOPE=user run_slot prepare qae 1 2>&1)"; rc=$?
[ "$rc" -eq 0 ] && [ "$(ls -A "$S/codex" | sort | tr '\n' ' ')" = "auth.json config.toml " ]; expect $? "prepare prunes the Codex store to auth.json and the tracked config.toml"
cmp -s "$S/codex/config.toml" "$S/codex-config.toml" && [ "$(mode_of "$S/codex/config.toml")" = 600 ] && [ "$(mode_of "$S/codex")" = 700 ] && [ "$(cat "$S/codex/auth.json")" = '{"auth_mode": "chatgpt"}' ]; expect $? "config.toml is the tracked one, 0600, the store 0700, and the login untouched"
cfg_cp="$(grep -n '^  cp "\$KNOWN_CI_CODEX_CONFIG" "\$store/config.toml"$' "$HELPER" | cut -d: -f1)"; to_job="$(grep -n '^  chown -hR "\$2" "\$store"$' "$HELPER" | cut -d: -f1)"
grep -qx "chown -hR 1001:1001 $S/codex" "$CALLLOG" && [ -n "$cfg_cp" ] && [ -n "$to_job" ] && [ "$cfg_cp" -lt "$to_job" ]; expect $? "the store (config.toml included) is handed to the job's uid 1001 after config.toml is written, never following a link"
grep -qx "truncate -s 10G $S/state/slot-qae-1.img" "$CALLLOG" && [ -d "$STORE/slot-qae-1" ] && [ ! -e "$STORE/slot-ci-1" ]; expect $? "a qae instance has its own slot image and store snapshot, never a ci instance's"
setup
listener_files qae 1 acme 4343
mkdir -p "$S/codex"; printf '{"auth_mode": "chatgpt"}\n' > "$S/codex/auth.json"
out="$(run_slot prepare qae 1 2>&1)"; rc=$?
[ "$rc" -eq 0 ] && grep -qx "chown -hR 1001:1001 $S/codex" "$CALLLOG"; expect $? "an org lane's qae slot resets and hands over its Codex store the same way"
setup
listener_files qae 1 acme/widgets 4343
mkdir -p "$S/codex"; rm -f "$S/codex-config.toml"
out="$(HELPER_SCOPE=user run_slot prepare qae 1 2>&1)"; rc=$?
[ "$rc" -ne 0 ] && grep -q "no tracked Codex config at $S/codex-config.toml" <<< "$out" && ! grep -q '^chown' "$CALLLOG"; expect $? "prepare fails, without handing the store over, when the tracked config is missing"
# One Codex login store per qae instance (KNOWN_CI_CODEX_STORE_<n>): an instance resets and hands over
# its own store and never another's, whose login a concurrent job may be using.
setup
listener_files qae 2 acme/widgets 4344
mkdir -p "$S/codex/sessions" "$S/codex-2/sessions"
printf '{"auth_mode": "chatgpt", "login": 1}\n' > "$S/codex/auth.json"; printf 'a running job\n' > "$S/codex/sessions/rollout-1.jsonl"
printf '{"auth_mode": "chatgpt", "login": 2}\n' > "$S/codex-2/auth.json"
out="$(KNOWN_CI_CODEX_STORE_2="$S/codex-2" HELPER_SCOPE=user run_slot prepare qae 2 2>&1)"; rc=$?
[ "$rc" -eq 0 ] && grep -qx "chown -hR 1001:1001 $S/codex-2" "$CALLLOG" && ! grep -q "$S/codex\( \|$\)" "$CALLLOG" \
  && [ "$(ls -A "$S/codex-2" | sort | tr '\n' ' ')" = "auth.json config.toml " ] && [ "$(cat "$S/codex/sessions/rollout-1.jsonl")" = 'a running job' ]
expect $? "qae instance 2 resets and hands over its own store, codex-2, and leaves instance 1's store as it is"
setup
listener_files qae 2 acme/widgets 4344
mkdir -p "$S/codex"; printf '{"auth_mode": "chatgpt"}\n' > "$S/codex/auth.json"
out="$(HELPER_SCOPE=user run_slot prepare qae 2 2>&1)"; rc=$?
[ "$rc" -ne 0 ] && grep -q "qae instance 2 has no Codex store of its own (KNOWN_CI_CODEX_STORE_2 is unset" <<< "$out" \
  && ! grep -qE '^(harden|truncate|mount|cp|chown)' "$CALLLOG"
expect $? "an instance without a store of its own (beyond qae_concurrency) is refused before anything is touched, and never borrows instance 1's"
out="$(HELPER_SCOPE=user SERVICE_RESULT=exit-code run_slot cleanup qae 2 2>&1)"; rc=$?
[ "$rc" -eq 0 ] && [ ! -e "$S/run/qae/2/job" ] && grep -q '^curl' "$CALLLOG" && ! grep -q '^chown' "$CALLLOG"
expect $? "its cleanup still deregisters the runner and frees the instance, and hands no store anywhere"
setup
listener_files qae 1 acme/widgets 4343
out="$(HELPER_SCOPE=user run_slot prepare qae 1 2>&1)"; rc=$?
[ "$rc" -ne 0 ] && grep -q "no Codex store directory at $S/codex" <<< "$out" && ! grep -q '^chown' "$CALLLOG"; expect $? "prepare refuses a missing store, handing nothing over"
setup
listener_files qae 1 acme/widgets 4343
mkdir -p "$S/elsewhere"; printf '{"auth_mode": "chatgpt"}\n' > "$S/elsewhere/auth.json"; ln -s "$S/elsewhere" "$S/codex"
out="$(HELPER_SCOPE=user run_slot prepare qae 1 2>&1)"; rc=$?
[ "$rc" -ne 0 ] && grep -q "no Codex store directory at $S/codex" <<< "$out" && ! grep -q '^chown' "$CALLLOG" && [ -f "$S/elsewhere/auth.json" ]
expect $? "prepare refuses a store that is a link, so no root chown or prune follows it elsewhere"

# The wait kind (an org lane's jobs that only wait for another job's result): a slot
# prepared exactly like a ci one, on its own slot image and store snapshot, and with no Codex store.
setup
listener_files wait 3 acme 4545
out="$(env PATH="$S/pathbin:$PATH" KNOWN_CI_NAME=box-ci KNOWN_CI_SCOPE=org KNOWN_CI_SLOT_GB=10 KNOWN_CI_BRIDGE=box-ci0 KNOWN_CI_GH_HOSTS="$S/ghhome/hosts.yml" \
  KNOWN_CI_STATE_DIR="$S/state" KNOWN_CI_RUN_DIR="$S/run" KNOWN_CI_STORE_DIR="$STORE" KNOWN_CI_IMAGE_TAG=tag1 KNOWN_CI_API=https://api.example.invalid KNOWN_CI_HARDEN="$S/harden.sh" \
  bash "$HELPER" prepare wait 3 2>&1)"; rc=$?
[ "$rc" -eq 0 ] && grep -qx "truncate -s 10G $S/state/slot-wait-3.img" "$CALLLOG" && grep -qx "mount -o loop,nodev,nosuid $S/state/slot-wait-3.img $S/state/slot-wait-3" "$CALLLOG" \
  && [ -d "$STORE/slot-wait-3" ] && [ ! -e "$STORE/slot-ci-3" ] && ! grep -q '^chown' "$CALLLOG" && grep -q "runner box-lane-wait-3-1727003000 (id 4545) of acme set 9" <<< "$out"
expect $? "a wait instance is prepared like a ci one on its own slot image and store snapshot, with no Codex store and none of the QAE variables"
out="$(SERVICE_RESULT=success run_slot cleanup wait 3 2>&1)"; rc=$?
[ "$rc" -eq 0 ] && grep -qx "docker rm -f box-ci-wait-3" "$CALLLOG" && grep -q "curl .*-X DELETE https://api.example.invalid/orgs/acme/actions/runners/4545 token=match" "$CALLLOG" \
  && [ ! -e "$S/run/wait/3/job" ] && [ ! -e "$STORE/slot-wait-3" ] && [ ! -e "$S/state/slot-wait-3.img" ]
expect $? "and cleaned up like one: container removed, runner deregistered, snapshot, slot image and job file gone"

# ---- cleanup <kind> <n> -----------------------------------------------------------------------------
setup
listener_files ci 1 acme 4242
run_slot prepare ci 1 >/dev/null 2>&1; : > "$CALLLOG"
out="$(SERVICE_RESULT=success run_slot cleanup ci 1 2>&1)"; rc=$?
[ "$rc" -eq 0 ] && grep -qx "docker rm -f box-ci-ci-1" "$CALLLOG" && grep -q "curl .*-X DELETE https://api.example.invalid/orgs/acme/actions/runners/4242 token=match" "$CALLLOG"; expect $? "cleanup removes the container and deregisters the job file's runner at the organisation's endpoint"
grep -qx "umount $S/state/slot-ci-1" "$CALLLOG" && [ ! -e "$S/state/slot-ci-1.img" ] && [ ! -e "$STORE/slot-ci-1" ] && [ ! -e "$S/run/ci/1/jit" ] && [ ! -e "$S/run/ci/1/job" ] && [ ! -e "$S/run/ci/1" ]; expect $? "cleanup unmounts and deletes the slot image, the store snapshot, and the jit and job files"
[ ! -e "$S/run/ci/1.failures" ]; expect $? "a successful run leaves no failure count"
{ ! grep -qF "$APP_SESSION_MARKER" "$CALLLOG" && ! grep -qF "$APP_SESSION_MARKER" <<< "$out" && ! grep -qF "$JIT_MARKER" "$CALLLOG"; }; expect $? "the installation token reaches curl on stdin, never an argv or a log line"
setup
listener_files ci 2 acme/widgets 4444
out="$(HELPER_SCOPE=user SERVICE_RESULT=success run_slot cleanup ci 2 2>&1)"; rc=$?
[ "$rc" -eq 0 ] && grep -q "curl .*-X DELETE https://api.example.invalid/repos/acme/widgets/actions/runners/4444 token=match" "$CALLLOG"; expect $? "a user lane's cleanup deregisters at the repository's endpoint"
setup
listener_files ci 1 acme/widgets 4242
out="$(SERVICE_RESULT=success run_slot cleanup ci 1 2>&1)"; rc=$?
[ "$rc" -eq 0 ] && ! grep -q '^curl' "$CALLLOG" && grep -q "the job file names no org target ('acme/widgets'); runner 4242 was not deregistered" <<< "$out" && [ ! -e "$S/run/ci/1/job" ]; expect $? "a job file whose target is not the scope's is never sent to an endpoint, and the instance is still freed"
# The listener's idle stop: it removed the runner itself.
setup
listener_files ci 1 acme 4242
: > "$S/run/ci/1/idle-stop"; printf '3\n' > "$S/run/ci/1.failures"
out="$(SERVICE_RESULT=exit-code run_slot cleanup ci 1 2>&1)"; rc=$?
[ "$rc" -eq 0 ] && ! grep -q '^curl' "$CALLLOG" && grep -q "idle-stopped: the listener removed runner box-lane-ci-1-1727003000 itself; nothing to deregister" <<< "$out"; expect $? "after an idle stop cleanup sends no DELETE"
[ ! -e "$S/run/ci/1.failures" ] && [ ! -e "$S/run/ci/1/idle-stop" ] && [ ! -e "$S/run/ci/1/job" ]; expect $? "an idle stop counts no failure (like the idle timeout), and its marker and the job file go"
# Failure accounting.
setup
listener_files ci 1 acme 4242
SERVICE_RESULT=exit-code run_slot cleanup ci 1 >/dev/null 2>&1
listener_files ci 1 acme 4242
SERVICE_RESULT=exit-code run_slot cleanup ci 1 >/dev/null 2>&1
[ "$(cat "$S/run/ci/1.failures")" = 2 ]; expect $? "consecutive failed runs of an instance are counted beside its run dir"
printf '3\n' > "$S/run/ci/1.failures"; listener_files ci 1 acme 4242; : > "$CALLLOG"
out="$(run_slot prepare ci 1 2>&1)"
grep -qx "sleep 40" "$CALLLOG" && grep -q "backing off 40s after 3 consecutive failed run(s)" <<< "$out"; expect $? "the third consecutive failure backs off 40s before the next job"
printf '12\n' > "$S/run/ci/1.failures"; listener_files ci 1 acme 4242; : > "$CALLLOG"
run_slot prepare ci 1 >/dev/null 2>&1
grep -qx "sleep 600" "$CALLLOG"; expect $? "the back-off is capped at ten minutes"
printf '4\n' > "$S/run/ci/1.failures"; listener_files ci 1 acme 4242
DELETE_STATUS=204 SERVICE_RESULT=timeout run_slot cleanup ci 1 >/dev/null 2>&1
[ ! -e "$S/run/ci/1.failures" ]; expect $? "an idle slot reaching RuntimeMaxSec (timeout, runner still registered) is not a failure"
listener_files ci 1 acme 4242
DELETE_STATUS=404 SERVICE_RESULT=timeout run_slot cleanup ci 1 >/dev/null 2>&1
[ "$(cat "$S/run/ci/1.failures")" = 1 ]; expect $? "a job killed at RuntimeMaxSec counts as a failure"
printf '2\n' > "$S/run/ci/1.failures"; rm -rf "$S/run/ci/1"; : > "$CALLLOG"
out="$(SERVICE_RESULT=exit-code run_slot cleanup ci 1 2>&1)"; rc=$?
[ "$rc" -eq 0 ] && ! grep -q '^curl' "$CALLLOG" && [ "$(cat "$S/run/ci/1.failures")" = 2 ]; expect $? "a unit with no job file (started by hand, refused) deregisters nothing and leaves the count alone"
setup
listener_files qae 1 acme/widgets 4343
mkdir -p "$S/codex/sessions/2026/10/05" "$S/codex/shell_snapshots"
printf '{"auth_mode": "chatgpt"}\n' > "$S/codex/auth.json"
printf 'fake rollout with FAKE-LOGIN-MARKER\n' > "$S/codex/sessions/2026/10/05/rollout-x.jsonl"
for db in logs_2.sqlite memories_1.sqlite state_5.sqlite; do printf 'FAKE-LOGIN-MARKER' > "$S/codex/$db"; done
printf 'model = "left-by-a-job"\n' > "$S/codex/config.toml"
HELPER_SCOPE=user SERVICE_RESULT=success run_slot cleanup qae 1 >/dev/null 2>&1
grep -qx "chown -hR box-lane-user: $S/codex" "$CALLLOG"; expect $? "a qae cleanup gives the store back to the lane user, whose login command must work between jobs"
[ "$(ls -A "$S/codex")" = auth.json ] && [ "$(cat "$S/codex/auth.json")" = '{"auth_mode": "chatgpt"}' ]; expect $? "a qae cleanup prunes the store to its login at once: no rollout or Codex database waits for the next job"
# The job file goes last: its absence is what frees the instance for the listener.
job_rm="$(grep -n '^  rm -f "\$JOB"$' "$HELPER" | cut -d: -f1)"
others_rm="$(grep -n '^  rm -f "\$JIT" "\$IDLE_STOP"$' "$HELPER" | cut -d: -f1)"
accounted="$(grep -n '^  if \[ "\$had_job" = 1 \]; then account_run' "$HELPER" | cut -d: -f1)"
fn_end="$(awk -v s="$job_rm" 'NR > s && /^}$/ { print NR; exit }' "$HELPER")"
[ -n "$job_rm" ] && [ -n "$others_rm" ] && [ -n "$accounted" ] && [ "$others_rm" -lt "$job_rm" ] && [ "$accounted" -lt "$job_rm" ] \
  && [ -z "$(sed -n "$((job_rm + 1)),$((fn_end - 1))p" "$HELPER" | grep -v '^  rmdir "\$SLOT_RUN"')" ]
expect $? "cleanup removes the job file after the jit, the marker and the accounting, and does nothing after it but drop the empty directory"
out="$(run_slot prepare build 1 2>&1)"; rc=$?
[ "$rc" -eq 2 ] && grep -q "kind must be ci, qae or wait, got 'build'" <<< "$out"; expect $? "a kind other than ci, qae or wait is refused"
out="$(run_slot prepare ci x 2>&1)"; rc=$?
[ "$rc" -eq 2 ]; expect $? "a non-numeric instance is refused"
out="$(run_slot cleanup ci 1 extra 2>&1)"; rc=$?
[ "$rc" -eq 2 ]; expect $? "an extra argument is refused"
out="$(run_slot prepare 1 2>&1)"; rc=$?
[ "$rc" -eq 2 ] && grep -q "usage:" <<< "$out"; expect $? "prepare without a kind is refused (only the listener's slots run this helper)"

# ---- the Codex store lock -------------------------------------------------------------------------
# chown, recording which store locks under <run>/qae are held while it runs (PROBE_RUN set).
probe_chown() {
  cat > "$S/pathbin/chown" <<'SH'
#!/bin/bash
echo "chown $*" >> "$CALLLOG"
held=""
for f in "${PROBE_RUN:-/nonexistent}"/qae/*.codex.lock; do
  [ -e "$f" ] && ! flock -n "$f" true && held+="$(basename "$f") "
done
[ -z "$held" ] || echo "locks held at chown $*: ${held% }" >> "$CALLLOG"
exit 0
SH
  chmod +x "$S/pathbin/chown"
}
setup; probe_chown
listener_files qae 1 acme/widgets 4343
mkdir -p "$S/codex"; printf '{"auth_mode": "chatgpt"}\n' > "$S/codex/auth.json"
out="$(PROBE_RUN="$S/run" HELPER_SCOPE=user run_slot prepare qae 1 2>&1)"; rc=$?
[ "$rc" -eq 0 ] && grep -qx "locks held at chown -hR 1001:1001 $S/codex: 1.codex.lock" "$CALLLOG" && flock -n "$S/run/qae/1.codex.lock" true
expect $? "prepare hands the store to the job holding instance 1's store lock, <run>/qae/1.codex.lock, and releases it after"
out="$(PROBE_RUN="$S/run" HELPER_SCOPE=user SERVICE_RESULT=success run_slot cleanup qae 1 2>&1)"; rc=$?
[ "$rc" -eq 0 ] && grep -qx "locks held at chown -hR box-lane-user: $S/codex: 1.codex.lock" "$CALLLOG" && [ -f "$S/run/qae/1.codex.lock" ] && [ ! -e "$S/run/qae/1" ]
expect $? "cleanup gives the store back holding the same lock, and the lock file, outside the store and the instance's run dir, stays"
setup
listener_files qae 1 acme/widgets 4343
mkdir -p "$S/codex" "$S/run/qae"; printf '{"auth_mode": "chatgpt"}\n' > "$S/codex/auth.json"
exec 9>"$S/run/qae/1.codex.lock"; flock 9
out="$(KNOWN_CI_CODEX_LOCK_WAIT=1 HELPER_SCOPE=user run_slot prepare qae 1 9>&- 2>&1)"; rc=$?
[ "$rc" -ne 0 ] && grep -q "the lock of Codex store $S/codex stayed held for 1s (the lane's keepalive refreshing it?); refusing to start a job on it" <<< "$out" && ! grep -q '^chown' "$CALLLOG"
expect $? "prepare waits a bounded time for a held store lock, then refuses the job and hands the store to nobody"
out="$(KNOWN_CI_CODEX_LOCK_WAIT=1 HELPER_SCOPE=user SERVICE_RESULT=exit-code run_slot cleanup qae 1 9>&- 2>&1)"; rc=$?
exec 9>&-
[ "$rc" -eq 0 ] && grep -q "the lock of Codex store $S/codex stayed held for 1s; the store stays as the job left it, for the next prepare to reset" <<< "$out" \
  && ! grep -q '^chown' "$CALLLOG" && [ ! -e "$S/run/qae/1/job" ]
expect $? "cleanup waits a bounded time for a held store lock, then leaves the store to the next prepare and still frees the instance"
grep -qx 'CODEX_LOCK_WAIT="${KNOWN_CI_CODEX_LOCK_WAIT:-120}"' "$HELPER" && grep -qx 'KEEPALIVE_TIMEOUT=90' "$HELPER" \
  && grep -qF 'timeout -k 10 "$KEEPALIVE_TIMEOUT" "$CODEX_BIN" exec' "$HELPER"
expect $? "a slot waits 120 s for its store's lock, longer than the keepalive can hold it (a 90 s Codex run, killed 10 s later)"

# ---- codex-keepalive --------------------------------------------------------------------------------
TOKEN_MARKER="fixture-refresh-token-dddd"
login_at() {  # store, days since its last refresh; the tokens are a fake marker no log may show
  mkdir -p "$1"
  printf '{"auth_mode": "chatgpt", "last_refresh": "%s", "tokens": {"refresh_token": "%s"}}\n' \
    "$(date -u -d "$2 days ago" +%Y-%m-%dT%H:%M:%S.123456789Z)" "$TOKEN_MARKER" > "$1/auth.json"
}
# A Codex CLI stand-in on the system PATH the keepalive gives it: records how it ran (as whom, on
# which store, with what stdin, whether the tracked config was there, which store locks were held),
# leaves session files behind, and unless CODEX_STUB_RC fails it or CODEX_STUB_REFRESH=0, rewrites
# last_refresh as Codex's refresh does.
keepalive_setup() {
  setup; probe_chown
  cat > "$S/codex-cli" <<'SH'
#!/bin/bash
cfg=absent; [ -f "$CODEX_HOME/config.toml" ] && cfg=present
held=""
for f in "$PROBE_RUN"/qae/*.codex.lock; do [ -e "$f" ] && ! flock -n "$f" true && held+="$(basename "$f") "; done
echo "codex $* CODEX_HOME=$CODEX_HOME HOME=$HOME PATH=$PATH as=${RUNUSER_AS:-root} stdin=$(readlink /proc/self/fd/0) config=$cfg held=${held% }" >> "$CALLLOG"
mkdir -p "$CODEX_HOME/sessions"; echo rollout > "$CODEX_HOME/sessions/rollout.jsonl"; echo db > "$CODEX_HOME/logs_2.sqlite"
[ "${CODEX_STUB_RC:-0}" = 0 ] || { echo "error: the stand-in failed" >&2; exit "$CODEX_STUB_RC"; }
[ "${CODEX_STUB_REFRESH:-1}" = 1 ] && sed -i "s/\"last_refresh\": \"[^\"]*\"/\"last_refresh\": \"$(date -u +%Y-%m-%dT%H:%M:%S.%NZ)\"/" "$CODEX_HOME/auth.json"
echo OK
SH
  chmod +x "$S/codex-cli"
}
run_keepalive() {
  env PATH="$S/pathbin:$PATH" KNOWN_CI_NAME=box-ci KNOWN_CI_STATE_DIR="$S/state" KNOWN_CI_RUN_DIR="$S/run" \
    KNOWN_CI_CODEX_STORE_1="$S/codex" KNOWN_CI_CODEX_CONFIG="$S/codex-config.toml" KNOWN_CI_CODEX_OWNER=box-lane-user \
    KNOWN_CI_CODEX_BIN="$S/codex-cli" PROBE_RUN="$S/run" bash "$HELPER" codex-keepalive
}
last_line() { grep -n -- "$1" "$CALLLOG" | tail -n 1 | cut -d: -f1; }
keepalive_setup
login_at "$S/codex" 1; cp "$S/codex/auth.json" "$S/auth-before"
out="$(run_keepalive 2>&1)"; rc=$?
[ "$rc" -eq 0 ] && ! grep -qE '^(codex|chown)' "$CALLLOG" && cmp -s "$S/codex/auth.json" "$S/auth-before" \
  && grep -qE "Codex store 1 \($S/codex\): last refreshed [0-9T:.-]+Z, 2[45]h ago; fresh, skipped \(Codex refreshes it at 10 days\)" <<< "$out"
expect $? "a store refreshed a day ago is left alone: no Codex run, no prune, no chown, its login untouched"
keepalive_setup
login_at "$S/codex" 11; printf 'obey me\n' > "$S/codex/AGENTS.md"; mkdir -p "$S/codex/skills/x"
before_ts="$(jq -r .last_refresh "$S/codex/auth.json")"
out="$(run_keepalive 2>&1)"; rc=$?
after_ts="$(jq -r .last_refresh "$S/codex/auth.json")"
[ "$rc" -eq 0 ]; expect $? "the keepalive exits 0 once it refreshed the stale store"
[ "$(grep -c '^codex ' "$CALLLOG")" = 1 ] \
  && grep -qE "^codex exec --skip-git-repo-check --ephemeral --sandbox read-only -C [^ ]+ Reply with the single word OK\. CODEX_HOME=$S/codex HOME=$S/state/home PATH=/usr/bin:/bin as=box-lane-user stdin=/dev/null config=present held=1\.codex\.lock$" "$CALLLOG"
expect $? "a store last refreshed 11 days ago gets one read-only codex exec as the lane user, CODEX_HOME that store, the tracked config in it, stdin closed, the system PATH, its lock held"
reset_chown="$(grep -n "^chown -hR box-lane-user: $S/codex\$" "$CALLLOG" | head -n 1 | cut -d: -f1)"; codex_at="$(last_line '^codex ')"
[ -n "$reset_chown" ] && [ "$reset_chown" -lt "$codex_at" ] && [ "$(last_line "^chown -hR box-lane-user: $S/codex\$")" -gt "$codex_at" ] \
  && [ "$(grep -c "^locks held at chown -hR box-lane-user: $S/codex: 1.codex.lock\$" "$CALLLOG")" = 2 ]
expect $? "the store is reset for the lane user before the run and given back to it after, both under the lock"
[ "$(ls -A "$S/codex")" = auth.json ]; expect $? "after the run the store is pruned to its login, as cleanup does: no session, database, AGENTS.md or skill is left"
[ "$after_ts" != "$before_ts" ] && [ "$(date -d "$after_ts" +%s)" -gt "$(date -d "$before_ts" +%s)" ] \
  && grep -qF "Codex store 1 ($S/codex): refreshed, last_refresh $before_ts -> $after_ts" <<< "$out"
expect $? "the keepalive checks that last_refresh moved, and logs the two timestamps"
{ ! grep -qF "$TOKEN_MARKER" <<< "$out" && ! grep -qF "$TOKEN_MARKER" "$CALLLOG"; } && flock -n "$S/run/qae/1.codex.lock" true
expect $? "no token reaches a log line or an argv, and the lock is free again after the run"
keepalive_setup
login_at "$S/codex" 11
out="$(CODEX_STUB_RC=3 run_keepalive 2>&1)"; rc=$?
[ "$rc" -eq 1 ] && grep -q "Codex store 1 ($S/codex): codex exec failed (exit 3), last_refresh still .*; its output ends: error: the stand-in failed" <<< "$out" \
  && grep -q "1 Codex store(s) could not be refreshed" <<< "$out" && [ "$(ls -A "$S/codex")" = auth.json ] && [ "$(last_line "^chown -hR box-lane-user: $S/codex\$")" -gt "$(last_line '^codex ')" ]
expect $? "a failed Codex run exits non-zero naming the store, and the store is still pruned and given back to the lane user"
keepalive_setup
login_at "$S/codex" 11
out="$(CODEX_STUB_REFRESH=0 run_keepalive 2>&1)"; rc=$?
[ "$rc" -eq 1 ] && grep -q "Codex store 1 ($S/codex): codex exec ran but last_refresh is still .*: Codex did not refresh the login" <<< "$out"
expect $? "a Codex run that leaves last_refresh where it was exits non-zero"
keepalive_setup
login_at "$S/codex" 11; cp "$S/codex/auth.json" "$S/auth-before"; mkdir -p "$S/run/qae"
exec 9>"$S/run/qae/1.codex.lock"; flock 9
out="$(run_keepalive 9>&- 2>&1)"; rc=$?
exec 9>&-
[ "$rc" -eq 0 ] && ! grep -qE '^(codex|chown)' "$CALLLOG" && cmp -s "$S/codex/auth.json" "$S/auth-before" \
  && grep -q "Codex store 1 ($S/codex): its lock is held (a QAE job is using it); skipped (the job's Codex refreshes the login)" <<< "$out"
expect $? "a stale store whose lock a job holds is skipped at once: no Codex run, no prune, no chown"
keepalive_setup
login_at "$S/codex" 11; listener_files qae 1 acme/widgets 4343
out="$(run_keepalive 2>&1)"; rc=$?
[ "$rc" -eq 0 ] && ! grep -qE '^(codex|chown)' "$CALLLOG" && grep -q "Codex store 1 ($S/codex): instance 1 has a job; skipped" <<< "$out"
expect $? "a stale store whose instance has a job file is skipped too (the gaps between a job's prepare, run and cleanup)"
keepalive_setup
login_at "$S/codex" 2; login_at "$S/codex-2" 12; mkdir -p "$S/codex-3"
out="$(KNOWN_CI_CODEX_STORE_2="$S/codex-2" KNOWN_CI_CODEX_STORE_3="$S/codex-3" run_keepalive 2>&1)"; rc=$?
[ "$rc" -eq 0 ] && [ "$(grep -c '^codex ' "$CALLLOG")" = 1 ] && grep -qE "^codex .* CODEX_HOME=$S/codex-2 .* held=2\.codex\.lock$" "$CALLLOG" \
  && grep -q "Codex store 1 ($S/codex): .*fresh, skipped" <<< "$out" && grep -q "Codex store 3 ($S/codex-3): no login yet; skipped" <<< "$out"
expect $? "each store is judged on its own: of a fresh, a stale and an unseeded store only the stale one runs Codex, under its own lock"
keepalive_setup
mkdir -p "$S/codex"; printf '{"auth_mode": "chatgpt"}\n' > "$S/codex/auth.json"
out="$(run_keepalive 2>&1)"; rc=$?
[ "$rc" -eq 1 ] && ! grep -q '^codex' "$CALLLOG" && grep -q "Codex store 1 ($S/codex): auth.json has no readable last_refresh" <<< "$out"
expect $? "a login without a last_refresh exits non-zero rather than pass unchecked"

# ---- the store snapshot the smoke and the image build share ---------------------------------------
setup
out="$(HELPER_TAG=tag9 run_slot snapshot smoke-1 2>&1)"; rc=$?
[ "$rc" -ne 0 ] && grep -q "no preloaded store for image tag tag9 at $STORE/golden-tag9" <<< "$out" && [ ! -e "$STORE/smoke-1" ]; expect $? "a snapshot fails when the image tag has no preloaded store"
setup
listener_files ci 1 acme 4242
out="$(CP_NOREFLINK=1 run_slot prepare ci 1 2>&1)"; rc=$?
[ "$rc" -ne 0 ] && grep -q "could not snapshot $STORE/golden-tag1 to $STORE/slot-ci-1" <<< "$out" && [ ! -e "$STORE/slot-ci-1" ]; expect $? "prepare fails, without a half-made snapshot, when the store takes no reflinks"
setup
out="$(run_slot snapshot smoke-42 2>&1)"; rc=$?
[ "$rc" -eq 0 ] && cmp -s "$STORE/golden-tag1/overlay2/layer1/diff/usr/bin/postgres" "$STORE/smoke-42/overlay2/layer1/diff/usr/bin/postgres" && ! grep -qE '^(curl|mount|mkfs|truncate)' "$CALLLOG"; expect $? "snapshot <name> makes the same reflink copy at <store>/<name> and touches nothing else"
out="$(run_slot discard smoke-42 2>&1)"; rc=$?
[ "$rc" -eq 0 ] && [ ! -e "$STORE/smoke-42" ]; expect $? "discard <name> removes it"
out="$(env -u KNOWN_CI_NAME KNOWN_CI_STORE_DIR="$STORE" KNOWN_CI_IMAGE_TAG=tag1 PATH="$S/pathbin:$PATH" bash "$HELPER" snapshot verify-7 2>&1)"; rc=$?
[ "$rc" -eq 0 ] && [ -d "$STORE/verify-7" ]; expect $? "snapshot needs only the store directory and the image tag (the image build calls it so)"
rm -rf "$STORE/verify-7"
# The helper's paths derive from the lane name too: a snapshot for lane other-ci with no path
# overrides looks for its golden store under /var/lib/other-ci/store (and stops there: it is absent).
out="$(env -u KNOWN_CI_STORE_DIR KNOWN_CI_NAME=other-ci KNOWN_CI_IMAGE_TAG=tag1 PATH="$S/pathbin:$PATH" bash "$HELPER" snapshot smoke-7 2>&1)"; rc=$?
[ "$rc" -ne 0 ] && grep -q "no preloaded store for image tag tag1 at /var/lib/other-ci/store/golden-tag1" <<< "$out"; expect $? "the helper's store defaults to /var/lib/<lane name>/store"
grep -qF 'RUN_DIR="${KNOWN_CI_RUN_DIR:-/run/${KNOWN_CI_NAME:-}}"' "$HELPER" && grep -qF 'HARDEN="${KNOWN_CI_HARDEN:-$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lane-firewall.sh}"' "$HELPER"
expect $? "the helper's run directory defaults to /run/<lane name>, and its firewall is the checkout's bin/lane-firewall.sh"
out="$(env -u KNOWN_CI_NAME -u KNOWN_CI_STORE_DIR KNOWN_CI_IMAGE_TAG=tag1 PATH="$S/pathbin:$PATH" bash "$HELPER" snapshot smoke-7 2>&1)"; rc=$?
[ "$rc" -eq 2 ] && grep -q "set KNOWN_CI_NAME or KNOWN_CI_STORE_DIR" <<< "$out"; expect $? "a snapshot with neither the lane name nor the store directory is refused"
out="$(run_slot snapshot 'bad name' 2>&1)"; rc=$?
[ "$rc" -eq 2 ] && [ ! -e "$STORE/bad name" ]; expect $? "a snapshot name that is not <word>-<digits> is refused"
setup
listener_files ci 1 acme 4242
out="$(HARDEN_RC=1 run_slot prepare ci 1 2>&1)"; rc=$?
[ "$rc" -ne 0 ] && grep -q "refusing to start a job" <<< "$out" && ! grep -q '^mount' "$CALLLOG"; expect $? "prepare fails closed without the lane firewall rules"

echo
echo "lane-slot-test: $pass passed, $fail failed"
[ "$fail" -eq 0 ]
