#!/bin/bash
# listener-health-test.sh: hermetic tests for bin/listener-health.sh.
# Stubs systemctl (is-active, restart) and journalctl (the newest line, and the last 10 minutes) on
# PATH; the verdict directory is a temporary one and the post-restart wait is 0 s. No unit, journal
# or /run path of the machine is read or written.
set -u
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SRC="$ROOT/bin/listener-health.sh"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
pass=0; fail=0
expect() { if [ "$1" -eq 0 ]; then pass=$((pass+1)); echo "✓ $2"; else fail=$((fail+1)); echo "✗ $2"; fi; }
mode_of() { python3 -c 'import os, sys; print(format(os.stat(sys.argv[1]).st_mode & 0o777, "o"))' "$1" 2>/dev/null; }
# The state a test starts from: the listener active, its newest journal line 10 s old, nothing in the
# last 10 minutes but heartbeats.
setup() {
  S="$TMP/s"
  rm -rf "$S"
  mkdir -p "$S/pathbin" "$S/run" "$S/st"
  echo active > "$S/st/active"
  echo $(( $(date +%s) - 10 )) > "$S/st/journal_ts"
  printf 'heartbeat: 1 scale sets served, 1 of 5 slots in use\n' > "$S/st/recent"
  CALLLOG="$S/calls.log"; : > "$CALLLOG"
  export CALLLOG ST="$S/st"
  cat > "$S/pathbin/systemctl" <<'SH'
#!/bin/bash
echo "systemctl $*" >> "$CALLLOG"
case "$1" in
  is-active) cat "$ST/active"; [ "$(cat "$ST/active")" = active ] ;;
  restart)
    [ "${RESTART_RC:-0}" = 0 ] || exit "$RESTART_RC"
    if [ "${RESTART_RECOVERS:-1}" = 1 ]; then echo active > "$ST/active"; date +%s > "$ST/journal_ts"; fi ;;
esac
SH
  # -n 1 -o short-unix: the newest line, stamped from $ST/journal_ts (none: an empty journal).
  # --since: the last 10 minutes, from $ST/recent.
  cat > "$S/pathbin/journalctl" <<'SH'
#!/bin/bash
echo "journalctl $*" >> "$CALLLOG"
case " $* " in
  *" --since "*) cat "$ST/recent" ;;
  *" -n 1 "*) ts="$(cat "$ST/journal_ts")"; [ "$ts" = none ] || echo "$ts.123456 box-1 listener[42]: heartbeat: 1 scale sets served, 1 of 5 slots in use" ;;
esac
SH
  cat > "$S/pathbin/sleep" <<'SH'
#!/bin/bash
echo "sleep $*" >> "$CALLLOG"
SH
  chmod +x "$S/pathbin/"*
}

run_health() {
  env PATH="$S/pathbin:$PATH" KNOWN_CI_LISTENER_HEALTH_DIR="$S/run" KNOWN_CI_LISTENER_HEALTH_SETTLE_SEC=0 bash "$SRC" "$@"
}
restarts() { grep -c '^systemctl restart ' "$CALLLOG" | tr -d ' '; }
state_line() { cat "$S/run/box-ci-listener-health"; }
UTC_RE='[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z'

# ---- healthy ------------------------------------------------------------------------------------------
setup
out="$(run_health box-ci 2>&1)"; rc=$?
[ "$rc" -eq 0 ] && [ "$(restarts)" = 0 ]; expect $? "an active listener with a fresh journal is healthy and not restarted"
state_line | grep -qxE "ok $UTC_RE" && [ "$(mode_of "$S/run/box-ci-listener-health")" = 644 ]; expect $? "the verdict is 'ok <utc>' in /run/<name>-listener-health (0644)"
grep -qE "^box-ci-listener.service: ok $UTC_RE$" <<< "$out"; expect $? "the verdict is logged"
grep -qx "systemctl is-active box-ci-listener.service" "$CALLLOG" && grep -q "^journalctl -u box-ci-listener.service -n 1 -o short-unix" "$CALLLOG"; expect $? "it reads the lane's own listener unit and journal"
[ ! -e "$S/run/box-ci-listener-health.restarted" ] && [ -z "$(find "$S/run" -name '*.tmp')" ]; expect $? "a healthy check writes no cooldown and leaves no temporary file"

# ---- a silent journal restarts the listener once, and it recovers ------------------------------------
setup
echo $(( $(date +%s) - 600 )) > "$S/st/journal_ts"
out="$(run_health box-ci 2>&1)"; rc=$?
[ "$rc" -eq 0 ] && [ "$(restarts)" = 1 ] && grep -qx "systemctl restart box-ci-listener.service" "$CALLLOG"; expect $? "a journal silent for 10 minutes restarts the listener once, and the re-check passes"
grep -q "box-ci-listener.service: journal silent for 6[0-9][0-9]s, restarting it once" <<< "$out" && state_line | grep -qxE "ok $UTC_RE"; expect $? "the restart and its reason are logged, and the verdict after recovery is ok"
grep -qx "sleep 0" "$CALLLOG" && [ "$(grep -n '^sleep' "$CALLLOG" | cut -d: -f1)" -gt "$(grep -n '^systemctl restart' "$CALLLOG" | cut -d: -f1)" ]; expect $? "it waits the settle time after the restart before it re-checks"
now="$(date +%s)"; last="$(cat "$S/run/box-ci-listener-health.restarted")"
[ $(( now - last )) -le 5 ]; expect $? "the restart's epoch is recorded in the cooldown file"
setup
echo none > "$S/st/journal_ts"
run_health box-ci >/dev/null 2>&1; rc=$?
[ "$rc" -eq 0 ] && [ "$(restarts)" = 1 ]; expect $? "an empty journal counts as silent: one restart"
setup
echo failed > "$S/st/active"
out="$(run_health box-ci 2>&1)"; rc=$?
[ "$rc" -eq 0 ] && [ "$(restarts)" = 1 ] && grep -q "listener failed, restarting it once" <<< "$out"; expect $? "a listener systemd gave up on (failed) is restarted once"

# ---- the restart fails ----------------------------------------------------------------------------------
setup
echo $(( $(date +%s) - 600 )) > "$S/st/journal_ts"
out="$(RESTART_RC=1 run_health box-ci 2>&1)"; rc=$?
[ "$rc" -ne 0 ] && [ "$(restarts)" = 1 ] && state_line | grep -qE "^unhealthy $UTC_RE journal silent for [0-9]+s; the restart failed$"; expect $? "a restart systemctl refuses leaves the lane unhealthy and the check exits non-zero"
[ -s "$S/run/box-ci-listener-health.restarted" ]; expect $? "a failed restart still starts the cooldown"
setup
echo $(( $(date +%s) - 600 )) > "$S/st/journal_ts"
out="$(RESTART_RECOVERS=0 run_health box-ci 2>&1)"; rc=$?
[ "$rc" -ne 0 ] && [ "$(restarts)" = 1 ] && state_line | grep -qE "^unhealthy $UTC_RE journal silent for [0-9]+s after a restart$"; expect $? "a listener still silent after its restart is unhealthy, exit non-zero"

# ---- the cooldown ---------------------------------------------------------------------------------------
setup
echo $(( $(date +%s) - 600 )) > "$S/st/journal_ts"
echo $(( $(date +%s) - 60 )) > "$S/run/box-ci-listener-health.restarted"
out="$(run_health box-ci 2>&1)"; rc=$?
[ "$rc" -ne 0 ] && [ "$(restarts)" = 0 ] && state_line | grep -qE "^unhealthy $UTC_RE journal silent for [0-9]+s; not restarted: the last restart was [0-9]+s ago \(one per 1800s\)$"
expect $? "a restart under 30 minutes ago blocks a second one: unhealthy, exit non-zero, no restart"
setup
echo $(( $(date +%s) - 600 )) > "$S/st/journal_ts"
echo $(( $(date +%s) - 1900 )) > "$S/run/box-ci-listener-health.restarted"
run_health box-ci >/dev/null 2>&1; rc=$?
[ "$rc" -eq 0 ] && [ "$(restarts)" = 1 ]; expect $? "a restart more than 30 minutes ago no longer blocks the next one"
setup
echo $(( $(date +%s) - 600 )) > "$S/st/journal_ts"
out="$(RESTART_RECOVERS=0 run_health box-ci 2>&1)"
: > "$CALLLOG"
run_health box-ci >/dev/null 2>&1; rc=$?
[ "$rc" -ne 0 ] && [ "$(restarts)" = 0 ] && state_line | grep -q "not restarted: the last restart was"; expect $? "two ticks on a lane with a real problem restart it once, not twice"

# ---- 401/403 from GitHub: credentials, never a restart ---------------------------------------------------
setup
printf '%s\n' "heartbeat: 1 scale sets served, 0 of 5 slots in use" \
  'session for set box-ci: request POST https://api.github.com/x failed(status="403 Forbidden"): unexpected status code 403 Forbidden: Resource not accessible by integration' > "$S/st/recent"
out="$(run_health box-ci 2>&1)"; rc=$?
[ "$rc" -ne 0 ] && [ "$(restarts)" = 0 ] && state_line | grep -qE "^unhealthy $UTC_RE credentials: 1 GitHub 401/403 lines in 10 minutes \(not restarted: a restart cannot grant a permission\)$"
expect $? "a 403 in the last 10 minutes is 'unhealthy credentials', exit non-zero, with no restart"
grep -q '^journalctl -u box-ci-listener.service --since 10 min ago -o cat' "$CALLLOG"; expect $? "the credentials window is the listener's journal of the last 10 minutes"
setup
echo $(( $(date +%s) - 600 )) > "$S/st/journal_ts"
printf '%s\n' "list repositories: GET /installation/repositories: HTTP 401: Bad credentials" > "$S/st/recent"
run_health box-ci >/dev/null 2>&1; rc=$?
[ "$rc" -ne 0 ] && [ "$(restarts)" = 0 ] && state_line | grep -q "credentials: 1 GitHub"; expect $? "a 401 (the listener's own HTTP 401) is credentials too, and wins over a silent journal: no restart"
setup
printf '%s\n' "mint box-lane-ci-1: unexpected status code: 403" 'token: failed to get access token for GitHub App auth (401 Unauthorized)' > "$S/st/recent"
run_health box-ci >/dev/null 2>&1; rc=$?
[ "$rc" -ne 0 ] && state_line | grep -q "credentials: 2 GitHub"; expect $? "the scaleset client's 'status code: 403' and '(401 Unauthorized)' forms count"
setup
printf '%s\n' "started box-ci-ci@1 for runner box-lane-ci-1-1790403123 (id 4031)" "job completed on box-lane-ci-2-1790401777" > "$S/st/recent"
run_health box-ci >/dev/null 2>&1; rc=$?
[ "$rc" -eq 0 ] && state_line | grep -qxE "ok $UTC_RE"; expect $? "a runner name or id that merely holds 401 or 403 is not a credentials line"

# ---- refusals ---------------------------------------------------------------------------------------------
setup
out="$(run_health ../etc 2>&1)"; rc=$?
[ "$rc" -eq 2 ] && grep -q "invalid lane name" <<< "$out" && [ -z "$(ls -A "$S/run")" ] && [ ! -s "$CALLLOG" ]; expect $? "a lane name that is not [a-z][a-z0-9-]* is refused before any read or write"
out="$(run_health 2>&1)"; rc=$?
[ "$rc" -eq 2 ] && grep -q "usage:" <<< "$out"; expect $? "no argument prints the usage and exits 2"
out="$(run_health --report 2>&1)"; rc=$?
[ "$rc" -eq 2 ]; expect $? "an option is not a lane name: --report is refused"

echo
echo "listener-health-test: $pass passed, $fail failed"
[ "$fail" -eq 0 ]
