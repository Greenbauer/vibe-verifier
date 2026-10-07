#!/bin/bash
# lane-slot-test.sh: hermetic tests for bin/lane-slot.sh, run as the listener's units run it
# (prepare|cleanup <kind> <n>), as the lane's reaper unit runs it (reap) and as the smoke and the
# image build run it (snapshot|discard).
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
# The store's trash: how many entries it holds, and an entry as a slot names one.
trash_count() { find "$STORE/trash" -mindepth 1 -maxdepth 1 2>/dev/null | wc -l | tr -d ' '; }
TRASH_NAME='^[0-9]{10}\.slot-(ci|qae|wait)-[0-9]+\.[0-9]+$'
# rm, recording every call and whether the trash's lock (PROBE_TRASH) is held while it runs. It
# refuses the path RM_FAIL names, and deletes RM_VANISH first, behind its caller's back. With
# RM_HOLD it takes that many seconds and records when it began and ended in $ST/rm-times, from
# which most_at_once reads how many deletes ran together.
REAL_RM="$(command -v rm)"; export REAL_RM
probe_rm() {
  cat > "$S/pathbin/rm" <<'SH'
#!/bin/bash
echo "rm $*" >> "$CALLLOG"
target="${*: -1}"
[ -z "${PROBE_TRASH:-}" ] || flock -n "$PROBE_TRASH" true || echo "trash lock held at rm $target" >> "$CALLLOG"
[ -z "${RM_VANISH:-}" ] || "$REAL_RM" -rf -- "$RM_VANISH"
if [ "${RM_FAIL:-}" = "$target" ]; then echo "rm: cannot remove '$target': Operation not permitted" >&2; exit 1; fi
[ -n "${RM_HOLD:-}" ] || exec "$REAL_RM" "$@"
echo "$(date +%s%N) 1" >> "$ST/rm-times"
/bin/sleep "$RM_HOLD"
"$REAL_RM" "$@"; rc=$?
echo "$(date +%s%N) -1" >> "$ST/rm-times"
exit "$rc"
SH
  chmod +x "$S/pathbin/rm"
}
most_at_once() { sort -n "$ST/rm-times" | awk '{ n += $2; if (n > most) most = n } END { print most + 0 }'; }
# True when no rm named the store copy or anything in the trash: the copy was renamed, not deleted.
no_delete_of() { ! grep -qE "^rm .*($STORE/$1|$STORE/trash)( |/|\$)" "$CALLLOG"; }

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

# The wait kind (an org lane's jobs that only wait for another job's result): a slot prepared like
# a ci one on its own slot image, with no Codex store, and with an empty inner Docker store: a job
# that runs no inner image gets no copy of the preloaded store, which nothing would read.
setup
listener_files wait 3 acme 4545
mkdir -p "$STORE/slot-wait-3/left-by-an-earlier-job"
out="$(env PATH="$S/pathbin:$PATH" KNOWN_CI_NAME=box-ci KNOWN_CI_SCOPE=org KNOWN_CI_SLOT_GB=10 KNOWN_CI_BRIDGE=box-ci0 KNOWN_CI_GH_HOSTS="$S/ghhome/hosts.yml" \
  KNOWN_CI_STATE_DIR="$S/state" KNOWN_CI_RUN_DIR="$S/run" KNOWN_CI_STORE_DIR="$STORE" KNOWN_CI_IMAGE_TAG=tag1 KNOWN_CI_API=https://api.example.invalid KNOWN_CI_HARDEN="$S/harden.sh" \
  bash "$HELPER" prepare wait 3 2>&1)"; rc=$?
[ "$rc" -eq 0 ] && grep -qx "truncate -s 10G $S/state/slot-wait-3.img" "$CALLLOG" && grep -qx "mount -o loop,nodev,nosuid $S/state/slot-wait-3.img $S/state/slot-wait-3" "$CALLLOG" \
  && [ -d "$STORE/slot-wait-3" ] && [ ! -e "$STORE/slot-ci-3" ] && ! grep -q '^chown' "$CALLLOG" && grep -q "runner box-lane-wait-3-1727003000 (id 4545) of acme set 9" <<< "$out"
expect $? "a wait instance is prepared like a ci one on its own slot image, with no Codex store and none of the QAE variables"
[ -z "$(ls -A "$STORE/slot-wait-3")" ] && [ "$(mode_of "$STORE/slot-wait-3")" = 700 ] && ! grep -q '^cp --reflink' "$CALLLOG" && ! grep -q '^df ' "$CALLLOG" \
  && grep -q "an empty inner Docker store made (a wait job gets no copy of the preloaded one)" <<< "$out"
expect $? "a wait slot's prepare makes no copy of the preloaded store: its store is an empty directory, mode 0700, and the store's room is never read for it"
[ "$(trash_count)" = 1 ] && [[ "$(ls -A "$STORE/trash")" =~ $TRASH_NAME ]] && [ -d "$STORE/trash/$(ls -A "$STORE/trash")/left-by-an-earlier-job" ]
expect $? "a leftover at a wait slot's store path is retired into the trash, never reused as the next job's store"
out="$(SERVICE_RESULT=success run_slot cleanup wait 3 2>&1)"; rc=$?
[ "$rc" -eq 0 ] && grep -qx "docker rm -f box-ci-wait-3" "$CALLLOG" && grep -q "curl .*-X DELETE https://api.example.invalid/orgs/acme/actions/runners/4545 token=match" "$CALLLOG" \
  && [ ! -e "$S/run/wait/3/job" ] && [ ! -e "$STORE/slot-wait-3" ] && [ ! -e "$S/state/slot-wait-3.img" ]
expect $? "and cleaned up like one: container removed, runner deregistered, its store, slot image and job file gone"
env PATH="$S/pathbin:$PATH" KNOWN_CI_STORE_DIR="$STORE" bash "$HELPER" reap >/dev/null 2>&1
[ "$(trash_count)" = 0 ] && cmp -s "$STORE/golden-tag1/overlay2/layer1/diff/usr/bin/postgres" <(printf 'bin\n')
expect $? "a wait slot's cleanup leaves nothing: after the reaper's pass the trash is empty, and the preloaded store is as it was"
setup
listener_files wait 1 acme 4546
out="$(HELPER_TAG=tag9 run_slot prepare wait 1 2>&1)"; rc=$?
[ "$rc" -eq 0 ] && [ -d "$STORE/slot-wait-1" ] && [ ! -e "$STORE/golden-tag9" ]; expect $? "a wait slot needs no preloaded store: its prepare succeeds for an image tag that has none"

# ---- cleanup <kind> <n> -----------------------------------------------------------------------------
setup; probe_rm
listener_files ci 1 acme 4242
run_slot prepare ci 1 >/dev/null 2>&1; : > "$CALLLOG"
out="$(SERVICE_RESULT=success run_slot cleanup ci 1 2>&1)"; rc=$?
[ "$rc" -eq 0 ] && grep -qx "docker rm -f box-ci-ci-1" "$CALLLOG" && grep -q "curl .*-X DELETE https://api.example.invalid/orgs/acme/actions/runners/4242 token=match" "$CALLLOG"; expect $? "cleanup removes the container and deregisters the job file's runner at the organisation's endpoint"
grep -qx "umount $S/state/slot-ci-1" "$CALLLOG" && [ ! -e "$S/state/slot-ci-1.img" ] && [ ! -e "$STORE/slot-ci-1" ] && [ ! -e "$S/run/ci/1/jit" ] && [ ! -e "$S/run/ci/1/job" ] && [ ! -e "$S/run/ci/1" ]; expect $? "cleanup unmounts, deletes the slot image and the jit and job files, and leaves no store snapshot at the slot's path"
[ ! -e "$S/run/ci/1.failures" ]; expect $? "a successful run leaves no failure count"
{ ! grep -qF "$APP_SESSION_MARKER" "$CALLLOG" && ! grep -qF "$APP_SESSION_MARKER" <<< "$out" && ! grep -qF "$JIT_MARKER" "$CALLLOG"; }; expect $? "the installation token reaches curl on stdin, never an argv or a log line"
# The store copy is not deleted in the stop path: it is renamed into the trash for the reaper.
entry="$(ls -A "$STORE/trash")"
[ "$(trash_count)" = 1 ] && [[ "$entry" =~ $TRASH_NAME ]] && [[ "$entry" == *.slot-ci-1.* ]] && cmp -s "$STORE/golden-tag1/overlay2/layer1/diff/usr/bin/postgres" "$STORE/trash/$entry/overlay2/layer1/diff/usr/bin/postgres" \
  && [ -f "$STORE/trash/$entry/image/overlay2/repositories.json" ] && no_delete_of slot-ci-1
expect $? "cleanup renames the job's store copy into the trash instead of deleting it: whole, under a name that carries the second and the instance"
grep -qx "systemctl start --no-block box-ci-store-reaper.service" "$CALLLOG" && [ "$(grep -c '^systemctl' "$CALLLOG")" = 1 ] && [ ! -e "$S/run/ci/1/job" ] && [ -d "$STORE/trash/$entry" ]
expect $? "cleanup starts the lane's reaper without waiting for it, and frees the instance while the copy is still undeleted"
listener_files ci 1 acme 4243
run_slot prepare ci 1 >/dev/null 2>&1; SERVICE_RESULT=success run_slot cleanup ci 1 >/dev/null 2>&1
[ "$(trash_count)" = 2 ] && [ -d "$STORE/trash/$entry" ] && no_delete_of slot-ci-1; expect $? "the next job on that instance retires its copy under a name of its own, beside the first"
# The kit's first apply on a machine with running slots: a slot that finishes after the machine
# pulled this helper and before its lane was provisioned again has no trash directory and no reaper
# unit, and the environment its template always set, which is all run_slot passes.
setup
cat > "$S/pathbin/systemctl" <<'SH'
#!/bin/bash
echo "systemctl $*" >> "$CALLLOG"
echo "Failed to start ${*: -1}: Unit ${*: -1} not found." >&2
exit 5
SH
listener_files ci 1 acme 4242
run_slot prepare ci 1 >/dev/null 2>&1
[ ! -e "$STORE/trash" ]; expect $? "a prepare with nothing to retire makes no trash directory"
out="$(SERVICE_RESULT=success run_slot cleanup ci 1 2>&1)"; rc=$?
[ "$rc" -eq 0 ] && [ "$(mode_of "$STORE/trash")" = 700 ] && [ "$(trash_count)" = 1 ] && [ ! -e "$STORE/slot-ci-1" ] && [ ! -e "$S/run/ci/1/job" ] \
  && grep -q "could not start box-ci-store-reaper.service (bin/provision-lane.sh writes it); its timer, or the next cleanup's start, empties $STORE/trash" <<< "$out" && ! grep -q "not found" <<< "$out"
expect $? "before the kit's first apply (no trash directory, no reaper unit yet) a cleanup makes the trash, 0700, retires its copy there, says the reaper could not be started, and still frees the instance"
setup; probe_rm
listener_files ci 1 acme 4242
run_slot prepare ci 1 >/dev/null 2>&1
mkdir -p "$S/elsewhere"; ln -s "$S/elsewhere" "$STORE/trash"; : > "$CALLLOG"
out="$(SERVICE_RESULT=success run_slot cleanup ci 1 2>&1)"; rc=$?
[ "$rc" -eq 0 ] && [ ! -e "$STORE/slot-ci-1" ] && [ -z "$(ls -A "$S/elsewhere")" ] && grep -qx "rm -rf -- $STORE/slot-ci-1" "$CALLLOG" && ! grep -q '^systemctl' "$CALLLOG" \
  && grep -q "could not move $STORE/slot-ci-1 into $STORE/trash; deleting it in place" <<< "$out" && [ ! -e "$S/run/ci/1/job" ]
expect $? "a trash that is a link takes nothing: the copy is deleted in place, as before the trash existed, nothing goes through the link, and the instance is still freed"
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

# ---- the store trash: a leftover copy, and room on the store --------------------------------------
setup; probe_rm
listener_files ci 1 acme 4242
mkdir -p "$STORE/slot-ci-1/overlay2/left-by-a-killed-cleanup"
out="$(run_slot prepare ci 1 2>&1)"; rc=$?
entry="$(ls -A "$STORE/trash")"
[ "$rc" -eq 0 ] && [ "$(trash_count)" = 1 ] && [[ "$entry" =~ $TRASH_NAME ]] && [ -d "$STORE/trash/$entry/overlay2/left-by-a-killed-cleanup" ] && no_delete_of slot-ci-1 \
  && grep -qx "systemctl start --no-block box-ci-store-reaper.service" "$CALLLOG" && before '^systemctl start --no-block' '^cp --reflink=always'
expect $? "prepare retires a leftover copy into the trash rather than deleting it, and starts the reaper, before it makes the new one"
[ ! -e "$STORE/slot-ci-1/overlay2/left-by-a-killed-cleanup" ] && cmp -s "$STORE/golden-tag1/overlay2/layer1/diff/usr/bin/postgres" "$STORE/slot-ci-1/overlay2/layer1/diff/usr/bin/postgres"
expect $? "and the job's store is a fresh copy of the preloaded one, with nothing of the leftover in it"
# Fail closed on disk: retired copies waiting in the trash must not fill the store unseen. The df
# stand-in reports a store short of room while the trash holds more than STORE_DF_SHORT_ABOVE entries.
backlog() { local t; for t in "$@"; do mkdir -p "$STORE/trash/$t.slot-ci-2.7/overlay2"; done; }
setup; probe_rm
listener_files ci 1 acme 4242
backlog 1700000300 1700000500 1700000100 1700000400 1700000200
out="$(STORE_DF_SHORT="1000 50 1000 500" STORE_DF_SHORT_WHILE="$STORE/trash" STORE_DF_SHORT_ABOVE=2 run_slot prepare ci 1 2>&1)"; rc=$?
[ "$rc" -eq 0 ] && [ "$(ls -A "$STORE/trash" | tr '\n' ' ')" = "1700000400.slot-ci-2.7 1700000500.slot-ci-2.7 " ] && [ -d "$STORE/slot-ci-1/overlay2/layer1" ] \
  && [ "$(grep -c '^rm -rf --one-file-system' "$CALLLOG")" = 3 ] && before "^rm -rf --one-file-system -- $STORE/trash/1700000100\." '^cp --reflink=always' \
  && before "^rm -rf --one-file-system -- $STORE/trash/1700000200\." '^cp --reflink=always' && before "^rm -rf --one-file-system -- $STORE/trash/1700000300\." '^cp --reflink=always' \
  && grep -q "the store filesystem at $STORE has under 10% of its space or inodes free and $STORE/trash holds retired copies; deleting from it here, before this job's copy" <<< "$out"
expect $? "a store short of space with a backlog in the trash: prepare deletes the oldest retired copies itself, three at a time, starts no more once there is room, then makes its copy"
setup; probe_rm
listener_files ci 1 acme 4242
backlog 1700000100
out="$(STORE_DF_SHORT="1000 500 1000 99" STORE_DF_SHORT_WHILE="$STORE/trash" run_slot prepare ci 1 2>&1)"; rc=$?
[ "$rc" -eq 0 ] && [ "$(trash_count)" = 0 ] && [ -d "$STORE/slot-ci-1/overlay2/layer1" ] && grep -q "deleting from it here" <<< "$out"; expect $? "a store short of inodes is short of room too"
setup; probe_rm
listener_files ci 1 acme 4242
backlog 1700000100
out="$(STORE_DF_RC=1 run_slot prepare ci 1 2>&1)"; rc=$?
[ "$rc" -eq 0 ] && [ "$(trash_count)" = 0 ] && [ -d "$STORE/slot-ci-1/overlay2/layer1" ]; expect $? "a reading of the store that cannot be made counts as short: the trash is emptied before the copy"
setup; probe_rm
listener_files ci 1 acme 4242
out="$(STORE_DF_SHORT="1000 50 1000 500" run_slot prepare ci 1 2>&1)"; rc=$?
[ "$rc" -eq 0 ] && [ -d "$STORE/slot-ci-1/overlay2/layer1" ] && ! grep -q "deleting from it here\|refusing" <<< "$out" && ! grep -q "^rm -rf --one-file-system" "$CALLLOG"
expect $? "a short store with an empty trash is not the backlog's doing: nothing is deleted and the copy is tried as before"
setup; probe_rm
listener_files ci 1 acme 4242
backlog 1700000100
out="$(RM_FAIL="$STORE/trash/1700000100.slot-ci-2.7" STORE_DF_SHORT="1000 50 1000 500" STORE_DF_SHORT_WHILE="$STORE/trash" run_slot prepare ci 1 2>&1)"; rc=$?
[ "$rc" -ne 0 ] && ! grep -q '^cp --reflink' "$CALLLOG" && [ ! -e "$STORE/slot-ci-1" ] \
  && grep -q "the store is still short of room and $STORE/trash holds copies that could not be deleted; refusing to start a job on it" <<< "$out"
expect $? "a store still short while the trash holds a copy that will not delete: prepare refuses the job, saying why, and makes no copy"
setup; probe_rm
listener_files ci 1 acme 4242
backlog 1700000100
exec 9<"$STORE/trash"; flock 9
out="$(KNOWN_CI_REAP_LOCK_WAIT=1 STORE_DF_SHORT="1000 50 1000 500" STORE_DF_SHORT_WHILE="$STORE/trash" run_slot prepare ci 1 9<&- 2>&1)"; rc=$?
exec 9<&-
[ "$rc" -ne 0 ] && ! grep -q '^cp --reflink' "$CALLLOG" && ! grep -q "^rm -rf --one-file-system" "$CALLLOG" && [ -d "$STORE/trash/1700000100.slot-ci-2.7" ] \
  && grep -q "a reaper has held the lock of $STORE/trash for 1s and the store is still short of room; refusing to start a job on it" <<< "$out"
expect $? "a store still short while another reaper holds the trash's lock: prepare waits a bounded time, deletes nothing beside it, and refuses the job"
grep -qx 'STORE_MIN_FREE_PCT="${KNOWN_CI_STORE_MIN_FREE_PCT:-10}"' "$HELPER" && grep -qx 'REAP_LOCK_WAIT="${KNOWN_CI_REAP_LOCK_WAIT:-120}"' "$HELPER"
expect $? "the store is short of room below a tenth of its space or inodes free, and a prepare waits 120 s for a reaper that holds the lock"

# ---- reap: the lane's store reaper ---------------------------------------------------------------
# As <lane>-store-reaper.service runs it; it needs the store and nothing else.
run_reap() { env PATH="$S/pathbin:$PATH" KNOWN_CI_STORE_DIR="$STORE" PROBE_TRASH="$STORE/trash" bash "$HELPER" reap "$@"; }
setup; probe_rm
for t in 1700000300 1700000100 1700000200; do mkdir -p "$STORE/trash/$t.slot-ci-1.7/overlay2/layer1"; printf 'x\n' > "$STORE/trash/$t.slot-ci-1.7/overlay2/layer1/file"; done
mkdir -p "$STORE/slot-ci-2/overlay2"; printf 'live\n' > "$STORE/slot-ci-2/overlay2/file"
out="$(run_reap 2>&1)"; rc=$?
[ "$rc" -eq 0 ] && [ -d "$STORE/trash" ] && [ "$(trash_count)" = 0 ]; expect $? "the reaper empties the trash and exits 0, keeping the trash directory itself"
[ "$(grep '^rm ' "$CALLLOG" | sort | tr '\n' '|')" = "rm -rf --one-file-system -- $STORE/trash/1700000100.slot-ci-1.7|rm -rf --one-file-system -- $STORE/trash/1700000200.slot-ci-1.7|rm -rf --one-file-system -- $STORE/trash/1700000300.slot-ci-1.7|" ]
expect $? "the reaper runs one rm for each entry, naming that entry alone by its path inside the trash"
[ "$(grep -c '^trash lock held at rm ' "$CALLLOG")" = 3 ] && flock -n "$STORE/trash" true; expect $? "it holds the trash's lock during every delete, and frees it when it is done"
[ "$(cat "$STORE/slot-ci-2/overlay2/file")" = live ] && [ "$(cat "$STORE/golden-tag1/overlay2/layer1/diff/usr/bin/postgres")" = bin ] && [ -f "$STORE/golden-tag1/image/overlay2/repositories.json" ]
expect $? "it never deletes a preloaded golden-* store or a live slot-* copy: only what is inside the trash"
grep -qE "^lane-slot\[reap\]: deleted 1700000100\.slot-ci-1\.7 in [0-9]+s$" <<< "$out" && [ "$(grep -c ': deleted ' <<< "$out")" = 3 ]; expect $? "it logs each entry it deleted and how long that took"
# A few at a time: three deletes run together, never more, and the oldest entries go first. Each
# held delete records when it began and ended, and a batch starts only after the one before it ended.
entries_of() { local t; for t in "$@"; do printf 'rm -rf --one-file-system -- %s/trash/%s.slot-ci-1.7|' "$STORE" "$t"; done; }
setup; probe_rm
for t in 1700000600 1700000100 1700000500 1700000200 1700000400 1700000300 1700000700; do mkdir -p "$STORE/trash/$t.slot-ci-1.7/overlay2"; done
out="$(RM_HOLD=0.3 run_reap 2>&1)"; rc=$?
[ "$rc" -eq 0 ] && [ "$(trash_count)" = 0 ] && [ "$(grep -c ' 1$' "$S/st/rm-times")" = 7 ] && [ "$(most_at_once)" = 3 ]
expect $? "the reaper deletes three entries at a time: with seven waiting, three deletes run together and never more than three"
[ "$(grep '^rm ' "$CALLLOG" | sed -n '1,3p' | sort | tr '\n' '|')" = "$(entries_of 1700000100 1700000200 1700000300)" ] && [ "$(grep '^rm ' "$CALLLOG" | sed -n '4,6p' | sort | tr '\n' '|')" = "$(entries_of 1700000400 1700000500 1700000600)" ] \
  && [ "$(grep '^rm ' "$CALLLOG" | sed -n '7,$p' | tr '\n' '|')" = "$(entries_of 1700000700)" ]
expect $? "oldest first: the three oldest entries are deleted together, then the next three, then the one that is left"
[ "$(grep -c '^trash lock held at rm ' "$CALLLOG")" = 7 ]; expect $? "and every one of those deletes runs under the one lock the reaper holds"
setup; probe_rm
for t in 1700000300 1700000100 1700000200; do mkdir -p "$STORE/trash/$t.slot-ci-1.7/overlay2"; done
out="$(KNOWN_CI_REAP_JOBS=1 RM_HOLD=0.1 run_reap 2>&1)"; rc=$?
[ "$rc" -eq 0 ] && [ "$(most_at_once)" = 1 ] && [ "$(grep '^rm ' "$CALLLOG" | tr '\n' '|')" = "$(entries_of 1700000100 1700000200 1700000300)" ]
expect $? "KNOWN_CI_REAP_JOBS sets how many: at 1 the reaper deletes one entry at a time, strictly oldest first"
setup; probe_rm
for t in 1700000300 1700000100 1700000200; do mkdir -p "$STORE/trash/$t.slot-ci-1.7/overlay2"; done
out="$(KNOWN_CI_REAP_JOBS=many run_reap 2>&1)"; rc=$?
[ "$rc" -eq 1 ] && ! grep -q '^rm ' "$CALLLOG" && [ "$(trash_count)" = 3 ] && grep -q "KNOWN_CI_REAP_JOBS must be a positive number, got 'many'" <<< "$out"
expect $? "a count that is not a positive number is refused, and nothing is deleted: it could not bound how many deletes run at once"
grep -qx 'REAP_JOBS="${KNOWN_CI_REAP_JOBS:-3}"' "$HELPER"; expect $? "three at a time is the default"
setup; probe_rm
mkdir -p "$STORE/trash/1700000100.slot-ci-1.7" "$STORE/trash/1700000200.slot-ci-1.7" "$STORE/trash/1700000300.slot-ci-1.7" "$STORE/trash/1700000400.slot-ci-1.7"
out="$(RM_VANISH="$STORE/trash/1700000400.slot-ci-1.7" run_reap 2>&1)"; rc=$?
[ "$rc" -eq 0 ] && [ "$(trash_count)" = 0 ] && ! grep -q "^rm .*/1700000400\." "$CALLLOG" && [ "$(grep -c '^rm ' "$CALLLOG")" = 3 ]
expect $? "an entry that vanishes after the reaper listed it is skipped: the reaper deletes the others and exits 0"
setup; probe_rm
mkdir -p "$STORE/trash/1700000100.slot-ci-1.7" "$STORE/trash/1700000200.slot-ci-1.7" "$STORE/trash/1700000300.slot-ci-1.7"
out="$(RM_FAIL="$STORE/trash/1700000100.slot-ci-1.7" run_reap 2>&1)"; rc=$?
[ "$rc" -eq 1 ] && [ "$(ls -A "$STORE/trash")" = 1700000100.slot-ci-1.7 ] && grep -q "could not delete $STORE/trash/1700000100.slot-ci-1.7" <<< "$out"
expect $? "an entry that will not delete makes the reaper exit 1, after it deleted every other entry"
setup; probe_rm
mkdir -p "$STORE/trash/1700000100.slot-ci-1.7"
exec 9<"$STORE/trash"; flock 9
out="$(run_reap 9<&- 2>&1)"; rc=$?
exec 9<&-
[ "$rc" -eq 0 ] && ! grep -q '^rm ' "$CALLLOG" && [ -d "$STORE/trash/1700000100.slot-ci-1.7" ] && grep -q "another reaper holds the lock of $STORE/trash and deletes what is there; nothing to do" <<< "$out"
expect $? "a second reaper finds the trash's lock held and exits 0 at once, deleting nothing: one reaper for a lane, so never more deletes than its few"
# Nothing outside the trash: no link is followed out of it, and it takes no path to delete.
setup; probe_rm
mkdir -p "$STORE/trash/1700000100.slot-ci-1.7/overlay2" "$STORE/slot-ci-2"; printf 'live\n' > "$STORE/slot-ci-2/file"
ln -s "$STORE/golden-tag1" "$STORE/trash/1700000100.slot-ci-1.7/overlay2/to-the-golden-store"
ln -s "$STORE/slot-ci-2" "$STORE/trash/1700000200.to-a-live-copy"
out="$(run_reap 2>&1)"; rc=$?
[ "$rc" -eq 0 ] && [ "$(trash_count)" = 0 ] && [ "$(cat "$STORE/golden-tag1/overlay2/layer1/diff/usr/bin/postgres")" = bin ] && [ "$(cat "$STORE/slot-ci-2/file")" = live ]
expect $? "the reaper follows no link out of the trash: a link inside an entry, and an entry that is itself a link, are removed and what they point to is untouched"
setup; probe_rm
mkdir -p "$S/elsewhere/keep"; ln -s "$S/elsewhere" "$STORE/trash"
out="$(run_reap 2>&1)"; rc=$?
[ "$rc" -eq 1 ] && [ -d "$S/elsewhere/keep" ] && ! grep -q '^rm ' "$CALLLOG" && grep -q "$STORE/trash is a link or not a directory; refusing to delete through it" <<< "$out"
expect $? "a trash that is a link is refused: the reaper deletes nothing through it and exits 1"
setup; probe_rm
mkdir -p "$STORE/trash/1700000100.slot-ci-1.7"
out="$(run_reap "$STORE/golden-tag1" 2>&1)"; rc=$?
[ "$rc" -eq 2 ] && grep -q "usage:" <<< "$out" && ! grep -q '^rm ' "$CALLLOG" && [ -d "$STORE/golden-tag1" ] && [ -d "$STORE/trash/1700000100.slot-ci-1.7" ]
expect $? "the reaper takes no path: given one it is refused and deletes nothing, so it cannot be pointed outside the trash"
setup; probe_rm
out="$(run_reap 2>&1)"; rc=$?
[ "$rc" -eq 0 ] && [ -z "$out" ] && [ ! -e "$STORE/trash" ] && ! grep -q '^rm ' "$CALLLOG"; expect $? "with no trash yet the reaper has nothing to do: exit 0, silent, and it makes nothing"
grep -qx 'TRASH_DIR="$STORE_DIR/trash"' "$HELPER" && grep -qx 'REAPER_UNIT="${KNOWN_CI_NAME:-}-store-reaper.service"' "$HELPER"
expect $? "the trash is a directory of the store itself, so retiring a copy is a rename inside one filesystem, and the reaper's unit is named for the lane"

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
[ ! -e "$STORE/trash" ] && ! grep -q '^systemctl' "$CALLLOG"; expect $? "the smoke's and the image build's copy is deleted before discard returns: it never goes through the trash, and no reaper is started for it"
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
