#!/bin/bash
# runner-entrypoint-test.sh: hermetic tests for image/entrypoint.sh.
# Stubs dockerd, docker, setpriv, chown, mount, mountpoint, supabase and sleep on PATH and runs the entrypoint as the
# current user against a fake runner package; no container, daemon, network or GitHub involved.
set -u
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SRC="$ROOT/image/entrypoint.sh"
TMP="$(mktemp -d)"
# Stop a dockerd stub left running by a case and wait for it to exit, so its "stopped" line can
# never land in the next case's fresh call log. The stub outlives the entrypoint, so its parent is
# whatever reaps orphans here; under a PID 1 that reaps nothing (su, in a bare container) the killed
# stub stays a zombie, which kill -0 still finds, so a zombie counts as gone and the wait is bounded.
stop_dockerd_stub() {
  local pid
  [ -f "$TMP/s/state/dockerd.pid" ] || return 0
  pid="$(cat "$TMP/s/state/dockerd.pid")"
  kill "$pid" 2>/dev/null
  for _ in $(seq 1 100); do
    kill -0 "$pid" 2>/dev/null || break
    [ "$(awk '{ print $3 }' "/proc/$pid/stat" 2>/dev/null)" = Z ] && break
    /bin/sleep 0.05
  done
  rm -f "$TMP/s/state/dockerd.pid"
}
cleanup() {
  stop_dockerd_stub
  rm -rf "$TMP"
}
trap cleanup EXIT
pass=0; fail=0
expect() { if [ "$1" -eq 0 ]; then pass=$((pass+1)); echo "✓ $2"; else fail=$((fail+1)); echo "✗ $2"; fi; }

# The one-time JIT config: a value that must never show up in argv, output, or any log.
SECRET="eyJqaXQiOiJvbmUtdGltZS1ydW5uZXItY29uZmlnLTdmM2E5MWMifQ"  # gitleaks:allow (a fake fixture, never a credential)
EXCLUDE="studio,imgproxy,mailpit,edge-runtime,logflare,vector"

setup() {
  stop_dockerd_stub
  rm -rf "$TMP/s"
  mkdir -p "$TMP/s/bin" "$TMP/s/state" "$TMP/s/run" "$TMP/s/home/lost+found" "$TMP/s/etc" "$TMP/s/var-lib-docker" \
    "$TMP/s/dist/bin" "$TMP/s/dist/externals/node24/bin"
  CALLLOG="$TMP/s/state/calls.log"; : > "$CALLLOG"
  export CALLLOG STATE="$TMP/s/state"
  printf '%s\n' "$SECRET" > "$TMP/s/jit"
  chmod 0400 "$TMP/s/jit"

  # The fake runner package, laid out like actions-runner-linux-x64: bin/, externals/, start scripts.
  printf 'listener\n' > "$TMP/s/dist/bin/Runner.Listener"
  printf 'node\n' > "$TMP/s/dist/externals/node24/bin/node"
  printf 'template\n' > "$TMP/s/dist/run-helper.sh.template"
  printf '#!/bin/bash\nsleep "$1"\n' > "$TMP/s/dist/safe_sleep.sh"
  # run.sh records how it was started. Like the real one it leaves a worker diag log when a job
  # ran (RAN_JOB), exits RUN_RC, and with RUN_WAIT_TERM waits for a relayed SIGTERM.
  cat > "$TMP/s/dist/run.sh" <<'SH'
#!/bin/bash
if [ "${ACTIONS_RUNNER_INPUT_JITCONFIG:-}" = "$EXPECTED_SECRET" ]; then jit=match; else jit=mismatch; fi
# What a job step inherits: the runner cancels a step with SIGINT first, and a bash step can trap a
# signal only if it was not already ignored when the shell started.
sigint=ignored; trap 'sigint=trapped' INT; kill -INT $$; trap - INT
sigign="$(awk '/^SigIgn:/ { print $2 }' /proc/self/status)"
if ps -eo args | grep -F -- "$EXPECTED_SECRET" | grep -v grep >/dev/null; then seen=visible; else seen=absent; fi
{
  echo "run.sh path=$0 as=${SETPRIV_AS:-none} pwd=$PWD home=$HOME user=${USER:-} jit_env=$jit ps_secret=$seen registry=${SUPABASE_INTERNAL_IMAGE_REGISTRY:-unset} trap=${RUNNER_MANUALLY_TRAP_SIG:-unset} sigint=$sigint sigign=$sigign"
} >> "$CALLLOG"
if [ "${RUN_WAIT_TERM:-0}" = 1 ]; then
  trap 'echo "run.sh got TERM" >> "$CALLLOG"; exit 143' TERM
  echo $$ > "$STATE/run.pid"
  touch "$STATE/run.started"
  while :; do /bin/sleep 0.1; done
fi
if [ "${RAN_JOB:-1}" = 1 ]; then mkdir -p "$PWD/_diag"; touch "$PWD/_diag/Worker_20260925-120000-utc.log"; fi
exit "${RUN_RC:-0}"
SH
  chmod +x "$TMP/s/dist/run.sh" "$TMP/s/dist/safe_sleep.sh"
  mkstubs
}

mkstubs() {
  local p="$TMP/s/bin"
  cat > "$p/dockerd" <<'SH'
#!/bin/bash
echo "dockerd $*" >> "$CALLLOG"
if [ "${DOCKERD_EXIT:-0}" = 1 ]; then echo "dockerd: failed to start daemon: boom"; exit 1; fi
echo $$ > "$STATE/dockerd.pid"
trap 'echo "dockerd stopped" >> "$CALLLOG"; rm -f "$STATE/dockerd.up"; exit 0' TERM
touch "$STATE/dockerd.up"
while :; do sleep 0.1; done
SH
  cat > "$p/docker" <<'SH'
#!/bin/bash
echo "docker $*" >> "$CALLLOG"
case "$1" in
  version) [ -f "$STATE/dockerd.up" ] && [ "${DOCKER_NEVER_READY:-0}" != 1 ] ;;
  ps) cat "$STATE/containers" 2>/dev/null; exit 0 ;;
  volume) cat "$STATE/volumes" 2>/dev/null; exit 0 ;;
  network) exit 0 ;;
  pull) echo "$2" >> "$STATE/pulled"; exit 0 ;;
  image) printf '%s\n' ${IMAGES:-public.ecr.aws/supabase/postgres:17.6.1 public.ecr.aws/supabase/gotrue:v2.180.0}; [ -f "$STATE/pulled" ] && cat "$STATE/pulled"; exit 0 ;;
esac
SH
  # setpriv drops to the named user for real; the stub records the request and marks the child.
  cat > "$p/setpriv" <<'SH'
#!/bin/bash
echo "setpriv $*" >> "$CALLLOG"
as=""
while [ $# -gt 0 ]; do
  case "$1" in
    --reuid=*) as="${1#--reuid=}" ;;
    --) shift; break ;;
  esac
  shift
done
SETPRIV_AS="$as" exec "$@"
SH
  cat > "$p/chown" <<'SH'
#!/bin/bash
echo "chown $*" >> "$CALLLOG"
SH
  # mount: a tmpfs over the read-only /run makes it writable and hides the JIT file mounted there.
  cat > "$p/mount" <<'SH'
#!/bin/bash
echo "mount $*" >> "$CALLLOG"
[ "${MOUNT_FAIL:-0}" = 1 ] && { echo "mount: permission denied"; exit 32; }
chmod 0755 "${@: -1}"
rm -f "$KNOWN_CI_JIT_FILE"
SH
  # mountpoint: a path is mounted when the case listed it in $STATE/mounts.
  cat > "$p/mountpoint" <<'SH'
#!/bin/bash
grep -qx -- "${@: -1}" "$STATE/mounts" 2>/dev/null
SH
  cat > "$p/supabase" <<'SH'
#!/bin/bash
echo "supabase $* in $PWD registry=${SUPABASE_INTERNAL_IMAGE_REGISTRY:-unset}" >> "$CALLLOG"
[ "$1" = start ] && [ -n "${SUPABASE_FAIL_IN:-}" ] && [ "$PWD" = "$SUPABASE_FAIL_IN" ] && exit 1
exit 0
SH
  cat > "$p/sleep" <<'SH'
#!/bin/bash
echo "sleep $*" >> "$CALLLOG"
[ "$1" = infinity ] && exit 0
exec /bin/sleep "$@"
SH
  chmod +x "$p/"*
}

# The entrypoint's environment for every case.
ep_env() {
  EP_ENV=(PATH="$TMP/s/bin:$PATH"
    KNOWN_CI_ALLOW_NON_ROOT="${ALLOW_NON_ROOT:-1}"
    KNOWN_CI_JIT_FILE="${JIT_UNDER_TEST:-$TMP/s/jit}"
    KNOWN_CI_HOME="$TMP/s/home"
    KNOWN_CI_RUNNER_DIST="$TMP/s/dist"
    KNOWN_CI_RUN_DIR="$TMP/s/run"
    KNOWN_CI_MANIFEST="$TMP/s/etc/preloaded-images"
    KNOWN_CI_DOCKER_ROOT="$TMP/s/var-lib-docker"
    KNOWN_CI_DOCKERD_TIMEOUT="${DOCKERD_TIMEOUT:-5}"
    EXPECTED_SECRET="$SECRET")
}
run_ep() {
  ep_env
  env "${EP_ENV[@]}" bash "$SRC" "$@" > "$TMP/s/out" 2>&1
}

line_of() { grep -n -- "$1" "$CALLLOG" | head -n1 | cut -d: -f1; }

# ---- one job: the happy path ---------------------------------------------------------------
setup
RUN_RC=7 run_ep; rc=$?
stop_dockerd_stub
[ "$rc" -eq 7 ]; expect $? "the container exits with the runner's exit code"
grep -q "jit_env=match" "$CALLLOG"; expect $? "the JIT config reaches the runner through ACTIONS_RUNNER_INPUT_JITCONFIG"
grep -q "ps_secret=absent" "$CALLLOG"; expect $? "while the runner runs, no process argv on the host shows the JIT config"
{ ! grep -qF -- "$SECRET" "$CALLLOG" && ! grep -qF -- "$SECRET" "$TMP/s/out"; }; expect $? "the JIT config is in no command's argv and in none of the entrypoint's output"
grep -qx -- "setpriv --reuid=runner --regid=runner --init-groups -- $TMP/s/home/actions-runner/run.sh" "$CALLLOG"; expect $? "the runner starts through setpriv as runner:runner with runner's groups"
grep -q "as=runner " "$CALLLOG"; expect $? "run.sh runs under the dropped identity, not as root"
# The preload exports SUPABASE_INTERNAL_IMAGE_REGISTRY for its own supabase calls; a job's environment
# comes from the runner and must not inherit it from the entrypoint (review on the registry change).
grep -q "run.sh .*registry=unset " "$CALLLOG"; expect $? "the job does not inherit the preload's registry override from the entrypoint"
grep -q "run.sh path=$TMP/s/home/actions-runner/run.sh " "$CALLLOG" && grep -q "pwd=$TMP/s/home/actions-runner home=$TMP/s/home user=runner " "$CALLLOG"; expect $? "the runner runs from its copy in the job's home (/home/runner/actions-runner), with HOME and USER set to runner's"
grep -q "trap=1" "$CALLLOG"; expect $? "RUNNER_MANUALLY_TRAP_SIG is set so stopping the container cancels the job"
# A background command of a non-interactive bash starts with SIGINT and SIGQUIT ignored, and the
# runner and every job step inherited that: a normal cancel's SIGINT never reached a step, which got
# only the SIGTERM 7.5 s later (cancel repro on a lane, 2026-10-05).
grep -q "run.sh .* sigint=trapped " "$CALLLOG"; expect $? "the runner starts with SIGINT at its default, so a job step can trap the runner's cancel SIGINT"
mask="$(sed -n 's/.* sigign=\([0-9a-f]*\)$/\1/p' "$CALLLOG" | head -n1)"
[ -n "$mask" ] && [ $(( 0x$mask & 0x6 )) -eq 0 ]; expect $? "neither SIGINT nor SIGQUIT is ignored in the runner's process tree (SigIgn was ${mask:-unread})"
R="$TMP/s/home/actions-runner"
[ -d "$R/bin" ] && [ ! -L "$R/bin" ] && [ -f "$R/bin/Runner.Listener" ]; expect $? "bin/ is a real copy, so the runner's root is writable (the image's /opt/actions-runner is read-only)"
[ -L "$R/externals" ] && [ "$(readlink "$R/externals")" = "$TMP/s/dist/externals" ]; expect $? "externals/ is linked read-only from the image, not copied"
[ -f "$R/run-helper.sh.template" ] && [ -x "$R/safe_sleep.sh" ]; expect $? "the start scripts are copied beside bin/"
grep -qx "chown runner:runner $TMP/s/home" "$CALLLOG" && grep -qx "chown runner:runner $R" "$CALLLOG"; expect $? "the home and the runner root are handed to runner before the runner starts"
d_line="$(line_of '^dockerd')"; v_line="$(grep -n '^docker version' "$CALLLOG" | tail -n1 | cut -d: -f1)"; s_line="$(line_of '^setpriv')"
[ -n "$d_line" ] && [ -n "$v_line" ] && [ -n "$s_line" ] && [ "$d_line" -lt "$v_line" ] && [ "$v_line" -lt "$s_line" ]; expect $? "the inner dockerd is started and answering before the runner starts"
[ -f "$TMP/s/run/dockerd.log" ]; expect $? "dockerd's output goes to /run/dockerd.log"
{ ! grep -q '^mount' "$CALLLOG"; }; expect $? "a writable /run (the unit's --tmpfs /run) is used as it is"

setup
run_ep; rc=$?
stop_dockerd_stub
[ "$rc" -eq 0 ] && grep -q "the runner ran its job and exited 0" "$TMP/s/out"; expect $? "a runner that ran its job and exited 0 ends the container with 0"
# A wait slot's inner Docker store is an empty directory (bin/lane-slot.sh), as is every job's on a
# lane that preloads nothing: the job mode asks nothing of what is under /var/lib/docker.
[ "$rc" -eq 0 ] && [ -z "$(ls -A "$TMP/s/var-lib-docker")" ] && [ ! -e "$TMP/s/etc/preloaded-images" ] && grep -qx "dockerd " "$CALLLOG" && ! grep -q '^docker image' "$CALLLOG"
expect $? "a job runs on an empty /var/lib/docker and with no preload manifest: the job mode starts the inner dockerd and neither reads nor requires preloaded images"

setup
RAN_JOB=0 run_ep; rc=$?
stop_dockerd_stub
[ "$rc" -eq 75 ] && grep -q "exited 0 without running a job" "$TMP/s/out"; expect $? "run.sh exiting 0 with no job run (a terminal listener error) is a failure, exit 75"

setup
ep_env
# Started directly, not through a function, so $! is the entrypoint itself (env execs bash).
RUN_WAIT_TERM=1 env "${EP_ENV[@]}" bash "$SRC" > "$TMP/s/out" 2>&1 & ep=$!
for _ in $(seq 1 100); do [ -f "$TMP/s/state/run.started" ] && break; /bin/sleep 0.1; done
kill -TERM "$ep"
for _ in $(seq 1 50); do kill -0 "$ep" 2>/dev/null || break; /bin/sleep 0.1; done
if kill -0 "$ep" 2>/dev/null; then pkill -KILL -P "$ep"; kill -KILL "$ep"; fi
wait "$ep"; rc=$?
# Never leave the stub runner behind, even when the relay under test is broken.
[ -f "$TMP/s/state/run.pid" ] && kill -KILL "$(cat "$TMP/s/state/run.pid")" 2>/dev/null
stop_dockerd_stub
[ "$rc" -eq 143 ] && grep -qx "run.sh got TERM" "$CALLLOG"; expect $? "SIGTERM to the container reaches run.sh, and its exit code is the container's"

# ---- refusals before anything starts ------------------------------------------------------
setup
JIT_UNDER_TEST="$TMP/s/missing" run_ep; rc=$?
[ "$rc" -eq 64 ] && grep -q "no JIT runner config" "$TMP/s/out" && ! grep -q '^dockerd' "$CALLLOG"; expect $? "no JIT config: exit 64 before dockerd starts"

setup
printf 'x\n' > "$TMP/s/home/.bash_history"
run_ep; rc=$?
[ "$rc" -eq 65 ] && grep -q "not a fresh slot volume (found .bash_history)" "$TMP/s/out"; expect $? "a home holding a previous job's file is refused (exit 65)"
{ ! grep -qE '^(dockerd|setpriv)' "$CALLLOG"; }; expect $? "a stale home starts neither dockerd nor the runner"

# A QAE job has the lane's Codex store mounted at .codex-qae: the one other entry a
# fresh home may hold, and only while the store is mounted there.
setup
mkdir "$TMP/s/home/.codex-qae"; printf '%s\n' "$TMP/s/home/.codex-qae" > "$STATE/mounts"
run_ep; rc=$?
stop_dockerd_stub
[ "$rc" -eq 0 ] && grep -q "^setpriv " "$CALLLOG"; expect $? "a fresh home holding the mounted Codex store runs its job"
setup
mkdir "$TMP/s/home/.codex-qae"
run_ep; rc=$?
[ "$rc" -eq 65 ] && grep -q "not a fresh slot volume (found .codex-qae)" "$TMP/s/out" && ! grep -qE '^(dockerd|setpriv)' "$CALLLOG"; expect $? "a .codex-qae directory with nothing mounted on it is a leftover, refused (exit 65)"

# chmod cannot make a directory unwritable for root, so these two run only as a normal user.
if [ "$(id -u)" != 0 ]; then
  setup
  chmod 0555 "$TMP/s/run"
  run_ep; rc=$?
  stop_dockerd_stub
  [ "$rc" -eq 0 ] && grep -qx "mount -t tmpfs -o mode=0755,nosuid,nodev,size=256m tmpfs $TMP/s/run" "$CALLLOG" \
    && grep -q "jit_env=match" "$CALLLOG" && grep -q '^setpriv' "$CALLLOG"; expect $? "a read-only /run gets a tmpfs mounted over it, after the JIT file under it was read"

  setup
  chmod 0555 "$TMP/s/run"
  MOUNT_FAIL=1 run_ep; rc=$?
  chmod 0755 "$TMP/s/run"
  [ "$rc" -eq 64 ] && grep -q "a tmpfs could not be mounted over it; run the container with --tmpfs /run" "$TMP/s/out" && ! grep -qE '^(dockerd|setpriv)' "$CALLLOG"; expect $? "a read-only /run that cannot be mounted over is refused, naming --tmpfs /run"
fi

setup
DOCKERD_EXIT=1 run_ep; rc=$?
[ "$rc" -eq 69 ] && grep -q "dockerd exited during startup" "$TMP/s/out" && grep -q "boom" "$TMP/s/out" && ! grep -q '^setpriv' "$CALLLOG"; expect $? "a dockerd that dies at startup fails the slot (exit 69) with its log tail, runner never started"

setup
DOCKER_NEVER_READY=1 DOCKERD_TIMEOUT=2 run_ep; rc=$?
stop_dockerd_stub
[ "$rc" -eq 69 ] && grep -q "dockerd not ready after 2s" "$TMP/s/out" && ! grep -q '^setpriv' "$CALLLOG"; expect $? "a dockerd that never answers fails the slot after the timeout"

if [ "$(id -u)" != 0 ]; then
  setup
  ALLOW_NON_ROOT=0 run_ep; rc=$?
  [ "$rc" -eq 64 ] && grep -q "must start as root" "$TMP/s/out"; expect $? "started as a non-root user, it refuses"
fi

setup
run_ep bogus; rc=$?
[ "$rc" -eq 64 ] && grep -q "unknown mode 'bogus'" "$TMP/s/out"; expect $? "an unknown mode is refused"

setup
run_ep hold; rc=$?
[ "$rc" -eq 0 ] && grep -qx "sleep infinity" "$CALLLOG" && ! grep -q '^dockerd' "$CALLLOG"; expect $? "hold only sleeps: the build container starts no dockerd until preload"

# ---- preload -------------------------------------------------------------------------------
setup
mkdir -p "$TMP/s/p/alpha/supabase" "$TMP/s/p/beta/packages/database/supabase"
touch "$TMP/s/p/alpha/supabase/config.toml" "$TMP/s/p/beta/packages/database/supabase/config.toml"
IMAGES="public.ecr.aws/supabase/postgres:17.6.1 public.ecr.aws/supabase/gotrue:v2.180.0 public.ecr.aws/supabase/postgres:15.8.1" \
  run_ep preload "$TMP/s/p/alpha" "$TMP/s/p/beta/packages/database"; rc=$?
[ "$rc" -eq 0 ]; expect $? "preload succeeds over two projects"
grep -qx "supabase start -x $EXCLUDE --ignore-health-check in $TMP/s/p/alpha registry=ghcr.io" "$CALLLOG" \
  && grep -qx "supabase stop --no-backup in $TMP/s/p/alpha registry=ghcr.io" "$CALLLOG" \
  && grep -qx "supabase start -x $EXCLUDE --ignore-health-check in $TMP/s/p/beta/packages/database registry=ghcr.io" "$CALLLOG" \
  && grep -qx "supabase stop --no-backup in $TMP/s/p/beta/packages/database registry=ghcr.io" "$CALLLOG"; expect $? "each project runs its own supabase start with the lane's exclusions, then stop --no-backup"
a_stop="$(line_of "supabase stop --no-backup in $TMP/s/p/alpha registry=ghcr.io")"; b_start="$(line_of "supabase start -x $EXCLUDE --ignore-health-check in $TMP/s/p/beta/packages/database registry=ghcr.io")"
[ -n "$a_stop" ] && [ -n "$b_start" ] && [ "$a_stop" -lt "$b_start" ]; expect $? "one stack at a time: a project is stopped before the next starts"
printf 'public.ecr.aws/supabase/gotrue:v2.180.0\npublic.ecr.aws/supabase/postgres:15.8.1\npublic.ecr.aws/supabase/postgres:17.6.1\nsupabase/pg_prove:3.36\n' > "$TMP/s/expected-manifest"
cmp -s "$TMP/s/expected-manifest" "$TMP/s/etc/preloaded-images"; expect $? "the manifest records every inner image, sorted, pg_prove included"
grep -qx "docker pull supabase/pg_prove:3.36" "$CALLLOG"; expect $? "the preload pulls the pg_prove image that supabase test db needs and supabase start never pulls"
grep -qx "dockerd stopped" "$CALLLOG" && [ ! -e "$TMP/s/run/docker.pid" ]; expect $? "dockerd is stopped cleanly before commit and leaves no pid file"
grep -qx "docker network prune -f" "$CALLLOG"; expect $? "leftover inner networks are pruned"
grep -q "inner store on disk: " "$TMP/s/out"; expect $? "the preload logs the inner store's size, the per-container Sysbox CE copy cost"

setup
mkdir -p "$TMP/s/p/alpha/supabase" "$TMP/s/p/beta/supabase"
touch "$TMP/s/p/alpha/supabase/config.toml" "$TMP/s/p/beta/supabase/config.toml"
SUPABASE_FAIL_IN="$TMP/s/p/beta" run_ep preload "$TMP/s/p/alpha" "$TMP/s/p/beta"; rc=$?
stop_dockerd_stub
[ "$rc" -eq 70 ] && grep -q "supabase start failed in $TMP/s/p/beta" "$TMP/s/out" && [ ! -e "$TMP/s/etc/preloaded-images" ]; expect $? "a failing supabase start fails the preload (exit 70) and writes no manifest"

setup
mkdir -p "$TMP/s/p/alpha/supabase"; touch "$TMP/s/p/alpha/supabase/config.toml"
printf 'c0ffee\n' > "$TMP/s/state/containers"
run_ep preload "$TMP/s/p/alpha"; rc=$?
stop_dockerd_stub
[ "$rc" -eq 70 ] && grep -q "inner containers remain" "$TMP/s/out" && [ ! -e "$TMP/s/etc/preloaded-images" ]; expect $? "a container left running blocks the commit (exit 70)"

setup
mkdir -p "$TMP/s/p/alpha/supabase"; touch "$TMP/s/p/alpha/supabase/config.toml"
printf 'supabase_db_alpha\n' > "$TMP/s/state/volumes"
run_ep preload "$TMP/s/p/alpha"; rc=$?
stop_dockerd_stub
[ "$rc" -eq 70 ] && grep -q "inner volumes remain" "$TMP/s/out"; expect $? "a database volume left behind blocks the commit (exit 70)"

setup
run_ep preload "$TMP/s/p/nowhere"; rc=$?
[ "$rc" -eq 64 ] && grep -q "no supabase/config.toml under" "$TMP/s/out" && ! grep -q '^dockerd' "$CALLLOG"; expect $? "a project without supabase/config.toml is refused before dockerd starts"

# ---- verify-preload ------------------------------------------------------------------------
setup
printf 'public.ecr.aws/supabase/gotrue:v2.180.0\npublic.ecr.aws/supabase/postgres:17.6.1\n' > "$TMP/s/etc/preloaded-images"
run_ep verify-preload; rc=$?
stop_dockerd_stub
[ "$rc" -eq 0 ] && grep -q "preload verified: 2 images present" "$TMP/s/out"; expect $? "verify-preload passes when the inner store holds exactly the manifest"

setup
printf 'public.ecr.aws/supabase/gotrue:v2.180.0\npublic.ecr.aws/supabase/postgres:17.6.1\npublic.ecr.aws/supabase/realtime:v2.51.0\n' > "$TMP/s/etc/preloaded-images"
run_ep verify-preload; rc=$?
stop_dockerd_stub
[ "$rc" -eq 1 ] && grep -q "realtime:v2.51.0" "$TMP/s/out"; expect $? "verify-preload fails and names the image the committed store lost"

setup
run_ep verify-preload; rc=$?
[ "$rc" -eq 1 ] && grep -q "no preload manifest" "$TMP/s/out"; expect $? "verify-preload fails on an image that was never preloaded"

echo
echo "runner-entrypoint-test: $pass passed, $fail failed"
[ "$fail" -eq 0 ]
